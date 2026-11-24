"""Lifecycle for optional, isolated PaddleOCR CPU inference."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Mapping

from ..errors import UserFacingError
from ..import_resume import atomic_write_json
from ..parsers.paddleocr_local import (
    PADDLE_OCR_VERSION, PADDLE_PROVIDER_ID, PADDLE_RUNTIME_LOCK, PADDLE_VERSION, PADDLE_WORKER,
    normalize_paddle_pages, paddle_available, paddle_cpu_env, paddle_cpu_installed,
    paddle_runtime_root, read_paddle_config, save_paddle_config, test_paddle_endpoint,
)
from ..parsers.openai_compatible import VISION_TEST_IMAGE_BASE64
from .install import (
    cancel_and_join_operations, check_cancelled, copy_response_to_file, install_uv_executable,
    request_cancel, stop_process, verify_download,
)
from .local_ocr_installer import current_platform_key, load_local_ocr_installer_manifest

_SUPPORTED = {"win32-x86_64", "darwin-arm64", "linux-x86_64"}
_ACTIVE = {"provisioning", "downloading_models"}


@dataclass
class InstallState:
    state: str = "not_installed"
    operation: str | None = None
    message: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None


class ManagedPaddleOCR:
    """Download and validate the first CPU profile on explicit user request."""

    component_id = PADDLE_PROVIDER_ID

    def __init__(self, root: Path, manifest_path: Path) -> None:
        self.root = root
        self.runtime = paddle_runtime_root(root)
        self.manifest_path = manifest_path
        self.lock = threading.RLock()
        self.state = InstallState()
        self.shutdown = False

    def summary(self) -> dict[str, object]:
        with self.lock:
            settings = read_paddle_config(self.root)
            installed = paddle_cpu_installed(self.root)
            return {
                "component_id": self.component_id,
                "supported": current_platform_key() in _SUPPORTED,
                "installed": installed,
                "busy": self.state.operation is not None,
                "state": self.state.state if self.state.operation or self.state.state == "failed"
                         else ("installed" if installed else "not_installed"),
                "message": self.state.message,
                "enabled": paddle_available(self.root),
                "endpoint": settings.endpoint,
                "version": PADDLE_OCR_VERSION,
            }

    def diagnostics(self) -> dict[str, object]:
        return self.summary()

    def perform(self, payload: Mapping[str, object]) -> dict[str, object]:
        action = payload.get("action")
        if action == "test":
            test_paddle_endpoint(str(payload.get("endpoint") or ""))
            return {"ok": True}
        if action == "cancel":
            request_cancel(self.state, self.lock, active_states=_ACTIVE)
            return self.summary()
        with self.lock:
            if self.shutdown or self.state.operation:
                raise UserFacingError("PaddleOCR 正在执行另一个操作。")
            if action == "save":
                endpoint = str(payload.get("endpoint") or "")
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise UserFacingError("PaddleOCR 启用状态无效。")
                if enabled and not endpoint and not paddle_cpu_installed(self.root):
                    raise UserFacingError("请先安装 CPU 组件或填写自部署地址。")
                save_paddle_config(self.root, endpoint, enabled)
            elif action == "install":
                if current_platform_key() not in _SUPPORTED:
                    raise UserFacingError("本机不支持托管安装；可连接已部署的 PaddleOCR。")
                if not paddle_cpu_installed(self.root):
                    self.state = InstallState(state="provisioning", operation="install", message="正在准备运行时…")
                    self.state.thread = threading.Thread(target=self._install, daemon=True, name="paddleocr-install")
                    self.state.thread.start()
            elif action == "uninstall":
                if not PADDLE_RUNTIME_LOCK.acquire(blocking=False):
                    raise UserFacingError("PaddleOCR 正在解析文献，请等待任务结束后再卸载。")
                try:
                    for child in ("cpu", "tools"):
                        if (self.runtime / child).exists():
                            shutil.rmtree(self.runtime / child)
                finally:
                    PADDLE_RUNTIME_LOCK.release()
                self.state = InstallState()
            elif action != "check":
                raise UserFacingError("不支持的 PaddleOCR 操作。")
            return self.summary()

    def close(self, timeout: float | None = None) -> bool:
        with self.lock:
            self.shutdown = True
        return cancel_and_join_operations([self.state], self.lock, timeout)

    def _run(self, args: list[str], staging: Path, env: dict[str, str], log: Path) -> None:
        with self.lock, log.open("ab") as stream:
            check_cancelled(self.state, UserFacingError)
            process = subprocess.Popen(
                args, cwd=staging, env=env, stdin=subprocess.DEVNULL, stdout=stream,
                stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.state.process = process
        try:
            if process.wait(timeout=3600):
                check_cancelled(self.state, UserFacingError)
                raise UserFacingError("PaddleOCR 运行时安装或推理验证失败，请查看 runtime/paddleocr-local/install.log。")
        except subprocess.TimeoutExpired as exc:
            raise UserFacingError("安装或推理验证超时。") from exc
        finally:
            stop_process(process)
            with self.lock:
                self.state.process = None

    def _install(self) -> None:
        staging = self.runtime / f".staging-{uuid.uuid4().hex}"
        try:
            _, platform = load_local_ocr_installer_manifest(self.manifest_path)
            assert platform is not None
            staging.mkdir(parents=True)
            log = self.runtime / "install.log"
            log.write_bytes(b"")

            def download(path: Path) -> None:
                request = urllib.request.Request(platform.uv.url, headers={"User-Agent": "MEFinder-PaddleOCR-installer"})
                with urllib.request.urlopen(request, timeout=30) as response, path.open("wb") as stream:
                    copy_response_to_file(
                        response, stream, downloaded=0,
                        raise_if_cancelled=lambda: check_cancelled(self.state, UserFacingError),
                        on_progress=lambda _: None,
                    )
                verify_download(path, expected_size=platform.uv.size, expected_sha256=platform.uv.sha256,
                                error=UserFacingError, size_message="uv 下载大小不匹配。", digest_message="uv 校验失败。")

            tool_dir = self.runtime / "tools" / f"uv-{platform.uv.version}"
            uv = tool_dir / PurePosixPath(platform.uv.member).name
            if not uv.is_file():
                uv = install_uv_executable(
                    uv=platform.uv, tool_dir=tool_dir,
                    staging=staging / "uv-download", archive_name="uv.archive", download=download,
                    remove_tree=lambda path: shutil.rmtree(path) if path.exists() else None, error=UserFacingError,
                )
            env = {
                **paddle_cpu_env(staging),
                "UV_PYTHON_INSTALL_DIR": str(self.runtime / "tools" / "python"),
                "UV_CACHE_DIR": str(self.runtime / "tools" / "cache"),
            }
            proxies = urllib.request.getproxies()
            for scheme in ("http", "https"):
                upper, lower = f"{scheme.upper()}_PROXY", f"{scheme}_proxy"
                if upper not in env and lower not in env and proxies.get(scheme):
                    env[upper] = proxies[scheme]
            self._run([str(uv), "venv", "--python", "3.12", "--managed-python",
                       "--relocatable", str(staging / "venv")], staging, env, log)
            python = staging / platform.venv_python
            self._run([str(uv), "pip", "install", "--python", str(python),
                       f"paddlepaddle=={PADDLE_VERSION}", f"paddleocr[doc-parser]=={PADDLE_OCR_VERSION}"],
                      staging, env, log)
            with self.lock:
                self.state.state = "downloading_models"
                self.state.message = "正在下载模型并验证推理…"
            probe = staging / "probe.png"
            output = staging / "probe.json"
            probe.write_bytes(base64.b64decode(VISION_TEST_IMAGE_BASE64))
            self._run([str(python), "-u", "-c", PADDLE_WORKER, str(probe), str(output)],
                      staging, paddle_cpu_env(staging), log)
            normalize_paddle_pages(json.loads(output.read_text(encoding="utf-8")), 1, 0)
            probe.unlink()
            output.unlink()
            atomic_write_json(staging / "receipt.json", {"paddleocr": PADDLE_OCR_VERSION, "paddle": PADDLE_VERSION})
            with self.lock:
                check_cancelled(self.state, UserFacingError)
                final = self.runtime / "cpu"
                if final.exists():
                    shutil.rmtree(final)
                staging.replace(final)
                settings = read_paddle_config(self.root)
                if not settings.endpoint:
                    save_paddle_config(self.root, "", True)
                self.state.state = "installed"
                self.state.message = "CPU 组件已通过推理验证。"
        except (OSError, ValueError, RuntimeError) as exc:
            with self.lock:
                cancelled = self.state.cancel_event.is_set()
                self.state.state = "not_installed" if cancelled else "failed"
                self.state.message = "已取消安装。" if cancelled else str(exc)
        finally:
            try:
                if staging.exists():
                    shutil.rmtree(staging)
            finally:
                with self.lock:
                    self.state.operation = None
                    self.state.thread = None

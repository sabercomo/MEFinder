"""Install and manage the independent alignment *compute* runtime.

Phase 2A moved the alignment compute phase into a separate process, but that
process was still launched with the main application's own interpreter — it
reused whatever NumPy / ONNX Runtime / fastembed happened to be bundled with the
app. Phase 2B makes the compute runtime a *managed component*: an isolated uv
virtual environment, installed under the runtime root, that owns the pinned
numeric stack. The main program never mutates its own environment (or another
component's) to install or upgrade it, and once installed the compute phase can
run without any numeric stack in the main process.

This module owns only the install / upgrade / uninstall / validate lifecycle of
that runtime and its cross-process safety. It never opens the official
database, never re-parses source files and never triggers OCR. The compute seam
itself lives in :mod:`me_finder.alignment_compute`; the launch resolution that
prefers an installed runtime lives there too (``installed_runtime_command``).

The mechanics (uv download + isolated venv, staging → publish → previous swap,
receipts, cancellation, download verification) deliberately mirror
:mod:`me_finder.managed_mineru`, the proven reference for a managed local
runtime, so the two share operational behaviour and reviewer intuition.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Mapping, Optional, Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen

from .alignment_compute import ALIGNMENT_COMPUTE_PROTOCOL
from .local_ocr_installer import (
    LOCAL_OCR_MANIFEST_FILE,
    PlatformManifest,
    current_platform_key,
    load_local_ocr_installer_manifest,
)
from .import_resume import atomic_write_json
from .runtime_location import component_runtime_root

# Component identifier used in the managed-component registry and HTTP layer.
COMPONENT_ID = "text-alignment-runtime"

# Bump when the installed layout or receipt schema changes incompatibly.
ALIGNMENT_RUNTIME_RECEIPT_SCHEMA = 1

# Directory names under components/text-alignment/.
_RUNTIME_DIR = "runtime"
_MODELS_DIR = "models"


class ManagedAlignmentRuntimeError(RuntimeError):
    pass


class _Cancelled(ManagedAlignmentRuntimeError):
    pass


@dataclass(frozen=True)
class AlignmentRuntimeManifest:
    runtime_version: str
    python: str
    packages: tuple[str, ...]
    platform: Optional[PlatformManifest]

    def identity(self) -> str:
        """Stable identity of what an install of this manifest should contain.

        The receipt records it; ``_update_available`` compares against it, so a
        changed pin set (not just a bumped format version) is offered as an
        update, and an install from a different pin set is never mistaken for
        the current one.
        """

        payload = json.dumps(
            {
                "runtime_version": self.runtime_version,
                "python": self.python,
                "packages": sorted(self.packages),
                "protocol": ALIGNMENT_COMPUTE_PROTOCOL,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class _RuntimeState:
    state: str = "not_installed"
    operation: Optional[str] = None
    progress: Optional[float] = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    message: str = ""
    error: str = ""
    uninstall_deferred: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    process: Optional[subprocess.Popen] = None
    thread: Optional[threading.Thread] = None


def load_alignment_runtime_manifest(
    path: Path,
    *,
    platform_key: Optional[str] = None,
) -> AlignmentRuntimeManifest:
    """Read the ``alignment`` runtime block and the shared platform matrix.

    The numeric packages (numpy / fastembed) are shared across platforms; the
    per-platform ONNX Runtime pin and uv download come from the shared
    ``platforms`` matrix, so there is one source of truth for both.
    """

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedAlignmentRuntimeError("对齐计算组件清单无法读取。") from exc
    raw = payload.get("alignment") if isinstance(payload, Mapping) else None
    if not isinstance(raw, Mapping):
        raise ManagedAlignmentRuntimeError("组件清单缺少对齐计算运行时定义。")
    packages = raw.get("packages")
    if not isinstance(packages, list) or not all(
        isinstance(item, str) and "==" in item for item in packages
    ):
        raise ManagedAlignmentRuntimeError("对齐计算运行时依赖必须固定版本。")
    required = {name.split("==", 1)[0].lower() for name in packages}
    if not {"numpy", "onnxruntime", "fastembed"}.issubset(required):
        raise ManagedAlignmentRuntimeError(
            "对齐计算运行时依赖必须包含 numpy、onnxruntime、fastembed。"
        )
    selected_key = platform_key or current_platform_key()
    # The shared platform matrix supplies only the uv download and the venv
    # interpreter path; the numeric stack (including ONNX Runtime) is pinned in
    # the alignment block, so the runtime is self-describing and independent of
    # the OCR components' ONNX Runtime choice.
    _engines, selected_platform = load_local_ocr_installer_manifest(
        Path(path), platform_key=selected_key
    )
    return AlignmentRuntimeManifest(
        runtime_version=str(raw.get("version") or ""),
        python=str(raw.get("python") or ""),
        packages=tuple(packages),
        platform=selected_platform,
    )


class _CrossProcessOperationLock:
    """A best-effort exclusive lock held for the length of one operation.

    Unlike an in-process ``threading.Lock``, this is an OS advisory lock on a
    file, so a second application instance or an independent process cannot
    install / upgrade / uninstall the same component concurrently. The lock is
    released by the operating system if the holder dies, so a crash mid-install
    never wedges the component permanently.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Optional[int] = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(handle)
            raise ManagedAlignmentRuntimeError(
                "另一个进程正在安装或卸载对齐计算组件，请稍后重试。"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                try:
                    os.lseek(handle, 0, os.SEEK_SET)
                    msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(handle)

    def __enter__(self) -> "_CrossProcessOperationLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


class ManagedAlignmentRuntime:
    """Manage the isolated alignment compute runtime as a local component."""

    component_id = COMPONENT_ID

    def __init__(
        self,
        runtime_root: Path,
        *,
        manifest_path: Path | Callable[[], Path] = LOCAL_OCR_MANIFEST_FILE,
        platform_key: Optional[str] = None,
        opener: Callable = urlopen,
        process_launcher: Callable = subprocess.Popen,
        catalog_summary: Optional[Callable[[], Dict[str, object]]] = None,
        models_component: Optional[object] = None,
        is_compute_active: Optional[Callable[[], bool]] = None,
        worker_context: Optional[Callable[[], tuple[str, Path]]] = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        # Colocate the runtime with the model cache under the stable machine
        # component directory (the models component uses the same wrapper), so
        # switching libraries never relocates or orphans the installed runtime.
        self.component_root = (
            component_runtime_root(self.runtime_root)
            / "components"
            / "text-alignment"
        )
        self.runtime_dir = self.component_root / _RUNTIME_DIR
        self.models_dir = self.component_root / _MODELS_DIR
        self._manifest_path = manifest_path
        self.platform_key = platform_key or current_platform_key()
        self.opener = opener
        self.process_launcher = process_launcher
        self._catalog_summary = catalog_summary
        self._models_component = models_component
        self._is_compute_active = is_compute_active or (lambda: False)
        self._worker_context = worker_context or default_worker_context
        self.manifest = load_alignment_runtime_manifest(
            self._current_manifest_path(), platform_key=self.platform_key
        )
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._state = _RuntimeState(
            state="installed" if self._installed() else "not_installed"
        )

    # --- manifest -------------------------------------------------------- #
    def _current_manifest_path(self) -> Path:
        manifest_path = self._manifest_path
        return manifest_path() if callable(manifest_path) else manifest_path

    def refresh_manifest(self) -> None:
        manifest = load_alignment_runtime_manifest(
            self._current_manifest_path(), platform_key=self.platform_key
        )
        with self._lock:
            if self._state.operation:
                return
            self.manifest = manifest

    # --- receipts / installed state -------------------------------------- #
    def _receipt_path(self) -> Path:
        return self.runtime_dir / "installed.json"

    def _receipt(self) -> Mapping[str, object]:
        try:
            value = json.loads(self._receipt_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, Mapping) else {}

    def _venv_python(self, root: Path) -> Optional[Path]:
        platform_manifest = self.manifest.platform
        if platform_manifest is None:
            return None
        return root / platform_manifest.venv_python

    def _installed(self) -> bool:
        """A runtime counts as installed only if the receipt AND the interpreter
        it names are both present. A half-written staging directory (crash mid
        install) is never reported as installed."""

        receipt = self._receipt()
        if receipt.get("schema_version") != ALIGNMENT_RUNTIME_RECEIPT_SCHEMA:
            return False
        python = self._venv_python(self.runtime_dir)
        if python is None or not python.exists():
            return False
        return bool(receipt.get("identity"))

    def _update_available(self) -> bool:
        if not self._installed():
            return False
        return self._receipt().get("identity") != self.manifest.identity()

    def installed_venv_python(self) -> Optional[Path]:
        """The interpreter of a validly installed runtime, else None.

        The launch resolver in :mod:`me_finder.alignment_compute` uses this to
        prefer the independent runtime over the main interpreter.
        """

        if not self._installed():
            return None
        return self._venv_python(self.runtime_dir)

    def _has_models(self) -> bool:
        models = self.models_dir
        if not models.is_dir():
            return False
        return any(models.iterdir())

    # --- summary --------------------------------------------------------- #
    def summary(self) -> Dict[str, object]:
        with self._lock:
            state = self._state
            receipt = self._receipt()
            result: Dict[str, object] = {
                "component_id": self.component_id,
                "supported": self.manifest.platform is not None,
                "platform": self.platform_key,
                "version": self.manifest.runtime_version,
                "installed": self._installed(),
                "installed_version": str(receipt.get("runtime_version") or ""),
                "update_available": self._update_available(),
                "has_models": self._has_models(),
                "runtime_dir": str(self.runtime_dir),
                "state": state.state,
                "operation": state.operation,
                "progress": state.progress,
                "downloaded_bytes": state.downloaded_bytes,
                "total_bytes": state.total_bytes,
                "uninstall_deferred": state.uninstall_deferred,
                "message": state.message,
                "error": state.error,
            }
        if self._catalog_summary is not None:
            result["catalog"] = self._catalog_summary()
        return result

    def diagnostics(self) -> Dict[str, object]:
        with self._lock:
            return {
                "component_id": self.component_id,
                "supported": self.manifest.platform is not None,
                "platform": self.platform_key,
                "installed": self._installed(),
                "update_available": self._update_available(),
                "operation": self._state.operation,
            }

    # --- operations ------------------------------------------------------ #
    def perform(self, payload: Mapping[str, object]) -> Dict[str, object]:
        action = str(payload.get("action") or "").strip().lower()
        if action == "cancel":
            self._cancel()
            return self.summary()
        if action not in {"install", "update", "uninstall", "validate"}:
            raise ManagedAlignmentRuntimeError("不支持的对齐计算组件操作。")
        self._start_operation(action)
        return self.summary()

    def _start_operation(self, action: str) -> None:
        installed = self._installed()
        if action == "install" and installed:
            raise ManagedAlignmentRuntimeError("对齐计算组件已安装。")
        if action == "update" and not self._update_available():
            raise ManagedAlignmentRuntimeError("对齐计算组件没有可安装的更新。")
        if action in {"uninstall", "validate"} and not installed:
            raise ManagedAlignmentRuntimeError("对齐计算组件尚未安装。")
        if action in {"install", "update"} and self.manifest.platform is None:
            raise ManagedAlignmentRuntimeError("当前平台不在对齐计算安装矩阵中。")
        if not self._operation_lock.acquire(blocking=False):
            raise ManagedAlignmentRuntimeError("对齐计算组件正在操作中。")
        initial = {
            "install": "provisioning",
            "update": "provisioning",
            "uninstall": "cleaning",
            "validate": "validating",
        }[action]
        with self._lock:
            state = self._state
            state.state = initial
            state.operation = action
            state.progress = None
            state.downloaded_bytes = 0
            state.total_bytes = 0
            state.uninstall_deferred = False
            state.message = ""
            state.error = ""
            state.cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._operation_worker,
                args=(action,),
                name=f"managed-alignment-runtime-{action}",
                daemon=True,
            )
            state.thread = thread
        thread.start()

    def _operation_worker(self, action: str) -> None:
        cross_lock = _CrossProcessOperationLock(self.component_root / ".operation.lock")
        try:
            cross_lock.acquire()
        except ManagedAlignmentRuntimeError as exc:
            self._set_state(
                "installed" if self._installed() else "not_installed",
                operation=None,
                message="操作失败",
                error=str(exc),
            )
            self._operation_lock.release()
            return
        try:
            if action in {"install", "update"}:
                self._install()
                message = "对齐计算组件安装完成"
                final_state = "installed"
            elif action == "validate":
                self._validate(self.runtime_dir)
                message = "对齐计算组件验证通过"
                final_state = "installed"
            else:
                deferred = self._uninstall_with_wait()
                if deferred == "cancelled":
                    self._set_state(
                        "installed",
                        operation=None,
                        message="卸载已取消",
                    )
                    return
                message = "对齐计算组件已卸载"
                final_state = "not_installed"
            self._set_state(final_state, operation=None, message=message)
        except _Cancelled:
            self._set_state(
                "installed" if self._installed() else "not_installed",
                operation=None,
                message="操作已取消",
            )
        except (
            ManagedAlignmentRuntimeError,
            OSError,
            ValueError,
            json.JSONDecodeError,
            subprocess.SubprocessError,
            tarfile.TarError,
            zipfile.BadZipFile,
            URLError,
        ) as exc:
            self._set_state(
                "installed" if self._installed() else "not_installed",
                operation=None,
                message="操作失败",
                error=str(exc),
            )
        finally:
            with self._lock:
                self._state.process = None
                self._state.thread = None
            cross_lock.release()
            self._operation_lock.release()

    def _install(self) -> None:
        platform_manifest = self.manifest.platform
        if platform_manifest is None:
            raise ManagedAlignmentRuntimeError("当前平台不在对齐计算安装矩阵中。")
        self.component_root.mkdir(parents=True, exist_ok=True)
        for stale in self.component_root.glob(".staging-*"):
            self._remove_tree(stale)
        staging = self.component_root / f".staging-{uuid.uuid4().hex}"
        final = self.runtime_dir
        previous = self.component_root / f".previous-{uuid.uuid4().hex}"
        published = False
        staging.mkdir(parents=True)
        try:
            uv_path = self._ensure_uv(platform_manifest)
            environment = self._install_environment(staging)
            install_log = staging / "install.log"
            self._set_state("provisioning", message="正在创建独立 Python 环境")
            self._run_command(
                [
                    str(uv_path),
                    "venv",
                    "--python",
                    self.manifest.python,
                    "--managed-python",
                    "--relocatable",
                    str(staging / "venv"),
                ],
                cwd=staging,
                environment=environment,
                log_path=install_log,
                timeout=1800,
            )
            python_path = staging / platform_manifest.venv_python
            self._set_state(
                "provisioning",
                message=f"正在安装对齐计算依赖（{len(self.manifest.packages)} 个）",
            )
            self._run_command(
                [
                    str(uv_path),
                    "pip",
                    "install",
                    "--python",
                    str(python_path),
                    *self.manifest.packages,
                ],
                cwd=staging,
                environment=environment,
                log_path=install_log,
                timeout=3600,
            )
            # Validate the *staged* runtime before publishing: the numeric stack
            # must actually import in the isolated interpreter (runtime load),
            # not merely exist on disk.
            self._set_state("validating", message="正在验证独立运行时")
            self._validate(staging)
            atomic_write_json(
                staging / "installed.json",
                {
                    "schema_version": ALIGNMENT_RUNTIME_RECEIPT_SCHEMA,
                    "runtime_version": self.manifest.runtime_version,
                    "python": self.manifest.python,
                    "platform": self.platform_key,
                    "protocol": ALIGNMENT_COMPUTE_PROTOCOL,
                    "packages": list(self.manifest.packages),
                    "identity": self.manifest.identity(),
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            if final.exists():
                final.replace(previous)
            staging.replace(final)
            published = True
            self._remove_tree(previous)
        except BaseException:
            # A failed or interrupted install must leave the previously working
            # runtime (if any) in place — never a half-published directory.
            if published:
                self._remove_tree(final)
            if previous.exists():
                previous.replace(final)
            raise
        finally:
            self._remove_tree(staging)

    def _validate(self, root: Path) -> None:
        """Run the compute worker's ``--probe`` with the runtime's interpreter.

        This is a real runtime-load check: the probe imports numpy / fastembed /
        onnxruntime *in the isolated interpreter* and completes the versioned
        protocol handshake. File existence or ``find_spec`` in the main process
        would prove nothing about the isolated environment.
        """

        python = self._venv_python(root)
        if python is None or not python.exists():
            raise ManagedAlignmentRuntimeError("独立运行时缺少 Python 解释器。")
        module, source_root = self._worker_context()
        control = root / "probe.ndjson"
        control.write_text("", encoding="utf-8")
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(source_root), existing) if p
        )
        self._run_command(
            [str(python), "-m", module, "--probe", str(control)],
            cwd=source_root,
            environment=environment,
            log_path=root / "validation.log",
            timeout=300,
        )
        messages = [
            json.loads(line)
            for line in control.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        hello = next((m for m in messages if m.get("type") == "hello"), None)
        if hello is None:
            error = next((m for m in messages if m.get("type") == "error"), None)
            detail = str(error.get("message")) if error else "未返回能力应答"
            raise ManagedAlignmentRuntimeError(f"独立运行时验证失败：{detail}")
        if hello.get("protocol") != ALIGNMENT_COMPUTE_PROTOCOL:
            raise ManagedAlignmentRuntimeError("独立运行时协议版本不兼容。")
        capabilities = dict(hello.get("capabilities") or {})
        missing = [n for n in ("numpy", "fastembed", "onnxruntime") if not capabilities.get(n)]
        if missing:
            raise ManagedAlignmentRuntimeError(
                "独立运行时缺少计算依赖：" + "、".join(missing)
            )

    def _uninstall_with_wait(self) -> str:
        """Uninstall, deferring until any in-flight compute task finishes.

        Product rule (2B): while the component is processing a task, uninstall
        waits — shown as "任务结束后卸载" — and stops accepting new tasks; it
        completes only after the current task publishes or fails.
        """

        state = self._state
        if self._is_compute_active():
            with self._lock:
                state.state = "uninstall_pending"
                state.uninstall_deferred = True
                state.message = "任务结束后卸载"
            while self._is_compute_active():
                if state.cancel_event.wait(0.2):
                    return "cancelled"
        self._set_state("cleaning", message="正在卸载对齐计算组件")
        self._perform_uninstall()
        return "done"

    def _perform_uninstall(self) -> None:
        # Remove the runtime, the shared uv tool cache and the managed Python,
        # then — per the confirmed product rule — the component's model files.
        # Failures propagate so the UI never shows "已卸载" over a runtime that
        # is still on disk.
        self._remove_tree(self.runtime_dir, strict=True)
        self._remove_tree(self.component_root / "_tools", strict=True)
        self._remove_tree(self.component_root / "_python", strict=True)
        self._delete_models()

    def _delete_models(self) -> None:
        # Reset the models component's receipts / in-memory state first (it also
        # refuses to delete a model mid-download), then remove the whole models
        # directory — it is exclusive to the alignment component, so a full
        # component uninstall clears every model file it owns, not only the
        # currently-registered model ids. Documents, notes and existing
        # alignment results live in the library database and are untouched.
        component = self._models_component
        if component is not None and hasattr(component, "delete_all_models"):
            component.delete_all_models()
        self._remove_tree(self.models_dir, strict=True)

    # --- uv + subprocess ------------------------------------------------- #
    def _ensure_uv(self, platform_manifest: PlatformManifest) -> Path:
        tool_dir = (
            self.component_root
            / "_tools"
            / f"uv-{platform_manifest.uv.version}-{platform_manifest.key}"
        )
        executable = tool_dir / PurePosixPath(platform_manifest.uv.member).name
        if executable.is_file():
            return executable
        staging = self.component_root / f".uv-{uuid.uuid4().hex}"
        archive = staging / "archive"
        staging.mkdir(parents=True)
        try:
            self._download_file(
                platform_manifest.uv.url,
                archive,
                platform_manifest.uv.size,
                platform_manifest.uv.sha256,
            )
            extracted = staging / executable.name
            if platform_manifest.uv.archive_type == "zip":
                with zipfile.ZipFile(archive) as bundle:
                    try:
                        member = bundle.getinfo(platform_manifest.uv.member)
                    except KeyError as exc:
                        raise ManagedAlignmentRuntimeError(
                            "uv 归档缺少清单指定的可执行文件。"
                        ) from exc
                    with bundle.open(member) as source, extracted.open("wb") as output:
                        shutil.copyfileobj(source, output)
            else:
                with tarfile.open(archive, "r:gz") as bundle:
                    try:
                        member = bundle.getmember(platform_manifest.uv.member)
                    except KeyError as exc:
                        raise ManagedAlignmentRuntimeError(
                            "uv 归档缺少清单指定的可执行文件。"
                        ) from exc
                    source = bundle.extractfile(member)
                    if source is None:
                        raise ManagedAlignmentRuntimeError("uv 归档入口无法读取。")
                    with source, extracted.open("wb") as output:
                        shutil.copyfileobj(source, output)
            extracted.chmod(0o755)
            archive.unlink()
            tool_dir.parent.mkdir(parents=True, exist_ok=True)
            self._remove_tree(tool_dir)
            staging.replace(tool_dir)
            return tool_dir / executable.name
        finally:
            self._remove_tree(staging)

    def _download_file(
        self,
        url: str,
        target: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        request = Request(url, headers={"User-Agent": "MEFinder-alignment-runtime-installer"})
        downloaded = 0
        with self._lock:
            self._state.total_bytes = expected_size
            self._state.downloaded_bytes = 0
        with self.opener(request, timeout=30) as response, target.open("wb") as output:
            while True:
                self._raise_if_cancelled()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                with self._lock:
                    self._state.downloaded_bytes = downloaded
                    if expected_size:
                        self._state.progress = min(downloaded / expected_size, 0.99)
        if target.stat().st_size != expected_size:
            raise ManagedAlignmentRuntimeError("uv 下载文件大小与清单不一致。")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise ManagedAlignmentRuntimeError("uv 下载文件 SHA-256 校验失败。")

    def _install_environment(self, staging: Path) -> Dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "UV_CACHE_DIR": str(staging / ".uv-cache"),
                "UV_PYTHON_INSTALL_DIR": str(self.component_root / "_python"),
                "UV_NO_PROGRESS": "1",
            }
        )
        return environment

    def _run_command(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
        timeout: int,
    ) -> None:
        self._raise_if_cancelled()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as output:
            process = self.process_launcher(
                list(command),
                cwd=str(cwd),
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
            with self._lock:
                self._state.process = process
            try:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if self._state.cancel_event.wait(0.1):
                        self._stop_process(process)
                        raise _Cancelled("操作已取消。")
                    if time.monotonic() >= deadline:
                        self._stop_process(process)
                        raise ManagedAlignmentRuntimeError("对齐计算安装子进程超时。")
            finally:
                with self._lock:
                    if self._state.process is process:
                        self._state.process = None
        if process.returncode:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise ManagedAlignmentRuntimeError(
                f"对齐计算安装子进程退出 {process.returncode}：{detail.strip()}"
            )

    # --- cancellation / state -------------------------------------------- #
    def _cancel(self) -> None:
        with self._lock:
            state = self._state
            if state.operation is None:
                return
            state.cancel_event.set()
            process = state.process
        if process is not None:
            self._stop_process(process)

    def _raise_if_cancelled(self) -> None:
        if self._state.cancel_event.is_set():
            raise _Cancelled("操作已取消。")

    def _set_state(
        self,
        new_state: str,
        *,
        operation: object = "keep",
        message: str = "",
        error: str = "",
    ) -> None:
        with self._lock:
            state = self._state
            state.state = new_state
            if operation != "keep":
                state.operation = operation  # type: ignore[assignment]
            if message:
                state.message = message
            state.error = error

    def close(self) -> None:
        with self._lock:
            state = self._state
            if state.operation is not None:
                state.cancel_event.set()
                process = state.process
            else:
                process = None
        if process is not None:
            self._stop_process(process)

    # --- helpers --------------------------------------------------------- #
    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        except OSError:
            pass

    @staticmethod
    def _remove_tree(path: Path, *, strict: bool = False) -> None:
        if strict:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            return
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            try:
                path.unlink()
            except OSError:
                pass


@dataclass(frozen=True)
class RuntimeLaunch:
    command: tuple[str, ...]
    env: Dict[str, str]
    cwd: str


def resolve_installed_runtime_launch(
    runtime_root: Path,
    *,
    platform_key: Optional[str] = None,
    manifest_path: Path | Callable[[], Path] = LOCAL_OCR_MANIFEST_FILE,
    worker_context: Optional[Callable[[], tuple[str, Path]]] = None,
) -> Optional[RuntimeLaunch]:
    """Launch parameters for the installed independent runtime, else ``None``.

    Read-only: it inspects the on-disk receipt and interpreter without touching
    the install lifecycle, so the compute seam can prefer the independent
    runtime whenever one is validly installed and otherwise fall back to the
    main interpreter (2A behaviour). A half-installed or receipt-less directory
    resolves to ``None`` — it is never launched as if installed.
    """

    selected_key = platform_key or current_platform_key()
    path = manifest_path() if callable(manifest_path) else manifest_path
    try:
        _engines, platform_manifest = load_local_ocr_installer_manifest(
            Path(path), platform_key=selected_key
        )
    except Exception:  # noqa: BLE001 - a bad manifest must not break launching
        return None
    if platform_manifest is None:
        return None
    runtime_dir = (
        component_runtime_root(Path(runtime_root))
        / "components"
        / "text-alignment"
        / _RUNTIME_DIR
    )
    receipt_path = runtime_dir / "installed.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != ALIGNMENT_RUNTIME_RECEIPT_SCHEMA
        or receipt.get("protocol") != ALIGNMENT_COMPUTE_PROTOCOL
        or not receipt.get("identity")
    ):
        return None
    python = runtime_dir / platform_manifest.venv_python
    if not python.exists():
        return None
    module, source_root = (worker_context or default_worker_context)()
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(source_root), existing) if p)
    return RuntimeLaunch(
        command=(str(python), "-m", module),
        env=env,
        cwd=str(source_root),
    )


def default_worker_context() -> tuple[str, Path]:
    """How to launch the compute worker from an external interpreter.

    Returns ``(module, source_root)``: the module to run with ``-m`` and the
    directory to place on ``PYTHONPATH`` / use as the working directory, so an
    interpreter that has only the numeric stack can still import the pure-Python
    compute code.

    * Development: the repository root exposes ``src.me_finder`` as a package.
    * Frozen: the bundled application source root ships the ``me_finder``
      package next to the runtime (packaging wiring is completed in phase 2C;
      the resolution is defined here so the launch path is testable now).
    """

    if getattr(sys, "frozen", False):
        from .runtime_location import app_root

        return "me_finder.alignment_compute_worker", app_root()
    return "src.me_finder.alignment_compute_worker", Path(__file__).resolve().parents[2]

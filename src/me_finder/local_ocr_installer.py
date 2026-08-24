"""Manifest-driven installer for the two managed local OCR runtimes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .import_resume import ResumeManifestError, atomic_write_json
from .local_ocr_runtime import local_ocr_engine_lock
from .local_ocr_settings import (
    LocalOCRError,
    clear_managed_local_ocr_engine,
    configure_managed_local_ocr_engine,
)


LOCAL_OCR_MANIFEST_FILE = Path(__file__).with_name("local_ocr_manifest.json")
LOCAL_OCR_COMPONENT_DIR = "components/local-ocr"
ACTIVE_INSTALL_STATES = frozenset(
    {
        "downloading",
        "verifying",
        "extracting",
        "provisioning",
        "validating",
        "cleaning",
    }
)
_UV_DOWNLOAD_PATTERN = re.compile(
    r"^Downloading (.+) \(([0-9]+(?:\.[0-9]+)?)(KiB|MiB|GiB)\)\r?$",
    re.MULTILINE,
)
_UV_DOWNLOADED_PATTERN = re.compile(r"^ Downloaded (.+?)\r?$", re.MULTILINE)
_BINARY_SIZE_MULTIPLIERS = {
    "KiB": 1024,
    "MiB": 1024 * 1024,
    "GiB": 1024 * 1024 * 1024,
}


class LocalOCRInstallerError(RuntimeError):
    pass


class _InstallCancelled(LocalOCRInstallerError):
    pass


@dataclass(frozen=True)
class EngineManifest:
    provider_id: str
    display_name: str
    version: str
    tag: str
    tarball_url: str
    tarball_size: int
    tarball_sha256: str
    archive_root: str
    script_path: Path
    sample_path: Path
    cli_extra_args: tuple[str, ...]
    dependencies: tuple[str, ...]
    license: str
    license_path: Path
    attribution: str
    modification_notice: str


@dataclass(frozen=True)
class UVManifest:
    version: str
    url: str
    size: int
    sha256: str
    archive_type: str
    member: str


@dataclass(frozen=True)
class PlatformManifest:
    key: str
    python: str
    venv_python: Path
    onnxruntime: str
    uv: UVManifest
    notes: str


@dataclass
class _EngineInstallState:
    state: str = "not_installed"
    operation: Optional[str] = None
    progress: Optional[float] = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    download_speed_bps: float = 0.0
    eta_seconds: Optional[int] = None
    download_samples: list[tuple[float, int]] = field(default_factory=list)
    message: str = ""
    error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    process: Optional[subprocess.Popen[bytes]] = None
    thread: Optional[threading.Thread] = None


def current_platform_key() -> str:
    machine = platform.machine().strip().lower()
    normalized_machine = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }.get(machine, machine)
    return f"{sys.platform}-{normalized_machine}"


def load_local_ocr_installer_manifest(
    path: Path = LOCAL_OCR_MANIFEST_FILE,
    *,
    platform_key: Optional[str] = None,
) -> tuple[Dict[str, EngineManifest], Optional[PlatformManifest]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalOCRInstallerError("本地 OCR 安装清单无法读取。") from exc
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise LocalOCRInstallerError("本地 OCR 安装清单版本不受支持。")
    raw_engines = raw.get("engines")
    raw_platforms = raw.get("platforms")
    if not isinstance(raw_engines, Mapping) or not isinstance(
        raw_platforms, Mapping
    ):
        raise LocalOCRInstallerError("本地 OCR 安装清单结构无效。")
    engines: Dict[str, EngineManifest] = {}
    for provider_id, value in raw_engines.items():
        if not isinstance(provider_id, str) or not isinstance(value, Mapping):
            raise LocalOCRInstallerError("本地 OCR 组件清单无效。")
        try:
            engines[provider_id] = EngineManifest(
                provider_id=provider_id,
                display_name=str(value["display_name"]),
                version=str(value["version"]),
                tag=str(value["tag"]),
                tarball_url=str(value["tarball_url"]),
                tarball_size=int(value["tarball_size"]),
                tarball_sha256=str(value["tarball_sha256"]),
                archive_root=str(value["archive_root"]),
                script_path=Path(str(value["script_path"])),
                sample_path=Path(str(value["sample_path"])),
                cli_extra_args=tuple(str(item) for item in value["cli_extra_args"]),
                dependencies=tuple(str(item) for item in value["dependencies"]),
                license=str(value["license"]),
                license_path=Path(str(value["license_path"])),
                attribution=str(value["attribution"]),
                modification_notice=str(value["modification_notice"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalOCRInstallerError(
                f"本地 OCR 组件清单无效：{provider_id}"
            ) from exc
    selected_key = platform_key or current_platform_key()
    raw_platform = raw_platforms.get(selected_key)
    if raw_platform is None:
        return engines, None
    if not isinstance(raw_platform, Mapping):
        raise LocalOCRInstallerError("本地 OCR 平台清单无效。")
    raw_uv = raw_platform.get("uv")
    if not isinstance(raw_uv, Mapping):
        raise LocalOCRInstallerError("本地 OCR uv 清单无效。")
    try:
        selected = PlatformManifest(
            key=selected_key,
            python=str(raw_platform["python"]),
            venv_python=Path(str(raw_platform["venv_python"])),
            onnxruntime=str(raw_platform["onnxruntime"]),
            uv=UVManifest(
                version=str(raw["uv_version"]),
                url=str(raw_uv["url"]),
                size=int(raw_uv["size"]),
                sha256=str(raw_uv["sha256"]),
                archive_type=str(raw_uv["archive_type"]),
                member=str(raw_uv["member"]),
            ),
            notes=str(raw_platform.get("notes") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LocalOCRInstallerError("本地 OCR 平台清单无效。") from exc
    return engines, selected


class LocalOCRInstaller:
    def __init__(
        self,
        runtime_root: Path,
        config_path: Path,
        *,
        manifest_path: Path | Callable[[], Path] = LOCAL_OCR_MANIFEST_FILE,
        platform_key: Optional[str] = None,
        catalog_summary: Optional[Callable[[], Dict[str, object]]] = None,
        process_launcher: Callable = subprocess.Popen,
    ) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.config_path = Path(config_path).resolve()
        self.component_root = self.runtime_root / LOCAL_OCR_COMPONENT_DIR
        self._manifest_path = manifest_path
        self._catalog_summary = catalog_summary
        self.process_launcher = process_launcher
        self.engines, self.platform = load_local_ocr_installer_manifest(
            self._current_manifest_path(),
            platform_key=platform_key,
        )
        self.platform_key = platform_key or current_platform_key()
        self._state_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._states = {
            provider_id: _EngineInstallState(
                state=(
                    "installed"
                    if self._managed_install_is_valid(provider_id)
                    else "not_installed"
                )
            )
            for provider_id in self.engines
        }

    def summary(self) -> Dict[str, object]:
        with self._state_lock:
            engines = [
                self._engine_summary(provider_id, state)
                for provider_id, state in self._states.items()
            ]
        summary = {
            "supported": self.platform is not None,
            "platform": self.platform_key,
            "platform_notes": self.platform.notes if self.platform else "",
            "uv_version": self.platform.uv.version if self.platform else None,
            "engines": engines,
        }
        if self._catalog_summary is not None:
            summary["catalog"] = self._catalog_summary()
        return summary

    def refresh_manifest(self) -> None:
        engines, selected = load_local_ocr_installer_manifest(
            self._current_manifest_path(),
            platform_key=self.platform_key,
        )
        with self._state_lock:
            if any(state.operation is not None for state in self._states.values()):
                return
            self.engines = engines
            self.platform = selected
            self._states = {
                provider_id: self._states.get(
                    provider_id,
                    _EngineInstallState(
                        state=(
                            "installed"
                            if self._managed_install_is_valid(provider_id)
                            else "not_installed"
                        )
                    ),
                )
                for provider_id in engines
            }

    def _current_manifest_path(self) -> Path:
        value = (
            self._manifest_path()
            if callable(self._manifest_path)
            else self._manifest_path
        )
        return Path(value)

    def perform(self, payload: Mapping[str, object]) -> Dict[str, object]:
        provider_id = str(payload.get("provider_id") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        if provider_id not in self.engines:
            raise LocalOCRInstallerError("未知的本地 OCR 组件。")
        if action == "cancel":
            self._cancel(provider_id)
            return self.summary()
        if action not in {"install", "update", "validate", "uninstall"}:
            raise LocalOCRInstallerError("不支持的本地 OCR 组件操作。")
        if self.platform is None:
            raise LocalOCRInstallerError(
                f"当前平台不在首发安装矩阵中：{self.platform_key}"
            )
        self._start_operation(provider_id, action)
        return self.summary()

    def _start_operation(self, provider_id: str, action: str) -> None:
        with self._state_lock:
            state = self._states[provider_id]
            if state.state in ACTIVE_INSTALL_STATES:
                raise LocalOCRInstallerError("该组件正在执行其他操作。")
            installed = self._managed_install_is_valid(provider_id)
            if action == "install" and installed:
                raise LocalOCRInstallerError("该组件已安装。")
            if action == "update" and not self._update_available(provider_id):
                raise LocalOCRInstallerError("该组件没有可安装的更新。")
            if action in {"validate", "uninstall"} and not installed:
                raise LocalOCRInstallerError("该组件尚未由 MEFinder 安装。")
        if not self._operation_lock.acquire(blocking=False):
            raise LocalOCRInstallerError("另一个 OCR 组件正在操作，请稍后再试。")
        engine_lock = local_ocr_engine_lock(provider_id)
        if not engine_lock.acquire(blocking=False):
            self._operation_lock.release()
            raise LocalOCRInstallerError(
                "该组件正在执行 OCR 任务，请等任务结束后再试。"
            )
        initial_state = {
            "install": "downloading",
            "update": "downloading",
            "validate": "validating",
            "uninstall": "cleaning",
        }[action]
        with self._state_lock:
            state = self._states[provider_id]
            state.state = initial_state
            state.operation = action
            state.progress = None
            state.downloaded_bytes = 0
            state.total_bytes = 0
            state.download_speed_bps = 0.0
            state.eta_seconds = None
            state.download_samples = []
            state.message = ""
            state.error = ""
            state.cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._operation_worker,
                args=(provider_id, action, engine_lock),
                name=f"local-ocr-{action}-{provider_id}",
                daemon=True,
            )
            state.thread = thread
        thread.start()

    def _operation_worker(
        self,
        provider_id: str,
        action: str,
        engine_lock: threading.Lock,
    ) -> None:
        try:
            if action in {"install", "update"}:
                self._install(provider_id)
                self._set_state(
                    provider_id,
                    "installed",
                    progress=1.0,
                    message="安装并验证完成",
                )
            elif action == "validate":
                self._revalidate(provider_id)
                self._set_state(
                    provider_id,
                    "installed",
                    progress=1.0,
                    message="重新验证通过",
                )
            else:
                self._uninstall(provider_id)
                self._set_state(
                    provider_id,
                    "not_installed",
                    progress=None,
                    message="已卸载",
                )
        except _InstallCancelled:
            fallback = (
                "not_installed" if action == "install" else "installed"
            )
            self._set_state(
                provider_id,
                fallback,
                progress=None,
                message="操作已取消",
            )
        except (
            LocalOCRInstallerError,
            LocalOCRError,
            ResumeManifestError,
            OSError,
            tarfile.TarError,
            zipfile.BadZipFile,
            json.JSONDecodeError,
            KeyError,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            fallback = (
                "installed"
                if self._managed_install_is_valid(provider_id)
                else "not_installed"
            )
            self._set_state(
                provider_id,
                fallback,
                progress=None,
                error=str(exc),
                message="操作失败",
            )
        finally:
            with self._state_lock:
                state = self._states[provider_id]
                state.process = None
                state.operation = None
                state.thread = None
            engine_lock.release()
            self._operation_lock.release()

    def _install(self, provider_id: str) -> None:
        assert self.platform is not None
        engine = self.engines[provider_id]
        staging = self.component_root / (
            f".staging-{provider_id}-{uuid.uuid4().hex}"
        )
        final = self.component_root / provider_id
        uv_created = False
        self.component_root.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        try:
            archive = staging / "source.tar.gz"
            self._set_state(
                provider_id,
                "downloading",
                message="正在下载源码与模型",
                total_bytes=engine.tarball_size,
            )
            self._download_file(
                provider_id,
                engine.tarball_url,
                archive,
                engine.tarball_size,
            )
            self._set_state(
                provider_id,
                "verifying",
                progress=None,
                message="正在校验源码包",
            )
            self._verify_file(
                archive,
                expected_size=engine.tarball_size,
                expected_sha256=engine.tarball_sha256,
            )
            self._set_state(
                provider_id,
                "extracting",
                message="正在解压组件",
            )
            source_dir = staging / "source"
            self._extract_engine_archive(provider_id, archive, source_dir, engine)
            archive.unlink()
            self._set_state(
                provider_id,
                "provisioning",
                message="正在准备 uv 与独立 Python",
            )
            uv_path, uv_created = self._ensure_uv(provider_id)
            venv_dir = staging / "venv"
            environment = os.environ.copy()
            environment.update(
                {
                    "UV_CACHE_DIR": str(staging / ".uv-cache"),
                    "UV_PYTHON_INSTALL_DIR": str(self.component_root / "_python"),
                    "UV_NO_PROGRESS": "1",
                }
            )
            install_log = staging / "install.log"
            self._run_command(
                provider_id,
                [
                    str(uv_path),
                    "venv",
                    "--python",
                    self.platform.python,
                    "--managed-python",
                    "--relocatable",
                    str(venv_dir),
                ],
                cwd=staging,
                environment=environment,
                log_path=install_log,
                timeout=1800,
                track_uv_downloads=True,
            )
            python_path = staging / self.platform.venv_python
            dependencies = (*engine.dependencies, self.platform.onnxruntime)
            self._set_state(
                provider_id,
                "provisioning",
                message="正在安装已固定的 CPU 依赖",
            )
            self._run_command(
                provider_id,
                [
                    str(uv_path),
                    "pip",
                    "install",
                    "--python",
                    str(python_path),
                    *dependencies,
                ],
                cwd=staging,
                environment=environment,
                log_path=install_log,
                timeout=1800,
                track_uv_downloads=True,
            )
            self._write_sbom(provider_id, python_path, staging / "sbom.spdx.json")
            self._set_state(
                provider_id,
                "validating",
                message="正在启动 CLI 并识别自带样图",
            )
            self._validate_runtime(
                provider_id,
                python_path=python_path,
                script_path=source_dir / engine.script_path,
                source_dir=source_dir,
                work_dir=staging / ".validation",
                log_path=staging / "validation.log",
            )
            shutil.rmtree(staging / ".validation")
            self._remove_tree(staging / ".uv-cache")
            self._write_install_receipt(staging, engine, dependencies)
            previous = self.component_root / (
                f".previous-{provider_id}-{uuid.uuid4().hex}"
            )
            published = False
            try:
                if final.exists():
                    final.replace(previous)
                staging.replace(final)
                published = True
                final_python = final / self.platform.venv_python
                final_script = final / "source" / engine.script_path
                configure_managed_local_ocr_engine(
                    self.config_path,
                    provider_id,
                    python_path=final_python,
                    script_path=final_script,
                )
            except (LocalOCRError, ResumeManifestError, OSError):
                if published:
                    self._remove_tree(final)
                if previous.exists():
                    previous.replace(final)
                raise
            self._remove_tree(previous)
        finally:
            self._remove_tree(staging)
            if not self._managed_install_is_valid(provider_id):
                if uv_created:
                    self._remove_uv_tool()
                self._cleanup_shared_runtime_if_unused()

    def _revalidate(self, provider_id: str) -> None:
        assert self.platform is not None
        engine = self.engines[provider_id]
        final = self.component_root / provider_id
        work_dir = self.component_root / (
            f".validation-{provider_id}-{uuid.uuid4().hex}"
        )
        try:
            self._validate_runtime(
                provider_id,
                python_path=final / self.platform.venv_python,
                script_path=final / "source" / engine.script_path,
                source_dir=final / "source",
                work_dir=work_dir,
                log_path=work_dir / "validation.log",
            )
        finally:
            self._remove_tree(work_dir)

    def _uninstall(self, provider_id: str) -> None:
        assert self.platform is not None
        engine = self.engines[provider_id]
        final = self.component_root / provider_id
        python_path = final / self.platform.venv_python
        script_path = final / "source" / engine.script_path
        clear_managed_local_ocr_engine(
            self.config_path,
            provider_id,
            python_path=python_path,
            script_path=script_path,
        )
        shutil.rmtree(final)
        self._cleanup_shared_runtime_if_unused()

    def _download_file(
        self,
        provider_id: str,
        url: str,
        target: Path,
        expected_size: int,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.stat().st_size if target.exists() else 0
        if existing > expected_size:
            target.unlink()
            existing = 0
        self._begin_download_progress(provider_id, existing, expected_size)
        if existing == expected_size:
            return
        attempts = 0
        while attempts < 4:
            self._raise_if_cancelled(provider_id)
            existing = target.stat().st_size if target.exists() else 0
            if existing == expected_size:
                return
            if existing > expected_size:
                target.unlink()
                existing = 0
                self._begin_download_progress(provider_id, 0, expected_size)
            headers = {"User-Agent": "MEFinder-local-ocr-installer"}
            if existing:
                headers["Range"] = f"bytes={existing}-"
            request = Request(url, headers=headers)
            try:
                with urlopen(request, timeout=30) as response:
                    status = getattr(response, "status", None)
                    if existing and status != 206:
                        replayed = 0
                        while replayed < existing:
                            self._raise_if_cancelled(provider_id)
                            chunk = response.read(
                                min(1024 * 1024, existing - replayed)
                            )
                            if not chunk:
                                raise LocalOCRInstallerError(
                                    "服务器无法重放下载断点。"
                                )
                            replayed += len(chunk)
                    mode = "ab" if existing else "wb"
                    downloaded = existing
                    with target.open(mode) as output:
                        while True:
                            self._raise_if_cancelled(provider_id)
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            downloaded += len(chunk)
                            self._set_download_progress(
                                provider_id, downloaded, expected_size
                            )
                if downloaded == expected_size:
                    return
                attempts += 1
            except HTTPError as exc:
                if exc.code == 416 and existing == expected_size:
                    return
                attempts += 1
                if attempts >= 4:
                    raise LocalOCRInstallerError(
                        f"下载失败（HTTP {exc.code}）。"
                    ) from exc
            except (URLError, TimeoutError, ConnectionError) as exc:
                attempts += 1
                if attempts >= 4:
                    raise LocalOCRInstallerError(f"下载中断：{exc}") from exc
        raise LocalOCRInstallerError("下载中断，重试后仍未完成。")

    def _verify_file(
        self,
        path: Path,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        if path.stat().st_size != expected_size:
            raise LocalOCRInstallerError("下载文件大小与清单不一致。")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        if digest.hexdigest().lower() != expected_sha256.lower():
            raise LocalOCRInstallerError("SHA-256 校验失败，已取消安装。")

    def _extract_engine_archive(
        self,
        provider_id: str,
        archive_path: Path,
        destination: Path,
        engine: EngineManifest,
    ) -> None:
        prefix = PurePosixPath(engine.archive_root)
        destination.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                self._raise_if_cancelled(provider_id)
                member_path = PurePosixPath(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or not member_path.parts
                    or member_path.parts[0] != prefix.name
                ):
                    raise LocalOCRInstallerError("源码包包含不安全路径。")
                relative = Path(*member_path.parts[1:])
                if not relative.parts:
                    continue
                target = destination / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise LocalOCRInstallerError("源码包包含不支持的文件类型。")
                source = archive.extractfile(member)
                if source is None:
                    raise LocalOCRInstallerError("源码包文件无法读取。")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    while True:
                        self._raise_if_cancelled(provider_id)
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
        required = (
            destination / engine.script_path,
            destination / engine.sample_path,
            destination / engine.license_path,
        )
        if not all(path.is_file() for path in required):
            raise LocalOCRInstallerError("源码包缺少 CLI、样图或许可证。")

    def _ensure_uv(self, provider_id: str) -> tuple[Path, bool]:
        assert self.platform is not None
        tool_dir = self._uv_tool_dir()
        executable = tool_dir / PurePosixPath(self.platform.uv.member).name
        receipt = tool_dir / "receipt.json"
        if executable.is_file() and receipt.is_file():
            return executable, False
        staging = self.component_root / f".uv-staging-{uuid.uuid4().hex}"
        archive = staging / "uv-archive"
        staging.mkdir(parents=True)
        try:
            self._set_state(
                provider_id,
                "provisioning",
                message=f"正在下载 uv {self.platform.uv.version}",
                total_bytes=self.platform.uv.size,
                downloaded_bytes=0,
            )
            self._download_file(
                provider_id,
                self.platform.uv.url,
                archive,
                self.platform.uv.size,
            )
            self._verify_file(
                archive,
                expected_size=self.platform.uv.size,
                expected_sha256=self.platform.uv.sha256,
            )
            extracted = staging / executable.name
            if self.platform.uv.archive_type == "zip":
                with zipfile.ZipFile(archive) as bundle:
                    info = bundle.getinfo(self.platform.uv.member)
                    with bundle.open(info) as source, extracted.open("wb") as output:
                        shutil.copyfileobj(source, output)
            elif self.platform.uv.archive_type == "tar.gz":
                with tarfile.open(archive, "r:gz") as bundle:
                    member = bundle.getmember(self.platform.uv.member)
                    if not member.isfile():
                        raise LocalOCRInstallerError("uv 归档入口无效。")
                    source = bundle.extractfile(member)
                    if source is None:
                        raise LocalOCRInstallerError("uv 可执行文件无法读取。")
                    with source, extracted.open("wb") as output:
                        shutil.copyfileobj(source, output)
            else:
                raise LocalOCRInstallerError("uv 归档格式不受支持。")
            extracted.chmod(0o755)
            atomic_write_json(
                staging / "receipt.json",
                {
                    "version": self.platform.uv.version,
                    "url": self.platform.uv.url,
                    "size": self.platform.uv.size,
                    "sha256": self.platform.uv.sha256,
                    "license": "Apache-2.0 OR MIT",
                },
            )
            archive.unlink()
            tool_dir.parent.mkdir(parents=True, exist_ok=True)
            if tool_dir.exists():
                shutil.rmtree(tool_dir)
            staging.replace(tool_dir)
            return tool_dir / executable.name, True
        finally:
            self._remove_tree(staging)

    def _validate_runtime(
        self,
        provider_id: str,
        *,
        python_path: Path,
        script_path: Path,
        source_dir: Path,
        work_dir: Path,
        log_path: Path,
    ) -> None:
        engine = self.engines[provider_id]
        work_dir.mkdir(parents=True, exist_ok=True)
        self._run_command(
            provider_id,
            [str(python_path), str(script_path), "--help"],
            cwd=script_path.parent,
            environment=os.environ.copy(),
            log_path=log_path,
            timeout=60,
        )
        source_sample = source_dir / engine.sample_path
        validation_input = work_dir / "validation-input.png"
        if source_sample.suffix.lower() == ".png":
            shutil.copyfile(source_sample, validation_input)
        else:
            self._run_command(
                provider_id,
                [
                    str(python_path),
                    "-c",
                    (
                        "from PIL import Image; import sys; "
                        "Image.open(sys.argv[1]).convert('RGB').save("
                        "sys.argv[2], format='PNG')"
                    ),
                    str(source_sample),
                    str(validation_input),
                ],
                cwd=work_dir,
                environment=os.environ.copy(),
                log_path=log_path,
                timeout=60,
            )
        output_dir = work_dir / "output"
        output_dir.mkdir()
        self._run_command(
            provider_id,
            [
                str(python_path),
                str(script_path),
                "--sourceimg",
                str(validation_input),
                "--output",
                str(output_dir),
                *engine.cli_extra_args,
            ],
            cwd=script_path.parent,
            environment=os.environ.copy(),
            log_path=log_path,
            timeout=900,
        )
        result_path = output_dir / "validation-input.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalOCRInstallerError(
                "样图识别未产生有效 JSON。"
            ) from exc
        if not isinstance(result, Mapping):
            raise LocalOCRInstallerError("样图识别 JSON 结构无效。")

    def _run_command(
        self,
        provider_id: str,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
        timeout: int,
        track_uv_downloads: bool = False,
    ) -> None:
        self._raise_if_cancelled(provider_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as output:
            progress_log_offset = output.tell()
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
            with self._state_lock:
                self._states[provider_id].process = process
            deadline = time.monotonic() + timeout
            next_progress_update = 0.0
            try:
                while process.poll() is None:
                    if self._states[provider_id].cancel_event.wait(0.1):
                        self._stop_process(process)
                        raise _InstallCancelled("操作已取消。")
                    now = time.monotonic()
                    if track_uv_downloads and now >= next_progress_update:
                        self._update_uv_download_progress(
                            provider_id,
                            log_path,
                            start_offset=progress_log_offset,
                        )
                        next_progress_update = now + 0.5
                    if now >= deadline:
                        self._stop_process(process)
                        raise LocalOCRInstallerError("安装子进程超时。")
            finally:
                with self._state_lock:
                    self._states[provider_id].process = None
        if track_uv_downloads:
            self._update_uv_download_progress(
                provider_id,
                log_path,
                start_offset=progress_log_offset,
            )
        if self._states[provider_id].cancel_event.is_set():
            raise _InstallCancelled("操作已取消。")
        if process.returncode:
            detail = log_path.read_text(
                encoding="utf-8", errors="replace"
            )[-2000:].strip()
            raise LocalOCRInstallerError(
                f"安装子进程退出 {process.returncode}：{detail}"
            )

    def _write_sbom(
        self,
        provider_id: str,
        python_path: Path,
        output_path: Path,
    ) -> None:
        raw_path = output_path.with_name(".sbom-packages.json")
        script = (
            "import importlib.metadata as m, json, sys; items=[]; "
            "[(items.append({'name': d.metadata.get('Name') or d.name, "
            "'version': d.version, 'license': d.metadata.get('License-Expression') "
            "or d.metadata.get('License') or 'NOASSERTION', "
            "'license_classifiers': [v for v in d.metadata.get_all('Classifier', []) "
            "if v.startswith('License ::')], "
            "'license_files': [str(f) for f in (d.files or []) if "
            "'.dist-info/licenses/' in str(f).lower() or "
            "str(f).lower().endswith(('.dist-info/license', '.dist-info/copying'))]})) "
            "for d in m.distributions()]; "
            "open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(items))"
        )
        self._run_command(
            provider_id,
            [str(python_path), "-c", script, str(raw_path)],
            cwd=output_path.parent,
            environment=os.environ.copy(),
            log_path=output_path.with_name("sbom.log"),
            timeout=60,
        )
        packages = json.loads(raw_path.read_text(encoding="utf-8"))
        raw_path.unlink()
        now = datetime.now(timezone.utc).isoformat()
        atomic_write_json(
            output_path,
            {
                "spdxVersion": "SPDX-2.3",
                "dataLicense": "CC0-1.0",
                "SPDXID": "SPDXRef-DOCUMENT",
                "name": f"MEFinder-{provider_id}-runtime",
                "documentNamespace": f"https://mefinder.local/spdx/{uuid.uuid4()}",
                "creationInfo": {
                    "created": now,
                    "creators": ["Tool: MEFinder-local-ocr-installer"],
                },
                "packages": [
                    {
                        "SPDXID": f"SPDXRef-Package-{index}",
                        "name": item["name"],
                        "versionInfo": item["version"],
                        "downloadLocation": "NOASSERTION",
                        "filesAnalyzed": False,
                        "licenseConcluded": "NOASSERTION",
                        "licenseDeclared": "NOASSERTION",
                    }
                    for index, item in enumerate(packages, start=1)
                ],
                "license_inventory": packages,
            },
        )

    def _write_install_receipt(
        self,
        staging: Path,
        engine: EngineManifest,
        dependencies: Sequence[str],
    ) -> None:
        assert self.platform is not None
        atomic_write_json(
            staging / "installed.json",
            {
                "schema_version": 1,
                "provider_id": engine.provider_id,
                "display_name": engine.display_name,
                "version": engine.version,
                "tag": engine.tag,
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "platform": self.platform.key,
                "python": self.platform.python,
                "source": {
                    "url": engine.tarball_url,
                    "size": engine.tarball_size,
                    "sha256": engine.tarball_sha256,
                    "license": engine.license,
                    "license_path": str(Path("source") / engine.license_path),
                    "attribution": engine.attribution,
                    "modification_notice": engine.modification_notice,
                },
                "uv": {
                    "version": self.platform.uv.version,
                    "url": self.platform.uv.url,
                    "size": self.platform.uv.size,
                    "sha256": self.platform.uv.sha256,
                },
                "dependencies": list(dependencies),
                "sbom": "sbom.spdx.json",
                "validation": {
                    "help": True,
                    "sample": str(engine.sample_path),
                    "input_mode": "single-page-png",
                    "json": True,
                },
            },
        )

    def _managed_install_is_valid(self, provider_id: str) -> bool:
        final = self.component_root / provider_id
        receipt_path = final / "installed.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(receipt, Mapping):
            return False
        engine = self.engines.get(provider_id)
        if engine is None or receipt.get("provider_id") != provider_id:
            return False
        if self.platform is None:
            return False
        return (
            (final / self.platform.venv_python).is_file()
            and (final / "source" / engine.script_path).is_file()
        )

    def _engine_summary(
        self,
        provider_id: str,
        state: _EngineInstallState,
    ) -> Dict[str, object]:
        engine = self.engines[provider_id]
        installed_at = None
        installed_tag = None
        receipt_path = self.component_root / provider_id / "installed.json"
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if isinstance(receipt, Mapping):
                    installed_at = receipt.get("installed_at")
                    installed_tag = receipt.get("tag")
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "provider_id": provider_id,
            "display_name": engine.display_name,
            "version": engine.version,
            "tag": engine.tag,
            "state": state.state,
            "operation": state.operation,
            "progress": state.progress,
            "downloaded_bytes": state.downloaded_bytes,
            "total_bytes": state.total_bytes,
            "download_speed_bps": round(state.download_speed_bps),
            "eta_seconds": state.eta_seconds,
            "message": state.message,
            "error": state.error,
            "managed": self._managed_install_is_valid(provider_id),
            "installed_at": installed_at,
            "installed_tag": installed_tag,
            "update_available": self._update_available(provider_id),
        }

    def _update_available(self, provider_id: str) -> bool:
        if not self._managed_install_is_valid(provider_id):
            return False
        receipt_path = self.component_root / provider_id / "installed.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(receipt, Mapping)
            and receipt.get("tag") != self.engines[provider_id].tag
        )

    def _set_state(
        self,
        provider_id: str,
        install_state: str,
        *,
        progress: Optional[float] = None,
        downloaded_bytes: Optional[int] = None,
        total_bytes: Optional[int] = None,
        message: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._state_lock:
            state = self._states[provider_id]
            state.state = install_state
            state.progress = progress
            state.downloaded_bytes = downloaded_bytes or 0
            state.total_bytes = total_bytes or 0
            state.download_speed_bps = 0.0
            state.eta_seconds = None
            state.download_samples = []
            if message is not None:
                state.message = message
            if error is not None:
                state.error = error

    def _begin_download_progress(
        self,
        provider_id: str,
        downloaded: int,
        total: int,
    ) -> None:
        with self._state_lock:
            state = self._states[provider_id]
            state.downloaded_bytes = downloaded
            state.total_bytes = total
            state.progress = min(1.0, downloaded / total)
            state.download_speed_bps = 0.0
            state.eta_seconds = None
            state.download_samples = [(time.monotonic(), downloaded)]

    def _update_uv_download_progress(
        self,
        provider_id: str,
        log_path: Path,
        *,
        start_offset: int = 0,
    ) -> None:
        with log_path.open("rb") as log:
            log.seek(start_offset)
            text = log.read().decode("utf-8", errors="replace")
        announced = {
            name: round(float(size) * _BINARY_SIZE_MULTIPLIERS[unit])
            for name, size, unit in _UV_DOWNLOAD_PATTERN.findall(text)
        }
        if not announced:
            return
        completed_names = set(_UV_DOWNLOADED_PATTERN.findall(text))
        completed = sum(
            size for name, size in announced.items() if name in completed_names
        )
        total = sum(announced.values())
        with self._state_lock:
            has_samples = bool(self._states[provider_id].download_samples)
        if not has_samples:
            self._begin_download_progress(provider_id, 0, total)
        self._set_download_progress(
            provider_id,
            completed,
            total,
            rolling_window=False,
        )

    def _set_download_progress(
        self,
        provider_id: str,
        downloaded: int,
        total: int,
        *,
        rolling_window: bool = True,
    ) -> None:
        with self._state_lock:
            state = self._states[provider_id]
            state.downloaded_bytes = downloaded
            state.total_bytes = total
            state.progress = min(1.0, downloaded / total)
            now = time.monotonic()
            state.download_samples.append((now, downloaded))
            if rolling_window:
                cutoff = now - 8
                while (
                    len(state.download_samples) > 2
                    and state.download_samples[1][0] < cutoff
                ):
                    state.download_samples.pop(0)
            started_at, started_bytes = state.download_samples[0]
            elapsed = now - started_at
            transferred = downloaded - started_bytes
            if elapsed >= 0.5 and transferred > 0:
                state.download_speed_bps = transferred / elapsed
                remaining = max(0, total - downloaded)
                state.eta_seconds = math.ceil(
                    remaining / state.download_speed_bps
                )
            elif elapsed >= 0.5:
                state.download_speed_bps = 0.0
                state.eta_seconds = None

    def _cancel(self, provider_id: str) -> None:
        with self._state_lock:
            state = self._states[provider_id]
            if state.state not in ACTIVE_INSTALL_STATES:
                raise LocalOCRInstallerError("该组件当前没有可取消的操作。")
            state.cancel_event.set()
            process = state.process
            state.message = "正在取消并清理…"
        if process is not None:
            self._stop_process(process)

    def _raise_if_cancelled(self, provider_id: str) -> None:
        if self._states[provider_id].cancel_event.is_set():
            raise _InstallCancelled("操作已取消。")

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _uv_tool_dir(self) -> Path:
        assert self.platform is not None
        return (
            self.component_root
            / "_tools"
            / f"uv-{self.platform.uv.version}-{self.platform.key}"
        )

    def _remove_uv_tool(self) -> None:
        if self.platform is not None:
            self._remove_tree(self._uv_tool_dir())

    def _cleanup_shared_runtime_if_unused(self) -> None:
        if any(
            self._managed_install_is_valid(provider_id)
            for provider_id in self.engines
        ):
            return
        self._remove_tree(self.component_root / "_tools")
        self._remove_tree(self.component_root / "_python")
        for directory in (self.component_root, self.component_root.parent):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)

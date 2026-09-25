"""Install and supervise MEFinder-managed MinerU runtimes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import shutil
import socket
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
from urllib.request import Request, getproxies, urlopen

from .import_resume import atomic_write_json
from .local_ocr_installer import (
    LOCAL_OCR_MANIFEST_FILE,
    PlatformManifest,
    current_platform_key,
    load_local_ocr_installer_manifest,
)
from .mineru_local_provider import MinerULocalConfig, MinerULocalProvider
from .parser_provider import ParserProviderError
from .mineru_local_settings import (
    clear_managed_mineru,
    configure_managed_mineru,
    mineru_local_config_summary,
)
from .tasks import TaskEvent


MANAGED_MINERU_COMPONENT_DIR = "components/mineru"
ACTIVE_STATES = frozenset(
    {"provisioning", "downloading_models", "validating", "starting", "cleaning"}
)
_MODEL_DOWNLOAD_ESTIMATES = {
    "pipeline": 2_595_586_833,
    "vlm": 2_328_028_720,
}
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
_MODEL_DOWNLOAD_ATTEMPTS = 3
_MODEL_DOWNLOAD_RETRY_DELAYS = (2, 5)
_MODEL_DOWNLOAD_STALL_SECONDS = 120
_PYPI_INSTALL_STALL_SECONDS = 300
_DOWNLOAD_SPEED_WINDOW_SECONDS = 30
_MODEL_NETWORK_ERROR_PATTERN = re.compile(
    r"ConnectionError|Network error|Request middleware error|ConnectError|"
    r"ReadTimeout|ReadError|Connection reset|Connection aborted|"
    r"Temporary failure in name resolution|I/O error|"
    r"error decoding response body|IncompleteRead|ChunkedEncodingError|"
    r"模型下载连续 2 分钟无数据变化",
    re.IGNORECASE,
)


class ManagedMinerUError(RuntimeError):
    pass


class _Cancelled(ManagedMinerUError):
    pass


@dataclass(frozen=True)
class MinerUProfile:
    profile_id: str
    display_name: str
    packages: tuple[str, ...]
    model_type: str
    backend: str
    minimum_memory_gb: int
    minimum_disk_gb: int
    tier: str = "standard"
    minimum_vram_gb: int = 0
    model_download_bytes: int = 0


@dataclass(frozen=True)
class MinerUManifest:
    version: str
    python: str
    profiles: Dict[str, MinerUProfile]
    supported_profiles: tuple[str, ...]
    platform: Optional[PlatformManifest]
    series: str = ""
    config_style: str = "tools_json"
    model_download_args: tuple[str, ...] = ()
    pinned_version: str = ""
    #: Every shipped series' config style, so an installed component can be
    #: read back by its own receipt even while the target version differs.
    series_config_styles: Mapping[str, str] = field(default_factory=dict)


@dataclass
class _ProfileState:
    state: str = "not_installed"
    operation: Optional[str] = None
    progress: Optional[float] = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    total_is_estimate: bool = False
    download_speed_bps: float = 0.0
    eta_seconds: Optional[int] = None
    download_samples: list[tuple[float, int]] = field(default_factory=list)
    message: str = ""
    error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    process: Optional[subprocess.Popen] = None
    thread: Optional[threading.Thread] = None


_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_MINERU_REQUIREMENT = re.compile(r"^mineru(\[[a-z0-9,_-]+\])?==(?P<version>[^\s]+)$")


def _version_key(value: str) -> Optional[tuple[int, int, int]]:
    match = _VERSION_PATTERN.match(str(value or "").strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _series_of(version: str) -> str:
    key = _version_key(version)
    return str(key[0]) if key else ""


def resolve_managed_mineru_version(
    path: Path,
    *,
    platform_key: Optional[str] = None,
    fetch: Optional[Callable[[str], bytes]] = None,
) -> Dict[str, object]:
    """Pick the newest MinerU release inside the manifest's compatible range.

    The pinned manifest version is always a usable answer: when PyPI cannot be
    reached, returns something unparseable, or offers nothing newer in range,
    the pinned version is returned unchanged so installs keep working offline.
    """

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedMinerUError("MinerU 组件清单无法读取。") from exc
    raw = payload.get("mineru") if isinstance(payload, Mapping) else None
    if not isinstance(raw, Mapping):
        raise ManagedMinerUError("组件清单缺少 MinerU 定义。")
    pinned = str(raw.get("version") or "")
    upgrade = raw.get("auto_upgrade")
    result: Dict[str, object] = {
        "pinned": pinned,
        "resolved": pinned,
        "source": "manifest",
        "detail": "",
    }
    if not isinstance(upgrade, Mapping):
        return result
    metadata_url = str(upgrade.get("metadata_url") or "")
    minimum = _version_key(str(upgrade.get("minimum") or pinned))
    below = _version_key(str(upgrade.get("below") or ""))
    pinned_key = _version_key(pinned)
    if not metadata_url or minimum is None or below is None or pinned_key is None:
        return result
    supported_series = {
        str(key) for key in (raw.get("series") or {})
    } if isinstance(raw.get("series"), Mapping) else set()
    reader = fetch or _read_url_bytes
    try:
        metadata = json.loads(reader(metadata_url).decode("utf-8"))
    except Exception as exc:  # network, TLS, JSON, decoding - all non-fatal
        result["detail"] = f"无法读取 PyPI 版本列表：{exc}"
        return result
    releases = metadata.get("releases") if isinstance(metadata, Mapping) else None
    if not isinstance(releases, Mapping):
        result["detail"] = "PyPI 版本列表结构无法识别。"
        return result
    best = pinned_key
    for candidate, files in releases.items():
        key = _version_key(str(candidate))
        if key is None or key <= best or not (minimum <= key < below):
            continue
        if str(key[0]) not in supported_series:
            continue
        if not isinstance(files, list) or not files:
            continue
        if all(
            isinstance(item, Mapping) and item.get("yanked") for item in files
        ):
            continue
        best = key
    resolved = ".".join(str(part) for part in best)
    result["resolved"] = resolved
    result["source"] = "pypi" if resolved != pinned else "manifest"
    return result


def _read_url_bytes(url: str) -> bytes:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=15) as response:  # noqa: S310 - manifest URL
        return response.read(8 * 1024 * 1024)


def load_managed_mineru_manifest(
    path: Path,
    *,
    platform_key: Optional[str] = None,
    version: Optional[str] = None,
) -> MinerUManifest:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedMinerUError("MinerU 组件清单无法读取。") from exc
    raw = payload.get("mineru") if isinstance(payload, Mapping) else None
    if not isinstance(raw, Mapping):
        raise ManagedMinerUError("组件清单缺少 MinerU 定义。")
    raw_profiles = raw.get("profiles")
    raw_platforms = raw.get("platforms")
    raw_series = raw.get("series")
    if (
        not isinstance(raw_profiles, Mapping)
        or not isinstance(raw_platforms, Mapping)
        or not isinstance(raw_series, Mapping)
    ):
        raise ManagedMinerUError("MinerU 组件清单结构无效。")
    selected_key = platform_key or current_platform_key()
    supported = raw_platforms.get(selected_key, [])
    if not isinstance(supported, list):
        raise ManagedMinerUError("MinerU 平台安装矩阵无效。")
    pinned = str(raw.get("version") or "")
    resolved_version = str(version or pinned)
    series = _series_of(resolved_version)
    series_manifest = raw_series.get(series)
    if not isinstance(series_manifest, Mapping):
        raise ManagedMinerUError(
            f"MinerU {resolved_version} 属于未支持的大版本系列，请更新 MEFinder。"
        )
    series_packages = series_manifest.get("packages")
    if not isinstance(series_packages, Mapping):
        raise ManagedMinerUError("MinerU 系列安装配方无效。")
    profiles: Dict[str, MinerUProfile] = {}
    for profile_id, item in raw_profiles.items():
        if not isinstance(profile_id, str) or not isinstance(item, Mapping):
            raise ManagedMinerUError("MinerU 组件配置无效。")
        packages = _series_profile_packages(
            series_packages.get(profile_id), selected_key, resolved_version
        )
        if profile_id in supported and not packages:
            raise ManagedMinerUError("MinerU 安装包未固定到目标版本。")
        profiles[profile_id] = MinerUProfile(
            profile_id=profile_id,
            display_name=str(item.get("display_name") or profile_id),
            packages=packages,
            model_type=str(item.get("model_type") or ""),
            backend=str(item.get("backend") or ""),
            tier=str(item.get("tier") or "standard"),
            minimum_memory_gb=int(item.get("minimum_memory_gb") or 0),
            minimum_disk_gb=int(item.get("minimum_disk_gb") or 0),
            minimum_vram_gb=int(item.get("minimum_vram_gb") or 0),
            model_download_bytes=int(
                item.get("model_download_bytes")
                or _MODEL_DOWNLOAD_ESTIMATES.get(profile_id, 0)
            ),
        )
    if any(
        not isinstance(item, str) or item not in profiles for item in supported
    ):
        raise ManagedMinerUError("MinerU 平台安装矩阵无效。")
    download_args = series_manifest.get("model_download_args")
    if not isinstance(download_args, list) or not all(
        isinstance(item, str) for item in download_args
    ):
        raise ManagedMinerUError("MinerU 模型下载参数无效。")
    _engines, selected_platform = load_local_ocr_installer_manifest(
        Path(path), platform_key=selected_key
    )
    return MinerUManifest(
        version=resolved_version,
        python=str(raw.get("python") or ""),
        profiles=profiles,
        supported_profiles=tuple(supported),
        platform=selected_platform,
        series=series,
        config_style=str(series_manifest.get("config_style") or "tools_json"),
        model_download_args=tuple(download_args),
        pinned_version=pinned,
        series_config_styles={
            str(key): str(item.get("config_style") or "tools_json")
            for key, item in raw_series.items()
            if isinstance(item, Mapping)
        },
    )


def _series_profile_packages(
    entry: object, platform_key: str, version: str
) -> tuple[str, ...]:
    """Resolve one profile's requirement list for a platform and version."""

    if not isinstance(entry, Mapping):
        return ()
    raw = entry.get(platform_key)
    if raw is None:
        raw = entry.get("default")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return ()
    packages: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return ()
        requirement = item.replace("{version}", version)
        match = _MINERU_REQUIREMENT.match(requirement)
        if match is not None and match.group("version") != version:
            # A mineru requirement that is not pinned to the resolved version
            # would silently install a different protocol generation.
            return ()
        packages.append(requirement)
    if not any(_MINERU_REQUIREMENT.match(item) for item in packages):
        return ()
    return tuple(packages)


def detect_mineru_hardware(
    *,
    platform_key: Optional[str] = None,
    command_runner: Callable = subprocess.run,
) -> Dict[str, object]:
    key = platform_key or current_platform_key()
    if key == "darwin-arm64":
        memory_mb = None
        detection_error = ""
        try:
            result = command_runner(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            memory_mb = int(result.stdout.strip()) // (1024 * 1024)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            detection_error = f"无法读取统一内存：{exc}"
        version = platform.mac_ver()[0]
        try:
            major_version = int(version.split(".", 1)[0])
        except ValueError:
            major_version = 0
            detection_error = detection_error or "无法识别 macOS 版本"
        supported = (
            memory_mb is not None
            and memory_mb >= 16 * 1024
            and major_version >= 14
        )
        return {
            "kind": "apple_silicon",
            "name": platform.processor() or "Apple Silicon",
            "vlm_supported": supported,
            "recommended_profile": "vlm" if supported else "pipeline",
            "vram_mb": None,
            "memory_mb": memory_mb,
            "compute_capability": None,
            "detection_error": detection_error,
        }
    if key not in {"win32-x86_64", "linux-x86_64"}:
        return {
            "kind": "cpu",
            "name": "CPU",
            "vlm_supported": False,
            "recommended_profile": "pipeline",
            "vram_mb": None,
            "compute_capability": None,
        }
    try:
        result = command_runner(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "kind": "cpu",
            "name": "CPU",
            "vlm_supported": False,
            "recommended_profile": "pipeline",
            "vram_mb": None,
            "compute_capability": None,
        }
    candidates = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            memory = int(float(parts[1]))
            capability = float(parts[2])
        except ValueError:
            continue
        candidates.append((memory, capability, parts[0]))
    if not candidates:
        raise ManagedMinerUError("nvidia-smi 未返回可识别的显卡信息。")
    memory, capability, name = max(candidates)
    supported = memory >= 8192 and capability >= 7.0
    return {
        "kind": "nvidia",
        "name": name,
        "vlm_supported": supported,
        "recommended_profile": "vlm" if supported else "pipeline",
        "vram_mb": memory,
        "compute_capability": capability,
    }


class ManagedMinerU:
    component_id = "mineru-local"

    def __init__(
        self,
        runtime_root: Path,
        config_path: Path,
        *,
        manifest_path: Path | Callable[[], Path] = LOCAL_OCR_MANIFEST_FILE,
        platform_key: Optional[str] = None,
        opener: Callable = urlopen,
        process_launcher: Callable = subprocess.Popen,
        hardware_detector: Optional[Callable[[], Dict[str, object]]] = None,
        catalog_summary: Optional[Callable[[], Dict[str, object]]] = None,
        version_fetch: Optional[Callable[[str], bytes]] = None,
    ) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.config_path = Path(config_path).resolve()
        self.component_root = self.runtime_root / MANAGED_MINERU_COMPONENT_DIR
        self._manifest_path = manifest_path
        self.platform_key = platform_key or current_platform_key()
        self.opener = opener
        self.process_launcher = process_launcher
        self._hardware_detector = hardware_detector or (
            lambda: detect_mineru_hardware(platform_key=self.platform_key)
        )
        self._hardware = self._hardware_detector()
        self._catalog_summary = catalog_summary
        self.manifest = load_managed_mineru_manifest(
            self._current_manifest_path(), platform_key=self.platform_key
        )
        self._version_fetch = version_fetch
        self._version_resolution: Dict[str, object] = {
            "pinned": self.manifest.pinned_version or self.manifest.version,
            "resolved": self.manifest.version,
            "source": "manifest",
            "detail": "",
        }
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._service_process: Optional[subprocess.Popen] = None
        self._service_profile = ""
        self._service_endpoint = ""
        self._service_log = None
        self._states = {
            profile_id: _ProfileState(
                state="installed" if self._installed(profile_id) else "not_installed"
            )
            for profile_id in self.manifest.profiles
        }

    def summary(self) -> Dict[str, object]:
        with self._lock:
            profiles = [
                self._profile_summary(profile_id, state)
                for profile_id, state in self._states.items()
            ]
            process = self._service_process
            running = process is not None and process.poll() is None
        result: Dict[str, object] = {
            "supported": self.manifest.platform is not None,
            "platform": self.platform_key,
            "version": self.manifest.version,
            "pinned_version": self.manifest.pinned_version or self.manifest.version,
            "series": self.manifest.series,
            "version_source": str(self._version_resolution.get("source") or "manifest"),
            "version_detail": str(self._version_resolution.get("detail") or ""),
            "hardware": dict(self._hardware),
            "profiles": profiles,
            "service": {
                "running": running,
                "profile": self._service_profile if running else "",
                "endpoint": self._service_endpoint if running else "",
            },
        }
        if self._catalog_summary is not None:
            result["catalog"] = self._catalog_summary()
        return result

    def refresh_available_version(self) -> Dict[str, object]:
        """Re-target the newest MinerU release the manifest declares compatible.

        Never raises on network trouble: an unreachable PyPI leaves the pinned
        manifest version in place, so the component stays installable offline.
        """

        with self._lock:
            busy = [
                profile_id
                for profile_id, state in self._states.items()
                if state.operation is not None
            ]
        if busy:
            # Re-targeting now would swap the recipe under a running install.
            with self._lock:
                self._version_resolution = {
                    **self._version_resolution,
                    "detail": "组件正在操作中，暂不检查新版本。",
                }
            return self.summary()
        resolution = self._resolve_version()
        with self._lock:
            self._version_resolution = dict(self._retarget(resolution))
        return self.summary()

    def _resolve_version(self) -> Dict[str, object]:
        return resolve_managed_mineru_version(
            self._current_manifest_path(),
            platform_key=self.platform_key,
            fetch=self._version_fetch,
        )

    def _retarget(self, resolution: Mapping[str, object]) -> Dict[str, object]:
        """Load the resolved version's recipe, keeping the current one on error."""

        resolved = str(resolution.get("resolved") or "")
        if not resolved or resolved == self.manifest.version:
            return dict(resolution)
        try:
            self.manifest = load_managed_mineru_manifest(
                self._current_manifest_path(),
                platform_key=self.platform_key,
                version=resolved,
            )
        except ManagedMinerUError as exc:
            return {
                **resolution,
                "resolved": self.manifest.version,
                "source": "manifest",
                "detail": str(exc),
            }
        return dict(resolution)

    def diagnostics(self) -> Dict[str, object]:
        summary = self.summary()
        profiles = summary["profiles"]
        return {
            "component_id": self.component_id,
            "supported": summary["supported"],
            "platform": summary["platform"],
            "installed": sum(bool(item["installed"]) for item in profiles),
            "active": sum(bool(item["operation"]) for item in profiles),
            "service_running": bool(summary["service"]["running"]),
        }

    def refresh_manifest(self) -> None:
        manifest = load_managed_mineru_manifest(
            self._current_manifest_path(), platform_key=self.platform_key
        )
        with self._lock:
            if any(state.operation for state in self._states.values()):
                return
            self.manifest = manifest
            self._states = {
                profile_id: self._states.get(
                    profile_id,
                    _ProfileState(
                        state=(
                            "installed" if self._installed(profile_id) else "not_installed"
                        )
                    ),
                )
                for profile_id in manifest.profiles
            }

    def perform(self, payload: Mapping[str, object]) -> Dict[str, object]:
        if str(payload.get("action") or "").strip().lower() == "check-updates":
            # Not profile-scoped: re-target the newest compatible release and
            # let the existing update_available flag drive the UI.
            return self.refresh_available_version()
        requested = str(payload.get("profile") or "auto").strip().lower()
        profile_id = (
            str(self._hardware["recommended_profile"])
            if requested == "auto"
            else requested
        )
        action = str(payload.get("action") or "").strip().lower()
        if profile_id not in self.manifest.profiles:
            raise ManagedMinerUError("未知的 MinerU 本地组件。")
        if profile_id not in self.manifest.supported_profiles:
            raise ManagedMinerUError("当前平台不支持该 MinerU 组件。")
        if profile_id == "vlm" and not self._hardware["vlm_supported"]:
            raise ManagedMinerUError("当前显卡未通过 VLM 最低要求检测。")
        if action == "cancel":
            self._cancel(profile_id)
            return self.summary()
        if action == "stop":
            self.stop()
            return self.summary()
        if action not in {"install", "update", "start", "validate", "uninstall"}:
            raise ManagedMinerUError("不支持的 MinerU 本地组件操作。")
        self._start_operation(profile_id, action)
        return self.summary()

    def start_installed_if_managed(self) -> bool:
        config = mineru_local_config_summary(self.config_path)
        profile_id = str(config.get("managed_profile") or "")
        if config.get("managed") is not True or not self._installed(profile_id):
            return False
        self._start_operation(profile_id, "start")
        return True

    def stop(self) -> None:
        with self._lock:
            process = self._service_process
            self._service_process = None
            self._service_profile = ""
            self._service_endpoint = ""
            log = self._service_log
            self._service_log = None
        if process is not None:
            self._stop_process(process)
        if log is not None:
            log.close()

    def close(self, timeout: float | None = 30) -> bool:
        """Cancel operations and wait for worker file handles to close."""

        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._lock:
            active = [state for state in self._states.values() if state.operation is not None]
            threads = [state.thread for state in active if state.thread is not None]
            processes = [state.process for state in active if state.process is not None]
            for state in active:
                state.cancel_event.set()
        for process in processes:
            self._stop_process(process)
        self.stop()
        for thread in threads:
            thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        stopped = all(not thread.is_alive() for thread in threads)
        if stopped:
            self.stop()
        return stopped

    def _start_operation(self, profile_id: str, action: str) -> None:
        installed = self._installed(profile_id)
        update_available = self._update_available(profile_id)
        if action == "install" and installed:
            raise ManagedMinerUError("该 MinerU 组件已安装。")
        if action == "update" and not update_available:
            raise ManagedMinerUError("该 MinerU 组件没有可安装的更新。")
        if action in {"start", "validate", "uninstall"} and not installed:
            raise ManagedMinerUError("该 MinerU 组件尚未安装。")
        if not self._operation_lock.acquire(blocking=False):
            raise ManagedMinerUError("另一个 MinerU 组件正在操作。")
        # Freeze the whole install recipe for this operation: a concurrent
        # "check-updates" must not be able to swap versions, extras, download
        # arguments or the config style half-way through an install.
        with self._lock:
            manifest = self.manifest
        initial = {
            "install": "provisioning",
            "update": "provisioning",
            "start": "starting",
            "validate": "validating",
            "uninstall": "cleaning",
        }[action]
        with self._lock:
            state = self._states[profile_id]
            state.state = initial
            state.operation = action
            state.progress = None
            state.downloaded_bytes = 0
            state.total_bytes = 0
            state.total_is_estimate = False
            state.download_speed_bps = 0.0
            state.eta_seconds = None
            state.download_samples = []
            state.message = ""
            state.error = ""
            state.cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._operation_worker,
                args=(profile_id, action, manifest),
                name=f"managed-mineru-{action}-{profile_id}",
                daemon=True,
            )
            state.thread = thread
        thread.start()

    def _operation_worker(
        self, profile_id: str, action: str, manifest: MinerUManifest
    ) -> None:
        try:
            if action in {"install", "update"}:
                # "Newest release inside the compatible range" is resolved here
                # rather than in check-updates only, or a plain install would
                # keep targeting the pinned manifest version.  This runs on the
                # worker thread (never the request thread) and after
                # state.operation is set, so check-updates cannot race it;
                # network trouble is not fatal because _retarget keeps the
                # current recipe.
                with self._lock:
                    self._version_resolution = dict(
                        self._retarget(self._resolve_version())
                    )
                    manifest = self.manifest
                self._install(profile_id, manifest)
                self._start_service(profile_id)
                message = "安装并启动完成"
            elif action == "start":
                self._start_service(profile_id)
                message = f"运行于 {self._service_endpoint}"
            elif action == "validate":
                self._validate(profile_id)
                message = "组件验证通过"
            else:
                self._uninstall(profile_id)
                message = "组件已卸载"
            self._set_state(
                profile_id,
                "installed" if action != "uninstall" else "not_installed",
                message=message,
            )
        except _Cancelled:
            self._set_state(
                profile_id,
                "installed" if self._installed(profile_id) else "not_installed",
                message="操作已取消",
            )
        except (
            ManagedMinerUError,
            OSError,
            ValueError,
            json.JSONDecodeError,
            subprocess.SubprocessError,
            tarfile.TarError,
            zipfile.BadZipFile,
        ) as exc:
            self._set_state(
                profile_id,
                "installed" if self._installed(profile_id) else "not_installed",
                message="操作失败",
                error=str(exc),
            )
        finally:
            with self._lock:
                state = self._states[profile_id]
                state.process = None
                state.operation = None
                state.thread = None
            self._operation_lock.release()

    def _install(self, profile_id: str, manifest: MinerUManifest) -> None:
        platform_manifest = manifest.platform
        if platform_manifest is None:
            raise ManagedMinerUError("当前平台不在 MinerU 安装矩阵中。")
        if self._service_profile == profile_id:
            self.stop()
        profile = manifest.profiles[profile_id]
        self.component_root.mkdir(parents=True, exist_ok=True)
        for stale in self.component_root.glob(".staging-*"):
            self._remove_tree(stale)
        staging = self.component_root / f".staging-{profile_id}-{uuid.uuid4().hex}"
        final = self.component_root / profile_id
        previous = self.component_root / f".previous-{profile_id}-{uuid.uuid4().hex}"
        published = False
        staging.mkdir(parents=True)
        try:
            uv_path = self._ensure_uv(profile_id, platform_manifest)
            environment = self._install_environment(staging, manifest)
            install_log = staging / "install.log"
            self._set_state(profile_id, "provisioning", message="正在创建独立 Python 环境")
            self._run_command(
                profile_id,
                [
                    str(uv_path),
                    "venv",
                    "--python",
                    manifest.python,
                    "--managed-python",
                    "--relocatable",
                    str(staging / "venv"),
                ],
                cwd=staging,
                environment=environment,
                log_path=install_log,
                timeout=1800,
                track_uv_downloads=True,
            )
            python_path = staging / platform_manifest.venv_python
            self._set_state(
                profile_id,
                "provisioning",
                message=f"正在解析并安装 MinerU {manifest.version} 依赖",
            )
            self._run_command(
                profile_id,
                [
                    str(uv_path),
                    "pip",
                    "install",
                    "--python",
                    str(python_path),
                    *profile.packages,
                ],
                cwd=staging,
                environment=environment,
                log_path=install_log,
                timeout=3600,
                track_uv_downloads=True,
                track_directory=staging / ".uv-cache",
                stall_timeout=_PYPI_INSTALL_STALL_SECONDS,
                stall_message=(
                    "从 PyPI 安装依赖连续 5 分钟无数据变化。"
                    "请检查网络或代理后重试。"
                ),
            )
            downloader = self._venv_executable(staging, "mineru-models-download")
            self._set_state(
                profile_id,
                "downloading_models",
                message=f"正在下载 {profile.display_name} 模型",
                total_bytes=profile.model_download_bytes,
                total_is_estimate=True,
            )
            for attempt in range(1, _MODEL_DOWNLOAD_ATTEMPTS + 1):
                try:
                    self._run_command(
                        profile_id,
                        [str(downloader), *self._model_download_args(profile, manifest)],
                        cwd=staging,
                        environment=environment,
                        log_path=staging / "models.log",
                        timeout=14400,
                        track_directory=staging / "models",
                        estimated_total_bytes=profile.model_download_bytes,
                        stall_timeout=_MODEL_DOWNLOAD_STALL_SECONDS,
                    )
                    break
                except ManagedMinerUError as exc:
                    if not _MODEL_NETWORK_ERROR_PATTERN.search(str(exc)):
                        raise
                    if attempt == _MODEL_DOWNLOAD_ATTEMPTS:
                        raise ManagedMinerUError(
                            "模型下载网络连接中断，已自动重试 2 次。"
                            "请检查网络或代理后重试。"
                        ) from exc
                    with self._lock:
                        self._states[profile_id].message = (
                            f"模型下载网络中断，正在自动重试（{attempt}/2）"
                        )
                    if self._states[profile_id].cancel_event.wait(
                        _MODEL_DOWNLOAD_RETRY_DELAYS[attempt - 1]
                    ):
                        raise _Cancelled("操作已取消。")
            atomic_write_json(
                staging / "installed.json",
                {
                    "schema_version": 1,
                    "profile": profile_id,
                    "version": manifest.version,
                    "series": manifest.series,
                    "config_style": manifest.config_style,
                    "backend": profile.backend,
                    "packages": list(profile.packages),
                    "platform": self.platform_key,
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            if final.exists():
                final.replace(previous)
            staging.replace(final)
            published = True
            if manifest.config_style != "mineru_home":
                self._rewrite_config_paths(final / "mineru.json", staging, final)
            self._validate(profile_id)
            self._remove_tree(previous)
        except (
            ManagedMinerUError,
            OSError,
            ValueError,
            json.JSONDecodeError,
            subprocess.SubprocessError,
            tarfile.TarError,
            zipfile.BadZipFile,
        ):
            if published:
                self._remove_tree(final)
            if previous.exists():
                previous.replace(final)
            raise
        finally:
            self._remove_tree(staging)

    def _validate(self, profile_id: str) -> None:
        final = self.component_root / profile_id
        executable = self._venv_executable(final, "mineru-api")
        self._run_command(
            profile_id,
            [str(executable), "--help"],
            cwd=final,
            environment=self._runtime_environment(
                final, self._installed_config_style(profile_id)
            ),
            log_path=final / "validation.log",
            timeout=120,
        )

    def _uninstall(self, profile_id: str) -> None:
        if self._service_profile == profile_id:
            self.stop()
        config = mineru_local_config_summary(self.config_path)
        if config.get("managed_profile") == profile_id:
            clear_managed_mineru(self.config_path)
        self._remove_tree(self.component_root / profile_id)
        if not any(self._installed(item) for item in self.manifest.profiles):
            self._remove_tree(self.component_root / "_tools")
            self._remove_tree(self.component_root / "_python")

    def _start_service(self, profile_id: str) -> None:
        self.stop()
        final = self.component_root / profile_id
        profile = self.manifest.profiles[profile_id]
        port = self._available_port()
        endpoint = f"http://127.0.0.1:{port}"
        log_path = final / "service.log"
        log = log_path.open("ab")
        try:
            process = self.process_launcher(
                [
                    str(self._venv_executable(final, "mineru-api")),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=str(final),
                env=self._runtime_environment(
                    final, self._installed_config_style(profile_id)
                ),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            log.close()
            raise
        with self._lock:
            self._service_process = process
            self._service_profile = profile_id
            self._service_endpoint = endpoint
            self._service_log = log
        deadline = time.monotonic() + 180
        provider = MinerULocalProvider(
            MinerULocalConfig(endpoint=endpoint, backend=profile.backend, timeout_seconds=2)
        )
        try:
            while time.monotonic() < deadline:
                self._raise_if_cancelled(profile_id)
                if process.poll() is not None:
                    self.stop()
                    detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                    raise ManagedMinerUError(
                        f"MinerU 本地服务启动失败：{detail.strip()}"
                    )
                try:
                    provider.health()
                    configure_managed_mineru(
                        self.config_path,
                        endpoint=endpoint,
                        backend=profile.backend,
                        profile=profile_id,
                    )
                    return
                except ParserProviderError:
                    time.sleep(0.5)
        except _Cancelled:
            self.stop()
            raise
        self.stop()
        raise ManagedMinerUError("MinerU 本地服务启动超时。")

    def _ensure_uv(
        self,
        profile_id: str,
        platform_manifest: PlatformManifest,
    ) -> Path:
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
                profile_id,
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
                        raise ManagedMinerUError(
                            "uv 归档缺少清单指定的可执行文件。"
                        ) from exc
                    with bundle.open(member) as source, extracted.open("wb") as output:
                        shutil.copyfileobj(source, output)
            else:
                with tarfile.open(archive, "r:gz") as bundle:
                    try:
                        member = bundle.getmember(platform_manifest.uv.member)
                    except KeyError as exc:
                        raise ManagedMinerUError(
                            "uv 归档缺少清单指定的可执行文件。"
                        ) from exc
                    source = bundle.extractfile(member)
                    if source is None:
                        raise ManagedMinerUError("uv 归档入口无法读取。")
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
        profile_id: str,
        url: str,
        target: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        request = Request(url, headers={"User-Agent": "MEFinder-MinerU-installer"})
        self._begin_download_progress(profile_id, 0, expected_size)
        downloaded = 0
        with self.opener(request, timeout=30) as response, target.open("wb") as output:
            while True:
                self._raise_if_cancelled(profile_id)
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                self._set_download_progress(profile_id, downloaded, expected_size)
        if target.stat().st_size != expected_size:
            raise ManagedMinerUError("uv 下载文件大小与清单不一致。")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise ManagedMinerUError("uv 下载文件 SHA-256 校验失败。")

    def _run_command(
        self,
        profile_id: str,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
        timeout: int,
        track_uv_downloads: bool = False,
        track_directory: Optional[Path] = None,
        estimated_total_bytes: int = 0,
        stall_timeout: Optional[float] = None,
        stall_message: str = "模型下载连续 2 分钟无数据变化。",
    ) -> None:
        self._raise_if_cancelled(profile_id)
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
            with self._lock:
                self._states[profile_id].process = process
            try:
                deadline = time.monotonic() + timeout
                next_progress_update = 0.0
                tracked_bytes = (
                    self._model_payload_size(track_directory)
                    if track_directory is not None
                    else 0
                )
                tracked_log_size = progress_log_offset
                last_activity_change = time.monotonic()
                while process.poll() is None:
                    if self._states[profile_id].cancel_event.wait(0.1):
                        self._stop_process(process)
                        raise _Cancelled("操作已取消。")
                    now = time.monotonic()
                    if now >= next_progress_update:
                        if track_uv_downloads:
                            self._update_uv_download_progress(
                                profile_id,
                                log_path,
                                start_offset=progress_log_offset,
                            )
                            current_log_size = log_path.stat().st_size
                            if current_log_size != tracked_log_size:
                                tracked_log_size = current_log_size
                                last_activity_change = now
                        if track_directory is not None:
                            current_bytes = self._model_payload_size(track_directory)
                            if not track_uv_downloads:
                                self._set_download_progress(
                                    profile_id,
                                    current_bytes,
                                    estimated_total_bytes,
                                    total_is_estimate=True,
                                )
                            if current_bytes != tracked_bytes:
                                tracked_bytes = current_bytes
                                last_activity_change = now
                            elif (
                                stall_timeout is not None
                                and now - last_activity_change >= stall_timeout
                            ):
                                self._stop_process(process)
                                raise ManagedMinerUError(stall_message)
                        next_progress_update = now + 0.5
                    if now >= deadline:
                        self._stop_process(process)
                        raise ManagedMinerUError("MinerU 安装子进程超时。")
            finally:
                with self._lock:
                    if self._states[profile_id].process is process:
                        self._states[profile_id].process = None
        if track_uv_downloads:
            self._update_uv_download_progress(
                profile_id,
                log_path,
                start_offset=progress_log_offset,
            )
        elif track_directory is not None:
            self._set_download_progress(
                profile_id,
                self._model_payload_size(track_directory),
                estimated_total_bytes,
                total_is_estimate=True,
            )
        if process.returncode:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise ManagedMinerUError(
                f"MinerU 安装子进程退出 {process.returncode}：{detail.strip()}"
            )

    def _model_download_args(
        self, profile: MinerUProfile, manifest: MinerUManifest
    ) -> list[str]:
        """Fill the series' download template for one profile."""

        substitutions = {
            "{model_type}": profile.model_type,
            "{tier}": profile.tier,
            "{profile}": profile.profile_id,
        }
        args: list[str] = []
        for item in manifest.model_download_args:
            for token, value in substitutions.items():
                item = item.replace(token, value)
            args.append(item)
        return args

    def _config_environment(self, root: Path, config_style: str) -> Dict[str, str]:
        """Point MinerU at the component directory the way its series expects.

        MinerU 3.x reads a JSON tools config named by ``MINERU_TOOLS_CONFIG_JSON``;
        4.x dropped that file and derives every path from ``MINERU_HOME``.
        """

        if config_style == "mineru_home":
            return {"MINERU_HOME": str(root)}
        return {"MINERU_TOOLS_CONFIG_JSON": str(root / "mineru.json")}

    def _installed_config_style(self, profile_id: str) -> str:
        """Read one installed component's config style from its own receipt.

        A component must keep being recognized and launched the way it was
        installed, even after ``check-updates`` re-targets a newer series.
        Receipts written before series support only ever held MinerU 3.x.
        """

        receipt = self._receipt(profile_id)
        recorded = str(receipt.get("config_style") or "")
        if recorded:
            return recorded
        series = str(receipt.get("series") or "")
        if series:
            return str(
                self.manifest.series_config_styles.get(series) or "tools_json"
            )
        return "tools_json"

    def _install_environment(
        self, staging: Path, manifest: MinerUManifest
    ) -> Dict[str, str]:
        environment = os.environ.copy()
        proxies = getproxies()
        for scheme in ("http", "https"):
            upper = f"{scheme.upper()}_PROXY"
            lower = f"{scheme}_proxy"
            if upper not in environment and lower not in environment:
                proxy = str(proxies.get(scheme) or "").strip()
                if proxy:
                    environment[upper] = proxy
        environment.update(
            {
                "UV_CACHE_DIR": str(staging / ".uv-cache"),
                "UV_PYTHON_INSTALL_DIR": str(self.component_root / "_python"),
                "UV_NO_PROGRESS": "1",
                **self._config_environment(staging, manifest.config_style),
                "HF_HOME": str(staging / "models/huggingface"),
                "MODELSCOPE_CACHE": str(staging / "models/modelscope"),
            }
        )
        environment.pop("MINERU_MODEL_SOURCE", None)
        return environment

    def _runtime_environment(
        self, final: Path, config_style: str
    ) -> Dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                **self._config_environment(final, config_style),
                "MINERU_MODEL_SOURCE": "local",
                "HF_HOME": str(final / "models/huggingface"),
                "MODELSCOPE_CACHE": str(final / "models/modelscope"),
                "MINERU_API_MAX_CONCURRENT_REQUESTS": "1",
            }
        )
        return environment

    def _profile_summary(
        self,
        profile_id: str,
        state: _ProfileState,
    ) -> Dict[str, object]:
        profile = self.manifest.profiles[profile_id]
        receipt = self._receipt(profile_id)
        result = {
            "profile": profile_id,
            "display_name": profile.display_name,
            "backend": profile.backend,
            "version": self.manifest.version,
            "supported": profile_id in self.manifest.supported_profiles,
            "installed": self._installed(profile_id),
            "installed_version": str(receipt.get("version") or ""),
            "update_available": self._update_available(profile_id),
            "minimum_vram_gb": profile.minimum_vram_gb,
            "minimum_memory_gb": profile.minimum_memory_gb,
            "minimum_disk_gb": profile.minimum_disk_gb,
            "state": state.state,
            "operation": state.operation,
            "progress": state.progress,
            "downloaded_bytes": state.downloaded_bytes,
            "total_bytes": state.total_bytes,
            "total_is_estimate": state.total_is_estimate,
            "download_speed_bps": round(state.download_speed_bps),
            "eta_seconds": state.eta_seconds,
            "message": state.message,
            "error": state.error,
        }
        result["task_event"] = TaskEvent.from_mapping(
            f"{self.component_id}:{profile_id}", result, unit="bytes"
        ).to_dict()
        return result

    def _receipt(self, profile_id: str) -> Mapping[str, object]:
        path = self.component_root / profile_id / "installed.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, Mapping) else {}

    def _installed(self, profile_id: str) -> bool:
        if profile_id not in self.manifest.profiles or self.manifest.platform is None:
            return False
        final = self.component_root / profile_id
        receipt = self._receipt(profile_id)
        return (
            receipt.get("profile") == profile_id
            and (final / self.manifest.platform.venv_python).is_file()
            and self._venv_executable(final, "mineru-api").is_file()
            and (
                self._installed_config_style(profile_id) == "mineru_home"
                or (final / "mineru.json").is_file()
            )
        )

    def _update_available(self, profile_id: str) -> bool:
        return self._installed(profile_id) and (
            self._receipt(profile_id).get("version") != self.manifest.version
        )

    def _set_state(
        self,
        profile_id: str,
        state_name: str,
        *,
        progress: Optional[float] = None,
        downloaded_bytes: Optional[int] = None,
        total_bytes: Optional[int] = None,
        total_is_estimate: bool = False,
        message: str,
        error: str = "",
    ) -> None:
        with self._lock:
            state = self._states[profile_id]
            state.state = state_name
            state.progress = progress
            state.downloaded_bytes = downloaded_bytes or 0
            state.total_bytes = total_bytes or 0
            state.total_is_estimate = total_is_estimate
            state.download_speed_bps = 0.0
            state.eta_seconds = None
            state.download_samples = []
            state.message = message
            state.error = error

    def _begin_download_progress(
        self,
        profile_id: str,
        downloaded: int,
        total: int,
        *,
        total_is_estimate: bool = False,
    ) -> None:
        with self._lock:
            state = self._states[profile_id]
            state.downloaded_bytes = downloaded
            state.total_bytes = total
            state.total_is_estimate = total_is_estimate
            state.progress = min(1.0, downloaded / total) if total else None
            state.download_speed_bps = 0.0
            state.eta_seconds = None
            state.download_samples = [(time.monotonic(), downloaded)]

    def _update_uv_download_progress(
        self,
        profile_id: str,
        log_path: Path,
        *,
        start_offset: int,
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
        with self._lock:
            has_samples = bool(self._states[profile_id].download_samples)
        if not has_samples:
            self._begin_download_progress(profile_id, 0, total)
        self._set_download_progress(profile_id, completed, total)

    def _set_download_progress(
        self,
        profile_id: str,
        downloaded: int,
        total: int,
        *,
        total_is_estimate: bool = False,
    ) -> None:
        with self._lock:
            state = self._states[profile_id]
            downloaded = max(0, downloaded)
            state.downloaded_bytes = downloaded
            state.total_bytes = total
            state.total_is_estimate = total_is_estimate
            state.progress = min(1.0, downloaded / total) if total else None
            now = time.monotonic()
            state.download_samples.append((now, downloaded))
            cutoff = now - _DOWNLOAD_SPEED_WINDOW_SECONDS
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
                state.eta_seconds = (
                    math.ceil(max(0, total - downloaded) / state.download_speed_bps)
                    if total else None
                )
            elif elapsed >= 0.5:
                state.download_speed_bps = 0.0
                state.eta_seconds = None

    @staticmethod
    def _model_payload_size(root: Path) -> int:
        if not root.exists():
            return 0
        total = 0
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            relative_parts = path.relative_to(root).parts
            if "logs" in relative_parts or ".locks" in relative_parts:
                continue
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                continue
        return total

    def _cancel(self, profile_id: str) -> None:
        with self._lock:
            state = self._states[profile_id]
            if state.state not in ACTIVE_STATES:
                raise ManagedMinerUError("该 MinerU 组件当前没有可取消的操作。")
            state.cancel_event.set()
            process = state.process
            state.message = "正在取消并清理…"
        if process is not None:
            self._stop_process(process)

    def _raise_if_cancelled(self, profile_id: str) -> None:
        if self._states[profile_id].cancel_event.is_set():
            raise _Cancelled("操作已取消。")

    def _current_manifest_path(self) -> Path:
        return Path(
            self._manifest_path()
            if callable(self._manifest_path)
            else self._manifest_path
        )

    def _venv_executable(self, root: Path, name: str) -> Path:
        if self.manifest.platform is None:
            raise ManagedMinerUError("当前平台没有 MinerU 运行时定义。")
        parent = (root / self.manifest.platform.venv_python).parent
        suffix = ".exe" if self.platform_key.startswith("win32-") else ""
        return parent / f"{name}{suffix}"

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @staticmethod
    def _rewrite_config_paths(path: Path, source: Path, destination: Path) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))

        def rewrite(item):
            if isinstance(item, str):
                return item.replace(str(source), str(destination))
            if isinstance(item, list):
                return [rewrite(child) for child in item]
            if isinstance(item, dict):
                return {key: rewrite(child) for key, child in item.items()}
            return item

        atomic_write_json(path, rewrite(value))

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)

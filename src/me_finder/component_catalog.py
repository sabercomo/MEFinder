"""Cached remote catalog for optional local parser components."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Mapping, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .import_resume import atomic_write_json


DEFAULT_COMPONENT_CATALOG_URL = (
    "https://github.com/sabercomo/MEFinder/releases/download/components-v1/"
    "mefinder-components-v1.json"
)
COMPONENT_CATALOG_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
MAX_COMPONENT_CATALOG_BYTES = 512 * 1024
_ALLOWED_CATALOG_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
_PLATFORM_RUNTIME = {
    "darwin-arm64": (
        "venv/bin/python",
        "tar.gz",
        "uv-aarch64-apple-darwin.tar.gz",
        "uv-aarch64-apple-darwin/uv",
    ),
    "darwin-x86_64": (
        "venv/bin/python",
        "tar.gz",
        "uv-x86_64-apple-darwin.tar.gz",
        "uv-x86_64-apple-darwin/uv",
    ),
    "win32-x86_64": (
        "venv/Scripts/python.exe",
        "zip",
        "uv-x86_64-pc-windows-msvc.zip",
        "uv-x86_64-pc-windows-msvc/uv.exe",
    ),
    "linux-x86_64": (
        "venv/bin/python",
        "tar.gz",
        "uv-x86_64-unknown-linux-gnu.tar.gz",
        "uv-x86_64-unknown-linux-gnu/uv",
    ),
}
_PINNED_REQUIREMENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?"
    r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*"
)


class ComponentCatalogError(RuntimeError):
    pass


def validate_component_catalog(payload: object) -> None:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ComponentCatalogError("组件清单版本不受支持。")
    if not isinstance(payload.get("engines"), Mapping):
        raise ComponentCatalogError("组件清单缺少本地 OCR 定义。")
    if not isinstance(payload.get("platforms"), Mapping):
        raise ComponentCatalogError("组件清单缺少平台定义。")
    engines = payload["engines"]
    if set(engines) != {"ndlocr-lite", "ndlkotenocr-lite"}:
        raise ComponentCatalogError("组件清单包含未知的本地 OCR 组件。")
    for provider_id, item in engines.items():
        if not isinstance(item, Mapping):
            raise ComponentCatalogError("本地 OCR 组件定义无效。")
        tag = str(item.get("tag") or "")
        expected_url = (
            f"https://codeload.github.com/ndl-lab/{provider_id}/tar.gz/refs/tags/"
            f"{tag}"
        )
        if (
            re.fullmatch(r"\d+\.\d+\.\d+", tag) is None
            or item.get("version") != tag
            or item.get("tarball_url") != expected_url
            or item.get("archive_root") != f"{provider_id}-{tag}"
        ):
            raise ComponentCatalogError("本地 OCR 下载地址不受信任。")
        _validate_size_and_digest(item, "本地 OCR")
        for path_key in ("script_path", "sample_path", "license_path"):
            if not _safe_relative_path(item.get(path_key)):
                raise ComponentCatalogError("本地 OCR 组件路径无效。")
        dependencies = item.get("dependencies")
        if not isinstance(dependencies, list) or not all(
            isinstance(requirement, str)
            and _PINNED_REQUIREMENT.fullmatch(requirement) is not None
            for requirement in dependencies
        ):
            raise ComponentCatalogError("本地 OCR 依赖必须固定版本。")
    platforms = payload["platforms"]
    uv_version = str(payload.get("uv_version") or "")
    if (
        set(platforms) != set(_PLATFORM_RUNTIME)
        or re.fullmatch(r"\d+\.\d+\.\d+", uv_version) is None
    ):
        raise ComponentCatalogError("组件清单平台矩阵无效。")
    for platform_key, item in platforms.items():
        uv = item.get("uv") if isinstance(item, Mapping) else None
        venv_python, archive_type, filename, member = _PLATFORM_RUNTIME[platform_key]
        expected_uv_url = (
            "https://releases.astral.sh/github/uv/releases/download/"
            f"{uv_version}/{filename}"
        )
        if (
            not isinstance(item, Mapping)
            or item.get("python") != "3.11"
            or item.get("venv_python") != venv_python
            or not re.fullmatch(
                r"onnxruntime==\d+\.\d+\.\d+",
                str(item.get("onnxruntime") or ""),
            )
            or not isinstance(uv, Mapping)
            or uv.get("url") != expected_uv_url
            or uv.get("archive_type") != archive_type
            or uv.get("member") != member
        ):
            raise ComponentCatalogError("uv 下载地址不受信任。")
        _validate_size_and_digest(uv, "uv")
    mineru = payload.get("mineru")
    if mineru is not None and not isinstance(mineru, Mapping):
        raise ComponentCatalogError("组件清单中的 MinerU 定义无效。")
    if isinstance(mineru, Mapping):
        version = str(mineru.get("version") or "")
        profiles = mineru.get("profiles")
        supported = mineru.get("platforms")
        if (
            re.fullmatch(r"\d+\.\d+\.\d+", version) is None
            or mineru.get("python") != "3.12"
            or not isinstance(profiles, Mapping)
            or set(profiles) != {"pipeline", "vlm"}
            or not isinstance(supported, Mapping)
        ):
            raise ComponentCatalogError("MinerU 组件版本或配置无效。")
        pipeline = profiles["pipeline"]
        vlm = profiles["vlm"]
        packages = vlm.get("packages") if isinstance(vlm, Mapping) else None
        if (
            not isinstance(pipeline, Mapping)
            or pipeline.get("package") != f"mineru[pipeline]=={version}"
            or not isinstance(packages, Mapping)
            or packages.get("darwin-arm64") != f"mineru[core,mlx]=={version}"
            or packages.get("win32-x86_64")
            != f"mineru[core,lmdeploy]=={version}"
            or packages.get("linux-x86_64")
            != f"mineru[core,vllm]=={version}"
            or pipeline.get("model_type") != "pipeline"
            or pipeline.get("backend") != "pipeline"
            or vlm.get("model_type") != "vlm"
            or vlm.get("backend") != "vlm-auto-engine"
            or supported
            != {
                "darwin-arm64": ["pipeline", "vlm"],
                "darwin-x86_64": ["pipeline"],
                "win32-x86_64": ["pipeline", "vlm"],
                "linux-x86_64": ["pipeline", "vlm"],
            }
        ):
            raise ComponentCatalogError("MinerU 安装包未固定到清单版本。")


def _safe_relative_path(value: object) -> bool:
    path = PurePosixPath(str(value or ""))
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def _validate_size_and_digest(item: Mapping[str, object], label: str) -> None:
    try:
        size = int(item.get("size") or item.get("tarball_size") or 0)
    except (TypeError, ValueError) as exc:
        raise ComponentCatalogError(f"{label} 文件大小无效。") from exc
    digest = str(item.get("sha256") or item.get("tarball_sha256") or "")
    if size <= 0 or re.fullmatch(r"[0-9a-f]{64}", digest.lower()) is None:
        raise ComponentCatalogError(f"{label} 文件校验信息无效。")


class ComponentCatalog:
    """Keep one validated catalog cache without blocking application startup."""

    def __init__(
        self,
        runtime_root: Path,
        bundled_path: Path,
        *,
        remote_url: str = DEFAULT_COMPONENT_CATALOG_URL,
        opener: Callable = urlopen,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.bundled_path = Path(bundled_path).resolve()
        self.remote_url = remote_url
        self.opener = opener
        self.clock = clock
        self.cache_dir = self.runtime_root / "components" / "catalog"
        self.cached_path = self.cache_dir / "manifest.json"
        self.state_path = self.cache_dir / "check.json"
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._revision = 0
        validate_component_catalog(self._read_json(self.bundled_path))
        if self.cached_path.is_file():
            try:
                validate_component_catalog(self._read_json(self.cached_path))
            except (OSError, json.JSONDecodeError, ComponentCatalogError):
                self.cached_path.unlink(missing_ok=True)

    def manifest_path(self) -> Path:
        with self._lock:
            return self.cached_path if self.cached_path.is_file() else self.bundled_path

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def summary(self) -> Dict[str, object]:
        state = self._load_state()
        last_checked_at = state.get("last_checked_at")
        return {
            "source": "remote" if self.cached_path.is_file() else "bundled",
            "remote_url": self.remote_url,
            "last_checked_at": last_checked_at,
            "next_check_at": (
                float(last_checked_at)
                + COMPONENT_CATALOG_CHECK_INTERVAL_SECONDS
                if isinstance(last_checked_at, (int, float))
                else None
            ),
            "checking": bool(self._thread and self._thread.is_alive()),
            "last_error": str(state.get("last_error") or ""),
        }

    def start_background_check(
        self,
        *,
        on_updated: Optional[Callable[[], None]] = None,
    ) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if not self._check_due():
                return False
            thread = threading.Thread(
                target=self._background_check,
                args=(on_updated,),
                name="component-catalog-check",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return True

    def check_now(self, *, force: bool = False) -> Dict[str, object]:
        with self._lock:
            if not force and not self._check_due():
                return self.summary()
        checked_at = self.clock()
        state = {"last_checked_at": checked_at, "last_error": ""}
        atomic_write_json(self.state_path, state)
        try:
            payload = self._download_catalog()
            validate_component_catalog(payload)
            atomic_write_json(self.cached_path, payload)
        except (
            OSError,
            TimeoutError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            ComponentCatalogError,
        ) as exc:
            state["last_error"] = str(exc)
            atomic_write_json(self.state_path, state)
            return self.summary()
        with self._lock:
            self._revision += 1
        return self.summary()

    def _background_check(
        self,
        on_updated: Optional[Callable[[], None]],
    ) -> None:
        before = self.revision
        self.check_now()
        if on_updated is not None and self.revision != before:
            on_updated()

    def _check_due(self) -> bool:
        last_checked_at = self._load_state().get("last_checked_at")
        return not isinstance(last_checked_at, (int, float)) or (
            self.clock() - float(last_checked_at)
            >= COMPONENT_CATALOG_CHECK_INTERVAL_SECONDS
        )

    def _download_catalog(self) -> Mapping[str, object]:
        parsed = urlparse(self.remote_url)
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_CATALOG_HOSTS:
            raise ComponentCatalogError("远程组件清单地址不受信任。")
        request = Request(
            self.remote_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "MEFinder-component-catalog",
            },
        )
        with self.opener(request, timeout=20) as response:
            raw = response.read(MAX_COMPONENT_CATALOG_BYTES + 1)
        if len(raw) > MAX_COMPONENT_CATALOG_BYTES:
            raise ComponentCatalogError("远程组件清单过大。")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ComponentCatalogError("远程组件清单必须是 JSON 对象。")
        return payload

    def _load_state(self) -> Dict[str, object]:
        try:
            value = self._read_json(self.state_path)
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _read_json(path: Path) -> object:
        return json.loads(Path(path).read_text(encoding="utf-8"))

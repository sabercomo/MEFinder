"""Machine-local configuration for the two supported NDL OCR runtimes."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from .import_resume import atomic_write_json, load_json_object
from .local_ocr_runtime import local_ocr_engine_lock


LOCAL_OCR_SCHEMA_VERSION = 1
LOCAL_OCR_CONFIG_FILE = "config/local_ocr.json"
_CONFIG_LOCK = threading.RLock()


class LocalOCRError(RuntimeError):
    pass


class LocalOCRCancelled(LocalOCRError):
    pass


@dataclass(frozen=True)
class LocalOCREngineConfig:
    provider_id: str
    display_name: str
    version: str
    python_path: Path
    script_path: Path
    enabled: bool
    weights_sha256: Optional[str] = None

    @property
    def configured(self) -> bool:
        return self.python_path.is_file() and self.script_path.is_file()


@dataclass(frozen=True)
class LocalOCRConfig:
    engines: Tuple[LocalOCREngineConfig, ...]
    render_dpi: int = 200
    probe_pages: int = 3
    pages_per_slice: int = 10
    timeout_seconds_per_page: int = 300
    blank_ink_ratio: float = 0.001

    @property
    def available_engines(self) -> Tuple[LocalOCREngineConfig, ...]:
        return tuple(
            engine
            for engine in self.engines
            if engine.enabled and engine.configured
        )


_ENGINE_SPECS = {
    "ndlocr-lite": ("NDL 日文 OCR", "1.2.3"),
    "ndlkotenocr-lite": ("NDL 古籍 OCR", "1.4.3"),
}


def resolve_local_ocr_config_path(root: Path) -> Path:
    return Path(root) / LOCAL_OCR_CONFIG_FILE


def load_local_ocr_config(
    config_path: Path,
    *,
    require_available: bool = False,
) -> LocalOCRConfig:
    data = load_json_object(Path(config_path)) or {}
    raw_engines = data.get("engines")
    if not isinstance(raw_engines, Mapping):
        raw_engines = {}
    engines = tuple(
        _engine_from_mapping(
            provider_id,
            raw_engines.get(provider_id),
            runtime_root=Path(config_path).parent.parent,
        )
        for provider_id in _ENGINE_SPECS
    )
    config = LocalOCRConfig(
        engines=engines,
        render_dpi=_bounded_int(data.get("render_dpi"), 200, 96, 400),
        probe_pages=_bounded_int(data.get("probe_pages"), 3, 1, 9),
        pages_per_slice=_bounded_int(
            data.get("pages_per_slice"), 10, 1, 50
        ),
        timeout_seconds_per_page=_bounded_int(
            data.get("timeout_seconds_per_page"), 300, 10, 3600
        ),
        blank_ink_ratio=_bounded_float(
            data.get("blank_ink_ratio"), 0.001, 0.0, 0.05
        ),
    )
    if require_available and not config.available_engines:
        raise LocalOCRError("尚未配置并启用本地 OCR 组件。")
    return config


def local_ocr_config_summary(config_path: Path) -> Dict[str, object]:
    config = load_local_ocr_config(config_path)
    return {
        "schema_version": LOCAL_OCR_SCHEMA_VERSION,
        "available": bool(config.available_engines),
        "render_dpi": config.render_dpi,
        "probe_pages": config.probe_pages,
        "pages_per_slice": config.pages_per_slice,
        "timeout_seconds_per_page": config.timeout_seconds_per_page,
        "blank_ink_ratio": config.blank_ink_ratio,
        "engines": [
            {
                "provider_id": engine.provider_id,
                "display_name": engine.display_name,
                "version": engine.version,
                "python_path": str(engine.python_path) if str(engine.python_path) != "." else "",
                "script_path": str(engine.script_path) if str(engine.script_path) != "." else "",
                "enabled": engine.enabled,
                "configured": engine.configured,
                "weights_sha256": engine.weights_sha256,
            }
            for engine in config.engines
        ],
    }


def local_ocr_available(root: Path) -> bool:
    return bool(
        load_local_ocr_config(
            resolve_local_ocr_config_path(root)
        ).available_engines
    )


def save_local_ocr_config(
    payload: Mapping[str, object],
    config_path: Path,
) -> Dict[str, object]:
    with _CONFIG_LOCK:
        return _save_local_ocr_config(payload, config_path)


def _save_local_ocr_config(
    payload: Mapping[str, object],
    config_path: Path,
) -> Dict[str, object]:
    if not isinstance(payload, Mapping):
        raise LocalOCRError("本地 OCR 设置必须是 JSON 对象。")
    raw_engines = payload.get("engines")
    if not isinstance(raw_engines, Mapping):
        raise LocalOCRError("本地 OCR 组件设置无效。")
    engines = {}
    for provider_id in _ENGINE_SPECS:
        raw = raw_engines.get(provider_id)
        if not isinstance(raw, Mapping):
            raise LocalOCRError(f"缺少组件设置：{provider_id}")
        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise LocalOCRError("组件启用状态必须是布尔值。")
        python_path = _absolute_path(raw.get("python_path"))
        script_path = _absolute_path(raw.get("script_path"))
        if enabled and (not python_path.is_file() or not script_path.is_file()):
            raise LocalOCRError("启用组件前请填写有效的 Python 和 ocr.py 路径。")
        engines[provider_id] = {
            "enabled": enabled,
            "python_path": str(python_path) if str(python_path) != "." else "",
            "script_path": str(script_path) if str(script_path) != "." else "",
            "weights_sha256": str(raw.get("weights_sha256") or "").strip(),
        }
    data = {
        "schema_version": LOCAL_OCR_SCHEMA_VERSION,
        "render_dpi": _bounded_int(payload.get("render_dpi"), 200, 96, 400),
        "probe_pages": _bounded_int(payload.get("probe_pages"), 3, 1, 9),
        "pages_per_slice": _bounded_int(
            payload.get("pages_per_slice"), 10, 1, 50
        ),
        "timeout_seconds_per_page": _bounded_int(
            payload.get("timeout_seconds_per_page"), 300, 10, 3600
        ),
        "blank_ink_ratio": _bounded_float(
            payload.get("blank_ink_ratio"), 0.001, 0.0, 0.05
        ),
        "engines": engines,
    }
    atomic_write_json(Path(config_path), data)
    return local_ocr_config_summary(config_path)


def configure_managed_local_ocr_engine(
    config_path: Path,
    provider_id: str,
    *,
    python_path: Path,
    script_path: Path,
) -> Dict[str, object]:
    if provider_id not in _ENGINE_SPECS:
        raise LocalOCRError("未知的本地 OCR 组件。")
    with _CONFIG_LOCK:
        config = load_local_ocr_config(config_path)
        payload = _config_payload(config)
        payload["engines"][provider_id] = {
            "enabled": True,
            "python_path": str(Path(python_path).absolute()),
            "script_path": str(Path(script_path).absolute()),
            "weights_sha256": "",
        }
        return _save_local_ocr_config(payload, config_path)


def clear_managed_local_ocr_engine(
    config_path: Path,
    provider_id: str,
    *,
    python_path: Path,
    script_path: Path,
) -> Dict[str, object]:
    if provider_id not in _ENGINE_SPECS:
        raise LocalOCRError("未知的本地 OCR 组件。")
    with _CONFIG_LOCK:
        config = load_local_ocr_config(config_path)
        current = next(
            engine
            for engine in config.engines
            if engine.provider_id == provider_id
        )
        if (
            current.python_path != Path(python_path).absolute()
            or current.script_path != Path(script_path).absolute()
        ):
            return local_ocr_config_summary(config_path)
        payload = _config_payload(config)
        payload["engines"][provider_id] = {
            "enabled": False,
            "python_path": "",
            "script_path": "",
            "weights_sha256": "",
        }
        return _save_local_ocr_config(payload, config_path)


def _config_payload(config: LocalOCRConfig) -> Dict[str, object]:
    return {
        "render_dpi": config.render_dpi,
        "probe_pages": config.probe_pages,
        "pages_per_slice": config.pages_per_slice,
        "timeout_seconds_per_page": config.timeout_seconds_per_page,
        "blank_ink_ratio": config.blank_ink_ratio,
        "engines": {
            engine.provider_id: {
                "enabled": engine.enabled,
                "python_path": (
                    str(engine.python_path)
                    if str(engine.python_path) != "."
                    else ""
                ),
                "script_path": (
                    str(engine.script_path)
                    if str(engine.script_path) != "."
                    else ""
                ),
                "weights_sha256": engine.weights_sha256 or "",
            }
            for engine in config.engines
        },
    }


def test_local_ocr_engine(
    payload: Mapping[str, object],
    config_path: Path,
) -> Dict[str, object]:
    provider_id = str(payload.get("provider_id") or "").strip()
    if provider_id not in _ENGINE_SPECS:
        raise LocalOCRError("未知的本地 OCR 组件。")
    if payload.get("python_path") or payload.get("script_path"):
        engine = _engine_from_mapping(provider_id, payload)
    else:
        engine = next(
            item
            for item in load_local_ocr_config(config_path).engines
            if item.provider_id == provider_id
        )
    if not engine.configured:
        raise LocalOCRError("请先填写有效的 Python 和 ocr.py 路径。")
    engine_lock = local_ocr_engine_lock(provider_id)
    if not engine_lock.acquire(blocking=False):
        raise LocalOCRError("该组件正在安装或执行 OCR 任务。")
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [str(engine.python_path), str(engine.script_path), "--help"],
            cwd=str(engine.script_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32"
                else 0
            ),
            check=False,
        )
    finally:
        engine_lock.release()
    if completed.returncode != 0:
        detail = completed.stdout.decode("utf-8", "replace")[-1000:].strip()
        raise LocalOCRError(
            f"{engine.display_name} 无法启动：{detail or completed.returncode}"
        )
    return {
        "ok": True,
        "provider_id": engine.provider_id,
        "display_name": engine.display_name,
        "version": engine.version,
        "latency_ms": int(round((time.perf_counter() - started) * 1000)),
    }


def _engine_from_mapping(
    provider_id: str,
    value: object,
    *,
    runtime_root: Path | None = None,
) -> LocalOCREngineConfig:
    raw = value if isinstance(value, Mapping) else {}
    display_name, version = _ENGINE_SPECS[provider_id]
    python_path = _absolute_path(raw.get("python_path"))
    if runtime_root is not None and str(python_path) != ".":
        managed_python = (
            Path(runtime_root)
            / "components"
            / "local-ocr"
            / provider_id
            / "venv"
            / "bin"
            / "python"
        )
        if (
            managed_python.is_file()
            and managed_python != python_path
            and managed_python.resolve() == python_path
        ):
            python_path = managed_python
    return LocalOCREngineConfig(
        provider_id=provider_id,
        display_name=display_name,
        version=version,
        python_path=python_path,
        script_path=_absolute_path(raw.get("script_path")),
        enabled=raw.get("enabled") is True,
        weights_sha256=(
            str(raw.get("weights_sha256") or "").strip() or None
        ),
    )


def _absolute_path(value: object) -> Path:
    text = str(value or "").strip()
    if not text:
        return Path()
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise LocalOCRError("本地 OCR 运行时路径必须是绝对路径。")
    return path


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise LocalOCRError("本地 OCR 数值设置无效。") from exc
    if not minimum <= number <= maximum:
        raise LocalOCRError("本地 OCR 数值设置超出允许范围。")
    return number


def _bounded_float(
    value: object,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LocalOCRError("本地 OCR 小数设置无效。") from exc
    if not minimum <= number <= maximum:
        raise LocalOCRError("本地 OCR 小数设置超出允许范围。")
    return number

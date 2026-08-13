"""Persist the opt-in MinerU Local endpoint beside the MinerU API config."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Mapping

from .mineru_api import MinerUError, read_mineru_config_data
from .mineru_local_provider import MinerULocalConfig, MinerULocalProvider


DEFAULT_MINERU_LOCAL_ENDPOINT = "http://127.0.0.1:8000"
DEFAULT_MINERU_LOCAL_BACKEND = "pipeline"


def mineru_local_config_summary(config_path: Path) -> Dict[str, object]:
    data = read_mineru_config_data(Path(config_path))
    return {
        "enabled": data.get("local_deployment_enabled") is True,
        "endpoint": str(
            data.get("local_deployment_endpoint")
            or DEFAULT_MINERU_LOCAL_ENDPOINT
        ).rstrip("/"),
        "backend": str(
            data.get("local_deployment_backend")
            or DEFAULT_MINERU_LOCAL_BACKEND
        ).strip(),
    }


def save_mineru_local_config(
    payload: Mapping[str, object],
    config_path: Path,
) -> Dict[str, object]:
    if not isinstance(payload, Mapping):
        raise MinerUError("本地部署设置必须是 JSON 对象。")
    if not isinstance(payload.get("enabled"), bool):
        raise MinerUError("启用本地部署必须是布尔值。")
    config = _config_from_payload(payload)
    path = Path(config_path)
    data = read_mineru_config_data(path)
    data.update(
        {
            "local_deployment_enabled": bool(payload["enabled"]),
            "local_deployment_endpoint": config.endpoint.rstrip("/"),
            "local_deployment_backend": config.backend,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)
    return mineru_local_config_summary(path)


def load_mineru_local_config(
    config_path: Path,
    *,
    require_enabled: bool = True,
) -> MinerULocalConfig:
    summary = mineru_local_config_summary(config_path)
    if require_enabled and not summary["enabled"]:
        raise MinerUError("尚未在设置中启用本地部署。")
    return MinerULocalConfig(
        endpoint=str(summary["endpoint"]),
        backend=str(summary["backend"]),
    )


def test_mineru_local_connection(
    payload: Mapping[str, object],
    config_path: Path,
) -> Dict[str, object]:
    config = (
        _config_from_payload(payload)
        if payload
        else load_mineru_local_config(config_path, require_enabled=False)
    )
    started = time.perf_counter()
    result = MinerULocalProvider(config).health()
    return {
        "ok": True,
        "message": "本地 MinerU 连接成功",
        "latency_ms": int(round((time.perf_counter() - started) * 1000)),
        "endpoint": config.endpoint.rstrip("/"),
        "backend": config.backend,
        "health": result,
    }


def _config_from_payload(payload: Mapping[str, object]) -> MinerULocalConfig:
    endpoint = str(
        payload.get("endpoint") or DEFAULT_MINERU_LOCAL_ENDPOINT
    ).strip().rstrip("/")
    backend = str(
        payload.get("backend") or DEFAULT_MINERU_LOCAL_BACKEND
    ).strip()
    if not backend:
        raise MinerUError("请填写本地 MinerU 解析后端。")
    try:
        return MinerULocalConfig(endpoint=endpoint, backend=backend)
    except ValueError as exc:
        raise MinerUError("本地服务地址必须是以 http:// 或 https:// 开头的网址。") from exc

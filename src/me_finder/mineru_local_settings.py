"""Persist the opt-in MinerU Local endpoint beside the MinerU API config."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Dict, Mapping

from .mineru_api import MinerUError, read_mineru_config_data
from .mineru_local_provider import (
    MINERU_LOCAL_PROTOCOLS,
    MINERU_PROTOCOL_AUTO,
    MinerULocalConfig,
    MinerULocalProvider,
)


DEFAULT_MINERU_LOCAL_ENDPOINT = "http://127.0.0.1:8000"
DEFAULT_MINERU_LOCAL_BACKEND = "pipeline"
DEFAULT_MINERU_LOCAL_PROTOCOL = MINERU_PROTOCOL_AUTO

# 本地部署指向「这台电脑」上的服务，数据目录放进网盘被多台电脑共用时不能跟着同步。
# 桌面版把它指到本机专属文件；未设置时（开发 / 便携）仍与 MinerU API 配置同文件。
MINERU_LOCAL_CONFIG_ENV = "ME_FINDER_MINERU_LOCAL_CONFIG"
LOCAL_DEPLOYMENT_PREFIX = "local_deployment_"


def mineru_local_settings_path(config_path: Path) -> Path:
    """Return the file holding this machine's local-deployment settings."""

    override = os.environ.get(MINERU_LOCAL_CONFIG_ENV, "").strip()
    return Path(override) if override else Path(config_path)


def legacy_managed_mineru_config(config_path: Path) -> Dict[str, object]:
    """Return the shared file's managed settings that were not migrated.

    Before per-machine settings, a managed runtime was recorded in the shared
    MinerU config. Only the managed runtime can tell whether that profile is
    installed for this machine, so migration of that case is left to it.
    """

    local_path = mineru_local_settings_path(config_path)
    if local_path == Path(config_path) or local_path.exists():
        return {}
    data = read_mineru_config_data(Path(config_path))
    if data.get("local_deployment_managed") is not True:
        return {}
    return _summary_from_data(data)


def _local_data(config_path: Path) -> Dict[str, object]:
    local_path = mineru_local_settings_path(config_path)
    if local_path == Path(config_path) or local_path.exists():
        return read_mineru_config_data(local_path)
    # 首次使用本机文件：沿用共享文件里的自部署设置；托管设置可能是另一台电脑装的，
    # 不在这里继承（见 legacy_managed_mineru_config）。
    legacy = {
        key: value
        for key, value in read_mineru_config_data(Path(config_path)).items()
        if key.startswith(LOCAL_DEPLOYMENT_PREFIX)
    }
    if legacy.get("local_deployment_managed") is True:
        return {}
    return legacy


def _write_local_data(config_path: Path, updates: Mapping[str, object]) -> Path:
    local_path = mineru_local_settings_path(config_path)
    data = _local_data(config_path)
    data.update(updates)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = local_path.with_name(f".{local_path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(local_path)
    return local_path


def mineru_local_config_summary(config_path: Path) -> Dict[str, object]:
    return _summary_from_data(_local_data(config_path))


def _summary_from_data(data: Mapping[str, object]) -> Dict[str, object]:
    return {
        "enabled": data.get("local_deployment_enabled") is True,
        "managed": data.get("local_deployment_managed") is True,
        "managed_profile": str(
            data.get("local_deployment_managed_profile") or ""
        ),
        "endpoint": str(
            data.get("local_deployment_endpoint")
            or DEFAULT_MINERU_LOCAL_ENDPOINT
        ).rstrip("/"),
        "backend": str(
            data.get("local_deployment_backend")
            or DEFAULT_MINERU_LOCAL_BACKEND
        ).strip(),
        "protocol": _normalized_protocol(data.get("local_deployment_protocol")),
        "tier": str(data.get("local_deployment_tier") or "").strip(),
        "has_api_key": bool(str(data.get("local_deployment_api_key") or "").strip()),
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
    updates: Dict[str, object] = {
        "local_deployment_enabled": bool(payload["enabled"]),
        "local_deployment_managed": False,
        "local_deployment_managed_profile": "",
        "local_deployment_endpoint": config.endpoint.rstrip("/"),
        "local_deployment_backend": config.backend,
        "local_deployment_protocol": config.protocol,
        "local_deployment_tier": config.tier,
    }
    if "api_key" in payload:
        updates["local_deployment_api_key"] = str(payload.get("api_key") or "").strip()
    _write_local_data(config_path, updates)
    return mineru_local_config_summary(config_path)


def configure_managed_mineru(
    config_path: Path,
    *,
    endpoint: str,
    backend: str,
    profile: str,
) -> Dict[str, object]:
    config = _config_from_payload({"endpoint": endpoint, "backend": backend})
    _write_local_data(
        config_path,
        {
            "local_deployment_enabled": True,
            "local_deployment_managed": True,
            "local_deployment_managed_profile": profile,
            "local_deployment_endpoint": config.endpoint.rstrip("/"),
            "local_deployment_backend": config.backend,
            "local_deployment_protocol": config.protocol,
            "local_deployment_tier": config.tier,
        },
    )
    return mineru_local_config_summary(config_path)


def clear_managed_mineru(config_path: Path) -> Dict[str, object]:
    if _local_data(config_path).get("local_deployment_managed") is not True:
        return mineru_local_config_summary(config_path)
    _write_local_data(
        config_path,
        {
            "local_deployment_enabled": False,
            "local_deployment_managed": False,
            "local_deployment_managed_profile": "",
        },
    )
    return mineru_local_config_summary(config_path)


def load_mineru_local_config(
    config_path: Path,
    *,
    require_enabled: bool = True,
) -> MinerULocalConfig:
    summary = mineru_local_config_summary(config_path)
    if require_enabled and not summary["enabled"]:
        raise MinerUError("尚未在设置中启用本地部署。")
    data = _local_data(config_path)
    return MinerULocalConfig(
        endpoint=str(summary["endpoint"]),
        backend=str(summary["backend"]),
        protocol=str(summary["protocol"]),
        tier=str(summary["tier"]),
        api_key=str(data.get("local_deployment_api_key") or "").strip(),
    )


def test_mineru_local_connection(
    payload: Mapping[str, object],
    config_path: Path,
) -> Dict[str, object]:
    if payload:
        config = _config_from_payload(payload)
        if not config.api_key and "api_key" not in payload:
            stored = _local_data(config_path)
            saved_key = str(stored.get("local_deployment_api_key") or "").strip()
            if saved_key:
                config = replace(config, api_key=saved_key)
    else:
        config = load_mineru_local_config(config_path, require_enabled=False)
    started = time.perf_counter()
    result = MinerULocalProvider(config).health()
    protocol = str(result.get("protocol") or "")
    version = str(result.get("mineru_version") or "")
    return {
        "ok": True,
        "message": _connection_message(protocol, version),
        "latency_ms": int(round((time.perf_counter() - started) * 1000)),
        "endpoint": config.endpoint.rstrip("/"),
        "backend": config.backend,
        "protocol": protocol,
        "mineru_version": version,
        "health": result,
    }


_PROTOCOL_LABELS = {
    "tasks": "MinerU 3.x 任务接口",
    "v1-jobs": "MinerU 4.x /v1 接口",
}


def _connection_message(protocol: str, version: str) -> str:
    label = _PROTOCOL_LABELS.get(protocol, "未知接口")
    if version:
        return f"本地 MinerU 连接成功（{label}，版本 {version}）"
    return f"本地 MinerU 连接成功（{label}）"


def _normalized_protocol(value: object) -> str:
    protocol = str(value or "").strip().lower()
    return protocol if protocol in MINERU_LOCAL_PROTOCOLS else DEFAULT_MINERU_LOCAL_PROTOCOL


def _config_from_payload(payload: Mapping[str, object]) -> MinerULocalConfig:
    endpoint = str(
        payload.get("endpoint") or DEFAULT_MINERU_LOCAL_ENDPOINT
    ).strip().rstrip("/")
    backend = str(
        payload.get("backend") or DEFAULT_MINERU_LOCAL_BACKEND
    ).strip()
    if not backend:
        raise MinerUError("请填写本地 MinerU 解析后端。")
    protocol = str(
        payload.get("protocol") or DEFAULT_MINERU_LOCAL_PROTOCOL
    ).strip().lower()
    if protocol not in MINERU_LOCAL_PROTOCOLS:
        raise MinerUError("本地部署接口协议只能是 auto、tasks 或 v1-jobs。")
    tier = str(payload.get("tier") or "").strip().lower()
    api_key = str(payload.get("api_key") or "").strip()
    try:
        return MinerULocalConfig(
            endpoint=endpoint,
            backend=backend,
            protocol=protocol,
            tier=tier,
            api_key=api_key,
        )
    except ValueError as exc:
        raise MinerUError("本地服务地址必须是以 http:// 或 https:// 开头的网址。") from exc

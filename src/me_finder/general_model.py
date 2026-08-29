"""A single self-hosted, OpenAI-compatible model as an optional local parser.

This reuses the vision (OpenAI-compatible) parsing backend in :mod:`vision_api`
— the same client, model discovery and connection test — but keeps its own
one-record config file so it stays *separate* from the "其他解析 API" provider
list and is surfaced under 本地 OCR instead.

Two deliberate differences from a hosted vision provider:

* one record, not a list (a single "bring-your-own local model" slot); and
* ``api_key`` may be empty — most self-hosted vLLM/Ollama/SGLang endpoints
  need no key, and the vision client already tolerates an empty token.

Import routes to it through the reserved ``provider_id`` below:
:func:`vision_api.load_vision_provider` delegates here for that id, so the whole
downstream vision path (rendering, resume, manifests) is unchanged.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Mapping, Optional

from .vision_api import (
    OpenAICompatibleVisionClient,
    VisionAPIError,
    VisionProviderConfig,
    _validated_url,
    discover_vision_models,
)

# Reserved id used to route an import job to the general local model through the
# existing vision provider path. Kept distinct from generated vision ids
# (``provider-<hex>``), so the two config stores never collide.
GENERAL_MODEL_PROVIDER_ID = "general-local-model"
GENERAL_MODEL_CONFIG_FILENAME = "general_model.local.json"
DEFAULT_GENERAL_MODEL_CONFIG_PATH = Path("config") / GENERAL_MODEL_CONFIG_FILENAME
DEFAULT_GENERAL_MODEL_NAME = "通用本地模型"
_MAX_KEY_LENGTH = 8192
_MAX_MODEL_LENGTH = 160
_MAX_NAME_LENGTH = 80


def resolve_general_model_config_path(root: Optional[Path] = None) -> Path:
    """Resolve the config path, honoring an explicit env override."""

    override = os.environ.get("ME_FINDER_GENERAL_MODEL_CONFIG", "").strip()
    if override:
        return Path(override)
    if root is not None:
        return Path(root) / DEFAULT_GENERAL_MODEL_CONFIG_PATH
    return DEFAULT_GENERAL_MODEL_CONFIG_PATH


def general_model_config_path_for(vision_config_path: Path) -> Path:
    """The general-model config that sits beside a given vision config file."""

    return Path(vision_config_path).parent / GENERAL_MODEL_CONFIG_FILENAME


def read_general_model_config(
    path: Path = DEFAULT_GENERAL_MODEL_CONFIG_PATH,
) -> Dict[str, object]:
    """Read the stored record, tolerating a missing or malformed file."""

    empty = {
        "api_base": "",
        "model": "",
        "api_key": "",
        "name": "",
        "enabled": False,
        "use_env_proxy": False,
    }
    path = Path(path)
    if not path.exists():
        return dict(empty)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(empty)
    if not isinstance(data, dict):
        return dict(empty)
    record = dict(empty)
    record.update(
        {
            "api_base": str(data.get("api_base") or ""),
            "model": str(data.get("model") or ""),
            "api_key": str(data.get("api_key") or ""),
            "name": str(data.get("name") or ""),
            "enabled": bool(data.get("enabled")),
            "use_env_proxy": bool(data.get("use_env_proxy")),
        }
    )
    return record


def general_model_summary(
    path: Path = DEFAULT_GENERAL_MODEL_CONFIG_PATH,
) -> Dict[str, object]:
    """UI-facing summary; never returns the stored key."""

    record = read_general_model_config(path)
    model = str(record.get("model") or "").strip()
    api_base = str(record.get("api_base") or "").strip()
    configured = bool(model and api_base)
    return {
        "provider_id": GENERAL_MODEL_PROVIDER_ID,
        "configured": configured,
        "enabled": bool(record.get("enabled")) and configured,
        "name": str(record.get("name") or "").strip() or DEFAULT_GENERAL_MODEL_NAME,
        "api_base": api_base,
        "model": model,
        "has_key": bool(str(record.get("api_key") or "").strip()),
        "use_env_proxy": bool(record.get("use_env_proxy")),
    }


def _validated_general_updates(
    updates: Mapping[str, object],
    existing: Mapping[str, object],
) -> Dict[str, object]:
    model = str(updates.get("model") or "").strip()
    if not model or len(model) > _MAX_MODEL_LENGTH:
        raise VisionAPIError("请填写有效的模型名称。")
    api_base = _validated_url(updates.get("api_base"))
    name = str(updates.get("name") or "").strip()
    if len(name) > _MAX_NAME_LENGTH:
        raise VisionAPIError("显示名称最多 80 个字符。")
    record: Dict[str, object] = {
        "api_base": api_base,
        "model": model,
        "name": name,
        "enabled": bool(updates.get("enabled", False)),
        "use_env_proxy": bool(updates.get("use_env_proxy")),
    }
    # api_key is optional (self-hosted endpoints often need none); a blank value
    # on edit keeps the previously stored key.
    api_key = str(updates.get("api_key") or "").strip()
    if api_key:
        if len(api_key) > _MAX_KEY_LENGTH:
            raise VisionAPIError("API Key 过长。")
        record["api_key"] = api_key
    else:
        record["api_key"] = str(existing.get("api_key") or "")
    return record


def save_general_model_config(
    updates: Mapping[str, object],
    path: Path = DEFAULT_GENERAL_MODEL_CONFIG_PATH,
) -> Dict[str, object]:
    """Validate and persist the single general-model record atomically."""

    path = Path(path)
    existing = read_general_model_config(path)
    record = _validated_general_updates(updates, existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    )
    try:
        with handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return general_model_summary(path)


def load_general_model_provider(
    path: Path = DEFAULT_GENERAL_MODEL_CONFIG_PATH,
) -> VisionProviderConfig:
    """Resolve the stored record into a runnable provider config.

    Raises :class:`VisionAPIError` when disabled or incomplete, mirroring
    :func:`vision_api.load_vision_provider`, but tolerating an empty key.
    """

    record = read_general_model_config(path)
    if not record.get("enabled"):
        raise VisionAPIError("通用本地模型尚未启用。")
    model = str(record.get("model") or "").strip()
    api_base = str(record.get("api_base") or "").strip()
    if not model or not api_base:
        raise VisionAPIError("通用本地模型尚未配置完整（需要服务地址和模型）。")
    return VisionProviderConfig(
        provider_id=GENERAL_MODEL_PROVIDER_ID,
        name=str(record.get("name") or "").strip() or DEFAULT_GENERAL_MODEL_NAME,
        api_base=_validated_url(api_base),
        api_key=str(record.get("api_key") or ""),
        model=model,
        enabled=True,
        use_env_proxy=bool(record.get("use_env_proxy")),
    )


def _config_from_payload(
    payload: Mapping[str, object],
    stored: Mapping[str, object],
) -> VisionProviderConfig:
    model = str(payload.get("model") or "").strip()
    api_base = str(payload.get("api_base") or "").strip()
    if not model or not api_base:
        raise VisionAPIError("请先填写服务地址和模型再测试连接。")
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key:
        api_key = str(stored.get("api_key") or "")
    return VisionProviderConfig(
        provider_id=GENERAL_MODEL_PROVIDER_ID,
        name=str(payload.get("name") or "").strip() or DEFAULT_GENERAL_MODEL_NAME,
        api_base=_validated_url(api_base),
        api_key=api_key,
        model=model,
        enabled=True,
        use_env_proxy=bool(payload.get("use_env_proxy")),
    )


def test_general_model(
    payload: Mapping[str, object],
    path: Path = DEFAULT_GENERAL_MODEL_CONFIG_PATH,
) -> Dict[str, object]:
    """Connection-test the current form values against the endpoint."""

    import time

    stored = read_general_model_config(path)
    config = _config_from_payload(payload, stored)
    started = time.monotonic()
    response = OpenAICompatibleVisionClient(config).test_connection()
    return {
        "ok": True,
        "provider_id": config.provider_id,
        "provider_name": config.name,
        "model": config.model,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "response_preview": response[:80],
    }


def discover_general_model_models(
    payload: Mapping[str, object],
    path: Path = DEFAULT_GENERAL_MODEL_CONFIG_PATH,
) -> Dict[str, object]:
    """List models the endpoint exposes, reusing the vision discovery core."""

    stored = read_general_model_config(path)
    probe = dict(payload)
    if not str(probe.get("api_key") or "").strip():
        probe["api_key"] = str(stored.get("api_key") or "")
    return discover_vision_models(probe, config_path=path)

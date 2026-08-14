"""OpenAI-compatible vision providers used as optional PDF parsers.

Provider credentials are stored in a separate local-only JSON file.  The
public summary helpers never return API keys.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence
from urllib.parse import urlparse

from .import_resume import (
    atomic_write_json,
    load_json_object,
    manifest_matches,
    options_fingerprint,
    quarantine_corrupt_manifest,
    refresh_manifest_progress,
    resume_summary,
    sha256_file,
    utc_now_iso,
)
from .pdf_extractors import load_pymupdf


DEFAULT_VISION_CONFIG_PATH = Path("config/vision_api.local.json")
DEFAULT_VISION_RESULT_DIR = Path("corpus/processed/vision/results")
DEFAULT_VISION_MANIFEST_DIR = Path("corpus/processed/vision/manifests")
DEFAULT_VISION_WORK_MANIFEST_DIR = DEFAULT_VISION_MANIFEST_DIR / "work"
VISION_PARSER_VERSION = "openai-compatible-vision-v2"
VISION_PROMPT_VERSION = "literal-transcription-v1"
VISION_RENDER_LONGEST_EDGE = 1800
VISION_RENDER_MIN_SCALE = 1.0
VISION_RENDER_MAX_SCALE = 2.0
MAX_PROVIDER_COUNT = 12
MAX_DISCOVERED_MODELS = 2000
MAX_MODELS_RESPONSE_BYTES = 4 * 1024 * 1024
VISION_TEST_IMAGE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAYklEQVR4nO3PQQ0A"
    "IRDAQEScf5f8TwQkZZOpgs7aw1v1wGkAdQB1AHUAdQB1AHUAdQB1AHVHgO9SAAAA"
    "AAAAAAAAAAAAAAAAAACzAC8EUAdQB1AHUAdQB1AHUAdQB1A3HvAD6EoDmRtp1t4A"
    "AAAASUVORK5CYII="
)
ProgressCallback = Callable[[Dict[str, object]], None]


class VisionAPIError(RuntimeError):
    def __init__(self, message: str, *, stop_document: bool = False) -> None:
        super().__init__(message)
        self.stop_document = bool(stop_document)


@dataclass(frozen=True)
class VisionProviderConfig:
    provider_id: str
    name: str
    api_base: str
    api_key: str = field(repr=False)
    model: str
    enabled: bool = True
    use_env_proxy: bool = False


def resolve_vision_config_path(root: Optional[Path] = None) -> Path:
    override = os.environ.get("ME_FINDER_VISION_CONFIG", "").strip()
    if override:
        return Path(override)
    if root is not None:
        return Path(root) / DEFAULT_VISION_CONFIG_PATH
    return DEFAULT_VISION_CONFIG_PATH


def read_vision_config_data(
    path: Path = DEFAULT_VISION_CONFIG_PATH,
) -> Dict[str, object]:
    path = Path(path)
    if not path.exists():
        return {
            "providers": [],
            "default_provider_id": None,
            "auto_fallback_from_mineru": False,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VisionAPIError("其他解析 API 配置文件无法读取。") from exc
    if not isinstance(data, dict):
        raise VisionAPIError("其他解析 API 配置必须是 JSON 对象。")
    providers = data.get("providers")
    if not isinstance(providers, list):
        data["providers"] = []
    data["auto_fallback_from_mineru"] = bool(data.get("auto_fallback_from_mineru"))
    return data


def _provider_summary(provider: Mapping[str, object]) -> Dict[str, object]:
    api_key = str(provider.get("api_key") or "").strip()
    return {
        "id": str(provider.get("id") or ""),
        "name": str(provider.get("name") or "未命名接口"),
        "api_type": "openai_compatible",
        "api_base": str(provider.get("api_base") or ""),
        "model": str(provider.get("model") or ""),
        "enabled": bool(provider.get("enabled", True)),
        "configured": bool(
            api_key
            and str(provider.get("api_base") or "").strip()
            and str(provider.get("model") or "").strip()
        ),
        "has_api_key": bool(api_key),
    }


def vision_config_summary(
    path: Path = DEFAULT_VISION_CONFIG_PATH,
) -> Dict[str, object]:
    data = read_vision_config_data(path)
    providers = [
        _provider_summary(item)
        for item in data.get("providers", [])
        if isinstance(item, dict) and item.get("id")
    ]
    configured_ids = [
        str(item["id"])
        for item in providers
        if item.get("configured") and item.get("enabled")
    ]
    default_provider_id = configured_ids[0] if configured_ids else ""
    return {
        "providers": providers,
        "default_provider_id": default_provider_id or None,
        "auto_fallback_from_mineru": bool(
            data.get("auto_fallback_from_mineru") and default_provider_id
        ),
        "has_configured_provider": bool(configured_ids),
    }


def _validated_url(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text or len(text) > 2048:
        raise VisionAPIError("请填写有效的 API 地址。")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VisionAPIError("API 地址必须是以 http:// 或 https:// 开头的网址。")
    if parsed.username or parsed.password:
        raise VisionAPIError("API 地址中不能包含用户名或密码。")
    return text


def _validated_provider_id(value: object, *, allow_empty: bool = False) -> str:
    provider_id = str(value or "").strip()
    if not provider_id and allow_empty:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", provider_id):
        raise VisionAPIError("解析服务 ID 无效。")
    return provider_id


def _eligible_provider_ids(providers: object) -> list[str]:
    if not isinstance(providers, list):
        return []
    return [
        str(item.get("id")).strip()
        for item in providers
        if isinstance(item, dict)
        and str(item.get("id") or "").strip()
        and item.get("enabled", True)
        and str(item.get("api_key") or "").strip()
        and str(item.get("api_base") or "").strip()
        and str(item.get("model") or "").strip()
    ]


def _write_vision_config(path: Path, data: Mapping[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(data), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def save_vision_provider(
    updates: Mapping[str, object],
    path: Path = DEFAULT_VISION_CONFIG_PATH,
) -> Dict[str, object]:
    data = read_vision_config_data(path)
    providers = [
        dict(item)
        for item in data.get("providers", [])
        if isinstance(item, dict) and item.get("id")
    ]
    previously_eligible_ids = _eligible_provider_ids(providers)
    requested_id = _validated_provider_id(updates.get("id"), allow_empty=True)
    provider_id = requested_id or f"provider-{uuid.uuid4().hex[:12]}"
    existing = next(
        (item for item in providers if str(item.get("id")) == provider_id),
        None,
    )
    if existing is None:
        if len(providers) >= MAX_PROVIDER_COUNT:
            raise VisionAPIError(f"最多只能保存 {MAX_PROVIDER_COUNT} 个解析接口。")
        existing = {"id": provider_id}
        providers.append(existing)

    name = str(updates.get("name") or "").strip()
    model = str(updates.get("model") or "").strip()
    api_key = str(updates.get("api_key") or "").strip()
    if not name or len(name) > 80:
        raise VisionAPIError("请填写 1–80 个字符的显示名称（会根据 API 地址自动生成，也可以自己修改）。")
    if not model or len(model) > 160:
        raise VisionAPIError("请填写有效的视觉模型名称。")
    existing.update(
        {
            "id": provider_id,
            "name": name,
            "api_type": "openai_compatible",
            "api_base": _validated_url(updates.get("api_base")),
            "model": model,
            "enabled": bool(updates.get("enabled", True)),
        }
    )
    if api_key:
        if len(api_key) > 8192:
            raise VisionAPIError("API Key 过长。")
        existing["api_key"] = api_key
    elif "api_key" not in existing:
        existing["api_key"] = ""

    data["providers"] = providers
    eligible_ids = _eligible_provider_ids(providers)
    data["default_provider_id"] = eligible_ids[0] if eligible_ids else None
    if not eligible_ids or not previously_eligible_ids:
        data["auto_fallback_from_mineru"] = False
    _write_vision_config(Path(path), data)
    return vision_config_summary(Path(path))


def delete_vision_provider(
    provider_id: str,
    path: Path = DEFAULT_VISION_CONFIG_PATH,
) -> Dict[str, object]:
    provider_id = _validated_provider_id(provider_id)
    data = read_vision_config_data(path)
    providers = [
        dict(item)
        for item in data.get("providers", [])
        if isinstance(item, dict) and str(item.get("id")) != provider_id
    ]
    data["providers"] = providers
    eligible_ids = _eligible_provider_ids(providers)
    data["default_provider_id"] = eligible_ids[0] if eligible_ids else None
    if not eligible_ids:
        data["auto_fallback_from_mineru"] = False
    _write_vision_config(Path(path), data)
    return vision_config_summary(Path(path))


def save_vision_policy(
    updates: Mapping[str, object],
    path: Path = DEFAULT_VISION_CONFIG_PATH,
) -> Dict[str, object]:
    data = read_vision_config_data(path)
    raw_auto_fallback = updates.get("auto_fallback_from_mineru")
    if not isinstance(raw_auto_fallback, bool):
        raise VisionAPIError("自动切换设置必须是布尔值。")
    auto_fallback = raw_auto_fallback
    provider_ids = _eligible_provider_ids(data.get("providers"))
    provider_id = provider_ids[0] if provider_ids else ""
    if auto_fallback and not provider_id:
        raise VisionAPIError("开启自动切换前，请先添加并启用一个解析接口。")
    data["default_provider_id"] = provider_id or None
    data["auto_fallback_from_mineru"] = auto_fallback
    _write_vision_config(Path(path), data)
    return vision_config_summary(Path(path))


def load_vision_provider(
    provider_id: Optional[str] = None,
    path: Path = DEFAULT_VISION_CONFIG_PATH,
) -> VisionProviderConfig:
    data = read_vision_config_data(path)
    requested = str(provider_id or "").strip()
    providers = [
        item for item in data.get("providers", []) if isinstance(item, dict)
    ]
    if not requested:
        eligible_ids = _eligible_provider_ids(providers)
        requested = eligible_ids[0] if eligible_ids else ""
    raw = next(
        (item for item in providers if str(item.get("id") or "") == requested),
        None,
    )
    if raw is None:
        raise VisionAPIError("没有找到可用的其他解析 API。")
    if not raw.get("enabled", True):
        raise VisionAPIError("所选解析 API 已停用。")
    api_key = str(raw.get("api_key") or "").strip()
    model = str(raw.get("model") or "").strip()
    if not api_key or not model:
        raise VisionAPIError("所选解析 API 尚未配置完整。")
    return VisionProviderConfig(
        provider_id=requested,
        name=str(raw.get("name") or requested),
        api_base=_validated_url(raw.get("api_base")),
        api_key=api_key,
        model=model,
        enabled=True,
        use_env_proxy=bool(raw.get("use_env_proxy")),
    )


def default_fallback_provider(
    path: Path = DEFAULT_VISION_CONFIG_PATH,
) -> Optional[VisionProviderConfig]:
    data = read_vision_config_data(path)
    if not data.get("auto_fallback_from_mineru"):
        return None
    try:
        return load_vision_provider(None, path)
    except VisionAPIError:
        return None


def _api_key_token(api_key: str) -> str:
    """Accept either a raw token or a copied ``Bearer …`` value."""

    token = str(api_key or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _auth_header_variants(api_key: str) -> list[tuple[str, Dict[str, str]]]:
    """Authentication styles used by common OpenAI-compatible relays."""

    token = _api_key_token(api_key)
    return [
        ("Bearer", {"Authorization": f"Bearer {token}"}),
        ("x-api-key", {"x-api-key": token}),
        ("x-goog-api-key", {"x-goog-api-key": token}),
    ]


def _chat_endpoints(api_base: str) -> list[str]:
    parsed = urlparse(api_base.rstrip("/"))
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    if lowered.endswith("/chat/completions"):
        return [parsed._replace(path=path, params="", fragment="").geturl()]
    if lowered.endswith("/models"):
        path = path[: -len("/models")]
    elif lowered.endswith("/responses"):
        path = path[: -len("/responses")]
    if not path:
        versioned = parsed._replace(
            path="/v1/chat/completions", params="", fragment=""
        ).geturl()
        unversioned = parsed._replace(
            path="/chat/completions", params="", fragment=""
        ).geturl()
        return [versioned, unversioned]
    return [
        parsed._replace(
            path=path + "/chat/completions", params="", fragment=""
        ).geturl()
    ]


def _chat_endpoint(api_base: str) -> str:
    return _chat_endpoints(api_base)[0]


def _responses_endpoints(api_base: str) -> list[str]:
    parsed = urlparse(api_base.rstrip("/"))
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    if lowered.endswith("/responses"):
        return [parsed._replace(path=path, params="", fragment="").geturl()]
    if lowered.endswith("/models"):
        path = path[: -len("/models")]
    elif lowered.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    if not path:
        versioned = parsed._replace(
            path="/v1/responses", params="", fragment=""
        ).geturl()
        unversioned = parsed._replace(
            path="/responses", params="", fragment=""
        ).geturl()
        return [versioned, unversioned]
    return [
        parsed._replace(path=path + "/responses", params="", fragment="").geturl()
    ]


def _responses_endpoint(api_base: str) -> str:
    return _responses_endpoints(api_base)[0]


def _models_endpoints(api_base: str) -> list[str]:
    parsed = urlparse(api_base.rstrip("/"))
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    if lowered.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    elif lowered.endswith("/responses"):
        path = path[: -len("/responses")]
    if not path:
        versioned = parsed._replace(path="/v1/models", params="", fragment="").geturl()
        unversioned = parsed._replace(path="/models", params="", fragment="").geturl()
        return [versioned, unversioned]
    if not path.lower().endswith("/models"):
        path += "/models"
    return [parsed._replace(path=path, params="", fragment="").geturl()]


def _models_endpoint(api_base: str) -> str:
    return _models_endpoints(api_base)[0]


def _model_has_image_input(item: Mapping[str, object]) -> bool:
    modalities = item.get("input_modalities") or item.get("modalities")
    return isinstance(modalities, list) and any(
        str(modality).strip().lower() in {"image", "images", "vision"}
        for modality in modalities
    )


def _vision_model_capability(
    model_id: str,
    item: Mapping[str, object],
) -> Dict[str, object]:
    """Classify model-list entries for the visual-parser picker.

    The catalog is advisory UI metadata, not an invocation allow-list.  Keep
    the labels deliberately simple for non-technical users: OCR models are
    promoted, documented capabilities are shown directly, and unknown models
    stay unconfirmed until the real image connection test succeeds.
    """

    normalized = model_id.strip().lower()
    basename = normalized.rsplit("/", 1)[-1]
    qwen37_max_snapshot = re.fullmatch(
        r"qwen3\.7-max-(20\d{2}-\d{2}-\d{2})",
        basename,
    )
    qwen37_max_has_vision = bool(
        qwen37_max_snapshot
        and qwen37_max_snapshot.group(1) >= "2026-06-08"
    )

    if "ocr" in basename:
        if basename == "qwen3.5-ocr":
            label = "OCR专用 · 推荐"
            priority = 0
        elif basename.endswith("-latest"):
            label = "OCR专用"
            priority = 1
        elif re.search(r"[-_]20\d{2}[-_]\d{2}[-_]\d{2}$", basename):
            label = "OCR专用 · 固定版本"
            priority = 2
        else:
            label = "OCR专用"
            priority = 3
        return {
            "capability": "ocr",
            "capability_label": label,
            "capability_priority": priority,
            "likely_vision": True,
        }

    vision_hints = (
        "vision",
        "qwen-vl",
        "qwen2-vl",
        "qwen2.5-vl",
        "qwen3-vl",
        "qwen3.5-",
        "qwen3.6-plus",
        "qwen3.6-flash",
        "qwen3.6-35b-a3b",
        "qwen3.7-plus",
        "qwen3.7-flash",
        "qwen3.7-max-2026-06-08",
        "qwen3.8-",
        "omni",
        "qvq",
        "internvl",
        "minicpm-v",
        "glm-4v",
        "glm-4.5v",
        "glm-4.6v",
        "kimi-vl",
        "yi-vision",
        "step-1v",
        "pixtral",
        "llava",
        "llama-vision",
        "llama-3.2-11b",
        "llama-3.2-90b",
        "doubao-vision",
        "seed-vl",
        "seed-vision",
        "hunyuan-vision",
        "ernie-vl",
        "minimax-vl",
        "moonshot-v1-vision",
        "kimi-k2.5",
        "kimi-k2.6",
        "kimi-k2.7",
        "kimi-k3",
        "minimax-m3",
        "gemini",
        "claude",
        "gpt-4o",
        "gpt-4.1",
        "gpt-5",
    )
    if qwen37_max_has_vision or _model_has_image_input(item) or any(
        hint in normalized for hint in vision_hints
    ):
        fast = "flash" in basename
        plus = "plus" in basename
        return {
            "capability": "vision",
            "capability_label": "支持图片",
            "capability_priority": 110 if fast else 100 if plus else 120,
            "likely_vision": True,
        }

    declared_modalities = item.get("input_modalities") or item.get("modalities")
    text_only = (
        (
            isinstance(declared_modalities, list)
            and bool(declared_modalities)
            and not _model_has_image_input(item)
        )
        or "deepseek" in normalized
        or (basename.startswith("qwen3.7-max") and not qwen37_max_has_vision)
        or basename == "qwen3.6-max-preview"
        or basename.startswith("qwen3-max")
        or basename.startswith("qwen-long")
        or basename in {"qwen-max", "qwen-plus", "qwen-turbo"}
        or basename in {"kimi-k2-thinking", "moonshot-kimi-k2-instruct"}
        or basename.startswith("kimi-k2-instruct")
        or re.fullmatch(r"glm-(?:4\.[5-7]|5(?:\.[12])?)(?:-.+)?", basename)
        or re.fullmatch(r"minimax-m2(?:\.[0-9]+)?(?:-.+)?", basename)
        or basename.startswith("mimo-v2.5")
    )
    if text_only:
        return {
            "capability": "text",
            "capability_label": "不支持图片",
            "capability_priority": 900,
            "likely_vision": False,
        }

    return {
        "capability": "unknown",
        "capability_label": "待确认 · 请测试",
        "capability_priority": 500,
        "likely_vision": False,
    }


def _likely_vision_model(model_id: str, item: Mapping[str, object]) -> bool:
    return bool(_vision_model_capability(model_id, item)["likely_vision"])


def _normalize_model_list(data: object) -> list[Dict[str, object]]:
    if isinstance(data, dict):
        candidates = data.get("data")
        if not isinstance(candidates, list):
            candidates = data.get("models")
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = None
    if not isinstance(candidates, list):
        raise VisionAPIError("接口返回的模型列表格式无法识别，请手动填写模型名称。")

    models: list[Dict[str, object]] = []
    seen: set[str] = set()
    for raw in candidates:
        if isinstance(raw, str):
            model_id = raw.strip()
            item: Mapping[str, object] = {}
        elif isinstance(raw, dict):
            model_id = str(
                raw.get("id") or raw.get("model") or raw.get("name") or ""
            ).strip()
            item = raw
        else:
            continue
        if not model_id or len(model_id) > 256 or model_id in seen:
            continue
        seen.add(model_id)
        owned_by = str(item.get("owned_by") or item.get("provider") or "").strip()
        capability = _vision_model_capability(model_id, item)
        models.append(
            {
                "id": model_id,
                "owned_by": owned_by[:160],
                **capability,
            }
        )
        if len(models) >= MAX_DISCOVERED_MODELS:
            break
    if not models:
        raise VisionAPIError("接口没有返回可选模型，请手动填写模型名称。")
    models.sort(
        key=lambda item: (
            int(item.get("capability_priority") or 0),
            str(item.get("id") or "").casefold(),
        )
    )
    return models


def _model_discovery_credentials(
    provider: Mapping[str, object],
    config_path: Path,
) -> tuple[str, str, str, bool]:
    provider_id = _validated_provider_id(provider.get("id"), allow_empty=True)
    existing: Mapping[str, object] = {}
    if provider_id:
        data = read_vision_config_data(config_path)
        existing = next(
            (
                item
                for item in data.get("providers", [])
                if isinstance(item, dict)
                and str(item.get("id") or "") == provider_id
            ),
            {},
        )
    api_base = str(provider.get("api_base") or existing.get("api_base") or "").strip()
    api_key = str(provider.get("api_key") or existing.get("api_key") or "").strip()
    name = str(provider.get("name") or existing.get("name") or "该接口").strip()
    if not api_key:
        raise VisionAPIError("请先填写 API Key，再获取模型列表。")
    if len(api_key) > 8192:
        raise VisionAPIError("API Key 过长。")
    return (
        _validated_url(api_base),
        api_key,
        name[:80] or "该接口",
        bool(existing.get("use_env_proxy")),
    )


def discover_vision_models(
    provider: Mapping[str, object],
    config_path: Path = DEFAULT_VISION_CONFIG_PATH,
    *,
    timeout: int = 45,
) -> Dict[str, object]:
    api_base, api_key, provider_name, use_env_proxy = _model_discovery_credentials(
        provider,
        Path(config_path),
    )
    opener = (
        urllib.request.build_opener()
        if use_env_proxy
        else urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )
    raw_bytes: Optional[bytes] = None
    last_unauthorized: Optional[urllib.error.HTTPError] = None
    last_forbidden: Optional[urllib.error.HTTPError] = None
    last_not_found: Optional[urllib.error.HTTPError] = None
    for endpoint in _models_endpoints(api_base):
        endpoint_not_found = False
        for _auth_name, auth_headers in _auth_header_variants(api_key):
            request = urllib.request.Request(
                endpoint,
                headers={"Accept": "application/json", **auth_headers},
                method="GET",
            )
            try:
                with opener.open(request, timeout=timeout) as response:
                    raw_bytes = response.read(MAX_MODELS_RESPONSE_BYTES + 1)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    last_unauthorized = exc
                    continue
                if exc.code == 403:
                    last_forbidden = exc
                    continue
                if exc.code == 404:
                    last_not_found = exc
                    endpoint_not_found = True
                    break
                if exc.code == 429:
                    message = "获取模型请求过于频繁，请稍后再试。"
                else:
                    message = f"获取模型列表失败（HTTP {exc.code}），可以改为手动填写。"
                raise VisionAPIError(message) from exc
            except urllib.error.URLError as exc:
                raise VisionAPIError(
                    f"{provider_name} 网络请求失败：{exc.reason}"
                ) from exc
        if raw_bytes is not None:
            break
        if endpoint_not_found:
            continue
    if raw_bytes is None:
        if last_forbidden is not None:
            raise VisionAPIError(
                "当前凭据无权枚举模型；这不代表推理接口不可用。请手动填写模型名称，"
                "保存后用“测试连接”验证。"
            ) from last_forbidden
        if last_unauthorized is not None:
            raise VisionAPIError(
                "API Key 未通过模型列表接口认证（已尝试 Bearer、x-api-key 与 "
                "x-goog-api-key）；也可以手动填写模型名称后用“测试连接”验证。"
            ) from last_unauthorized
        if last_not_found is not None:
            raise VisionAPIError(
                "该接口不支持自动获取模型列表，请手动填写模型名称。"
            ) from last_not_found
        raise VisionAPIError("接口没有返回模型列表，请手动填写模型名称。")
    if len(raw_bytes) > MAX_MODELS_RESPONSE_BYTES:
        raise VisionAPIError("模型列表响应过大，请手动填写模型名称。")
    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise VisionAPIError("接口返回了非 JSON 模型列表，请手动填写模型名称。") from exc
    models = _normalize_model_list(data)
    return {
        "models": models,
        "count": len(models),
        "provider_name": provider_name,
    }


def _message_text(data: Mapping[str, object]) -> str:
    try:
        choices = data["choices"]
        message = choices[0]["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VisionAPIError("接口响应中没有可用的模型输出。") from exc
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        return "\n".join(texts).strip()
    raise VisionAPIError("接口返回了无法识别的模型输出格式。")


def _responses_input(messages: Sequence[Mapping[str, object]]) -> list[Dict[str, object]]:
    converted: list[Dict[str, object]] = []
    for message in messages:
        role = str(message.get("role") or "user").strip() or "user"
        content = message.get("content")
        parts: list[Dict[str, object]] = []
        if isinstance(content, str):
            parts.append({"type": "input_text", "text": content})
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, Mapping):
                    continue
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"text", "input_text"}:
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append({"type": "input_text", "text": text})
                elif item_type in {"image_url", "input_image"}:
                    image_value = item.get("image_url")
                    if isinstance(image_value, Mapping):
                        image_value = image_value.get("url")
                    if isinstance(image_value, str) and image_value:
                        parts.append(
                            {"type": "input_image", "image_url": image_value}
                        )
        if parts:
            converted.append({"role": role, "content": parts})
    return converted


def _responses_text(data: Mapping[str, object]) -> str:
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    output = data.get("output")
    if not isinstance(output, list):
        raise VisionAPIError("接口响应中没有可用的模型输出。")
    texts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            text = part.get("text")
            if isinstance(text, Mapping):
                text = text.get("value")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    result = "\n".join(texts).strip()
    if not result:
        raise VisionAPIError("接口响应中没有可用的模型输出。")
    return result


def _prefers_responses_api(api_base: str, model: str) -> bool:
    path = urlparse(api_base.rstrip("/")).path.rstrip("/").lower()
    if path.endswith("/responses"):
        return True
    if path.endswith("/chat/completions"):
        return False
    normalized_model = str(model or "").strip().lower()
    if normalized_model.startswith("gpt-5") or "codex" in normalized_model:
        return True
    # CC Switch and similar Responses configurations commonly store the origin
    # as the base URL.  Versioned OpenAI-compatible Chat Completions bases are
    # normally saved with a trailing /v1.
    return not path


def _unsupported_api_route(status: int, detail: str) -> bool:
    if status in {404, 405}:
        return True
    if status != 400:
        return False
    normalized = detail.casefold()
    markers = (
        "unknown endpoint",
        "unsupported endpoint",
        "unsupported api",
        "route not found",
        "no route",
        "invalid url",
    )
    return any(marker in normalized for marker in markers)


class OpenAICompatibleVisionClient:
    def __init__(self, config: VisionProviderConfig) -> None:
        self.config = config
        self._working_endpoint: Optional[str] = None
        self._working_auth_name: Optional[str] = None
        self._working_protocol: Optional[str] = None
        if config.use_env_proxy:
            self.opener = urllib.request.build_opener()
        else:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({})
            )

    def _request(
        self, messages: list[Dict[str, object]], timeout: int = 240
    ) -> str:
        protocol_order = (
            ["responses", "chat"]
            if _prefers_responses_api(self.config.api_base, self.config.model)
            else ["chat", "responses"]
        )
        if self._working_protocol:
            protocol_order = [self._working_protocol] + [
                protocol
                for protocol in protocol_order
                if protocol != self._working_protocol
            ]
        auth_variants = _auth_header_variants(self.config.api_key)
        if self._working_endpoint and self._working_auth_name:
            auth_variants = sorted(
                auth_variants,
                key=lambda item: item[0] != self._working_auth_name,
            )
        raw: Optional[str] = None
        response_protocol: Optional[str] = None
        last_auth_error: Optional[urllib.error.HTTPError] = None
        last_not_found: Optional[urllib.error.HTTPError] = None
        for protocol in protocol_order:
            if protocol == "responses":
                payload = {
                    "model": self.config.model,
                    "input": _responses_input(messages),
                    "max_output_tokens": 4096,
                }
                endpoints = _responses_endpoints(self.config.api_base)
            else:
                payload = {
                    "model": self.config.model,
                    "messages": messages,
                    "temperature": 0,
                    "max_tokens": 4096,
                }
                endpoints = _chat_endpoints(self.config.api_base)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if self._working_endpoint and protocol == self._working_protocol:
                endpoints = [self._working_endpoint] + [
                    endpoint
                    for endpoint in endpoints
                    if endpoint != self._working_endpoint
                ]
            for endpoint in endpoints:
                endpoint_not_found = False
                for auth_name, auth_headers in auth_variants:
                    request = urllib.request.Request(
                        endpoint,
                        data=body,
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            **auth_headers,
                        },
                        method="POST",
                    )
                    try:
                        with self.opener.open(request, timeout=timeout) as response:
                            raw = response.read().decode("utf-8", errors="replace")
                        self._working_endpoint = endpoint
                        self._working_auth_name = auth_name
                        self._working_protocol = protocol
                        response_protocol = protocol
                        break
                    except urllib.error.HTTPError as exc:
                        if exc.code in {401, 403}:
                            last_auth_error = exc
                            continue
                        detail = exc.read().decode(
                            "utf-8", errors="replace"
                        )[:500]
                        if _unsupported_api_route(exc.code, detail):
                            last_not_found = exc
                            endpoint_not_found = True
                            break
                        raise VisionAPIError(
                            f"{self.config.name} 返回 HTTP {exc.code}："
                            f"{detail or '请求失败'}",
                            stop_document=True,
                        ) from exc
                    except urllib.error.URLError as exc:
                        raise VisionAPIError(
                            f"{self.config.name} 网络请求失败：{exc.reason}",
                            stop_document=True,
                        ) from exc
                if raw is not None:
                    break
                if endpoint_not_found:
                    continue
            if raw is not None:
                break
        if raw is None:
            if last_auth_error is not None:
                raise VisionAPIError(
                    f"{self.config.name} 认证失败（已尝试 Bearer、x-api-key 与 "
                    "x-goog-api-key）。",
                    stop_document=True,
                ) from last_auth_error
            if last_not_found is not None:
                raise VisionAPIError(
                    f"{self.config.name} 没有可用的 OpenAI Responses 或 "
                    "Chat Completions 地址。",
                    stop_document=True,
                ) from last_not_found
            raise VisionAPIError(
                f"{self.config.name} 没有返回响应。",
                stop_document=True,
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VisionAPIError(
                f"{self.config.name} 返回了非 JSON 响应。",
                stop_document=True,
            ) from exc
        if not isinstance(data, dict):
            raise VisionAPIError(
                f"{self.config.name} 返回格式无效。",
                stop_document=True,
            )
        try:
            if response_protocol == "responses":
                return _responses_text(data)
            return _message_text(data)
        except VisionAPIError as exc:
            raise VisionAPIError(
                str(exc),
                stop_document=True,
            ) from exc

    def extract_page(self, image_bytes: bytes, mime_type: str = "image/png") -> str:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        messages: list[Dict[str, object]] = [
            {
                "role": "system",
                "content": (
                    "你是文献 PDF 的逐页文字识别器。只转写图片中真实可见的文字，"
                    "按自然阅读顺序输出；保留段落、标题、脚注和页码；不要总结、解释、"
                    "补写或使用 Markdown 代码围栏。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "请完整转写这一页。若页面没有文字，返回空字符串。",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{encoded}",
                        },
                    },
                ],
            },
        ]
        text = self._request(messages)
        fenced = re.fullmatch(r"```(?:text|markdown)?\s*(.*?)\s*```", text, re.S)
        return (fenced.group(1) if fenced else text).strip()

    def test_connection(self) -> str:
        return self._request(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "这是一项视觉输入能力测试。若能读取随附图片，"
                                "且看到中央深色方块，请仅回复 OK。"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    "data:image/png;base64,"
                                    f"{VISION_TEST_IMAGE_BASE64}"
                                )
                            },
                        },
                    ],
                }
            ],
            timeout=60,
        )


def test_vision_provider(
    provider_id: str,
    config_path: Path = DEFAULT_VISION_CONFIG_PATH,
) -> Dict[str, object]:
    config = load_vision_provider(provider_id, config_path)
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


def _atomic_write_json_list(path: Path, payload: Sequence[Mapping[str, object]]) -> Path:
    """Atomically publish the list format consumed by ``pdf_extractors``."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            encoder = json.JSONEncoder(
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("[")
            for index, item in enumerate(payload):
                if index:
                    handle.write(",")
                for chunk in encoder.iterencode(item):
                    handle.write(chunk)
            handle.write("]\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _vision_parse_options(provider: VisionProviderConfig) -> Dict[str, object]:
    """Options whose changes make old page checkpoints unsafe to reuse."""

    return {
        "provider_id": provider.provider_id,
        "api_base": provider.api_base,
        "model": provider.model,
        "parser_version": VISION_PARSER_VERSION,
        "prompt_version": VISION_PROMPT_VERSION,
        "render_longest_edge": VISION_RENDER_LONGEST_EDGE,
        "render_min_scale": VISION_RENDER_MIN_SCALE,
        "render_max_scale": VISION_RENDER_MAX_SCALE,
    }


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(Path(path).resolve())


def _new_vision_work_manifest(
    *,
    root: Path,
    pdf_path: Path,
    source_file_id: str,
    provider: VisionProviderConfig,
    file_hash: str,
    total_pages: int,
    parse_options: Mapping[str, object],
    parse_options_fingerprint: str,
    task_id: str,
    result_dir: Path,
) -> Dict[str, object]:
    pages: list[Dict[str, object]] = []
    for page_idx in range(total_pages):
        pages.append(
            {
                "page_number": page_idx + 1,
                "page_idx": page_idx,
                "status": "pending",
                "attempts": 0,
                "checkpoint": _relative_to_root(
                    result_dir / "pages" / f"page-{page_idx + 1:06d}.json",
                    root,
                ),
            }
        )
    manifest: Dict[str, object] = {
        "api": "openai_compatible_vision",
        "parser": "openai_compatible",
        "parser_version": VISION_PARSER_VERSION,
        "pdf_path": str(pdf_path),
        "file_name": pdf_path.name,
        "file_hash": file_hash,
        "data_id_prefix": source_file_id,
        "total_pages": total_pages,
        "provider_id": provider.provider_id,
        "provider_name": provider.name,
        "model": provider.model,
        "parse_options": dict(parse_options),
        "parse_options_fingerprint": parse_options_fingerprint,
        "task_id": task_id,
        "result_dir": _relative_to_root(result_dir, root),
        "created_at": utc_now_iso(),
        "resume_count": 0,
        "pages": pages,
    }
    return refresh_manifest_progress(manifest, units=pages)


def _normalize_vision_work_pages(
    manifest: Dict[str, object],
    *,
    root: Path,
    result_dir: Path,
    total_pages: int,
) -> list[Dict[str, object]]:
    existing = {
        int(item.get("page_idx")): dict(item)
        for item in manifest.get("pages", [])
        if isinstance(item, dict)
        and isinstance(item.get("page_idx"), int)
        and 0 <= int(item["page_idx"]) < total_pages
    }
    pages: list[Dict[str, object]] = []
    for page_idx in range(total_pages):
        item = existing.get(page_idx, {})
        status = str(item.get("status") or "pending").lower()
        if status == "running":
            status = "interrupted"
        try:
            attempts = max(0, int(item.get("attempts") or 0))
        except (TypeError, ValueError):
            attempts = 0
        normalized: Dict[str, object] = {
            **item,
            "page_number": page_idx + 1,
            "page_idx": page_idx,
            "status": status,
            "attempts": attempts,
            "checkpoint": _relative_to_root(
                result_dir / "pages" / f"page-{page_idx + 1:06d}.json",
                root,
            ),
        }
        pages.append(normalized)
    manifest["pages"] = pages
    return pages


def _load_valid_vision_page_checkpoint(
    path: Path,
    *,
    page_idx: int,
    file_hash: str,
    provider: VisionProviderConfig,
    parse_options_fingerprint: str,
) -> Optional[Dict[str, object]]:
    try:
        checkpoint = load_json_object(path)
    except Exception:
        return None
    if checkpoint is None:
        return None
    if not (
        checkpoint.get("type") == "text"
        and isinstance(checkpoint.get("text"), str)
        and checkpoint.get("parser") == "openai_compatible"
        and checkpoint.get("page_idx") == page_idx
        and checkpoint.get("file_hash") == file_hash
        and checkpoint.get("provider_id") == provider.provider_id
        and checkpoint.get("model") == provider.model
        and checkpoint.get("parse_options_fingerprint")
        == parse_options_fingerprint
    ):
        return None
    return checkpoint


def _vision_progress_payload(
    manifest: Mapping[str, object],
    *,
    provider: VisionProviderConfig,
    page: Optional[int] = None,
    page_status: Optional[str] = None,
) -> Dict[str, object]:
    summary = resume_summary(manifest)
    payload: Dict[str, object] = {
        "phase": "vision_processing",
        "completed": summary["completed_page_count"],
        "total": summary["total_pages"],
        "provider_id": provider.provider_id,
        "provider_name": provider.name,
        "resume": summary,
    }
    if page is not None:
        payload["page"] = page
    if page_status:
        payload["page_status"] = page_status
    return payload


def parse_pdf_with_vision_provider(
    root: Path,
    pdf_path: Path,
    source_file_id: str,
    provider_id: Optional[str] = None,
    *,
    config_path: Optional[Path] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> Dict[str, object]:
    root = Path(root)
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise VisionAPIError("待解析 PDF 不存在。")
    fitz = load_pymupdf()
    if fitz is None:
        raise VisionAPIError("其他视觉 API 解析需要 PyMuPDF 才能逐页渲染 PDF。")
    config_path = Path(config_path or resolve_vision_config_path(root))
    provider = load_vision_provider(provider_id, config_path)
    client = OpenAICompatibleVisionClient(provider)
    file_hash = sha256_file(pdf_path)
    parse_options = _vision_parse_options(provider)
    parse_fingerprint = options_fingerprint(parse_options)
    task_id = (
        f"{file_hash[:16]}-{provider.provider_id}-{parse_fingerprint}"
    )
    result_dir = (
        root
        / DEFAULT_VISION_RESULT_DIR
        / source_file_id
        / f"task-{task_id}"
    )
    page_dir = result_dir / "pages"
    page_dir.mkdir(parents=True, exist_ok=True)
    work_manifest_dir = root / DEFAULT_VISION_WORK_MANIFEST_DIR
    work_manifest_path = (
        work_manifest_dir / f"vision-{source_file_id}-{task_id}.json"
    )
    final_manifest_dir = root / DEFAULT_VISION_MANIFEST_DIR
    final_manifest_path = final_manifest_dir / f"vision-{source_file_id}.json"
    document = fitz.open(str(pdf_path))
    try:
        total = len(document)
        if total <= 0:
            raise VisionAPIError("PDF 没有可解析的页面。")
        try:
            manifest = load_json_object(work_manifest_path)
        except Exception:
            quarantine_corrupt_manifest(work_manifest_path)
            manifest = None
        compatible_manifest = False
        if manifest is not None:
            try:
                compatible_manifest = bool(
                    isinstance(manifest.get("pages"), list)
                    and all(
                        isinstance(item, dict)
                        for item in manifest.get("pages", [])
                    )
                    and manifest_matches(
                        manifest,
                        file_hash=file_hash,
                        parser="openai_compatible",
                        parse_options_fingerprint=parse_fingerprint,
                    )
                    and int(manifest.get("total_pages") or 0) == total
                    and str(manifest.get("data_id_prefix") or "")
                    == source_file_id
                    and str(manifest.get("provider_id") or "")
                    == provider.provider_id
                    and str(manifest.get("model") or "") == provider.model
                )
            except (TypeError, ValueError):
                compatible_manifest = False
        if manifest is not None and not compatible_manifest:
            quarantine_corrupt_manifest(work_manifest_path)
            manifest = None
        if manifest is None:
            manifest = _new_vision_work_manifest(
                root=root,
                pdf_path=pdf_path,
                source_file_id=source_file_id,
                provider=provider,
                file_hash=file_hash,
                total_pages=total,
                parse_options=parse_options,
                parse_options_fingerprint=parse_fingerprint,
                task_id=task_id,
                result_dir=result_dir,
            )
        else:
            manifest["resume_count"] = int(manifest.get("resume_count") or 0) + 1
            manifest.pop("halt_reason", None)

        pages = _normalize_vision_work_pages(
            manifest,
            root=root,
            result_dir=result_dir,
            total_pages=total,
        )
        checkpoints: Dict[int, Dict[str, object]] = {}
        for item in pages:
            page_idx = int(item["page_idx"])
            checkpoint_path = page_dir / f"page-{page_idx + 1:06d}.json"
            checkpoint = _load_valid_vision_page_checkpoint(
                checkpoint_path,
                page_idx=page_idx,
                file_hash=file_hash,
                provider=provider,
                parse_options_fingerprint=parse_fingerprint,
            )
            if checkpoint is not None:
                checkpoints[page_idx] = checkpoint
                item["status"] = "completed"
                item.pop("error", None)
                item["checkpoint"] = _relative_to_root(checkpoint_path, root)
            elif str(item.get("status") or "").lower() == "completed":
                item["status"] = "interrupted"
                item["error"] = "页面检查点缺失或损坏，需要重新解析。"

        refresh_manifest_progress(manifest, units=pages)
        atomic_write_json(work_manifest_path, manifest)
        if on_progress:
            on_progress(_vision_progress_payload(manifest, provider=provider))

        fatal_page_error: Optional[str] = None
        for page_index in range(total):
            item = pages[page_index]
            if page_index in checkpoints:
                continue
            item["status"] = "running"
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item.pop("error", None)
            refresh_manifest_progress(manifest, units=pages)
            atomic_write_json(work_manifest_path, manifest)
            if on_progress:
                on_progress(
                    _vision_progress_payload(
                        manifest,
                        provider=provider,
                        page=page_index + 1,
                        page_status="running",
                    )
                )
            try:
                page = document.load_page(page_index)
                rect = page.rect
                longest = max(float(rect.width), float(rect.height), 1.0)
                scale = min(
                    VISION_RENDER_MAX_SCALE,
                    max(
                        VISION_RENDER_MIN_SCALE,
                        VISION_RENDER_LONGEST_EDGE / longest,
                    ),
                )
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(scale, scale),
                    alpha=False,
                )
                text = client.extract_page(pixmap.tobytes("png"))
                checkpoint = {
                    "type": "text",
                    "text": text,
                    "page_idx": page_index,
                    "parser": "openai_compatible",
                    "provider_id": provider.provider_id,
                    "model": provider.model,
                    "file_hash": file_hash,
                    "parse_options_fingerprint": parse_fingerprint,
                    "completed_at": utc_now_iso(),
                }
                checkpoint_path = page_dir / f"page-{page_index + 1:06d}.json"
                atomic_write_json(checkpoint_path, checkpoint)
                checkpoints[page_index] = checkpoint
                item["status"] = "completed"
                item["checkpoint"] = _relative_to_root(checkpoint_path, root)
                item.pop("error", None)
            except Exception as exc:
                item["status"] = "failed"
                item["error"] = (str(exc).strip() or exc.__class__.__name__)[:1000]
                if isinstance(exc, VisionAPIError) and exc.stop_document:
                    # Authentication, quota, model, HTTP and network failures are
                    # generally document-wide.  Stop after the first one rather
                    # than issuing hundreds of predictably failing paid calls.
                    fatal_page_error = item["error"]
                    manifest["halt_reason"] = fatal_page_error
            refresh_manifest_progress(manifest, units=pages)
            atomic_write_json(work_manifest_path, manifest)
            if on_progress:
                on_progress(
                    _vision_progress_payload(
                        manifest,
                        provider=provider,
                        page=page_index + 1,
                        page_status=str(item["status"]),
                    )
                )
            if fatal_page_error is not None:
                break
    finally:
        document.close()

    failed_pages = list(manifest.get("failed_pages") or [])
    if failed_pages or len(checkpoints) != int(manifest["total_pages"]):
        failed_labels = [
            str(item.get("page"))
            for item in failed_pages
            if isinstance(item, dict) and item.get("page")
        ]
        detail = "、".join(failed_labels[:20])
        suffix = f"（第 {detail} 页）" if detail else ""
        reason = str(manifest.get("halt_reason") or "").strip()
        reason_suffix = f"。已停止继续请求：{reason}" if reason else ""
        raise VisionAPIError(
            f"视觉解析仍有 {len(failed_pages)} 个失败页{suffix}；"
            f"已保存其他页面，下次只重试未完成页面{reason_suffix}。"
        )

    content = [checkpoints[index] for index in range(int(manifest["total_pages"]))]
    _atomic_write_json_list(result_dir / "content_list.json", content)
    atomic_write_json(
        result_dir / "layout.json",
        {
            "_version_name": VISION_PARSER_VERSION,
            "provider_id": provider.provider_id,
            "provider_name": provider.name,
            "model": provider.model,
        },
    )
    relative_result = result_dir.resolve().relative_to(root.resolve())
    manifest["status"] = "completed"
    manifest["finished_at"] = utc_now_iso()
    refresh_manifest_progress(manifest, units=pages)
    atomic_write_json(work_manifest_path, manifest)
    final_manifest = {
        "api": "openai_compatible_vision",
        "pdf_path": str(pdf_path),
        "file_name": pdf_path.name,
        "file_hash": file_hash,
        "data_id_prefix": source_file_id,
        "total_pages": len(content),
        "provider_id": provider.provider_id,
        "provider_name": provider.name,
        "model": provider.model,
        "parser": "openai_compatible",
        "parser_version": VISION_PARSER_VERSION,
        "parse_options_fingerprint": parse_fingerprint,
        "work_manifest": _relative_to_root(work_manifest_path, root),
        **resume_summary(manifest, manifest_path=work_manifest_path),
        "segments": [
            {
                "data_id": f"{source_file_id}-{provider.provider_id}",
                "page_ranges": f"1-{len(content)}",
                "status": "completed",
                "result_dir": str(relative_result),
                "result_dirs": [str(relative_result)],
                "parser": "openai_compatible",
                "provider_id": provider.provider_id,
                "provider_name": provider.name,
                "model": provider.model,
            }
        ],
    }
    atomic_write_json(final_manifest_path, final_manifest)
    return {
        "manifest_path": str(final_manifest_path),
        "work_manifest_path": str(work_manifest_path),
        "provider_id": provider.provider_id,
        "provider_name": provider.name,
        "model": provider.model,
        "pages": len(content),
        "status": "completed",
        "resume": resume_summary(manifest, manifest_path=work_manifest_path),
    }

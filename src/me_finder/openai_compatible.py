"""OpenAI-compatible vision transport: config-agnostic protocol layer.

Split out of :mod:`vision_api` so that every caller that only needs to *talk* to
an OpenAI-compatible endpoint — the hosted vision provider list, and the single
self-hosted general model — depends on this module instead of on each other.
It owns the wire format only: URL/endpoint shapes, auth header variants, the
chat/responses request bodies, model-list normalization and the client itself.

It deliberately knows nothing about where credentials are stored, so it never
imports a configuration module and can never take part in an import cycle.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Sequence
from urllib.parse import urlparse


MAX_DISCOVERED_MODELS = 2000

MAX_MODELS_RESPONSE_BYTES = 4 * 1024 * 1024

VISION_TEST_IMAGE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAYklEQVR4nO3PQQ0A"
    "IRDAQEScf5f8TwQkZZOpgs7aw1v1wGkAdQB1AHUAdQB1AHUAdQB1AHVHgO9SAAAA"
    "AAAAAAAAAAAAAAAAAACzAC8EUAdQB1AHUAdQB1AHUAdQB1A3HvAD6EoDmRtp1t4A"
    "AAAASUVORK5CYII="
)

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

def validated_url(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text or len(text) > 2048:
        raise VisionAPIError("请填写有效的 API 地址。")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VisionAPIError("API 地址必须是以 http:// 或 https:// 开头的网址。")
    if parsed.username or parsed.password:
        raise VisionAPIError("API 地址中不能包含用户名或密码。")
    return text

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
        "deepseek-v4",
        "deepseek-vl",
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


def list_models(
    api_base: str,
    api_key: str,
    provider_name: str = "该接口",
    *,
    use_env_proxy: bool = False,
    timeout: int = 45,
) -> Dict[str, object]:
    """Enumerate an endpoint's models over HTTP.

    Credential *resolution* is the caller's business; this only performs the
    request. ``api_key`` may be empty, which is normal for a self-hosted
    endpoint that requires no authentication.
    """

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

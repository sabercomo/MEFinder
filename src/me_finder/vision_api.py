"""OpenAI-compatible vision providers used as optional PDF parsers.

Provider credentials are stored in a separate local-only JSON file.  The
public summary helpers never return API keys.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence

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

from .general_model import (
    GENERAL_MODEL_PROVIDER_ID,
    general_model_config_path_for,
    load_general_model_provider,
)
from .openai_compatible import (
    OpenAICompatibleVisionClient,
    VisionAPIError,
    VisionProviderConfig,
    list_models,
    validated_url,
)


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
ProgressCallback = Callable[[Dict[str, object]], None]


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
            "api_base": validated_url(updates.get("api_base")),
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
    # The general local model keeps its own one-record config beside this file;
    # route the reserved id there so the whole downstream vision path is shared.
    if requested == GENERAL_MODEL_PROVIDER_ID:
        return load_general_model_provider(general_model_config_path_for(path))
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
        api_base=validated_url(raw.get("api_base")),
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
        validated_url(api_base),
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
    """List a *configured* provider's models, filling blanks from stored config."""

    api_base, api_key, provider_name, use_env_proxy = _model_discovery_credentials(
        provider,
        Path(config_path),
    )
    return list_models(
        api_base,
        api_key,
        provider_name,
        use_env_proxy=use_env_proxy,
        timeout=timeout,
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

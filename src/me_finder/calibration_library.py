"""Page-calibration library projection built from persisted source state."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from .auto_page_mapping import has_manual_mapping
from .bibliographic_metadata import METADATA_FIELDS, is_valid_bibliographic_value, metadata_missing_fields
from .collection_metadata import infer_collection_metadata


_CJK_RE = re.compile(r"[㐀-鿿]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_WORD_RE = re.compile(r"[a-zà-öø-ÿā-ž]+")

_SIMPLIFIED_HINTS = frozenset(
    "这为个国书学与发后业专东对会时来们从实应说还进过开关门无长华"
    "简体阅页数术论历经现总类区记语释鉴丛编辑设责处条线归译读写"
)
_TRADITIONAL_HINTS = frozenset(
    "這為個國書學與發後業專東對會時來們從實應說還進過開關門無長華"
    "簡體閱頁數術論歷經現總類區記語釋鑑叢編輯設責處條線歸譯讀寫"
)
_LANGUAGE_STOPWORDS = {
    "en": frozenset("the of and to in a is that for as with by on from this are be or an which not it was at have has".split()),
    "de": frozenset("der die das und ist zu den von mit sich des auf für als im dem nicht ein eine auch werden aus bei durch sind war wird".split()),
    "fr": frozenset("le la les de des du et est à en un une que qui pour dans sur avec par ce cette pas sont au aux se".split()),
    "es": frozenset("el la los las de del y en un una que por para con es no se al como más son".split()),
    "it": frozenset("il lo la i gli le di del della e è un una che per con in non si come sono".split()),
    "pt": frozenset("o a os as de do da e em um uma que por para com é não se ao como são".split()),
    "ru": frozenset("и в не на что с по для как это из к о от же за был быть при".split()),
}


def _item_language(title: object, author: object, file_name: object = None) -> str:
    """中文文献（含中译本）与外文文献两类。

    文件名是最后一道只读兜底：即使自动书目识别暂时给出了错误的拉丁字母
    标题，也不能把一个明确以中文命名的本地文献整批归进外文。
    """

    if _CJK_RE.search(str(title or "")):
        return "chinese"
    if _CJK_RE.search(str(author or "")):
        return "chinese"
    if _CJK_RE.search(Path(str(file_name or "")).stem):
        return "chinese"
    return "foreign"


def _item_language_code(
    sample: object,
    title: object,
    author: object,
    file_name: object = None,
) -> str:
    """Identify the language used by the indexed body text.

    Script distinctions are deterministic.  Closely related Latin languages
    use common function words from a bounded body sample; a tie stays
    unidentified instead of being guessed from a short title.
    """

    fallback = " ".join(
        value
        for value in (
            str(title or "").strip(),
            str(author or "").strip(),
            Path(str(file_name or "")).stem,
        )
        if value
    )
    body = str(sample or "").strip()
    text = f"{body}\n{fallback}" if body else fallback
    if not text:
        return "und"

    kana_count = len(_KANA_RE.findall(text))
    hangul_count = len(_HANGUL_RE.findall(text))
    han_count = len(_CJK_RE.findall(text))
    if kana_count >= 8 and kana_count * 10 >= max(han_count, 1):
        return "ja"
    if hangul_count >= 8 and hangul_count * 5 >= max(han_count, 1):
        return "ko"
    if han_count:
        simplified = sum(text.count(char) for char in _SIMPLIFIED_HINTS)
        traditional = sum(text.count(char) for char in _TRADITIONAL_HINTS)
        if traditional > simplified:
            return "zh-Hant"
        return "zh-Hans"

    lowered = text.casefold()
    if _CYRILLIC_RE.search(lowered):
        words = re.findall(r"[\u0400-\u04ff]+", lowered)
        russian_score = sum(word in _LANGUAGE_STOPWORDS["ru"] for word in words)
        return "ru" if russian_score >= 2 else "und"

    words = _WORD_RE.findall(lowered)
    if not words:
        return "und"
    scores = {
        code: sum(word in stopwords for word in words)
        for code, stopwords in _LANGUAGE_STOPWORDS.items()
        if code != "ru"
    }
    scores["de"] += 3 * sum(char in lowered for char in "äöüß")
    scores["fr"] += 3 * sum(char in lowered for char in "àâçéèêëîïôûùÿœæ")
    scores["es"] += 3 * sum(char in lowered for char in "ñ¿¡")
    scores["pt"] += 3 * sum(char in lowered for char in "ãõ")
    best_score = max(scores.values())
    winners = [code for code, score in scores.items() if score == best_score]
    return winners[0] if best_score >= 2 and len(winners) == 1 else "und"


# 文献列表只需要书名、作者、分类和状态。映射区间、识别证据和 PDF 剖面
# 在真实语料上占 `/api/library` 负载的四分之三以上，却只有详情抽屉会读，
# 因此摘要投影把它们整组去掉，改由 `build_library_detail` 按需返回。
SUMMARY_DROPPED_ITEM_FIELDS = frozenset(
    {
        "pdf_profile",
        "segments",
        "mapping_evidence",
        "metadata_evidence",
        "metadata_conflicts",
        "exception_pages",
        "failure_reasons",
    }
)
SUMMARY_DROPPED_METADATA_FIELDS = frozenset({"metadata_evidence", "metadata_conflicts"})


STATUS_LABELS = {
    "manual_mapped": "页码已校准 · 人工映射",
    "auto_mapped_high": "页码已校准 · 自动映射",
    "needs_review": "页码待确认",
    "unmapped": "页码尚未检测",
    "auto_mapping_failed": "页码自动检测失败",
    "mapping": "正在检测",
    "source_missing": "原文件缺失",
}


def build_library(
    root: Path,
    source_files: Sequence[Mapping[str, object]],
    volumes: Sequence[Mapping[str, object]],
    works: Sequence[Mapping[str, object]],
    documents: Sequence[Mapping[str, object]],
    latest_runs: Optional[Mapping[str, Mapping[str, object]]] = None,
    active_source_ids: Optional[Iterable[str]] = None,
    language_samples: Optional[Mapping[str, str]] = None,
) -> Dict[str, object]:
    """Return the unified library payload for every source.

    Each item is the raw source record merged with the PDF calibration
    projection where applicable; Word sources get resolved display fields
    (title/author/works_count) instead of calibration status. ``stats``
    stays PDF-only. ``volumes``/``works`` ride along as lookup tables for
    the detail drawer.
    """

    root = Path(root).resolve()
    calibration = build_calibration_library(
        root, source_files, volumes, documents,
        latest_runs=latest_runs, active_source_ids=active_source_ids,
    )
    projection_by_id = {
        str(item.get("source_file_id")): item for item in calibration["items"]
    }
    source_by_id = {
        str(item.get("source_file_id")): item
        for item in source_files
        if item.get("source_file_id")
    }
    normalized_volumes: List[Dict[str, object]] = []
    for raw_volume in volumes:
        volume = dict(raw_volume)
        source = source_by_id.get(str(volume.get("source_file_id") or ""), {})
        collection = infer_collection_metadata(
            volume.get("corpus_title"),
            volume.get("display_title"),
            volume.get("document_title"),
            source.get("display_title"),
            source.get("title"),
            source.get("file_name"),
        )
        if collection:
            volume["primary_structure"] = collection["primary_structure"]
            if collection.get("author"):
                volume["collection_author"] = collection["author"]
        normalized_volumes.append(volume)
    volume_by_source = {
        str(item.get("source_file_id")): item
        for item in normalized_volumes
        if item.get("source_file_id")
    }
    works_by_volume: Dict[str, List[Mapping[str, object]]] = {}
    for work in works:
        works_by_volume.setdefault(str(work.get("volume_id")), []).append(work)

    language_samples = language_samples or {}
    items: List[Dict[str, object]] = []
    for source in source_files:
        source_id = str(source.get("source_file_id") or "")
        if not source_id:
            continue
        item = dict(source)
        volume = volume_by_source.get(source_id, {})
        metadata = source.get("bibliographic_metadata") if isinstance(source.get("bibliographic_metadata"), Mapping) else {}
        explicit_author = _first_valid(metadata.get("author"), source.get("author"))
        editor = _first_valid(
            metadata.get("editor"), metadata.get("editors"), metadata.get("chief_editor"),
            source.get("editor"), source.get("editors"), source.get("chief_editor"),
        )
        explicit_responsibility = explicit_author or _editor_responsibility(editor)
        projection = projection_by_id.get(source_id)
        if projection is not None:
            item.update(projection)
            explicit_responsibility = _first_valid(item.get("author"), explicit_responsibility)
        else:
            volume_works = works_by_volume.get(str(volume.get("volume_id")), []) if volume else []
            first_work = volume_works[0] if volume_works else {}
            item["title"] = _first_valid(
                volume.get("display_title"),
                source.get("display_title"),
                source.get("title"),
                Path(str(source.get("file_name") or "")).stem,
            ) or source_id
            item["author"] = _first_valid(
                metadata.get("author"), source.get("author"), first_work.get("author_label")
            )
            item["works_count"] = len(volume_works)
            item["imported_at"] = source.get("imported_at") or source.get("last_modified")
            item["modified_at"] = source.get("last_modified") or source.get("imported_at")
            source_path = _source_path(root, source)
            item["source_exists"] = bool(source_path and source_path.exists())
        collection = infer_collection_metadata(
            volume.get("corpus_title"),
            volume.get("display_title"),
            item.get("title"),
            item.get("display_title"),
            item.get("file_name"),
        )
        if collection:
            inferred_author = collection.get("author")
            if inferred_author == "马克思、恩格斯":
                item["author"] = inferred_author
            elif explicit_responsibility:
                item["author"] = explicit_responsibility
            else:
                item["author"] = inferred_author
        auto_language_code = _item_language_code(
            language_samples.get(source_id),
            item.get("title"),
            item.get("author"),
            item.get("file_name"),
        )
        # 人工指定的语言优先于自动识别（自动会在英译本+外文前页等场景判成 und）。
        bibliographic_metadata = source.get("bibliographic_metadata")
        manual_language_code = (
            str(bibliographic_metadata.get("language_code_manual") or "").strip()
            if isinstance(bibliographic_metadata, Mapping)
            else ""
        )
        language_code = manual_language_code or auto_language_code
        item["language_code"] = language_code
        item["language_code_auto"] = auto_language_code
        item["language_code_manual"] = manual_language_code
        item["language"] = (
            "chinese"
            if language_code.startswith("zh-")
            else "foreign"
            if language_code != "und"
            else _item_language(
                item.get("title"), item.get("author"), item.get("file_name")
            )
        )
        bibliographic = item.get("bibliographic_metadata") if isinstance(item.get("bibliographic_metadata"), Mapping) else {}
        item["document_type"] = (
            item.get("document_type")
            or bibliographic.get("document_type")
            or ("book" if item.get("source_type") == "pdf" else None)
        )
        items.append(item)
    return {
        "items": items,
        "stats": calibration["stats"],
        "volumes": normalized_volumes,
        "works": list(works),
    }


def summarize_library(payload: Mapping[str, object]) -> Dict[str, object]:
    """Return the list-only projection of :func:`build_library`.

    Keeps every field the library list, filters, sorting and search scope
    read; drops the per-document evidence and the works table, which only
    the detail drawer needs.
    """

    items: List[Dict[str, object]] = []
    for item in payload.get("items", []) or []:
        if not isinstance(item, Mapping):
            continue
        light = {
            key: value
            for key, value in item.items()
            if key not in SUMMARY_DROPPED_ITEM_FIELDS
        }
        metadata = item.get("bibliographic_metadata")
        if isinstance(metadata, Mapping):
            light["bibliographic_metadata"] = {
                key: value
                for key, value in metadata.items()
                if key not in SUMMARY_DROPPED_METADATA_FIELDS
            }
        items.append(light)
    return {
        "view": "summary",
        "items": items,
        "stats": payload.get("stats") or {},
        "volumes": list(payload.get("volumes") or []),
    }


def build_library_detail(
    payload: Mapping[str, object], source_file_id: str
) -> Optional[Dict[str, object]]:
    """Return the full record, volume and works for one source, or ``None``."""

    source_id = str(source_file_id or "").strip()
    if not source_id:
        return None
    item = next(
        (
            entry
            for entry in payload.get("items", []) or []
            if isinstance(entry, Mapping)
            and str(entry.get("source_file_id") or "") == source_id
        ),
        None,
    )
    if item is None:
        return None
    volume = next(
        (
            entry
            for entry in payload.get("volumes", []) or []
            if isinstance(entry, Mapping)
            and str(entry.get("source_file_id") or "") == source_id
        ),
        None,
    )
    volume_id = str(volume.get("volume_id") or "") if volume else ""
    works = [
        entry
        for entry in (payload.get("works", []) or [])
        if isinstance(entry, Mapping)
        and volume_id
        and str(entry.get("volume_id") or "") == volume_id
    ]
    return {"item": dict(item), "volume": dict(volume) if volume else None, "works": works}


def build_calibration_library(
    root: Path,
    source_files: Sequence[Mapping[str, object]],
    volumes: Sequence[Mapping[str, object]],
    documents: Sequence[Mapping[str, object]],
    latest_runs: Optional[Mapping[str, Mapping[str, object]]] = None,
    active_source_ids: Optional[Iterable[str]] = None,
) -> Dict[str, object]:
    """Return card-ready PDF records and status totals."""

    root = Path(root).resolve()
    volume_by_source = {
        str(item.get("source_file_id")): item
        for item in volumes
        if item.get("source_file_id")
    }
    document_by_source = {
        str(item.get("source_file_id")): item
        for item in documents
        if item.get("source_file_id")
    }
    latest_runs = latest_runs or {}
    active = {str(value) for value in (active_source_ids or [])}
    items: List[Dict[str, object]] = []
    for source in source_files:
        if str(source.get("source_type") or "") != "pdf":
            continue
        source_id = str(source.get("source_file_id") or "")
        if not source_id:
            continue
        document = document_by_source.get(source_id, {})
        volume = volume_by_source.get(source_id, {})
        profile = source.get("pdf_profile") if isinstance(source.get("pdf_profile"), Mapping) else {}
        auto_mapping = profile.get("auto_page_mapping") if isinstance(profile.get("auto_page_mapping"), Mapping) else {}
        page_mapping = document.get("page_mapping") if isinstance(document.get("page_mapping"), Mapping) else {}
        source_path = _source_path(root, source)
        source_exists = bool(source_path and source_path.exists())
        status = _mapping_status(source_id, source_exists, document, profile, active)
        segments = _display_segments(page_mapping, auto_mapping, status)
        confidence = max(
            [_float(item.get("mapping_confidence") or item.get("confidence")) for item in segments] or [0.0]
        )
        run = latest_runs.get(source_id, {})
        metadata = source.get("bibliographic_metadata") if isinstance(source.get("bibliographic_metadata"), Mapping) else {}
        bibliographic = dict(metadata)
        for field in METADATA_FIELDS:
            for candidate in (document.get(field), source.get(field)):
                if is_valid_bibliographic_value(candidate):
                    bibliographic[field] = candidate
                    break
        if document.get("document_type") or metadata.get("document_type"):
            bibliographic["document_type"] = document.get("document_type") or metadata.get("document_type")
        missing_metadata = metadata_missing_fields(bibliographic)
        title = _first_valid(
            document.get("title"),
            metadata.get("title"),
            source.get("title"),
            source.get("display_title"),
            volume.get("display_title"),
            Path(str(source.get("file_name") or "")).stem,
        ) or "未命名 PDF"
        author = _first_valid(document.get("author"), metadata.get("author"), source.get("author"))
        translator = _first_valid(document.get("translator"), metadata.get("translator"), source.get("translator"))
        publisher = _first_valid(document.get("publisher"), metadata.get("publisher"), source.get("publisher"))
        parser_type = str(profile.get("detected_pdf_type") or "")
        parser_label = str(
            profile.get("parser_label")
            or profile.get("provider_name")
            or ("MinerU" if parser_type == "mineru_structured" else "PDF")
        )
        internal_copy = bool(
            source_path
            and _is_within(source_path, root / "corpus" / "raw_pdf")
        )
        items.append(
            {
                "source_file_id": source_id,
                "document_id": source.get("document_id") or document.get("document_id"),
                "title": title,
                "author": author,
                "translator": translator,
                "publisher": publisher,
                "metadata_status": metadata.get("metadata_status") or document.get("metadata_status"),
                "metadata_missing_fields": missing_metadata,
                "file_name": source.get("file_name"),
                "size_bytes": source.get("size_bytes"),
                "page_count": profile.get("pdf_page_count"),
                "parser_type": parser_type,
                "parser_label": parser_label,
                "status": status,
                "status_label": STATUS_LABELS[status],
                "status_group": _status_group(status),
                "mapping_method": _mapping_method(page_mapping, auto_mapping, status),
                "mapping_summary": _mapping_summary(segments),
                "mapping_segment_count": len(segments),
                "mapping_confidence": confidence,
                "confidence_level": _confidence_level(confidence),
                "segments": segments,
                "exception_pages": list(auto_mapping.get("exception_pages") or []),
                "mapping_evidence": [item.get("mapping_evidence") for item in segments if item.get("mapping_evidence")],
                "failure_reasons": list(profile.get("mapping_failure_reasons") or auto_mapping.get("failure_reasons") or []),
                "imported_at": run.get("started_at") or source.get("imported_at") or source.get("last_modified"),
                "modified_at": page_mapping.get("updated_at") or run.get("finished_at") or source.get("last_modified"),
                "source_exists": source_exists,
                "internal_copy": internal_copy,
                "can_delete_internal_copy": internal_copy and source_exists,
            }
        )
    stats = _stats(items)
    return {"items": items, "stats": stats}


def _mapping_status(
    source_id: str,
    source_exists: bool,
    document: Mapping[str, object],
    profile: Mapping[str, object],
    active: set[str],
) -> str:
    if source_id in active:
        return "mapping"
    if not source_exists:
        return "source_missing"
    if has_manual_mapping(dict(document)):
        return "manual_mapped"
    value = str(
        profile.get("mapping_status")
        or ((profile.get("auto_page_mapping") or {}).get("mapping_status") if isinstance(profile.get("auto_page_mapping"), Mapping) else "")
        or ""
    )
    if value in {"manual_mapped", "manual_override"}:
        return "manual_mapped"
    if value == "auto_mapped_high":
        return "auto_mapped_high"
    if value in {"auto_mapped_medium", "needs_review", "auto_mapping_suggested"}:
        return "needs_review"
    if value in {"auto_mapping_failed", "failed"}:
        return "auto_mapping_failed"
    if value in {"mapping", "detecting", "processing"}:
        return "mapping"
    if value == "source_missing":
        return "source_missing"
    return "unmapped"


def _display_segments(
    page_mapping: Mapping[str, object],
    auto_mapping: Mapping[str, object],
    status: str,
) -> List[Dict[str, object]]:
    if status == "manual_mapped":
        source = page_mapping.get("segments") or []
    else:
        source = auto_mapping.get("applied_segments") or auto_mapping.get("selected_segments") or []
    return [dict(item) for item in source if isinstance(item, Mapping)]


def _mapping_method(
    page_mapping: Mapping[str, object], auto_mapping: Mapping[str, object], status: str
) -> str:
    if status == "manual_mapped":
        return "manual_segment"
    return str(auto_mapping.get("method") or page_mapping.get("method") or "uncalibrated")


def _mapping_summary(segments: Sequence[Mapping[str, object]]) -> Optional[str]:
    if not segments:
        return None
    first = segments[0]
    start = _int(first.get("pdf_page_start"))
    citation = first.get("citation_page_start")
    if start is None:
        return f"{len(segments)} 个映射区间"
    if citation in (None, ""):
        return f"PDF 第 {start + 1} 页起不映射引用页码"
    return f"PDF 第 {start + 1} 页 → 引用第 {citation} 页"


def _stats(items: Sequence[Mapping[str, object]]) -> Dict[str, int]:
    stats = {"total": len(items), "calibrated": 0, "pending": 0, "review": 0, "failed": 0, "mapping": 0}
    for item in items:
        group = str(item.get("status_group") or "")
        if group in stats:
            stats[group] += 1
    return stats


def _status_group(status: str) -> str:
    if status in {"manual_mapped", "auto_mapped_high"}:
        return "calibrated"
    if status == "needs_review":
        return "review"
    if status in {"auto_mapping_failed", "source_missing"}:
        return "failed"
    if status == "mapping":
        return "mapping"
    return "pending"


def _first_valid(*values: object) -> Optional[str]:
    for value in values:
        if is_valid_bibliographic_value(value):
            return str(value).strip()
    return None


def _editor_responsibility(editor: object) -> Optional[str]:
    value = _first_valid(editor)
    if not value:
        return None
    if re.search(r"(?:，|,\s*)?(?:主编|编著|编)$", value):
        return value
    if "编委会" in value:
        return f"{value}（编）"
    return f"{value}（主编）"


def _source_path(root: Path, source: Mapping[str, object]) -> Optional[Path]:
    relative = str(source.get("relative_path") or "").strip()
    if not relative:
        return None
    path = (root / relative).resolve()
    return path if path == root or root in path.parents else None


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: object) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _confidence_level(value: float) -> str:
    if value >= 0.9:
        return "high"
    if value >= 0.7:
        return "medium"
    return "low" if value > 0 else "unknown"

"""Consistent, conservative page labels and citation-page capabilities.

Presentation wording and the machine-readable verification decision live
together here so search results and the structured reader cannot silently
disagree about whether a page number is safe to cite.  Final citation
formatting remains the responsibility of :mod:`me_finder.citations`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class PageDisplayResult:
    """The user-facing page label and its provenance note."""

    display: str
    note: str
    page_source_type: str


@dataclass(frozen=True)
class CitationPageResolution:
    """Machine-readable citation-page capability for one record."""

    verified: bool
    start: Optional[str]
    end: Optional[str]
    page_source_type: str


_VERIFIED_PDF_SOURCE_TYPES = {
    "calibrated",
    "manual_page",
    "fixed_offset",
    "manual_segment",
    "printed_page_ocr",
    # Existing automatic mappings also write citation_page fields.  Keeping
    # them here makes this helper usable with already indexed v0.2.2 data.
    "numeric_bookmark_sequence",
    "native_pdf_edge_sequence",
    "ocr_sequence",
    "ocr_sequence_with_structure",
    "combined_sequence",
}

_VERIFIED_WORD_SOURCE_TYPES = {
    "epub_page_list",
    "epub_pagebreak",
    "section_break_verified",
    "word_rendered_page",
    "printed_page_marker",
}

_SOURCE_NOTES = {
    "calibrated": "PDF 引用页码已校准",
    "manual_page": "PDF 页码来自人工逐页校准",
    "fixed_offset": "PDF 页码来自固定偏移映射",
    "manual_segment": "PDF 页码来自人工分段映射",
    "printed_page_ocr": "PDF 页码来自视觉印刷页码识别，已验证",
    "numeric_bookmark_sequence": "PDF 页码来自数字书签序列",
    "native_pdf_edge_sequence": "PDF 页码来自页边数字序列",
    "ocr_sequence": "PDF 页码来自 OCR 页码序列",
    "ocr_sequence_with_structure": "PDF 页码来自 OCR 页码序列与结构证据",
    "combined_sequence": "PDF 页码来自多来源序列",
    "pdf_page_label": "PDF Page Label，已抽样验证",
    "uncalibrated": "PDF 引用页码尚未校准",
    "mixed": "跨页命中涉及不同页码来源，须分别核验",
    "section_break_inferred": "分节推断页码，尚未人工验证",
    "section_break_verified": "分节页码，已验证",
    "word_rendered_page": "排版引擎页码",
    "printed_page_marker": "印刷页码锚点",
    "epub_page_list": "EPUB 出版方页码表",
    "epub_pagebreak": "EPUB 出版方分页标记",
    "toc_range_bound": "目录页码范围，非段落级精确页码",
    "unknown": "页码尚未解析",
}


def build_page_display(fields: Mapping[str, object]) -> PageDisplayResult:
    """Build a page label from an existing paragraph or PDF-page record.

    ``page_source_type`` is authoritative when present; the two historical
    aliases are accepted so page-level mapping records can use the same API.
    A PDF physical index is only ever shown with an explicit uncalibrated
    warning.  It is never promoted to a citation page.
    """

    source_type = _page_source_type(fields)
    document_type = str(fields.get("source_type") or "").strip().lower()

    if source_type == "section_break_inferred":
        page = _word_page_range(fields)
        display = f"第 {page} 页（分节推断，未验证）" if page else "页码尚未解析"
        return PageDisplayResult(display, _SOURCE_NOTES[source_type], source_type)

    if source_type == "toc_range_bound":
        page = _toc_page_range(fields)
        display = (
            f"目录范围 {page}（非段落精确页码）"
            if page
            else "目录范围未解析（非段落精确页码）"
        )
        return PageDisplayResult(display, _SOURCE_NOTES[source_type], source_type)

    if source_type in _VERIFIED_WORD_SOURCE_TYPES:
        page = _word_page_range(fields)
        display = f"第 {page} 页" if page else "页码尚未解析"
        return PageDisplayResult(display, _SOURCE_NOTES[source_type], source_type)

    if source_type == "unknown" and document_type != "pdf" and not _has_pdf_location(fields):
        return PageDisplayResult("页码尚未解析", _SOURCE_NOTES[source_type], source_type)

    citation_page = _citation_page_range(fields)
    explicitly_unverified = _is_explicitly_unverified(fields)

    if source_type == "mixed":
        if citation_page:
            display = f"引用页码候选：{citation_page}（来源混合，需核验）"
        else:
            display = _unverified_pdf_display(fields, mixed=True)
        return PageDisplayResult(display, _SOURCE_NOTES[source_type], source_type)

    if (
        citation_page
        and not explicitly_unverified
        and (source_type in _VERIFIED_PDF_SOURCE_TYPES or source_type == "pdf_page_label")
    ):
        return PageDisplayResult(
            f"引用页码：{citation_page}",
            _SOURCE_NOTES[source_type],
            source_type,
        )

    if source_type == "pdf_page_label" or _pdf_label_range(fields):
        label = _pdf_label_range(fields)
        if label:
            return PageDisplayResult(
                f"PDF 标签页：{label}，引用页码尚未校准",
                "PDF Page Label 尚未验证，不能作为引用页码",
                source_type,
            )

    if explicitly_unverified:
        note = "页码映射尚未验证，不能作为引用页码"
    elif source_type == "pdf_page_label":
        note = "PDF Page Label 尚未验证，不能作为引用页码"
    elif source_type in _VERIFIED_PDF_SOURCE_TYPES and not citation_page:
        note = "页码映射缺少可用的引用页码，尚不能生成带页码引文"
    elif source_type not in _SOURCE_NOTES:
        note = "页码来源未识别，引用页码尚未验证"
    else:
        note = _SOURCE_NOTES.get(source_type, _SOURCE_NOTES["uncalibrated"])
    return PageDisplayResult(_unverified_pdf_display(fields), note, source_type)


def page_source_note(page_source_type: object) -> str:
    """Return the standard note for a source type.

    This compatibility-sized function can replace the duplicate note mapping
    currently embedded in ``search.py``.
    """

    source_type = str(page_source_type or "unknown").strip() or "unknown"
    return _SOURCE_NOTES.get(source_type, "页码来源未说明")


def page_is_verified(fields: Mapping[str, object]) -> bool:
    """Return whether the record has a citation-safe page number.

    This shared capability check keeps search results and reader citations
    from treating physical PDF indexes, inferred DOCX sections, or legacy DOC
    table-of-contents ranges as verified citation pages.
    """

    return resolve_citation_page(fields).verified


def resolve_citation_page(fields: Mapping[str, object]) -> CitationPageResolution:
    """Resolve only citation-safe page labels, never physical PDF positions."""

    source_type = _page_source_type(fields)
    document_type = str(fields.get("source_type") or "").strip().lower()
    if _is_explicitly_unverified(fields):
        return CitationPageResolution(False, None, None, source_type)

    if source_type in _VERIFIED_WORD_SOURCE_TYPES:
        start = _first_value(fields, ("original_page_start",))
        end = _first_value(fields, ("original_page_end",))
        if start is None:
            legacy = _clean_value(fields.get("page_display"))
            start = _strip_page_wrapping(legacy) if legacy else None
        return CitationPageResolution(
            start is not None,
            start,
            end or start,
            source_type,
        )

    if document_type == "pdf" or _has_pdf_location(fields):
        start = _first_value(
            fields,
            ("citation_page_start", "citation_page", "original_page_start"),
        )
        end = _first_value(
            fields,
            ("citation_page_end", "original_page_end"),
        )
        verified = bool(
            start
            and (
                source_type in _VERIFIED_PDF_SOURCE_TYPES
                or source_type == "pdf_page_label"
            )
        )
        return CitationPageResolution(
            verified,
            start if verified else None,
            (end or start) if verified else None,
            source_type,
        )

    return CitationPageResolution(False, None, None, source_type)


def _page_source_type(fields: Mapping[str, object]) -> str:
    for key in ("page_source_type", "page_mapping_method", "mapping_method"):
        value = fields.get(key)
        if value not in (None, ""):
            return str(value).strip() or "unknown"
    return "unknown"


def _citation_page_range(fields: Mapping[str, object]) -> Optional[str]:
    return _range_from_keys(
        fields,
        ("citation_page_start", "citation_page", "original_page_start"),
        ("citation_page_end", "original_page_end"),
    )


def _pdf_label_range(fields: Mapping[str, object]) -> Optional[str]:
    return _range_from_keys(
        fields,
        ("pdf_page_start_label", "pdf_page_label"),
        ("pdf_page_end_label",),
    )


def _word_page_range(fields: Mapping[str, object]) -> Optional[str]:
    page = _range_from_keys(
        fields,
        ("original_page_start",),
        ("original_page_end",),
    )
    if page:
        return _strip_page_wrapping(page)
    legacy = _clean_value(fields.get("page_display"))
    return _strip_page_wrapping(legacy) if legacy else None


def _toc_page_range(fields: Mapping[str, object]) -> Optional[str]:
    page = _range_from_keys(
        fields,
        ("toc_page_start", "original_page_start"),
        ("toc_page_end", "original_page_end"),
    )
    if page:
        return _strip_page_wrapping(page)
    legacy = _clean_value(fields.get("page_display"))
    return _strip_page_wrapping(legacy) if legacy else None


def _physical_page_range(fields: Mapping[str, object]) -> Optional[str]:
    start_number = _first_int(
        fields,
        ("pdf_page_start_number_1based", "pdf_page_number_1based"),
    )
    end_number = _first_int(fields, ("pdf_page_end_number_1based",))
    if start_number is None:
        start_index = _first_int(fields, ("pdf_page_start_index", "pdf_page_index"))
        if start_index is None:
            return None
        start_number = start_index + 1
    if end_number is None:
        end_index = _first_int(fields, ("pdf_page_end_index",))
        end_number = end_index + 1 if end_index is not None else start_number
    return _format_range(str(start_number), str(end_number))


def _unverified_pdf_display(fields: Mapping[str, object], mixed: bool = False) -> str:
    physical_page = _physical_page_range(fields)
    if physical_page:
        suffix = "页码来源混合且尚未验证" if mixed else "引用页码尚未校准"
        return f"PDF 第 {physical_page} 页，{suffix}"
    if mixed:
        return "PDF 页码来源混合且尚未验证"
    return "PDF 引用页码尚未校准"


def _range_from_keys(
    fields: Mapping[str, object],
    start_keys: Sequence[str],
    end_keys: Sequence[str],
) -> Optional[str]:
    start = _first_value(fields, start_keys)
    if start is None:
        return None
    end = _first_value(fields, end_keys)
    return _format_range(start, end)


def _first_value(fields: Mapping[str, object], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        value = _clean_value(fields.get(key))
        if value is not None:
            return value
    return None


def _first_int(fields: Mapping[str, object], keys: Sequence[str]) -> Optional[int]:
    for key in keys:
        value = fields.get(key)
        if value in (None, "") or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _clean_value(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _format_range(start: str, end: Optional[str]) -> str:
    if end and end != start:
        return f"{start}–{end}"
    return start


def _strip_page_wrapping(value: str) -> str:
    text = value.strip()
    match = re.fullmatch(r"第\s*(.+?)\s*页", text)
    if match:
        text = match.group(1).strip()
    # Legacy DOC records use ASCII hyphens for simple page ranges.
    range_match = re.fullmatch(
        r"([0-9ivxlcdmIVXLCDM]+)\s*[-–—]\s*([0-9ivxlcdmIVXLCDM]+)",
        text,
    )
    if range_match:
        return f"{range_match.group(1)}–{range_match.group(2)}"
    return text


def _is_explicitly_unverified(fields: Mapping[str, object]) -> bool:
    for key in ("citation_page_verified", "page_mapping_verified", "page_verified"):
        if key in fields and fields.get(key) is False:
            return True
    return False


def _has_pdf_location(fields: Mapping[str, object]) -> bool:
    keys: Tuple[str, ...] = (
        "pdf_page_id",
        "pdf_page_index",
        "pdf_page_start_index",
        "pdf_page_number_1based",
        "pdf_page_start_number_1based",
        "pdf_page_label",
        "pdf_page_start_label",
    )
    return any(fields.get(key) not in (None, "") for key in keys)

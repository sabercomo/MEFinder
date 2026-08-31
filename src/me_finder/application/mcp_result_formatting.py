"""Shape raw search / reader / bibliographic rows into MCP output objects.

Pure formatting: every function maps a plain ``fields`` mapping to the JSON
shape the MCP contract advertises. No persistence, no transport — the
verification service imports these to build each tool response, and
``parallel_passage_service`` receives ``_search_match`` as its formatter.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence


def _first_text(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            text = "、".join(str(item).strip() for item in value if str(item).strip())
        else:
            text = str(value).strip()
        if text:
            return text
    return None


def _first_present(fields: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        value = fields.get(key)
        if value is not None:
            return value
    return None


def _physical_page(
    fields: Mapping[str, object],
    source_type: str,
) -> dict[str, object]:
    if source_type != "pdf":
        return {
            "start_index": None,
            "end_index": None,
            "start_label": None,
            "end_label": None,
        }
    start_index = _first_present(fields, "pdf_page_start_index", "pdf_page_index")
    end_index = _first_present(fields, "pdf_page_end_index", "pdf_page_index")
    start_label = _first_text(
        _first_present(fields, "pdf_page_start_label", "pdf_page_label")
    )
    end_label = _first_text(
        _first_present(fields, "pdf_page_end_label", "pdf_page_label")
    )
    return {
        "start_index": int(start_index) if start_index is not None else None,
        "end_index": int(end_index) if end_index is not None else None,
        "start_label": start_label,
        "end_label": end_label or start_label,
    }


def _citation_page(
    fields: Mapping[str, object],
    source_type: str,
    physical_page: Mapping[str, object],
) -> dict[str, object]:
    verified_value = _first_present(
        fields,
        "citation_page_verified",
        "page_verified",
    )
    verified = bool(verified_value)
    start = _first_text(fields.get("citation_page_start")) if verified else None
    end = _first_text(fields.get("citation_page_end")) if verified else None
    if verified:
        status = "calibrated" if source_type == "pdf" else "verified"
    elif source_type == "pdf" and physical_page.get("start_index") is not None:
        status = "uncalibrated"
    else:
        status = "unavailable"
    return {
        "start": start,
        "end": end or start,
        "status": status,
    }


def _page_mapping(fields: Mapping[str, object]) -> dict[str, object]:
    confidence = _first_present(
        fields,
        "page_mapping_confidence",
        "mapping_confidence",
        "page_confidence",
    )
    return {
        "method": _first_text(
            _first_present(
                fields,
                "page_mapping_method",
                "mapping_method",
                "page_source_type",
            )
        ),
        "confidence": float(confidence) if confidence is not None else None,
        "confidence_level": _first_text(fields.get("mapping_confidence_level")),
        "note": _first_text(fields.get("page_note")),
    }


def _search_match(fields: Mapping[str, object]) -> dict[str, object]:
    source_type = str(fields["source_type"])
    internal_match_type = str(fields["match_type"])
    physical_page = _physical_page(fields, source_type)
    reader_start = _first_present(
        fields,
        "pdf_page_start_index" if source_type == "pdf" else "paragraph_index",
    )
    return {
        "paragraph_id": str(fields["paragraph_id"]),
        "source_file_id": str(fields["source_file_id"]),
        "source_type": source_type,
        "document_title": _first_text(fields.get("document_title")),
        "work_title": _first_text(fields.get("work_title")),
        "author": _first_text(fields.get("author_label")),
        "matched_text": str(fields["matched_text"]),
        "paragraph_text": str(fields["paragraph_text"]),
        "context_before": fields["context_before"],
        "context_after": fields["context_after"],
        "match_type": (
            "fuzzy" if internal_match_type == "ngram_fuzzy" else internal_match_type
        ),
        "match_score": float(fields["match_score"]),
        "physical_page": physical_page,
        "citation_page": _citation_page(fields, source_type, physical_page),
        "page_mapping": _page_mapping(fields),
        "reader": {
            "unit": "pdf_page" if source_type == "pdf" else "word_paragraph",
            "start": int(reader_start),
        },
    }


def _passage_match(fields: Mapping[str, object]) -> dict[str, object]:
    source_type = str(fields["source_type"])
    physical_page = _physical_page(fields, source_type)
    reader_start = _first_present(
        fields,
        "pdf_page_start_index" if source_type == "pdf" else "paragraph_index",
    )
    relevance = fields["relevance"]
    if not isinstance(relevance, Mapping):
        raise ValueError("检索引擎返回了无效的 relevance")
    citation_formats = fields["citation_formats"]
    if not isinstance(citation_formats, Mapping):
        raise ValueError("检索引擎返回了无效的 citation_formats")
    return {
        "paragraph_id": str(fields["paragraph_id"]),
        "source_file_id": str(fields["source_file_id"]),
        "source_type": source_type,
        "document_title": _first_text(fields.get("document_title")),
        "work_title": _first_text(fields.get("work_title")),
        "author": _first_text(fields.get("author_label")),
        "paragraph_text": str(fields["paragraph_text"]),
        "preview": str(fields["matched_text"]),
        "context_before": fields["context_before"],
        "context_after": fields["context_after"],
        "relevance": {
            "rank": int(relevance["rank"]),
            "method": str(relevance["method"]),
            "score": float(relevance["score"]),
        },
        "physical_page": physical_page,
        "citation_page": _citation_page(fields, source_type, physical_page),
        "page_mapping": _page_mapping(fields),
        "citation": {
            "chinese": str(citation_formats["chinese"]),
            "gb": str(citation_formats["gb"]),
            "chinese_status": str(citation_formats["chinese_status"]),
            "gb_status": str(citation_formats["gb_status"]),
        },
        "reader": {
            "unit": "pdf_page" if source_type == "pdf" else "word_paragraph",
            "start": int(reader_start),
        },
    }


_BIBLIOGRAPHIC_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("isbn", ("isbn",)),
    ("cip", ("图书在版编目", "(cip)", "（cip）", "cip数据", "cip 数据")),
    ("publisher", ("出版社", "出版发行", "出版發行", "press", "verlag", "éditions")),
    ("price", ("定价", "定 价", "售价")),
    ("responsibility", ("责任编辑", "責任編輯", "责任印制", "装帧设计", "封面设计")),
)
_YEAR_RE = re.compile(r"(?:1[89]\d{2}|20\d{2})\s*年")


def _bibliographic_hints(text: str) -> list[str]:
    """Flag copyright-page cues found verbatim in already-parsed page text."""

    lowered = text.casefold()
    hints = [
        name
        for name, tokens in _BIBLIOGRAPHIC_CUES
        if any(token in lowered for token in tokens)
    ]
    if _YEAR_RE.search(text):
        hints.append("year")
    return hints


def _is_likely_copyright_page(hints: Sequence[str]) -> bool:
    if {"isbn", "cip"} & set(hints):
        return True
    return len(hints) >= 3


def _bibliographic_page(
    fields: Mapping[str, object],
    source_type: str,
) -> dict[str, object]:
    physical_page = _physical_page(fields, source_type)
    position = _first_present(
        fields,
        "pdf_page_index" if source_type == "pdf" else "paragraph_index",
    )
    text = str(fields["text_raw"])
    hints = _bibliographic_hints(text)
    return {
        "item_type": str(fields["item_type"]),
        "position": int(position),
        "text": text,
        "is_empty": bool(fields.get("is_empty", not text.strip())),
        "physical_page": physical_page,
        "citation_page": _citation_page(fields, source_type, physical_page),
        "hints": hints,
        "likely_copyright_page": _is_likely_copyright_page(hints),
    }


def _reader_item(
    fields: Mapping[str, object],
    source_type: str,
) -> dict[str, object]:
    physical_page = _physical_page(fields, source_type)
    position = _first_present(
        fields,
        "pdf_page_index" if source_type == "pdf" else "paragraph_index",
    )
    text = str(fields["text_raw"])
    citation_formats = fields["citation_formats"]
    if not isinstance(citation_formats, Mapping):
        raise ValueError("结构化阅读器返回了无效的 citation_formats")
    return {
        "item_type": str(fields["item_type"]),
        "anchor_id": _first_text(fields.get("anchor_id")),
        "position": int(position),
        "text": text,
        "is_empty": bool(fields.get("is_empty", not text.strip())),
        "physical_page": physical_page,
        "citation_page": _citation_page(fields, source_type, physical_page),
        "page_mapping": _page_mapping(fields),
        "page_display": str(fields["page_display"]),
        "page_verified": bool(fields["page_verified"]),
        "citation_formats": {
            "chinese": str(citation_formats["chinese"]),
            "gb": str(citation_formats["gb"]),
            "page_verified": bool(citation_formats["page_verified"]),
            "can_copy": bool(citation_formats["can_copy"]),
        },
    }

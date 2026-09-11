"""Result assembly: turn one scored candidate into the frozen search payload.

Assembly consumes anchors (:mod:`search_anchors`) and citation information
(:mod:`search_citation`) and emits the response dict whose keys, offset units
and page fields are pinned by ``tests/test_search_pipeline_contract.py``.
It never queries candidates and never re-ranks them.
"""

from __future__ import annotations

import html
import json
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from .citations import build_citation_formats
from .normalization import trim_for_display
from .page_display import build_page_display, resolve_citation_page
from .search_anchors import page_match_spans, resolve_spread_hit
from .search_citation import build_copy_text, citation_metadata, hit_page
from .search_scoring import relative_relevance


class ResultStore(Protocol):
    """Library lookups the assembler needs; the facade implements it."""

    sources_by_id: Dict[str, Dict[str, object]]
    volumes_by_id: Dict[str, Dict[str, object]]
    works_by_id: Dict[str, Dict[str, object]]

    def pdf_page_record(self, source_file_id: str, page_index: int) -> Optional[Dict[str, object]]:
        ...

    def context(self, paragraph: Dict[str, object], before: bool) -> List[Dict[str, str]]:
        ...


def format_candidate(candidate: Dict[str, object], store: ResultStore) -> Dict[str, object]:
    paragraph = candidate.get("paragraph")
    if not isinstance(paragraph, dict):
        raise ValueError("Invalid search candidate payload.")
    return format_result(
        paragraph,
        str(candidate.get("match_type") or "exact"),
        float(candidate.get("match_score") or 0.0),
        int(candidate.get("match_start") or 0),
        int(candidate.get("match_end") or 0),
        store,
    )


def format_result(
    paragraph: Dict[str, object],
    match_type: str,
    score: float,
    start: int,
    end: int,
    store: ResultStore,
) -> Dict[str, object]:
    raw = str(paragraph.get("text_raw") or "")
    # Search offsets are Python string offsets, i.e. Unicode code points.
    # Keep that contract explicit because JavaScript string offsets use
    # UTF-16 code units and must be converted by the reader UI.
    start = max(0, min(start, len(raw)))
    end = max(start, min(end, len(raw)))
    matched = raw[start:end] if end > start else trim_for_display(raw, 80)
    match_quote = raw[start:end][:50] if end > start else ""
    source_type = str(paragraph.get("source_type") or "word")
    page_match_spans_list = page_match_spans(paragraph, source_type, start, end, len(raw))
    spread_hit = resolve_spread_hit(paragraph, page_match_spans_list, store.pdf_page_record)
    page_fields = dict(paragraph)
    if spread_hit:
        page_fields["citation_page_start"] = spread_hit["citation_page_start"]
        page_fields["citation_page_end"] = spread_hit["citation_page_end"]
        page_fields["printed_page_start"] = spread_hit["citation_page_start"]
        page_fields["printed_page_end"] = spread_hit["citation_page_end"]
        page_fields["citation_page_label_start"] = spread_hit["citation_page_start"]
        page_fields["citation_page_label_end"] = spread_hit["citation_page_end"]
        try:
            page_fields["citation_page_number_start"] = int(
                str(spread_hit["citation_page_start"])
            )
            page_fields["citation_page_number_end"] = int(
                str(spread_hit["citation_page_end"])
            )
        except ValueError:
            page_fields["citation_page_number_start"] = None
            page_fields["citation_page_number_end"] = None
    page_display = build_page_display(page_fields)
    citation_page = resolve_citation_page(page_fields)
    page = page_display.display
    page_note = page_display.note
    copy_text, volume_display = build_copy_text(paragraph, source_type, page, raw)
    citation_formats = build_citation_formats(
        citation_metadata(
            paragraph, source_type, store.sources_by_id, store.volumes_by_id, store.works_by_id
        ),
        hit_page(page_fields, source_type, page),
    )
    return {
        "paragraph_id": paragraph["paragraph_id"],
        "volume_id": paragraph["volume_id"],
        "volume_number": paragraph["volume_number"],
        "volume_display": volume_display,
        "work_id": paragraph.get("work_id"),
        "work_title": paragraph.get("work_title") or "未识别文献",
        "document_title": paragraph.get("document_title"),
        "author_label": paragraph.get("author_label"),
        "source_type": source_type,
        "source_file_id": paragraph.get("source_file_id"),
        "page": page,
        "page_source_type": page_display.page_source_type,
        "page_note": page_note,
        "page_confidence": paragraph.get("page_confidence"),
        "open_source_url": paragraph.get("open_source_url"),
        "pdf_page_start_index": paragraph.get("pdf_page_start_index"),
        "pdf_page_end_index": paragraph.get("pdf_page_end_index"),
        "pdf_page_start_label": paragraph.get("pdf_page_start_label"),
        "pdf_page_end_label": paragraph.get("pdf_page_end_label"),
        "printed_page_start": page_fields.get("printed_page_start"),
        "printed_page_end": page_fields.get("printed_page_end"),
        "citation_page_start": citation_page.start,
        "citation_page_end": citation_page.end,
        "citation_page_verified": citation_page.verified,
        "citation_page_number_start": page_fields.get("citation_page_number_start"),
        "citation_page_number_end": page_fields.get("citation_page_number_end"),
        "citation_page_label_start": page_fields.get("citation_page_label_start"),
        "citation_page_label_end": page_fields.get("citation_page_label_end"),
        "page_scope": paragraph.get("page_scope"),
        "page_mapping_method": paragraph.get("page_mapping_method"),
        "page_mapping_confidence": paragraph.get("page_mapping_confidence"),
        "mapping_method": paragraph.get("mapping_method"),
        "mapping_confidence": paragraph.get("mapping_confidence"),
        "mapping_confidence_level": paragraph.get("mapping_confidence_level"),
        "mapping_evidence": paragraph.get("mapping_evidence"),
        "segment_id": paragraph.get("segment_id"),
        "layout_mode": paragraph.get("layout_mode"),
        "logical_page_side": spread_hit.get("logical_page_side") if spread_hit else None,
        "spread_hit_precision": (
            spread_hit.get("precision")
            if spread_hit
            else "range_fallback" if paragraph.get("layout_mode") == "spread" else None
        ),
        "is_cross_page": paragraph.get("is_cross_page", False),
        "matched_text": matched,
        "match_quote": match_quote,
        "match_start": start,
        "match_end": end,
        "match_offset_unit": "unicode_codepoint",
        "page_match_spans": page_match_spans_list,
        "precise_highlight_available": bool(page_match_spans_list),
        "paragraph_text": raw,
        "highlighted_html": highlight_html(raw, start, end),
        "context_before": store.context(paragraph, before=True),
        "context_after": store.context(paragraph, before=False),
        "match_score": round(float(score), 4),
        "match_type": match_type,
        "original_file_name": paragraph.get("original_file_name"),
        "paragraph_index": paragraph.get("paragraph_index"),
        "copy_text": copy_text,
        "citation_formats": citation_formats,
    }


def format_passage(
    paragraph: Dict[str, object],
    rank: int,
    raw_score: float,
    raw_scores: Sequence[float],
    method: str,
    store: ResultStore,
) -> Dict[str, object]:
    # Passages have no verbatim span; reuse the shared formatter with an empty
    # match window (start == end) so ``matched_text`` becomes a preview snippet.
    result = format_result(paragraph, "relevance", 0.0, 0, 0, store)
    result["relevance"] = {
        "rank": rank,
        "method": method,
        "score": relative_relevance(raw_score, raw_scores),
    }
    return result


def highlight_html(text: str, start: int, end: int) -> str:
    text = text or ""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    return (
        html.escape(text[:start])
        + "<mark>"
        + html.escape(text[start:end])
        + "</mark>"
        + html.escape(text[end:])
    )


# ----------------------------------------------------------------------
# Adjacent-paragraph context (one real page away for PDFs, one paragraph
# otherwise).  The engine owns backend state; these helpers take it explicitly.
# ----------------------------------------------------------------------


def paragraph_context(
    db: object,
    by_volume: Dict[str, List[Dict[str, object]]],
    paragraph: Dict[str, object],
    before: bool,
    backend: str,
) -> List[Dict[str, str]]:
    if backend == "sqlite":
        return sql_context(db, paragraph, before)
    volume_id = str(paragraph.get("volume_id"))
    pidx = int(paragraph.get("paragraph_index", 0))
    source_file_id = str(paragraph.get("source_file_id") or "")
    plist = [
        item
        for item in by_volume[volume_id]
        if not source_file_id
        or str(item.get("source_file_id") or "") == source_file_id
    ]
    if str(paragraph.get("source_type") or "word") == "pdf":
        bounds = pdf_page_bounds(paragraph)
        if bounds is None:
            return []
        start_page, end_page = bounds
        positioned = [
            item
            for item in plist
            if (
                int(item.get("paragraph_index") or 0) < pidx
                if before
                else int(item.get("paragraph_index") or 0) > pidx
            )
        ]
        if before:
            positioned.reverse()
        for item in positioned:
            item_bounds = pdf_page_bounds(item)
            if not is_real_pdf_page(item, item_bounds):
                continue
            item_start, item_end = item_bounds
            if (before and item_end < start_page) or (
                not before and item_start > end_page
            ):
                return [context_item(item)]
        return []

    candidates = [
        item
        for item in plist
        if str(item.get("source_type") or "word") != "pdf"
        and str(item.get("text_raw") or "").strip()
        and (
            int(item.get("paragraph_index") or 0) < pidx
            if before
            else int(item.get("paragraph_index") or 0) > pidx
        )
    ]
    if not candidates:
        return []
    selected = (
        max(candidates, key=lambda item: int(item.get("paragraph_index") or 0))
        if before
        else min(candidates, key=lambda item: int(item.get("paragraph_index") or 0))
    )
    return [context_item(selected)]


def sql_context(db: object, paragraph: Dict[str, object], before: bool) -> List[Dict[str, str]]:
    if db is None:
        return []
    source_file_id = str(paragraph.get("source_file_id") or "")
    volume_id = paragraph.get("volume_id")
    source_column = "source_file_id" if source_file_id else "volume_id"
    source_value = source_file_id if source_file_id else volume_id
    source_predicate = (
        f"{source_column} IS NULL" if source_value is None else f"{source_column} = ?"
    )
    source_args = [] if source_value is None else [source_value]

    if str(paragraph.get("source_type") or "word") == "pdf":
        bounds = pdf_page_bounds(paragraph)
        if bounds is None:
            return []
        start_page, end_page = bounds
        paragraph_index = int(paragraph.get("paragraph_index") or 0)
        if before:
            position_predicate = "paragraph_index < ?"
            order = "DESC"
        else:
            position_predicate = "paragraph_index > ?"
            order = "ASC"
        position_index = (
            "idx_paragraphs_source_position"
            if source_file_id
            else "idx_paragraphs_volume_position"
        )
        sql = (
            "/* pdf_context */ "
            "SELECT p.paragraph_id, p.text_raw, p.pdf_page_start_index, "
            "p.pdf_page_end_index, p.payload_json "
            f"FROM paragraphs AS p INDEXED BY {position_index} "
            f"WHERE p.{source_predicate} AND p.source_type = 'pdf' "
            f"AND p.{position_predicate} ORDER BY p.paragraph_index {order}"
        )
        for row in db.execute(sql, [*source_args, paragraph_index]):
            candidate = sql_context_candidate(row)
            candidate_bounds = pdf_page_bounds(candidate)
            if not is_real_pdf_page(candidate, candidate_bounds):
                continue
            candidate_start, candidate_end = candidate_bounds
            if (before and candidate_end < start_page) or (
                not before and candidate_start > end_page
            ):
                return [context_item(candidate)]
        return []

    paragraph_index = int(paragraph.get("paragraph_index") or 0)
    if before:
        order = "DESC"
        predicate = "paragraph_index < ?"
    else:
        order = "ASC"
        predicate = "paragraph_index > ?"
    sql = (
        "SELECT paragraph_id, text_raw FROM paragraphs "
        f"WHERE {source_predicate} AND source_type != 'pdf' "
        f"AND trim(text_raw) != '' AND {predicate} "
        f"ORDER BY paragraph_index {order} LIMIT 1"
    )
    row = db.execute(sql, [*source_args, paragraph_index]).fetchone()
    if row is None:
        return []
    return [
        {
            "paragraph_id": str(row["paragraph_id"]),
            "text": str(row["text_raw"] or ""),
        }
    ]


def context_item(paragraph: Dict[str, object]) -> Dict[str, str]:
    return {
        "paragraph_id": str(paragraph["paragraph_id"]),
        "text": str(paragraph.get("text_raw") or ""),
    }


def sql_context_candidate(row: object) -> Dict[str, object]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "paragraph_id": row["paragraph_id"],
        "text_raw": row["text_raw"],
        "pdf_page_start_index": row["pdf_page_start_index"],
        "pdf_page_end_index": row["pdf_page_end_index"],
        "is_cross_page": payload.get("is_cross_page", False),
    }


def pdf_page_bounds(paragraph: Dict[str, object]) -> Optional[Tuple[int, int]]:
    start = paragraph.get("pdf_page_start_index")
    end = paragraph.get("pdf_page_end_index")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end < start
    ):
        return None
    return start, end


def is_real_pdf_page(
    paragraph: Dict[str, object], bounds: Optional[Tuple[int, int]]
) -> bool:
    if (
        bounds is None
        or bounds[0] != bounds[1]
        or not str(paragraph.get("text_raw") or "").strip()
    ):
        return False
    paragraph_id = str(paragraph.get("paragraph_id") or "").upper()
    return not paragraph.get("is_cross_page") and "-CROSS-" not in paragraph_id

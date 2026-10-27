"""Build searchable PDF paragraphs and exact page-local character anchors.

Consumes page records only; this module does not open PDFs or parser caches.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .normalization import compact_text, normalize_pdf_text, punctuationless_text, split_sentences
from .pdf_extraction_types import PDFExtractionError
from .pdf_page_mapping import mapped_page_display


CROSS_PAGE_TAIL_CHARS = 900


CROSS_PAGE_HEAD_CHARS = 900


PAGE_DECORATION_BLOCK_TYPES = {
    "header", "page_header", "footer", "page_footer", "page_number",
    "page_footnote", "footnote", "aside_text",
}


def attach_page_block_offsets(
    page_text: str,
    blocks: Sequence[Dict[str, object]],
) -> None:
    """Attach exact Unicode-codepoint intervals to layout blocks when possible.

    Layout extraction and page text extraction can disagree for malformed
    PDFs.  Unaligned blocks deliberately keep no offsets so a later search hit
    falls back to the physical page's full citation range instead of guessing
    a left/right side.
    """

    cursor = 0
    for block in blocks:
        raw_block_text = str(block.get("text") or "")
        candidates = [raw_block_text]
        stripped = raw_block_text.strip()
        if stripped and stripped != raw_block_text:
            candidates.append(stripped)
        matched_start = -1
        matched_text = ""
        for candidate in candidates:
            if not candidate:
                continue
            matched_start = page_text.find(candidate, cursor)
            if matched_start >= 0:
                matched_text = candidate
                break
        if matched_start < 0:
            continue
        matched_end = matched_start + len(matched_text)
        block["page_char_start"] = matched_start
        block["page_char_end"] = matched_end
        block["offset_unit"] = "unicode_codepoint"
        cursor = matched_end


def make_pdf_paragraphs(
    source_file_id: str,
    document_id: str,
    title: str,
    author: object,
    original_file_name: str,
    pages: Sequence[Dict[str, object]],
    work_id: object,
) -> List[Dict[str, object]]:
    paragraphs: List[Dict[str, object]] = []
    for page in pages:
        page_text = str(page.get("text_raw") or "")
        page_start, page_end = stripped_text_bounds(page_text)
        text = page_text[page_start:page_end]
        if not text:
            continue
        text_source_spans = [
            make_text_source_span(
                paragraph_text=text,
                paragraph_char_start=0,
                page=page,
                page_text=page_text,
                page_char_start=page_start,
                page_char_end=page_end,
            )
        ]
        paragraph = base_pdf_paragraph(
            source_file_id,
            document_id,
            title,
            author,
            original_file_name,
            work_id,
            text,
            int(page["pdf_page_index"]),
            int(page["pdf_page_index"]),
            page,
            page,
            paragraph_index=int(page["pdf_page_index"]) * 2,
            paragraph_id=f"{source_file_id}-P{int(page['pdf_page_index']):06d}",
            is_cross_page=False,
            text_source_spans=text_source_spans,
        )
        paragraphs.append(paragraph)
    for left, right in zip(pages, pages[1:]):
        left_page_text = str(left.get("text_raw") or "")
        right_page_text = str(right.get("text_raw") or "")
        left_body_start, left_body_end = (
            page_body_bounds(left) or stripped_text_bounds(left_page_text)
        )
        right_bounds = page_body_bounds(right)
        if right_bounds:
            right_body_start, right_body_end = right_bounds
            right_body_text = right_page_text[right_body_start:right_body_end]
        else:
            right_body_text, right_body_start, right_body_end = strip_pdf_page_header_for_cross(
                right_page_text
            )
        if left_body_start == left_body_end or not right_body_text:
            continue
        left_slice_start = max(left_body_start, left_body_end - CROSS_PAGE_TAIL_CHARS)
        left_slice_end = left_body_end
        right_slice_start = right_body_start
        right_slice_end = min(right_body_end, right_body_start + CROSS_PAGE_HEAD_CHARS)
        left_slice = left_page_text[left_slice_start:left_slice_end]
        right_slice = right_page_text[right_slice_start:right_slice_end]
        cross_text = f"{left_slice}\n{right_slice}"
        if len(punctuationless_text(cross_text)) < 80:
            continue
        text_source_spans = [
            make_text_source_span(
                paragraph_text=cross_text,
                paragraph_char_start=0,
                page=left,
                page_text=left_page_text,
                page_char_start=left_slice_start,
                page_char_end=left_slice_end,
            ),
            make_text_source_span(
                paragraph_text=cross_text,
                paragraph_char_start=len(left_slice) + 1,
                page=right,
                page_text=right_page_text,
                page_char_start=right_slice_start,
                page_char_end=right_slice_end,
            ),
        ]
        paragraph = base_pdf_paragraph(
            source_file_id,
            document_id,
            title,
            author,
            original_file_name,
            work_id,
            cross_text,
            int(left["pdf_page_index"]),
            int(right["pdf_page_index"]),
            left,
            right,
            paragraph_index=int(left["pdf_page_index"]) * 2 + 1,
            paragraph_id=f"{source_file_id}-CROSS-{int(left['pdf_page_index']):06d}-{int(right['pdf_page_index']):06d}",
            is_cross_page=True,
            text_source_spans=text_source_spans,
        )
        paragraphs.append(paragraph)
    return paragraphs


def stripped_text_bounds(text: str) -> Tuple[int, int]:
    """Return the half-open bounds used by ``str.strip()`` without rebuilding text."""

    value = text or ""
    without_leading = value.lstrip()
    if not without_leading:
        return len(value), len(value)
    start = len(value) - len(without_leading)
    end = len(value.rstrip())
    return start, max(start, end)


def make_text_source_span(
    *,
    paragraph_text: str,
    paragraph_char_start: int,
    page: Mapping[str, object],
    page_text: str,
    page_char_start: int,
    page_char_end: int,
) -> Dict[str, object]:
    """Build and validate one Unicode-codepoint source interval."""

    length = page_char_end - page_char_start
    paragraph_char_end = paragraph_char_start + length
    if (
        paragraph_char_start < 0
        or page_char_start < 0
        or length <= 0
        or paragraph_char_end > len(paragraph_text)
        or page_char_end > len(page_text)
        or paragraph_text[paragraph_char_start:paragraph_char_end]
        != page_text[page_char_start:page_char_end]
    ):
        raise PDFExtractionError("PDF paragraph text_source_spans invariant failed")
    return {
        "paragraph_char_start": paragraph_char_start,
        "paragraph_char_end": paragraph_char_end,
        "offset_unit": "unicode_codepoint",
        "pdf_page_id": str(page["pdf_page_id"]),
        "pdf_page_index": int(page["pdf_page_index"]),
        "page_text_hash": str(page.get("page_text_hash") or pdf_page_text_hash(page_text)),
        "page_char_start": page_char_start,
        "page_char_end": page_char_end,
    }


def base_pdf_paragraph(
    source_file_id: str,
    document_id: str,
    title: str,
    author: object,
    original_file_name: str,
    work_id: object,
    text: str,
    start_index: int,
    end_index: int,
    start_page: Dict[str, object],
    end_page: Dict[str, object],
    paragraph_index: int,
    paragraph_id: str,
    is_cross_page: bool,
    text_source_spans: Sequence[Mapping[str, object]],
) -> Dict[str, object]:
    start_citation = start_page.get("citation_page_start") or start_page.get("citation_page")
    end_citation = end_page.get("citation_page_end") or end_page.get("citation_page")
    calibrated = bool(start_citation and end_citation)
    method = (
        start_page.get("page_mapping_method")
        if start_page.get("page_mapping_method") == end_page.get("page_mapping_method")
        else "mixed"
    )
    confidence = min(
        float(start_page.get("page_mapping_confidence") or 0.0),
        float(end_page.get("page_mapping_confidence") or 0.0),
    )
    start_segment = start_page.get("segment_id")
    end_segment = end_page.get("segment_id")
    segment_id = start_segment if start_segment == end_segment else None
    start_scope = start_page.get("page_scope")
    end_scope = end_page.get("page_scope")
    page_scope = start_scope if start_scope == end_scope else "mixed" if start_scope or end_scope else None
    start_evidence = start_page.get("mapping_evidence")
    end_evidence = end_page.get("mapping_evidence")
    mapping_evidence = start_evidence if start_evidence == end_evidence else {
        "start_page": start_evidence,
        "end_page": end_evidence,
    } if start_evidence or end_evidence else None
    record: Dict[str, object] = {
        "paragraph_id": paragraph_id,
        "source_type": "pdf",
        "source_file_id": source_file_id,
        "document_id": document_id,
        "volume_id": document_id,
        "volume_number": None,
        "volume_display": title,
        "work_id": work_id,
        "work_title": title,
        "document_title": title,
        "author_label": author,
        "paragraph_index": paragraph_index,
        "source_order": paragraph_index,
        "section_index": None,
        "text_raw": text,
        "style_name": None,
        "alignment": None,
        "font_summary": None,
        "is_title_candidate": False,
        "is_toc_entry": False,
        "is_index_entry": False,
        "original_page_start": str(start_citation) if calibrated else None,
        "original_page_end": str(end_citation) if calibrated else None,
        "page_source_type": str(method or "uncalibrated"),
        "page_confidence": confidence if calibrated else 0.0,
        "page_display": mapped_page_display(
            start_index,
            end_index,
            str(start_citation) if calibrated else None,
            str(end_citation) if calibrated else None,
        ),
        "original_file_name": original_file_name,
        "eligible_for_search": len(punctuationless_text(text)) >= 20,
        "pdf_page_start_index": start_index,
        "pdf_page_end_index": end_index,
        "pdf_page_start_label": start_page.get("pdf_page_label"),
        "pdf_page_end_label": end_page.get("pdf_page_label"),
        "printed_page_start": start_page.get("printed_page_start") or start_page.get("printed_page"),
        "printed_page_end": end_page.get("printed_page_end") or end_page.get("printed_page"),
        "citation_page_start": str(start_citation) if calibrated else None,
        "citation_page_end": str(end_citation) if calibrated else None,
        "citation_page_number_start": start_page.get(
            "citation_page_number_start", start_page.get("citation_page_number")
        ),
        "citation_page_number_end": end_page.get(
            "citation_page_number_end", end_page.get("citation_page_number")
        ),
        "citation_page_label_start": start_page.get(
            "citation_page_label_start", start_page.get("citation_page_label")
        ),
        "citation_page_label_end": end_page.get(
            "citation_page_label_end", end_page.get("citation_page_label")
        ),
        "page_scope": page_scope,
        "page_mapping_method": str(method or "uncalibrated"),
        "page_mapping_confidence": confidence if calibrated else 0.0,
        "mapping_method": str(method or "uncalibrated"),
        "mapping_confidence": confidence if calibrated else 0.0,
        "mapping_confidence_level": start_page.get("mapping_confidence_level")
        if start_page.get("mapping_confidence_level") == end_page.get("mapping_confidence_level")
        else "mixed" if start_page.get("mapping_confidence_level") or end_page.get("mapping_confidence_level") else None,
        "mapping_evidence": mapping_evidence,
        "segment_id": segment_id,
        "layout_mode": start_page.get("layout_mode")
        if start_page.get("layout_mode") == end_page.get("layout_mode")
        else "mixed",
        "reading_direction": start_page.get("reading_direction")
        if start_page.get("reading_direction") == end_page.get("reading_direction")
        else "mixed",
        "gutter_x": start_page.get("gutter_x")
        if start_page.get("gutter_x") == end_page.get("gutter_x")
        else None,
        "is_cross_page": is_cross_page,
        "text_source_spans": [dict(span) for span in text_source_spans],
        "text_source": "native_text",
        "block_index": None,
        "bbox": None,
        "bbox_refs": None,
        "mineru_block_ids": None,
        "open_source_url": f"/source/{source_file_id}#page={start_index + 1}",
    }
    enrich_pdf_paragraph_text(record)
    return record


def enrich_pdf_paragraph_text(record: Dict[str, object]) -> None:
    text = str(record.get("text_raw") or "")
    record["normalized_text"] = normalize_pdf_text(text)
    record["compact_text"] = compact_text(text)
    record["plain_text"] = punctuationless_text(text)
    record["sentences"] = split_sentences(text)
    record["text_hash"] = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def pdf_page_text_hash(text: str) -> str:
    """Return the stable short hash used to validate page-local deep links."""

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def page_body_bounds(page: Mapping[str, object]) -> Optional[Tuple[int, int]]:
    """Span from the first to the last body block of a parsed page.

    A paragraph that runs over a page break continues from the last body line
    of one page to the first body line of the next; running heads, footnotes
    and folios sit between them in page text.  ``None`` when the parser gave no
    block roles or some block has no exact offsets, so callers keep the
    plain-text fallback.
    """

    blocks = [
        block for block in page.get("blocks") or []
        if isinstance(block, Mapping) and str(block.get("text") or "").strip()
    ]
    if not blocks or not all(
        isinstance(block.get("page_char_start"), int)
        and isinstance(block.get("page_char_end"), int)
        for block in blocks
    ):
        return None
    body = [
        block for block in blocks
        if str(block.get("mineru_type") or block.get("parser_type") or "").strip().casefold()
        not in PAGE_DECORATION_BLOCK_TYPES
    ]
    if not body or len(body) == len(blocks):
        return None
    return int(body[0]["page_char_start"]), int(body[-1]["page_char_end"])


def strip_pdf_page_header_for_cross(text: str) -> Tuple[str, int, int]:
    """Return a header-free continuous slice and its bounds in the original page.

    ``splitlines(keepends=True)`` is used only to locate the first retained
    character.  The returned text is sliced directly from ``text`` so CRLF and
    Unicode line separators remain byte-for-byte/character-for-character intact.
    """

    page_text = text or ""
    body_start, body_end = stripped_text_bounds(page_text)
    if body_start == body_end:
        return "", body_end, body_end

    lines = page_text[body_start:body_end].splitlines(keepends=True)
    retained_start = body_start
    for line in lines[:8]:
        value = line.strip()
        if not value:
            retained_start += len(line)
            continue
        compact = re.sub(r"\s+", "", value)
        alpha = [ch for ch in compact if ch.isalpha()]
        uppercase_ratio = (
            sum(1 for ch in alpha if ch.upper() == ch and ch.lower() != ch) / max(len(alpha), 1)
            if alpha
            else 0.0
        )
        if re.fullmatch(r"\d{1,4}|[ivxlcdmIVXLCDM]{1,8}", compact):
            retained_start += len(line)
            continue
        if len(compact) <= 40 and alpha and uppercase_ratio >= 0.75:
            retained_start += len(line)
            continue
        break
    while retained_start < body_end and page_text[retained_start].isspace():
        retained_start += 1
    return page_text[retained_start:body_end], retained_start, body_end

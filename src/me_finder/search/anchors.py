"""Location anchors: map paragraph matches onto physical source pages.

This module owns the PDF anchor contract only.  Word records may carry
paragraph/page metadata, but they must never claim exact PDF-page
highlighting.  Gaps between spans (notably the newline joining the two halves
of a CROSS paragraph) intentionally have no page mapping.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

PAGE_RECORD_LOOKUP = Callable[[str, int], Optional[Dict[str, object]]]


def page_match_spans(
    paragraph: Dict[str, object],
    source_type: str,
    match_start: int,
    match_end: int,
    paragraph_length: int,
) -> List[Dict[str, object]]:
    """Map a paragraph match onto the exact source-page text ranges."""

    if source_type != "pdf" or match_end <= match_start:
        return []
    source_spans = paragraph.get("text_source_spans")
    if not isinstance(source_spans, list):
        return []
    paragraph_raw = str(paragraph.get("text_raw") or "")

    mapped: List[Dict[str, object]] = []
    for span in source_spans:
        if not isinstance(span, dict):
            continue
        offset_unit = span.get("offset_unit")
        if offset_unit not in (None, "unicode_codepoint"):
            continue
        page_id = span.get("pdf_page_id")
        paragraph_start = span.get("paragraph_char_start")
        paragraph_end = span.get("paragraph_char_end")
        page_start = span.get("page_char_start")
        page_end = span.get("page_char_end")
        if not page_id or not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (paragraph_start, paragraph_end, page_start, page_end)
        ):
            continue
        if not (
            0 <= paragraph_start <= paragraph_end <= paragraph_length
            and 0 <= page_start <= page_end
            and paragraph_end - paragraph_start == page_end - page_start
        ):
            continue

        overlap_start = max(match_start, paragraph_start)
        overlap_end = min(match_end, paragraph_end)
        if overlap_start >= overlap_end:
            continue
        mapped_start = page_start + (overlap_start - paragraph_start)
        mapped_end = page_start + (overlap_end - paragraph_start)
        mapped_span: Dict[str, object] = {
            "pdf_page_id": str(page_id),
            "page_char_start": mapped_start,
            "page_char_end": mapped_end,
            # Keep a page-local recovery quote.  CROSS matches need a
            # different fragment for each physical page; the paragraph-
            # level quote can include the unmapped joiner and therefore
            # cannot occur verbatim on either page.  Slice the original
            # paragraph text with the same Unicode-codepoint overlap used
            # for offset mapping, and keep the existing 50-codepoint bound.
            "match_quote": paragraph_raw[overlap_start:overlap_end][:50],
        }
        if "page_text_hash" in span:
            # Preserve the producer's value verbatim.  A future reader
            # compares it with the current page hash before trusting the
            # saved offsets and falls back to searching ``match_quote``.
            mapped_span["page_text_hash"] = span.get("page_text_hash")
        page_index = span.get("pdf_page_index")
        if isinstance(page_index, int) and not isinstance(page_index, bool):
            mapped_span["pdf_page_index"] = page_index
        mapped.append(mapped_span)
    return mapped


def resolve_spread_hit(
    paragraph: Dict[str, object],
    page_match_spans: List[Dict[str, object]],
    page_record: PAGE_RECORD_LOOKUP,
) -> Optional[Dict[str, object]]:
    """Resolve a hit to logical spread pages, or fall back to its range."""

    if paragraph.get("layout_mode") != "spread" or not page_match_spans:
        return None
    source_file_id = str(paragraph.get("source_file_id") or "")
    if not source_file_id:
        return None

    citation_pages: List[str] = []
    resolved_sides: List[str] = []
    for span in page_match_spans:
        page_index = span_pdf_page_index(span)
        if page_index is None:
            return None
        page = page_record(source_file_id, page_index)
        if not page or page.get("layout_mode") != "spread":
            return None
        sides = spread_sides_for_span(page, span)
        if not sides:
            return None
        start_label = page.get("citation_page_start") or page.get("citation_page")
        end_label = page.get("citation_page_end") or start_label
        if not start_label or not end_label:
            return None
        direction = "rtl" if page.get("reading_direction") == "rtl" else "ltr"
        side_order = ("right", "left") if direction == "rtl" else ("left", "right")
        page_labels = {
            side_order[0]: str(start_label),
            side_order[1]: str(end_label),
        }
        ordered_sides = [side for side in side_order if side in sides]
        labels = [page_labels[side] for side in ordered_sides]
        for label in labels:
            if not citation_pages or citation_pages[-1] != label:
                citation_pages.append(label)
        side_label = ordered_sides[0] if len(ordered_sides) == 1 else "both"
        resolved_sides.append(side_label)
        span["logical_page_side"] = side_label
        span["citation_page_start"] = labels[0]
        span["citation_page_end"] = labels[-1]

    if not citation_pages:
        return None
    unique_sides = set(resolved_sides)
    return {
        "citation_page_start": citation_pages[0],
        "citation_page_end": citation_pages[-1],
        "logical_page_side": next(iter(unique_sides)) if len(unique_sides) == 1 else "both",
        "precision": "exact_region",
    }


def span_pdf_page_index(span: Dict[str, object]) -> Optional[int]:
    value = span.get("pdf_page_index")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    match = re.search(r"-PAGE-(\d+)\Z", str(span.get("pdf_page_id") or ""))
    return int(match.group(1)) if match else None


def spread_sides_for_span(
    page: Dict[str, object],
    span: Dict[str, object],
) -> set[str]:
    start = span.get("page_char_start")
    end = span.get("page_char_end")
    if not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (start, end)
    ):
        return set()
    gutter_x = page.get("gutter_x")
    try:
        gutter = float(gutter_x)
    except (TypeError, ValueError):
        gutter = 0.5
    if not 0.3 <= gutter <= 0.7:
        gutter = 0.5

    sides: set[str] = set()
    blocks = page.get("blocks")
    if not isinstance(blocks, list):
        return sides
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_start = block.get("page_char_start")
        block_end = block.get("page_char_end")
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (block_start, block_end)
        ):
            continue
        if max(int(start), block_start) >= min(int(end), block_end):
            continue
        bbox = normalized_block_bbox(page, block)
        if bbox is None:
            return set()
        x0, _, x1, _ = bbox
        if x0 < gutter < x1 and min(gutter - x0, x1 - gutter) > 0.05:
            return set()
        sides.add("left" if (x0 + x1) / 2 < gutter else "right")
    return sides


def normalized_block_bbox(
    page: Dict[str, object],
    block: Dict[str, object],
) -> Optional[Tuple[float, float, float, float]]:
    raw = block.get("bbox_normalized")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            values = tuple(float(value) for value in raw)
        except (TypeError, ValueError):
            values = ()
        if len(values) == 4 and 0 <= values[0] <= values[2] <= 1:
            return values[0], values[1], values[2], values[3]
    raw = block.get("bbox")
    try:
        width = float(page.get("page_width") or 0)
        height = float(page.get("page_height") or 0)
        values = (
            tuple(float(value) for value in raw)
            if isinstance(raw, (list, tuple))
            else ()
        )
    except (TypeError, ValueError):
        return None
    if len(values) != 4 or width <= 0 or height <= 0:
        return None
    return (
        values[0] / width,
        values[1] / height,
        values[2] / width,
        values[3] / height,
    )

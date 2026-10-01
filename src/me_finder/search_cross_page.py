"""Narrow a CROSS paragraph's citation pages to the page its hit is on.

A CROSS paragraph joins the tail of one PDF page to the head of the next, so
its record carries a two-page citation range.  When the matched text lies
wholly on one of those pages, the search result should cite that page alone.
Spread layouts are handled by :func:`search_anchors.resolve_spread_hit`.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .search_anchors import span_pdf_page_index

_CROSS_PAGE_FIELD_PAIRS = (
    ("citation_page_start", "citation_page_end"),
    ("original_page_start", "original_page_end"),
    ("printed_page_start", "printed_page_end"),
    ("citation_page_number_start", "citation_page_number_end"),
    ("citation_page_label_start", "citation_page_label_end"),
    ("pdf_page_start_label", "pdf_page_end_label"),
    ("pdf_page_start_index", "pdf_page_end_index"),
)


def resolve_cross_page_side(
    paragraph: Dict[str, object],
    page_match_spans: List[Dict[str, object]],
) -> Optional[str]:
    """Return "start"/"end" when a single-layout CROSS hit stays on one page.

    A hit touching both physical pages, or any span whose page cannot be
    identified, keeps the paragraph's full range.
    """

    if paragraph.get("layout_mode") in ("spread", "mixed") or not page_match_spans:
        return None
    start_index = paragraph.get("pdf_page_start_index")
    end_index = paragraph.get("pdf_page_end_index")
    if not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (start_index, end_index)
    ) or start_index == end_index:
        return None
    hit_pages = {span_pdf_page_index(span) for span in page_match_spans}
    if hit_pages == {start_index}:
        return "start"
    if hit_pages == {end_index}:
        return "end"
    return None


def narrow_to_cross_page_side(page_fields: Dict[str, object], side: str) -> None:
    """Collapse the page fields used for display and citation onto one side.

    Only the citation/display copy is narrowed; the paragraph record keeps its
    full range.  Each side was written from its own page record, so the chosen
    side is that page's own label and nothing is inferred.
    """

    for start_key, end_key in _CROSS_PAGE_FIELD_PAIRS:
        value = page_fields.get(start_key if side == "start" else end_key)
        page_fields[start_key] = value
        page_fields[end_key] = value

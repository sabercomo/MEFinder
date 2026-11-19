"""Resolve text excerpts against the current index without parsing source files."""

from __future__ import annotations

import json
import re
import sqlite3
from bisect import bisect_right
from typing import NamedTuple

from ..errors import UserFacingError
from ..page_display import resolve_citation_page
from ..pdf.extractors import pdf_page_text_hash
from ..persistence.materials_store import iter_units, paragraph_position, read_material_paragraph
from ..search.anchors import page_match_spans, resolve_spread_hit
from ..search.cross_page import narrow_to_cross_page_side, resolve_cross_page_side

MAX_TEXT_LENGTH = 100_000
MAX_UNITS = 50
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ANCHOR = re.compile(r"[A-Za-z0-9._:-]{1,256}")


class Unit(NamedTuple):
    index: int
    anchor: str
    fields: dict

    @property
    def text(self) -> str:
        """Return the indexed text used for Unicode-codepoint offsets."""
        return self.fields["text_raw"]


def validate_selection(selection: object) -> dict:
    """Validate the request from a search result or structured reader."""
    if not isinstance(selection, dict):
        raise UserFacingError("摘录必须包含原文和起止位置")
    fields = {key: selection.get(key) for key in (
        "source_id", "kind", "start_anchor", "end_anchor", "start_offset", "end_offset", "text"
    )}
    source, kind, start, end, a, b, text = fields.values()
    if not isinstance(source, str) or not _SOURCE_ID.fullmatch(source):
        raise UserFacingError("摘录的文献编号无效")
    if kind not in ("paragraph", "pdf_page") or any(
        not isinstance(value, str) or not _ANCHOR.fullmatch(value) for value in (start, end)
    ):
        raise UserFacingError("摘录的页码锚点无效")
    if type(a) is not int or type(b) is not int or not 0 <= a <= 2**63 - 1 or not 1 <= b <= 2**63 - 1 or (start == end and b <= a):
        raise UserFacingError("摘录的字符区间无效")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH:
        raise UserFacingError("请选择 1–100000 字符的原文")
    return fields


def _read_range(connection: sqlite3.Connection, selection: dict) -> list[Unit]:
    source_id, kind = selection["source_id"], selection["kind"]
    anchor_ids = (selection["start_anchor"], selection["end_anchor"])
    if kind == "paragraph" and anchor_ids[0] == anchor_ids[1]:
        row = read_material_paragraph(connection, source_id, anchor_ids[0])
        return [Unit(*row)] if row else []
    if kind == "pdf_page":
        prefix = re.escape(source_id) + r"-PAGE-(\d{1,10})"
        positions = [
            int(match[1]) if (match := re.fullmatch(prefix, anchor)) else None
            for anchor in anchor_ids
        ]
    else:
        positions = [paragraph_position(connection, source_id, anchor) for anchor in anchor_ids]
    start, end = positions
    if start is None or end is None or not 0 <= end - start < MAX_UNITS:
        return []
    units = [Unit(*row) for row in iter_units(connection, source_id, kind, start, end)]
    if not units or (units[0].anchor, units[-1].anchor) != anchor_ids:
        return []
    if [unit.index for unit in units] != list(range(start, end + 1)):
        return []
    if kind == "paragraph" and len(units) > 1:
        works = {unit.fields.get("work_id") for unit in units}
        if len(works) > 1 or None in works or "" in works:
            return []
    return units


def resolve_selection(connection: sqlite3.Connection, source_row: sqlite3.Row, selection: dict) -> tuple[dict, dict] | None:
    """Verify offsets and construct a citation snapshot and reader target."""
    kind, source_id = selection["kind"], selection["source_id"]
    source_type = source_row["source_type"]
    if kind == "pdf_page" and source_type != "pdf":
        return None
    units = _read_range(connection, selection)
    if not units:
        return None
    start, end = selection["start_offset"], selection["end_offset"]
    if start >= len(units[0].text) or end > len(units[-1].text):
        return None
    offsets = [(start if i == 0 else 0, end if i == len(units) - 1 else len(unit.text))
               for i, unit in enumerate(units)]
    if "\n".join(unit.text[a:b] for unit, (a, b) in zip(units, offsets)) != selection["text"]:
        return None

    def pdf_page(_source_id: str, index: int) -> dict | None:
        return next((row[2] for row in iter_units(connection, source_id, "pdf_page", index, index)), None)

    highlights = []
    citation_pages = []
    for unit, (a, b) in zip(units, offsets):
        fields = dict(unit.fields)
        if kind == "pdf_page" or source_type == "word":
            spans = [{
                "anchor_id": unit.anchor, "pdf_page_id": unit.anchor if kind == "pdf_page" else None,
                "pdf_page_index": unit.index if kind == "pdf_page" else None,
                "page_char_start": a, "page_char_end": b,
                "page_text_hash": pdf_page_text_hash(unit.text), "match_quote": unit.text[a:b][:50],
            }]
        else:
            spans = page_match_spans(fields, source_type, a, b, len(unit.text))
        if source_type == "pdf":
            spread = resolve_spread_hit(fields, spans, pdf_page)
            if spread:
                fields.update({
                    "citation_page_start": spread["citation_page_start"],
                    "citation_page_end": spread["citation_page_end"],
                })
            else:
                side = resolve_cross_page_side(fields, spans)
                if side:
                    narrow_to_cross_page_side(fields, side)
        highlights.extend(spans)
        citation_pages.append(resolve_citation_page(fields))

    source = json.loads(source_row["payload_json"])
    first = units[0].fields
    bibliography = source.get("bibliographic_metadata") or {}
    title = str(first.get("document_title") or first.get("work_title") or source.get("display_title")
                or bibliography.get("title") or source.get("title") or source_row["file_name"] or source_id)
    author = str(first.get("author_label") or bibliography.get("author")
                 or source.get("author") or source.get("author_label") or "")
    verified = all(page.verified for page in citation_pages)
    fields = {
        **selection, "title": title, "author": author,
        "citation_start": citation_pages[0].start if verified else None,
        "citation_end": citation_pages[-1].end if verified else None,
    }
    first_span = highlights[0] if highlights else {}
    page_index = first_span.get("pdf_page_index")
    if page_index is None:
        page_index = first.get("pdf_page_start_index")
    reader = {
        "sourceId": source_id, "title": title,
        "targetIndex": units[0].index if source_type == "word" else page_index,
        "anchorId": first_span.get("anchor_id") or first_span.get("pdf_page_id") or (
            units[0].anchor if source_type == "word" else ""
        ),
        "paragraphId": units[0].anchor if source_type == "word" else "",
        "pageMatchSpans": highlights, "matchOffsetUnit": "unicode_codepoint",
        "preciseHighlightAvailable": bool(highlights),
    }
    return fields, reader


def relocate_selection(connection: sqlite3.Connection, source_row: sqlite3.Row, selection: dict) -> tuple[dict, dict] | None:
    """Relocate only a unique literal occurrence in the original source."""
    units = [Unit(*row) for row in iter_units(
        connection, selection["source_id"], selection["kind"], 0, 2**63 - 1
    )]
    if not units:
        return None
    starts = [0]
    for unit in units[:-1]:
        starts.append(starts[-1] + len(unit.text) + 1)
    corpus = "\n".join(unit.text for unit in units)
    text = selection["text"]
    position = corpus.find(text)
    if position < 0 or corpus.find(text, position + 1) >= 0:
        return None
    last = position + len(text) - 1
    start_index, end_index = bisect_right(starts, position) - 1, bisect_right(starts, last) - 1
    return resolve_selection(connection, source_row, {
        **selection, "start_anchor": units[start_index].anchor, "end_anchor": units[end_index].anchor,
        "start_offset": position - starts[start_index], "end_offset": last - starts[end_index] + 1,
    })

"""Read model for reviewing one pair's body ranges before alignment.

The automatic detector in :mod:`me_finder.alignment_regions` decides where each
book's main text starts and ends.  This module exposes what the user needs to
check that decision and correct it: the two books' real indexed segments, their
existing page anchors, a chapter list derived from the same heading detection,
and the range currently in force (a saved human review when one applies to this
exact segmentation, otherwise the automatic detection).

Nothing here re-parses a source file or edits stored text.  Ranges are segment
indices, so they are bound to the current segment sets of these two documents;
a re-parse creates a new segmentation and an old review no longer applies.
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from .alignment_regions import alignment_body_bounds
from .page_display import build_page_display
from .persistence.alignment_store import (
    alignment_read_connection,
    body_range_write_transaction,
    first_segment_on_pdf_page,
    paragraph_anchor_rows,
    pdf_anchor_rows,
    read_segment_window,
    segment_count,
    segment_set_owner,
)
from .semantic_alignment import _document_heading_positions
from .text_alignment import (
    InvalidAlignmentRequest,
    WriteWindow,
    _json_object,
    _require_pair,
    _segment_set,
    _segment_set_language,
    _source_kind,
    _source_row,
    _validate_nonnegative_integer,
    _validate_source_id,
    latest_reviewed_body_ranges,
)


DEFAULT_SEGMENT_WINDOW = 9
MAX_SEGMENT_WINDOW = 40
MAX_OUTLINE_ENTRIES = 300
_SNIPPET_LIMIT = 160
_OUTLINE_TITLE_LIMIT = 60


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return text.strip()


def _outline(texts: Sequence[str]) -> List[Dict[str, object]]:
    """List chapter positions using the same headings the detector reads.

    Titles are the segment's own first line, never a generated label, so a
    chapter the parser never produced cannot appear here.
    """

    indices = sorted(
        {
            index
            for key, index in _document_heading_positions(texts).items()
            if key.startswith("chapter:") or key.startswith("preface:")
        }
    )
    entries: List[Dict[str, object]] = []
    for index in indices[:MAX_OUTLINE_ENTRIES]:
        title = _first_line(texts[index])[:_OUTLINE_TITLE_LIMIT]
        if title:
            entries.append({"segment_index": index, "title": title})
    return entries


def _pdf_page_anchors(
    connection: sqlite3.Connection, source_id: str, segment_ids: Sequence[str]
) -> Dict[str, Dict[str, object]]:
    if not segment_ids:
        return {}
    pages, page_rows = pdf_anchor_rows(connection, source_id, segment_ids)
    anchors: Dict[int, Dict[str, object]] = {}
    for page_index in sorted(set(pages.values())):
        row = page_rows[page_index]
        fields = _json_object(row["payload_json"]) if row is not None else {}
        fields.update({"source_type": "pdf", "pdf_page_index": page_index})
        display = build_page_display(fields)
        anchors[page_index] = {
            "page_display": display.display,
            "page_note": display.note,
            "page_source_type": display.page_source_type,
            "physical_page_1based": page_index + 1,
            "paragraph_index": None,
        }
    return {
        segment_id: anchors[page_index] for segment_id, page_index in pages.items()
    }


def _paragraph_anchors(
    connection: sqlite3.Connection, source_id: str, segment_ids: Sequence[str]
) -> Dict[str, Dict[str, object]]:
    if not segment_ids:
        return {}
    positions, paragraph_rows = paragraph_anchor_rows(connection, source_id, segment_ids)
    anchors: Dict[int, Dict[str, object]] = {}
    for paragraph_index in sorted(set(positions.values())):
        row = paragraph_rows[paragraph_index]
        fields = _json_object(row["payload_json"]) if row is not None else {}
        if row is not None:
            fields.update(
                {
                    "source_type": "word",
                    "paragraph_index": paragraph_index,
                    "page_display": row["page_display"],
                    "page_source_type": row["page_source_type"],
                }
            )
        display = build_page_display(fields)
        anchors[paragraph_index] = {
            "page_display": display.display,
            "page_note": display.note,
            "page_source_type": display.page_source_type,
            "physical_page_1based": None,
            "paragraph_index": paragraph_index,
        }
    return {
        segment_id: anchors[paragraph_index]
        for segment_id, paragraph_index in positions.items()
    }


def _anchors(
    connection: sqlite3.Connection,
    source_id: str,
    source_kind: str,
    segment_ids: Sequence[str],
) -> Dict[str, Dict[str, object]]:
    if source_kind == "pdf":
        return _pdf_page_anchors(connection, source_id, segment_ids)
    return _paragraph_anchors(connection, source_id, segment_ids)


_UNANCHORED = {
    "page_display": "页码尚未解析",
    "page_note": None,
    "page_source_type": "unknown",
    "physical_page_1based": None,
    "paragraph_index": None,
}


def _segment_payload(
    segment_index: int,
    segment_id: str,
    text: str,
    anchors: Mapping[str, Dict[str, object]],
    *,
    snippet_only: bool = False,
) -> Dict[str, object]:
    anchor = anchors.get(segment_id, _UNANCHORED)
    payload: Dict[str, object] = {
        "segment_index": segment_index,
        "segment_id": segment_id,
        "text": text[:_SNIPPET_LIMIT] if snippet_only else text,
    }
    payload.update(anchor)
    return payload


def _current_bounds(
    reviewed: Mapping[str, object] | None, side: str, texts: Sequence[str]
) -> Tuple[int, int]:
    if reviewed is not None:
        bounds = reviewed.get(side)
        if (
            isinstance(bounds, list)
            and len(bounds) == 2
            and all(type(value) is int for value in bounds)
            and 0 <= bounds[0] < bounds[1] <= len(texts)
        ):
            return bounds[0], bounds[1]
    return alignment_body_bounds(texts)


def read_pair_body_ranges(
    db_path: Path,
    document_group_id: object,
    pivot_source_file_id: object,
    target_source_file_id: object,
    *,
    write_window: WriteWindow | None = None,
) -> Dict[str, object]:
    """Describe both books' current body range and how to browse their text.

    Segmenting a document that has never been aligned writes its segment set,
    which is why this runs on a writable connection.  No alignment is computed
    and no source file is re-parsed.
    """

    group_id = str(document_group_id or "").strip()
    if not group_id:
        raise InvalidAlignmentRequest("document_group_id is required")
    pivot_id = _validate_source_id(pivot_source_file_id)
    target_id = _validate_source_id(target_source_file_id)
    transaction_window = write_window or nullcontext
    with transaction_window():
        with body_range_write_transaction(db_path) as connection:
            _require_pair(connection, group_id, pivot_id, target_id)
            sides: List[Dict[str, object]] = []
            prepared = []
            for side, source_id in (("pivot", pivot_id), ("target", target_id)):
                segment_set_id, segments = _segment_set(connection, source_id)
                prepared.append((side, source_id, segment_set_id, segments))
            reviewed = latest_reviewed_body_ranges(
                connection, prepared[0][2], prepared[1][2]
            )
            for side, source_id, segment_set_id, segments in prepared:
                texts = [text for _segment_id, text in segments]
                source_kind = _source_kind(_source_row(connection, source_id))
                start, end = _current_bounds(reviewed, side, texts)
                bound_ids = [segments[start][0], segments[end - 1][0]]
                anchors = _anchors(connection, source_id, source_kind, bound_ids)
                sides.append(
                    {
                        "side": side,
                        "source_file_id": source_id,
                        "segment_set_id": segment_set_id,
                        "source_kind": source_kind,
                        "language_code": _segment_set_language(
                            connection, segment_set_id
                        ),
                        "segment_count": len(segments),
                        # PDF text carries a physical page per segment, so a page
                        # jump is meaningful there; text formats are browsed by
                        # chapter and segment position instead of an invented page.
                        "locator_kind": (
                            "pdf_page" if source_kind == "pdf" else "segment"
                        ),
                        "body_start_index": start,
                        # Shown to the user as the last included segment; the
                        # stored interval stays half-open.
                        "body_end_index": end - 1,
                        "outline": _outline(texts),
                        "start_segment": _segment_payload(
                            start, segments[start][0], texts[start], anchors,
                            snippet_only=True,
                        ),
                        "end_segment": _segment_payload(
                            end - 1, segments[end - 1][0], texts[end - 1], anchors,
                            snippet_only=True,
                        ),
                    }
                )
    return {
        "document_group_id": group_id,
        "range_source": "reviewed" if reviewed is not None else "detected",
        "sides": sides,
    }


def read_body_range_segments(
    db_path: Path,
    source_file_id: object,
    segment_set_id: object,
    *,
    start: object = 0,
    count: object = DEFAULT_SEGMENT_WINDOW,
    pdf_page: object = None,
) -> Dict[str, object]:
    """Return one window of indexed segments with their existing page anchors."""

    source_id = _validate_source_id(source_file_id)
    set_id = str(segment_set_id or "").strip()
    if not set_id:
        raise InvalidAlignmentRequest("segment_set_id 必须提供。")
    requested_start = _validate_nonnegative_integer("start", start)
    window = _validate_nonnegative_integer("count", count)
    if not 1 <= window <= MAX_SEGMENT_WINDOW:
        raise InvalidAlignmentRequest(
            f"count 必须在 1 到 {MAX_SEGMENT_WINDOW} 之间。"
        )
    page_number = (
        None if pdf_page is None
        else _validate_nonnegative_integer("pdf_page", pdf_page)
    )
    with alignment_read_connection(db_path) as connection:
        if segment_set_owner(connection, set_id) != source_id:
            raise InvalidAlignmentRequest("Segment 集与文献不匹配，请重新打开正文范围")
        source_kind = _source_kind(_source_row(connection, source_id))
        total = segment_count(connection, set_id)
        if total == 0:
            raise InvalidAlignmentRequest("文献没有可用于对齐的 Segment。")
        if page_number is not None:
            if source_kind != "pdf":
                raise InvalidAlignmentRequest("只有 PDF 文献可以按页跳转")
            located = first_segment_on_pdf_page(
                connection, set_id, source_id, max(page_number - 1, 0)
            )
            if located is None:
                raise InvalidAlignmentRequest("这一页没有可选文本，请换一页")
            requested_start = located
        offset = max(0, min(requested_start, total - 1))
        rows = read_segment_window(connection, set_id, offset, window)
        anchors = _anchors(
            connection,
            source_id,
            source_kind,
            [str(row["segment_id"]) for row in rows],
        )
        segments = [
            _segment_payload(
                int(row["order_index"]),
                str(row["segment_id"]),
                str(row["text_raw"] or ""),
                anchors,
            )
            for row in rows
        ]
    return {
        "source_file_id": source_id,
        "segment_set_id": set_id,
        "segment_count": total,
        "start": offset,
        "segments": segments,
    }

"""Read-only SQLite queries behind the translation-comparison workspace.

Every function takes the caller's connection so one overview or link-window
request reads a single snapshot.  Status rules (staleness, review counting,
indirect routes) stay in ``translation_works``; this module only fetches rows.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional, Sequence

# Span tables per reader kind; internal constants, never caller input.
_SPAN_TABLES = {
    "pdf": ("text_segment_spans", "pdf_page_index", "page_char_start", "page_char_end"),
    "word": (
        "text_segment_paragraph_spans",
        "paragraph_index",
        "paragraph_char_start",
        "paragraph_char_end",
    ),
}


def _span_table(kind: str) -> tuple[str, str, str, str]:
    return _SPAN_TABLES["pdf" if kind == "pdf" else "word"]


def _placeholders(values: Sequence[object]) -> str:
    return ",".join("?" for _ in values)


# ── work membership and reading state ─────────────────────────────────────


def read_reading_position_row(connection: sqlite3.Connection, group_id: str) -> Optional[sqlite3.Row]:
    """Return the stored reader position of a work, or ``None``."""

    return connection.execute(
        "SELECT left_source_file_id, right_source_file_id, item_index, "
        "char_offset, updated_at FROM document_group_reading_positions "
        "WHERE document_group_id = ?",
        (group_id,),
    ).fetchone()


def read_member_ids(connection: sqlite3.Connection, group_id: str) -> List[str]:
    """Return a work's member source ids in display order."""

    return [
        str(row[0])
        for row in connection.execute(
            "SELECT source_file_id FROM document_group_members "
            "WHERE document_group_id = ? ORDER BY member_order, source_file_id",
            (group_id,),
        )
    ]


def read_suggestion_dismissal_rows(connection: sqlite3.Connection) -> List[sqlite3.Row]:
    """Return every dismissed same-title suggestion, oldest first."""

    return connection.execute(
        "SELECT source_file_ids_json FROM document_group_suggestion_dismissals "
        "ORDER BY created_at"
    ).fetchall()


def read_groups(connection: sqlite3.Connection, source_id: str = "") -> List[sqlite3.Row]:
    """Return works (id and base version), optionally only those containing ``source_id``."""

    query = "SELECT document_group_id, base_source_file_id FROM document_groups"
    if source_id:
        query += (
            " WHERE document_group_id IN (SELECT document_group_id "
            "FROM document_group_members WHERE source_file_id = ?)"
        )
    return connection.execute(query, (source_id,) if source_id else ()).fetchall()


def read_latest_language_code(connection: sqlite3.Connection, source_id: str) -> Optional[sqlite3.Row]:
    """Return the language of the newest segmentation of a source."""

    return connection.execute(
        "SELECT language_code FROM segment_sets WHERE source_file_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (source_id,),
    ).fetchone()


# ── segments, runs and links ───────────────────────────────────────────────


def read_segment_texts(connection: sqlite3.Connection, segment_set_id: str) -> List[str]:
    """Return the raw text of every segment in a set, in order."""

    return [
        str(row[0])
        for row in connection.execute(
            "SELECT text_raw FROM text_segments WHERE segment_set_id = ? "
            "ORDER BY order_index",
            (segment_set_id,),
        )
    ]


def read_completed_run_segment_sets(connection: sqlite3.Connection) -> List[Sequence[object]]:
    """Return pivot set, target set and parameters of every completed run."""

    return connection.execute(
        "SELECT pivot_segment_set_id, target_segment_set_id, parameters_json "
        "FROM alignment_runs WHERE status = 'completed'"
    ).fetchall()


def read_pivot_status_counts(connection: sqlite3.Connection, run_id: str) -> List[sqlite3.Row]:
    """Return distinct pivot segments per link review status for one run."""

    return connection.execute(
        "SELECT l.review_status, COUNT(DISTINCT m.segment_id) AS segment_count "
        "FROM alignment_links l JOIN alignment_link_members m "
        "ON m.alignment_link_id = l.alignment_link_id "
        "WHERE l.alignment_run_id = ? AND m.side = 'pivot' "
        "GROUP BY l.review_status",
        (run_id,),
    ).fetchall()


def read_rejected_link_members(connection: sqlite3.Connection, run_id: str) -> List[sqlite3.Row]:
    """Return link id, side and segment of every member of a run's rejected links."""

    return connection.execute(
        "SELECT l.alignment_link_id, m.side, m.segment_id "
        "FROM alignment_links l JOIN alignment_link_members m "
        "ON m.alignment_link_id = l.alignment_link_id "
        "WHERE l.alignment_run_id = ? AND l.review_status = 'rejected'",
        (run_id,),
    ).fetchall()


def read_segment_ids_in_items(
    connection: sqlite3.Connection,
    kind: str,
    segment_set_id: str,
    source_id: str,
    start_index: int,
    end_index: int,
) -> List[str]:
    """Return segments of a set that touch reader items ``start..end``, in order."""

    table, item_column, _start, _end = _span_table(kind)
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT s.segment_id, s.order_index "
            f"FROM {table} p "
            "JOIN text_segments s ON s.segment_id = p.segment_id "
            "WHERE s.segment_set_id = ? AND p.source_file_id = ? "
            f"AND p.{item_column} BETWEEN ? AND ? ORDER BY s.order_index",
            (segment_set_id, source_id, start_index, end_index),
        )
    ]


def read_segment_spans(
    connection: sqlite3.Connection, kind: str, segment_ids: Sequence[str]
) -> List[sqlite3.Row]:
    """Return ``segment_id``/``item_index``/``char_start``/``char_end`` spans in span order."""

    table, item_column, start_column, end_column = _span_table(kind)
    return connection.execute(
        f"SELECT segment_id, {item_column} AS item_index, {start_column} AS "
        f"char_start, {end_column} AS char_end FROM {table} "
        f"WHERE segment_id IN ({_placeholders(segment_ids)}) ORDER BY span_order",
        list(segment_ids),
    ).fetchall()


def read_links_touching_segments(
    connection: sqlite3.Connection, run_id: str, side: str, segment_ids: Sequence[str]
) -> List[sqlite3.Row]:
    """Return the run's links with a ``side`` member among ``segment_ids``."""

    return connection.execute(
        "SELECT DISTINCT l.alignment_link_id, l.order_index, l.review_status, "
        "l.confidence FROM alignment_links l JOIN alignment_link_members m "
        "ON m.alignment_link_id = l.alignment_link_id "
        f"WHERE l.alignment_run_id = ? AND m.side = ? AND m.segment_id IN ({_placeholders(segment_ids)}) "
        "ORDER BY l.order_index",
        (run_id, side, *segment_ids),
    ).fetchall()


def read_link_members(connection: sqlite3.Connection, link_ids: Sequence[str]) -> List[sqlite3.Row]:
    """Return every member of the given links, in segment order."""

    return connection.execute(
        "SELECT m.alignment_link_id, m.side, m.segment_id FROM alignment_link_members m "
        "JOIN text_segments s ON s.segment_id = m.segment_id "
        f"WHERE m.alignment_link_id IN ({_placeholders(link_ids)}) ORDER BY s.order_index",
        list(link_ids),
    ).fetchall()


def read_deferred_segment_keys(
    connection: sqlite3.Connection, source_id: str, target_id: str, source_set_id: str
) -> set[str]:
    """Return source segment keys whose review was deferred for this pair."""

    return {
        str(row[0])
        for row in connection.execute(
            "SELECT source_segment_key FROM alignment_review_deferrals "
            "WHERE source_file_id = ? AND target_source_file_id = ? "
            "AND source_segment_set_id = ?",
            (source_id, target_id, source_set_id),
        )
    }


def read_nearest_link_with_counterpart(
    connection: sqlite3.Connection, run_id: str, side: str, other: str, order_index: int
) -> Optional[sqlite3.Row]:
    """Return the link nearest to ``order_index`` on ``side`` that has an ``other`` member."""

    return connection.execute(
        "SELECT l.alignment_link_id FROM alignment_links l "
        "JOIN alignment_link_members sm ON sm.alignment_link_id = l.alignment_link_id "
        "AND sm.side = ? JOIN text_segments s ON s.segment_id = sm.segment_id "
        "WHERE l.alignment_run_id = ? AND EXISTS (SELECT 1 FROM alignment_link_members tm "
        "WHERE tm.alignment_link_id = l.alignment_link_id AND tm.side = ?) "
        "ORDER BY ABS(s.order_index - ?) LIMIT 1",
        (side, run_id, other, order_index),
    ).fetchone()


def read_link_side_segment_ids(connection: sqlite3.Connection, link_id: str, side: str) -> List[str]:
    """Return one side's segments of a link, in segment order."""

    return [
        str(member[0])
        for member in connection.execute(
            "SELECT m.segment_id FROM alignment_link_members m "
            "JOIN text_segments s ON s.segment_id = m.segment_id "
            "WHERE m.alignment_link_id = ? AND m.side = ? ORDER BY s.order_index",
            (link_id, side),
        )
    ]

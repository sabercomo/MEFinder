"""SQLite operations for alignment generation and manual corrections.

The caller owns route and segment validation. This store keeps the proposal,
confirmation, and revocation SQL on the same connection and transaction as
those checks.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Sequence

from .connection import connect_index, open_writable_index, table_exists
from .schema_installers import install_text_alignment_schema


@contextmanager
def alignment_read_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Keep one read connection open during alignment validation and display."""

    connection = connect_index(str(db_path))
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def override_write_transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open one immediate write transaction for a correction operation."""

    connection = open_writable_index(Path(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        connection.rollback()
        raise
    finally:
        connection.close()


def insert_pending_override(
    connection: sqlite3.Connection,
    context: dict[str, object],
    override_id: str,
    confirmation_token: str,
    evidence_json: str,
    timestamp: str,
) -> None:
    """Replace an earlier pending proposal for the same selection."""

    install_text_alignment_schema(connection)
    connection.execute(
        "DELETE FROM alignment_manual_overrides WHERE source_file_id = ? "
        "AND target_source_file_id = ? AND source_segment_set_id = ? "
        "AND source_segment_key = ? AND status = 'pending'",
        (
            context["source_file_id"],
            context["target_source_file_id"],
            context["source_segment_set_id"],
            context["source_segment_key"],
        ),
    )
    connection.execute(
        "INSERT INTO alignment_manual_overrides(override_id, "
        "document_group_id, source_file_id, target_source_file_id, "
        "source_segment_set_id, target_segment_set_id, source_segment_key, "
        "source_segment_ids_json, target_segment_ids_json, status, "
        "confirmation_token, evidence_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (
            override_id,
            context["document_group_id"],
            context["source_file_id"],
            context["target_source_file_id"],
            context["source_segment_set_id"],
            context["target_segment_set_id"],
            context["source_segment_key"],
            json.dumps(
                context["source_segment_ids"], ensure_ascii=False, separators=(",", ":")
            ),
            json.dumps(
                context["target_segment_ids"], ensure_ascii=False, separators=(",", ":")
            ),
            confirmation_token,
            evidence_json,
            timestamp,
        ),
    )


def load_override(connection: sqlite3.Connection, override_id: str) -> sqlite3.Row | None:
    """Load a proposal when the correction table exists."""

    if not table_exists(connection, "alignment_manual_overrides"):
        return None
    return connection.execute(
        "SELECT * FROM alignment_manual_overrides WHERE override_id = ?", (override_id,)
    ).fetchone()


def revoke_expired_override(
    connection: sqlite3.Connection, override_id: str, timestamp: str
) -> None:
    """Persist a stale proposal before its caller reports the conflict."""

    connection.execute(
        "UPDATE alignment_manual_overrides SET status = 'revoked', "
        "revoked_at = ? WHERE override_id = ?",
        (timestamp, override_id),
    )
    connection.commit()


def confirm_pending_override(
    connection: sqlite3.Connection, row: sqlite3.Row, override_id: str, timestamp: str
) -> None:
    """Retire the prior confirmation before activating this proposal."""

    connection.execute(
        "UPDATE alignment_manual_overrides SET status = 'revoked', "
        "revoked_at = ? WHERE status = 'confirmed' AND source_file_id = ? "
        "AND target_source_file_id = ? AND source_segment_set_id = ? "
        "AND source_segment_key = ?",
        (
            timestamp,
            str(row["source_file_id"]),
            str(row["target_source_file_id"]),
            str(row["source_segment_set_id"]),
            str(row["source_segment_key"]),
        ),
    )
    connection.execute(
        "UPDATE alignment_manual_overrides SET status = 'confirmed', "
        "confirmed_at = ? WHERE override_id = ?",
        (timestamp, override_id),
    )


def revoke_override_row(
    connection: sqlite3.Connection, override_id: str, timestamp: str
) -> None:
    """Persist revocation of a pending or confirmed proposal."""

    connection.execute(
        "UPDATE alignment_manual_overrides SET status = 'revoked', "
        "revoked_at = ? WHERE override_id = ?",
        (timestamp, override_id),
    )


def read_override_rows(
    db_path: Path,
    *,
    source_file_id: str | None,
    target_source_file_id: str | None,
    status: str | None,
    limit: int,
) -> list[sqlite3.Row]:
    """Read the requested audit rows, newest first."""

    filters: list[str] = []
    parameters: list[object] = []
    for column, value in (
        ("source_file_id", source_file_id),
        ("target_source_file_id", target_source_file_id),
        ("status", status),
    ):
        if value is not None:
            filters.append(f"{column} = ?")
            parameters.append(value)
    connection = connect_index(str(db_path))
    try:
        if not table_exists(connection, "alignment_manual_overrides"):
            return []
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        return connection.execute(
            "SELECT override_id, document_group_id, source_file_id, "
            "target_source_file_id, source_segment_ids_json, "
            "target_segment_ids_json, status, evidence_json, created_at, "
            "confirmed_at, revoked_at FROM alignment_manual_overrides "
            f"{where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
    finally:
        connection.close()


def read_alignment_recipe_rows(db_path: Path) -> list[sqlite3.Row]:
    """Read completed recipe rows from an existing index."""

    connection = connect_index(str(db_path))
    try:
        if not table_exists(connection, "alignment_runs"):
            return []
        return connection.execute(
            "SELECT document_group_id, pivot_source_file_id, "
            "target_source_file_id, algorithm, algorithm_version, "
            "parameters_json "
            "FROM alignment_runs WHERE status = 'completed' "
            "ORDER BY document_group_id, pivot_source_file_id, target_source_file_id"
        ).fetchall()
    finally:
        connection.close()


def alignment_database_file(connection: sqlite3.Connection) -> str:
    """Return the attached main index file for its model-cache location."""

    return str(connection.execute("PRAGMA database_list").fetchone()[2])


def recipe_sources_and_group_exist(
    connection: sqlite3.Connection, group_id: str, pivot_id: str, target_id: str
) -> bool:
    """Check whether both source files and their group survived a rebuild."""

    present = connection.execute(
        "SELECT COUNT(*) FROM source_files WHERE source_file_id IN (?, ?)",
        (pivot_id, target_id),
    ).fetchone()[0]
    group_present = connection.execute(
        "SELECT 1 FROM document_groups WHERE document_group_id = ?", (group_id,)
    ).fetchone()
    return present == 2 and group_present is not None


@contextmanager
def alignment_recipe_replace_transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Replace recipes in one immediate transaction, including regeneration."""

    connection = open_writable_index(Path(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        install_text_alignment_schema(connection)
        connection.execute("DELETE FROM alignment_runs")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def body_range_write_transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Publish segment sets created while reviewing both books in one transaction."""

    connection = open_writable_index(Path(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        install_text_alignment_schema(connection)
        yield connection
        connection.commit()
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        connection.rollback()
        raise
    finally:
        connection.close()


def pdf_anchor_rows(
    connection: sqlite3.Connection, source_id: str, segment_ids: Sequence[str]
) -> tuple[dict[str, int], dict[int, sqlite3.Row | None]]:
    """Return segment-to-physical-page links and their stored page payloads."""

    placeholders = ",".join("?" for _ in segment_ids)
    pages = {
        str(row["segment_id"]): int(row["page_index"])
        for row in connection.execute(
            "SELECT segment_id, MIN(pdf_page_index) AS page_index "
            f"FROM text_segment_spans WHERE source_file_id = ? AND segment_id IN ({placeholders}) "
            "GROUP BY segment_id",
            (source_id, *segment_ids),
        )
    }
    page_rows = {
        page_index: connection.execute(
            "SELECT payload_json FROM pdf_pages WHERE source_file_id = ? "
            "AND pdf_page_index = ? ORDER BY row_id LIMIT 1",
            (source_id, page_index),
        ).fetchone()
        for page_index in sorted(set(pages.values()))
    }
    return pages, page_rows


def paragraph_anchor_rows(
    connection: sqlite3.Connection, source_id: str, segment_ids: Sequence[str]
) -> tuple[dict[str, int], dict[int, sqlite3.Row | None]]:
    """Return segment-to-paragraph links and their stored publisher-page fields."""

    placeholders = ",".join("?" for _ in segment_ids)
    positions = {
        str(row["segment_id"]): int(row["paragraph_index"])
        for row in connection.execute(
            "SELECT segment_id, MIN(paragraph_index) AS paragraph_index "
            "FROM text_segment_paragraph_spans WHERE source_file_id = ? "
            f"AND segment_id IN ({placeholders}) GROUP BY segment_id",
            (source_id, *segment_ids),
        )
    }
    paragraph_rows = {
        paragraph_index: connection.execute(
            "SELECT page_display, page_source_type, payload_json FROM paragraphs "
            "WHERE source_file_id = ? AND paragraph_index = ? ORDER BY rowid LIMIT 1",
            (source_id, paragraph_index),
        ).fetchone()
        for paragraph_index in sorted(set(positions.values()))
    }
    return positions, paragraph_rows


def segment_set_owner(connection: sqlite3.Connection, segment_set_id: str) -> str | None:
    """Return the SourceFile owning one segment set."""

    row = connection.execute(
        "SELECT source_file_id FROM segment_sets WHERE segment_set_id = ?",
        (segment_set_id,),
    ).fetchone()
    return str(row["source_file_id"]) if row is not None else None


def segment_count(connection: sqlite3.Connection, segment_set_id: str) -> int:
    """Count segments in an indexed set."""

    return int(
        connection.execute(
            "SELECT COUNT(*) FROM text_segments WHERE segment_set_id = ?",
            (segment_set_id,),
        ).fetchone()[0]
    )


def first_segment_on_pdf_page(
    connection: sqlite3.Connection, segment_set_id: str, source_id: str, page_index: int
) -> int | None:
    """Find the first indexed segment on one physical PDF page."""

    row = connection.execute(
        "SELECT MIN(s.order_index) AS order_index FROM text_segment_spans p "
        "JOIN text_segments s ON s.segment_id = p.segment_id "
        "WHERE s.segment_set_id = ? AND p.source_file_id = ? "
        "AND p.pdf_page_index = ?",
        (segment_set_id, source_id, page_index),
    ).fetchone()
    return int(row["order_index"]) if row is not None and row["order_index"] is not None else None


def read_segment_window(
    connection: sqlite3.Connection, segment_set_id: str, offset: int, limit: int
) -> list[sqlite3.Row]:
    """Read one ordered window of already-indexed segments."""

    return connection.execute(
        "SELECT segment_id, order_index, text_raw FROM text_segments "
        "WHERE segment_set_id = ? AND order_index >= ? ORDER BY order_index LIMIT ?",
        (segment_set_id, offset, limit),
    ).fetchall()


@contextmanager
def generation_write_transaction(
    db_path: Path, *, install_schema: bool = False
) -> Iterator[sqlite3.Connection]:
    """Keep each preparation or publication phase in its original transaction."""

    connection = open_writable_index(Path(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        if install_schema:
            install_text_alignment_schema(connection)
        yield connection
        connection.commit()
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        connection.rollback()
        raise
    finally:
        connection.close()


def generation_source_row(connection: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    """Read a source's fields needed for alignment."""

    return connection.execute(
        "SELECT source_file_id, source_type, file_name, payload_json "
        "FROM source_files WHERE source_file_id = ?",
        (source_id,),
    ).fetchone()


def generation_page_rows(connection: sqlite3.Connection, source_id: str) -> list[sqlite3.Row]:
    """Read ordered PDF page payloads for segmentation."""

    return connection.execute(
        "SELECT pdf_page_index, payload_json FROM pdf_pages "
        "WHERE source_file_id = ? ORDER BY pdf_page_index, row_id",
        (source_id,),
    ).fetchall()


def generation_paragraph_rows(
    connection: sqlite3.Connection, source_id: str
) -> list[sqlite3.Row]:
    """Read ordered EPUB paragraph payloads for segmentation."""

    return connection.execute(
        "SELECT paragraph_id, paragraph_index, text_raw, payload_json "
        "FROM paragraphs WHERE source_file_id = ? ORDER BY paragraph_index, rowid",
        (source_id,),
    ).fetchall()


def existing_segment_set(
    connection: sqlite3.Connection,
    source_id: str,
    text_hash: str,
    segmenter: str,
    version: str,
) -> sqlite3.Row | None:
    """Find a reusable segment set for the exact source text and version."""

    return connection.execute(
        "SELECT segment_set_id FROM segment_sets WHERE source_file_id = ? "
        "AND source_text_hash = ? AND segmenter = ? AND segmenter_version = ?",
        (source_id, text_hash, segmenter, version),
    ).fetchone()


def segment_rows(connection: sqlite3.Connection, segment_set_id: str) -> list[sqlite3.Row]:
    """Read segment ids and text in original order."""

    return connection.execute(
        "SELECT segment_id, text_raw FROM text_segments "
        "WHERE segment_set_id = ? ORDER BY order_index",
        (segment_set_id,),
    ).fetchall()


def insert_segment_set(
    connection: sqlite3.Connection,
    segment_set_id: str,
    source_id: str,
    text_hash: str,
    segmenter: str,
    version: str,
    language_code: str,
    timestamp: str,
) -> None:
    """Create the set identity before inserting its segments."""

    connection.execute(
        "INSERT INTO segment_sets(segment_set_id, source_file_id, "
        "source_text_hash, segmenter, segmenter_version, language_code, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (segment_set_id, source_id, text_hash, segmenter, version, language_code, timestamp),
    )


def insert_segment_rows(
    connection: sqlite3.Connection,
    segments: Sequence[tuple[object, ...]],
    page_spans: Sequence[tuple[object, ...]],
    paragraph_spans: Sequence[tuple[object, ...]],
) -> None:
    """Store segments and exact source spans on the same connection."""

    connection.executemany(
        "INSERT INTO text_segments(segment_id, segment_set_id, order_index, text_raw) "
        "VALUES (?, ?, ?, ?)",
        segments,
    )
    if page_spans:
        connection.executemany(
            "INSERT INTO text_segment_spans(segment_id, source_file_id, "
            "pdf_page_index, page_char_start, page_char_end, span_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            page_spans,
        )
    if paragraph_spans:
        connection.executemany(
            "INSERT INTO text_segment_paragraph_spans(segment_id, source_file_id, "
            "paragraph_id, paragraph_index, paragraph_char_start, "
            "paragraph_char_end, span_order) VALUES (?, ?, ?, ?, ?, ?, ?)",
            paragraph_spans,
        )


def previous_segment_set_id(
    connection: sqlite3.Connection,
    source_id: str,
    segmenter: str,
    current_segment_set_id: str,
    version: str,
) -> str | None:
    """Find the latest previous-version set for vector reuse."""

    row = connection.execute(
        "SELECT segment_set_id FROM segment_sets "
        "WHERE source_file_id = ? AND segmenter = ? "
        "AND segment_set_id <> ? AND segmenter_version <> ? "
        "ORDER BY created_at DESC LIMIT 1",
        (source_id, segmenter, current_segment_set_id, version),
    ).fetchone()
    return str(row["segment_set_id"]) if row is not None else None


def segment_text_rows(connection: sqlite3.Connection, segment_set_id: str) -> list[sqlite3.Row]:
    """Read reusable text from a previous segment set."""

    return connection.execute(
        "SELECT text_raw FROM text_segments WHERE segment_set_id = ? "
        "ORDER BY order_index",
        (segment_set_id,),
    ).fetchall()


def generation_segment_language(
    connection: sqlite3.Connection, segment_set_id: str
) -> sqlite3.Row | None:
    """Read the language stored with a segment set."""

    return connection.execute(
        "SELECT language_code FROM segment_sets WHERE segment_set_id = ?",
        (segment_set_id,),
    ).fetchone()


def generation_group_exists(connection: sqlite3.Connection, group_id: str) -> bool:
    """Whether the requested document group exists."""

    return connection.execute(
        "SELECT 1 FROM document_groups WHERE document_group_id = ?", (group_id,)
    ).fetchone() is not None


def generation_group_members(connection: sqlite3.Connection, group_id: str) -> set[str]:
    """Return source ids belonging to the requested group."""

    return {
        str(row["source_file_id"])
        for row in connection.execute(
            "SELECT source_file_id FROM document_group_members "
            "WHERE document_group_id = ?",
            (group_id,),
        )
    }


def supersede_completed_runs(
    connection: sqlite3.Connection, group_id: str, pivot_id: str, target_id: str
) -> None:
    """Retire the previous completed pair before inserting its replacement."""

    connection.execute(
        "UPDATE alignment_runs SET status = 'superseded' "
        "WHERE document_group_id = ? AND pivot_source_file_id = ? "
        "AND target_source_file_id = ? AND status = 'completed'",
        (group_id, pivot_id, target_id),
    )


def insert_alignment_run(connection: sqlite3.Connection, values: tuple[object, ...]) -> None:
    """Store one completed alignment run and its parameters."""

    connection.execute(
        "INSERT INTO alignment_runs(alignment_run_id, document_group_id, "
        "pivot_source_file_id, target_source_file_id, pivot_segment_set_id, "
        "target_segment_set_id, algorithm, algorithm_version, parameters_json, "
        "status, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        values,
    )


def insert_alignment_links(
    connection: sqlite3.Connection,
    links: Sequence[tuple[object, ...]],
    members: Sequence[tuple[object, ...]],
) -> None:
    """Store links and their ordered segment membership."""

    connection.executemany(
        "INSERT INTO alignment_links(alignment_link_id, alignment_run_id, "
        "order_index, cost, confidence, anchor_key, review_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        links,
    )
    connection.executemany(
        "INSERT INTO alignment_link_members(alignment_link_id, side, "
        "segment_id, member_order) VALUES (?, ?, ?, ?)",
        members,
    )


def reviewed_body_parameters(
    connection: sqlite3.Connection, pivot_set_id: str, target_set_id: str
) -> str | None:
    """Load parameters from the latest reviewed body-range run."""

    row = connection.execute(
        "SELECT parameters_json FROM alignment_runs WHERE pivot_segment_set_id=? "
        "AND target_segment_set_id=? AND json_extract(parameters_json,'$.body_range_source')='reviewed' "
        "ORDER BY created_at DESC LIMIT 1",
        (pivot_set_id, target_set_id),
    ).fetchone()
    return str(row[0]) if row is not None else None


def completed_run_candidates(
    connection: sqlite3.Connection,
    group_id: str,
    pivot_id: str,
    target_id: str,
    pivot_set_id: str,
    target_set_id: str,
    algorithm: str,
    version: str,
) -> list[sqlite3.Row]:
    """Load completed pair runs eligible for exact-configuration reuse."""

    return connection.execute(
        "SELECT alignment_run_id, parameters_json FROM alignment_runs "
        "WHERE document_group_id = ? AND pivot_source_file_id = ? "
        "AND target_source_file_id = ? AND pivot_segment_set_id = ? "
        "AND target_segment_set_id = ? AND algorithm = ? "
        "AND algorithm_version = ? AND status = 'completed' "
        "AND NOT EXISTS (SELECT 1 FROM alignment_links l "
        "WHERE l.alignment_run_id = alignment_runs.alignment_run_id "
        "AND l.confidence IS NULL) "
        "ORDER BY completed_at DESC, rowid DESC",
        (group_id, pivot_id, target_id, pivot_set_id, target_set_id, algorithm, version),
    ).fetchall()


def completed_run_status_counts(
    connection: sqlite3.Connection, run_id: str
) -> dict[str, int]:
    """Count stored review statuses for a reused pair run."""

    return {
        str(row["review_status"]): int(row["link_count"])
        for row in connection.execute(
            "SELECT review_status, COUNT(*) AS link_count "
            "FROM alignment_links WHERE alignment_run_id = ? "
            "GROUP BY review_status",
            (run_id,),
        )
    }


def selection_pdf_segment_ids(
    connection: sqlite3.Connection,
    segment_set_id: str,
    source_id: str,
    start_page: int,
    end_page: int,
    start_offset: int,
    end_offset: int,
) -> List[str]:
    if start_page == end_page:
        rows = connection.execute(
            "SELECT DISTINCT s.segment_id, s.order_index FROM text_segments s "
            "JOIN text_segment_spans p ON p.segment_id = s.segment_id "
            "WHERE s.segment_set_id = ? AND p.source_file_id = ? "
            "AND p.pdf_page_index = ? AND p.page_char_end > ? "
            "AND p.page_char_start < ? ORDER BY s.order_index",
            (
                segment_set_id,
                source_id,
                start_page,
                start_offset,
                end_offset,
            ),
        ).fetchall()
        return [str(row["segment_id"]) for row in rows]
    rows = connection.execute(
        "SELECT DISTINCT s.segment_id, s.order_index FROM text_segments s "
        "JOIN text_segment_spans p ON p.segment_id = s.segment_id "
        "WHERE s.segment_set_id = ? AND p.source_file_id = ? AND ("
        "(p.pdf_page_index = ? AND p.page_char_end > ?) OR "
        "(p.pdf_page_index > ? AND p.pdf_page_index < ?) OR "
        "(p.pdf_page_index = ? AND p.page_char_start < ?)) "
        "ORDER BY s.order_index",
        (
            segment_set_id,
            source_id,
            start_page,
            start_offset,
            start_page,
            end_page,
            end_page,
            end_offset,
        ),
    ).fetchall()
    return [str(row["segment_id"]) for row in rows]


def selection_paragraph_segment_ids(
    connection: sqlite3.Connection,
    segment_set_id: str,
    source_id: str,
    start_paragraph: int,
    end_paragraph: int,
    start_offset: int,
    end_offset: int,
) -> List[str]:
    if start_paragraph == end_paragraph:
        rows = connection.execute(
            "SELECT DISTINCT s.segment_id, s.order_index FROM text_segments s "
            "JOIN text_segment_paragraph_spans p ON p.segment_id = s.segment_id "
            "WHERE s.segment_set_id = ? AND p.source_file_id = ? "
            "AND p.paragraph_index = ? AND p.paragraph_char_end > ? "
            "AND p.paragraph_char_start < ? ORDER BY s.order_index",
            (
                segment_set_id,
                source_id,
                start_paragraph,
                start_offset,
                end_offset,
            ),
        ).fetchall()
        return [str(row["segment_id"]) for row in rows]
    rows = connection.execute(
        "SELECT DISTINCT s.segment_id, s.order_index FROM text_segments s "
        "JOIN text_segment_paragraph_spans p ON p.segment_id = s.segment_id "
        "WHERE s.segment_set_id = ? AND p.source_file_id = ? AND ("
        "(p.paragraph_index = ? AND p.paragraph_char_end > ?) OR "
        "(p.paragraph_index > ? AND p.paragraph_index < ?) OR "
        "(p.paragraph_index = ? AND p.paragraph_char_start < ?)) "
        "ORDER BY s.order_index",
        (
            segment_set_id,
            source_id,
            start_paragraph,
            start_offset,
            start_paragraph,
            end_paragraph,
            end_paragraph,
            end_offset,
        ),
    ).fetchall()
    return [str(row["segment_id"]) for row in rows]


def latest_pair_run(
    connection: sqlite3.Connection,
    document_group_id: str,
    left_source_id: str,
    right_source_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM alignment_runs WHERE document_group_id = ? "
        "AND status = 'completed' AND "
        "((pivot_source_file_id = ? AND target_source_file_id = ?) OR "
        "(pivot_source_file_id = ? AND target_source_file_id = ?)) "
        "ORDER BY completed_at DESC, rowid DESC LIMIT 1",
        (
            document_group_id,
            left_source_id,
            right_source_id,
            right_source_id,
            left_source_id,
        ),
    ).fetchone()


def target_source_row(connection: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    """Read the source fields needed to list alignment targets."""

    return connection.execute(
        "SELECT source_type, payload_json FROM source_files WHERE source_file_id = ?",
        (source_id,),
    ).fetchone()


def target_group_for_source(
    connection: sqlite3.Connection, source_id: str
) -> sqlite3.Row | None:
    """Find the group and pivot containing a source."""

    return connection.execute(
        "SELECT g.document_group_id, g.base_source_file_id "
        "FROM document_group_members m JOIN document_groups g "
        "ON g.document_group_id = m.document_group_id "
        "WHERE m.source_file_id = ?",
        (source_id,),
    ).fetchone()


def target_group_members(
    connection: sqlite3.Connection, group_id: str
) -> list[sqlite3.Row]:
    """Read ordered source metadata for a group's target list."""

    return connection.execute(
        "SELECT m.source_file_id, m.version_label, s.source_type, "
        "s.file_name, s.payload_json "
        "FROM document_group_members m JOIN source_files s "
        "ON s.source_file_id = m.source_file_id "
        "WHERE m.document_group_id = ? ORDER BY m.member_order",
        (group_id,),
    ).fetchall()


def body_selected_orders(
    connection: sqlite3.Connection, segment_ids: Sequence[str]
) -> list[sqlite3.Row]:
    """Read the order of each selected segment for body-range validation."""

    return connection.execute(
        "SELECT order_index FROM text_segments WHERE segment_id IN ("
        + ",".join("?" for _ in segment_ids) + ")",
        tuple(segment_ids),
    ).fetchall()


def body_segment_texts(
    connection: sqlite3.Connection, segment_set_id: str
) -> list[sqlite3.Row]:
    """Read the text sequence used to recheck a detected body range."""

    return connection.execute(
        "SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index",
        (segment_set_id,),
    ).fetchall()


def links_for_segments(
    connection: sqlite3.Connection,
    run_id: str,
    side: str,
    segment_ids: Sequence[str],
) -> list[sqlite3.Row]:
    """Read alignment links touching the selected source segments."""

    placeholders = ",".join("?" for _ in segment_ids)
    return connection.execute(
        "SELECT DISTINCT l.alignment_link_id, l.order_index, l.review_status "
        "FROM alignment_links l JOIN alignment_link_members m "
        "ON m.alignment_link_id = l.alignment_link_id "
        f"WHERE l.alignment_run_id = ? AND m.side = ? AND m.segment_id IN ({placeholders}) "
        "ORDER BY l.order_index",
        (run_id, side, *segment_ids),
    ).fetchall()


def link_members_for_links(
    connection: sqlite3.Connection, link_ids: Sequence[str]
) -> list[sqlite3.Row]:
    """Read the ordered members of selected alignment links."""

    placeholders = ",".join("?" for _ in link_ids)
    return connection.execute(
        "SELECT m.alignment_link_id, m.side, m.segment_id, s.order_index "
        "FROM alignment_link_members m "
        "JOIN text_segments s ON s.segment_id = m.segment_id "
        f"WHERE m.alignment_link_id IN ({placeholders}) "
        "ORDER BY m.alignment_link_id, s.order_index",
        link_ids,
    ).fetchall()


def selected_segment_orders(
    connection: sqlite3.Connection, segment_ids: Sequence[str]
) -> list[sqlite3.Row]:
    """Read selected segment orders for semantic paragraph fallback."""

    placeholders = ",".join("?" for _ in segment_ids)
    return connection.execute(
        "SELECT order_index FROM text_segments "
        f"WHERE segment_id IN ({placeholders}) ORDER BY order_index",
        tuple(segment_ids),
    ).fetchall()


def semantic_segment_rows(
    connection: sqlite3.Connection, segment_set_id: str
) -> list[sqlite3.Row]:
    """Read a full segment sequence for semantic paragraph fallback."""

    return connection.execute(
        "SELECT segment_id, order_index, text_raw FROM text_segments "
        "WHERE segment_set_id = ? ORDER BY order_index",
        (segment_set_id,),
    ).fetchall()


def selected_segment_first_order(
    connection: sqlite3.Connection, segment_ids: Sequence[str]
) -> sqlite3.Row | None:
    """Find the first selected order for paragraph-anchor fallback."""

    placeholders = ",".join("?" for _ in segment_ids)
    return connection.execute(
        "SELECT MIN(order_index) AS first_order FROM text_segments "
        f"WHERE segment_id IN ({placeholders})",
        tuple(segment_ids),
    ).fetchone()


def paragraph_anchor_segment(
    connection: sqlite3.Connection, segment_set_id: str, order_index: int
) -> sqlite3.Row | None:
    """Read the segment at a matched paragraph heading anchor."""

    return connection.execute(
        "SELECT segment_id FROM text_segments WHERE segment_set_id = ? "
        "AND order_index = ?",
        (segment_set_id, order_index),
    ).fetchone()


def ordered_segments_in_set(
    connection: sqlite3.Connection,
    segment_set_id: str,
    segment_ids: Sequence[str],
) -> List[sqlite3.Row]:
    unique_ids = list(dict.fromkeys(str(segment_id) for segment_id in segment_ids))
    if not unique_ids:
        return []
    placeholders = ",".join("?" for _ in unique_ids)
    return connection.execute(
        "SELECT segment_id, order_index, text_raw FROM text_segments "
        f"WHERE segment_set_id = ? AND segment_id IN ({placeholders}) "
        "ORDER BY order_index",
        (segment_set_id, *unique_ids),
    ).fetchall()


def candidate_aligned_rows(
    connection: sqlite3.Connection, segment_set_id: str, segment_ids: Sequence[str]
) -> list[sqlite3.Row]:
    """Read the aligned segment positions anchoring a candidate window."""

    placeholders = ",".join("?" for _ in segment_ids)
    return connection.execute(
        "SELECT segment_id, order_index FROM text_segments "
        f"WHERE segment_set_id = ? AND segment_id IN ({placeholders}) "
        "ORDER BY order_index",
        (segment_set_id, *segment_ids),
    ).fetchall()


def candidate_window_rows(
    connection: sqlite3.Connection, segment_set_id: str, start: int, end: int
) -> list[sqlite3.Row]:
    """Read candidates and one neighboring segment on each side."""

    return connection.execute(
        "SELECT segment_id, order_index, text_raw FROM text_segments "
        "WHERE segment_set_id = ? AND order_index BETWEEN ? AND ? "
        "ORDER BY order_index",
        (segment_set_id, start, end),
    ).fetchall()


def candidate_pdf_spans(
    connection: sqlite3.Connection, segment_ids: Sequence[str]
) -> list[sqlite3.Row]:
    """Read precise PDF spans for the candidate segments."""

    placeholders = ",".join("?" for _ in segment_ids)
    return connection.execute(
        "SELECT p.segment_id, p.pdf_page_index, p.page_char_start, "
        "p.page_char_end, s.order_index, p.span_order "
        "FROM text_segment_spans p JOIN text_segments s "
        "ON s.segment_id = p.segment_id "
        f"WHERE p.segment_id IN ({placeholders}) "
        "ORDER BY s.order_index, p.span_order",
        segment_ids,
    ).fetchall()


def candidate_page_rows(
    connection: sqlite3.Connection, source_id: str, page_indices: Sequence[int]
) -> list[sqlite3.Row]:
    """Read PDF page payloads used to materialize candidate spans."""

    placeholders = ",".join("?" for _ in page_indices)
    return connection.execute(
        "SELECT pdf_page_index, payload_json FROM pdf_pages "
        f"WHERE source_file_id = ? AND pdf_page_index IN ({placeholders})",
        (source_id, *page_indices),
    ).fetchall()


def candidate_paragraph_spans(
    connection: sqlite3.Connection, segment_ids: Sequence[str]
) -> list[sqlite3.Row]:
    """Read precise EPUB paragraph spans for the candidate segments."""

    placeholders = ",".join("?" for _ in segment_ids)
    return connection.execute(
        "SELECT p.segment_id, p.paragraph_id, p.paragraph_index, "
        "p.paragraph_char_start, p.paragraph_char_end, s.order_index, "
        "p.span_order FROM text_segment_paragraph_spans p "
        "JOIN text_segments s ON s.segment_id = p.segment_id "
        f"WHERE p.segment_id IN ({placeholders}) "
        "ORDER BY s.order_index, p.span_order",
        segment_ids,
    ).fetchall()


def candidate_paragraph_rows(
    connection: sqlite3.Connection, source_id: str, paragraph_indices: Sequence[int]
) -> list[sqlite3.Row]:
    """Read EPUB paragraph payloads used to materialize candidate spans."""

    placeholders = ",".join("?" for _ in paragraph_indices)
    return connection.execute(
        "SELECT paragraph_id, paragraph_index, text_raw, payload_json "
        "FROM paragraphs WHERE source_file_id = ? "
        f"AND paragraph_index IN ({placeholders})",
        (source_id, *paragraph_indices),
    ).fetchall()


def direct_alignment_run(
    connection: sqlite3.Connection, source_id: str, target_id: str
) -> sqlite3.Row | None:
    """Find the latest completed direct alignment for two sources."""

    return connection.execute(
        "SELECT * FROM alignment_runs WHERE status = 'completed' AND "
        "((pivot_source_file_id = ? AND target_source_file_id = ?) OR "
        "(pivot_source_file_id = ? AND target_source_file_id = ?)) "
        "ORDER BY completed_at DESC, rowid DESC LIMIT 1",
        (source_id, target_id, target_id, source_id),
    ).fetchone()


def route_group_for_pair(
    connection: sqlite3.Connection, source_id: str, target_id: str
) -> sqlite3.Row | None:
    """Find the shared document group and pivot for a non-direct route."""

    return connection.execute(
        "SELECT g.document_group_id, g.base_source_file_id "
        "FROM document_groups g "
        "JOIN document_group_members source_member "
        "ON source_member.document_group_id = g.document_group_id "
        "JOIN document_group_members target_member "
        "ON target_member.document_group_id = g.document_group_id "
        "WHERE source_member.source_file_id = ? "
        "AND target_member.source_file_id = ?",
        (source_id, target_id),
    ).fetchone()


def confirmed_override_rows(
    connection: sqlite3.Connection,
    source_id: str,
    target_id: str,
    source_set_id: str,
    target_set_id: str,
) -> list[sqlite3.Row]:
    """Read confirmed corrections, newest first, for a specific segment-set pair."""

    return connection.execute(
        "SELECT override_id, source_segment_key, source_segment_ids_json, "
        "target_segment_ids_json, evidence_json FROM alignment_manual_overrides "
        "WHERE source_file_id = ? AND target_source_file_id = ? "
        "AND source_segment_set_id = ? AND target_segment_set_id = ? "
        "AND status = 'confirmed' ORDER BY confirmed_at DESC, override_id",
        (source_id, target_id, source_set_id, target_set_id),
    ).fetchall()


def endpoint_paragraph_rows(
    connection: sqlite3.Connection, source_id: str, paragraph_indices: Sequence[int]
) -> list[sqlite3.Row]:
    """Read selected EPUB paragraphs before validating selection offsets."""

    placeholders = ",".join("?" for _ in paragraph_indices)
    return connection.execute(
        "SELECT paragraph_index, text_raw FROM paragraphs "
        f"WHERE source_file_id = ? AND paragraph_index IN ({placeholders})",
        (source_id, *paragraph_indices),
    ).fetchall()


def target_pdf_spans(
    connection: sqlite3.Connection, segment_ids: Sequence[str]
) -> list[sqlite3.Row]:
    """Read the final PDF spans for located target segments."""

    placeholders = ",".join("?" for _ in segment_ids)
    return connection.execute(
        "SELECT p.pdf_page_index, p.page_char_start, p.page_char_end, "
        "s.order_index, p.span_order FROM text_segment_spans p "
        "JOIN text_segments s ON s.segment_id = p.segment_id "
        f"WHERE p.segment_id IN ({placeholders}) "
        "ORDER BY s.order_index, p.span_order",
        segment_ids,
    ).fetchall()


def target_paragraph_spans(
    connection: sqlite3.Connection, segment_ids: Sequence[str]
) -> list[sqlite3.Row]:
    """Read the final EPUB spans for located target segments."""

    placeholders = ",".join("?" for _ in segment_ids)
    return connection.execute(
        "SELECT p.paragraph_id, p.paragraph_index, "
        "p.paragraph_char_start, p.paragraph_char_end, "
        "s.order_index, p.span_order FROM text_segment_paragraph_spans p "
        "JOIN text_segments s ON s.segment_id = p.segment_id "
        f"WHERE p.segment_id IN ({placeholders}) "
        "ORDER BY s.order_index, p.span_order",
        segment_ids,
    ).fetchall()

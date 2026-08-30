"""Additive index-schema installers owned by the persistence layer.

These functions create (and additively migrate) the document-group and
text-alignment tables on an already-open connection.  They live here — not in
the domain modules that use them — so that :mod:`persistence.migrations` can
depend *downward* on schema instead of importing the domain modules upward.
The domain modules import these installers from persistence; migrations invoke
them directly.  Kept idempotent so both runtime setup and migration can call
them safely.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def install_document_group_schema(connection: sqlite3.Connection) -> bool:
    """Create the two group tables on an open connection if absent (idempotent)."""

    changed = False
    members_table_missing = not _table_exists(connection, "document_group_members")
    if not _table_exists(connection, "document_groups"):
        connection.execute(
            """
            CREATE TABLE document_groups (
                document_group_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                base_source_file_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        changed = True
    else:
        # An index migrated under the (reverted) 837d808 folders+groups feature
        # already has a document_groups table WITHOUT base_source_file_id (837 kept
        # membership as a source_files column, not a base pointer). Add it additively.
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(document_groups)")
        }
        if "base_source_file_id" not in columns:
            connection.execute(
                "ALTER TABLE document_groups ADD COLUMN base_source_file_id TEXT"
            )
            changed = True
    if members_table_missing:
        connection.execute(
            """
            CREATE TABLE document_group_members (
                document_group_id TEXT NOT NULL
                    REFERENCES document_groups(document_group_id) ON DELETE CASCADE,
                source_file_id TEXT NOT NULL UNIQUE,
                version_label TEXT,
                member_order INTEGER NOT NULL DEFAULT 0,
                added_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_group_members_group "
            "ON document_group_members(document_group_id)"
        )
        changed = True
        source_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(source_files)")
        }
        if "document_group_id" in source_columns:
            legacy_members = connection.execute(
                "SELECT s.source_file_id, s.document_group_id "
                "FROM source_files s JOIN document_groups g "
                "ON g.document_group_id = s.document_group_id "
                "WHERE s.document_group_id IS NOT NULL "
                "AND TRIM(s.document_group_id) <> '' "
                "ORDER BY s.document_group_id, s.source_file_id"
            ).fetchall()
            timestamp = _now()
            member_orders: dict[str, int] = {}
            for source_file_id, document_group_id in legacy_members:
                member_order = member_orders.get(document_group_id, 0)
                connection.execute(
                    "INSERT INTO document_group_members"
                    "(document_group_id, source_file_id, version_label, "
                    "member_order, added_at) VALUES (?, ?, NULL, ?, ?)",
                    (document_group_id, source_file_id, member_order, timestamp),
                )
                member_orders[document_group_id] = member_order + 1
    return changed


def install_text_alignment_schema(connection: sqlite3.Connection) -> bool:
    """Install the additive segmentation/alignment tables."""

    changed = False
    if not _table_exists(connection, "segment_sets"):
        statements = (
        """
        CREATE TABLE segment_sets (
            segment_set_id TEXT PRIMARY KEY,
            source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id) ON DELETE CASCADE,
            source_text_hash TEXT NOT NULL,
            segmenter TEXT NOT NULL,
            segmenter_version TEXT NOT NULL,
            language_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_file_id, source_text_hash, segmenter, segmenter_version)
        )
        """,
        """
        CREATE TABLE text_segments (
            segment_id TEXT PRIMARY KEY,
            segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
            order_index INTEGER NOT NULL,
            text_raw TEXT NOT NULL,
            UNIQUE(segment_set_id, order_index)
        )
        """,
        """
        CREATE TABLE text_segment_spans (
            segment_id TEXT NOT NULL REFERENCES text_segments(segment_id) ON DELETE CASCADE,
            source_file_id TEXT NOT NULL,
            pdf_page_index INTEGER NOT NULL,
            page_char_start INTEGER NOT NULL,
            page_char_end INTEGER NOT NULL,
            span_order INTEGER NOT NULL,
            PRIMARY KEY(segment_id, span_order)
        )
        """,
        """
        CREATE TABLE alignment_runs (
            alignment_run_id TEXT PRIMARY KEY,
            document_group_id TEXT NOT NULL REFERENCES document_groups(document_group_id) ON DELETE CASCADE,
            pivot_source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id) ON DELETE CASCADE,
            target_source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id) ON DELETE CASCADE,
            pivot_segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
            target_segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
            algorithm TEXT NOT NULL,
            algorithm_version TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """,
        """
        CREATE TABLE alignment_links (
            alignment_link_id TEXT PRIMARY KEY,
            alignment_run_id TEXT NOT NULL REFERENCES alignment_runs(alignment_run_id) ON DELETE CASCADE,
            order_index INTEGER NOT NULL,
            cost REAL NOT NULL,
            review_status TEXT NOT NULL,
            UNIQUE(alignment_run_id, order_index)
        )
        """,
        """
        CREATE TABLE alignment_link_members (
            alignment_link_id TEXT NOT NULL REFERENCES alignment_links(alignment_link_id) ON DELETE CASCADE,
            side TEXT NOT NULL CHECK(side IN ('pivot', 'target')),
            segment_id TEXT NOT NULL REFERENCES text_segments(segment_id) ON DELETE CASCADE,
            member_order INTEGER NOT NULL,
            PRIMARY KEY(alignment_link_id, side, member_order),
            UNIQUE(alignment_link_id, segment_id)
        )
        """,
        "CREATE INDEX idx_segment_sets_source ON segment_sets(source_file_id)",
        "CREATE INDEX idx_segment_spans_source_page ON text_segment_spans(source_file_id, pdf_page_index, page_char_start, page_char_end)",
        "CREATE INDEX idx_alignment_runs_pair ON alignment_runs(document_group_id, pivot_source_file_id, target_source_file_id, status)",
        "CREATE INDEX idx_alignment_members_segment ON alignment_link_members(segment_id, side)",
        )
        for statement in statements:
            connection.execute(statement)
        changed = True
    if not _table_exists(connection, "text_segment_paragraph_spans"):
        connection.execute(
            "CREATE TABLE text_segment_paragraph_spans ("
            "segment_id TEXT NOT NULL REFERENCES text_segments(segment_id) ON DELETE CASCADE, "
            "source_file_id TEXT NOT NULL, paragraph_id TEXT NOT NULL, "
            "paragraph_index INTEGER NOT NULL, paragraph_char_start INTEGER NOT NULL, "
            "paragraph_char_end INTEGER NOT NULL, span_order INTEGER NOT NULL, "
            "PRIMARY KEY(segment_id, span_order))"
        )
        changed = True
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_segment_paragraph_spans_source_position "
        "ON text_segment_paragraph_spans(source_file_id, paragraph_index, "
        "paragraph_char_start, paragraph_char_end)"
    )
    return changed

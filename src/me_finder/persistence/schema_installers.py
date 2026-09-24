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

from .connection import table_exists


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def install_document_group_schema(connection: sqlite3.Connection) -> bool:
    """Create the two group tables on an open connection if absent (idempotent)."""

    changed = False
    members_table_missing = not table_exists(connection, "document_group_members")
    if not table_exists(connection, "document_groups"):
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
    if not table_exists(connection, "segment_sets"):
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
            confidence REAL,
            anchor_key TEXT,
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
    if table_exists(connection, "alignment_links"):
        alignment_link_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(alignment_links)")
        }
        if "confidence" not in alignment_link_columns:
            connection.execute("ALTER TABLE alignment_links ADD COLUMN confidence REAL")
            changed = True
        if "anchor_key" not in alignment_link_columns:
            connection.execute("ALTER TABLE alignment_links ADD COLUMN anchor_key TEXT")
            changed = True
    if not table_exists(connection, "text_segment_paragraph_spans"):
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
    if not table_exists(connection, "alignment_manual_overrides"):
        # Human-confirmed corrections to the automatic cross-version mapping.
        # A row is written as ``pending`` by an agent proposal and only starts
        # affecting reads after the user confirms it (``confirmed``); it can be
        # reverted (``revoked``) at any time.  Everything is keyed on segment
        # ids, so a re-segmentation cascade-deletes stale overrides.
        connection.execute(
            """
            CREATE TABLE alignment_manual_overrides (
                override_id TEXT PRIMARY KEY,
                document_group_id TEXT NOT NULL REFERENCES document_groups(document_group_id) ON DELETE CASCADE,
                source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id) ON DELETE CASCADE,
                target_source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id) ON DELETE CASCADE,
                source_segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
                target_segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
                source_segment_key TEXT NOT NULL,
                source_segment_ids_json TEXT NOT NULL,
                target_segment_ids_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'revoked')),
                confirmation_token TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                revoked_at TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_manual_overrides_lookup ON alignment_manual_overrides("
            "source_file_id, target_source_file_id, source_segment_set_id, "
            "source_segment_key, status)"
        )
        # At most one active correction per source selection and segment-set.
        connection.execute(
            "CREATE UNIQUE INDEX idx_manual_overrides_active ON "
            "alignment_manual_overrides(source_file_id, target_source_file_id, "
            "source_segment_set_id, source_segment_key) WHERE status = 'confirmed'"
        )
        changed = True
    return changed


def install_translation_workspace_schema(connection: sqlite3.Connection) -> bool:
    """Install the v7 tables behind the translation-comparison workspace.

    * ``document_group_reading_positions`` — the last version pair and anchor a
      user read inside one work ("继续阅读"). Anchors are item index + code-point
      offset in the left version, so resuming never invents a page number.
    * ``alignment_review_deferrals`` — low-confidence links the user chose to
      leave for later ("暂不处理"). Keyed like manual overrides, so a
      re-segmentation cascade-deletes them.
    * ``document_group_suggestion_dismissals`` — same-title grouping suggestions
      the user rejected ("不是同一作品"), keyed on the sorted source-id set.
    """

    changed = False
    if not table_exists(connection, "document_group_reading_positions"):
        connection.execute(
            """
            CREATE TABLE document_group_reading_positions (
                document_group_id TEXT PRIMARY KEY
                    REFERENCES document_groups(document_group_id) ON DELETE CASCADE,
                left_source_file_id TEXT NOT NULL
                    REFERENCES source_files(source_file_id) ON DELETE CASCADE,
                right_source_file_id TEXT
                    REFERENCES source_files(source_file_id) ON DELETE SET NULL,
                item_index INTEGER NOT NULL,
                char_offset INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        changed = True
    if not table_exists(connection, "alignment_review_deferrals"):
        connection.execute(
            """
            CREATE TABLE alignment_review_deferrals (
                source_file_id TEXT NOT NULL
                    REFERENCES source_files(source_file_id) ON DELETE CASCADE,
                target_source_file_id TEXT NOT NULL
                    REFERENCES source_files(source_file_id) ON DELETE CASCADE,
                source_segment_set_id TEXT NOT NULL
                    REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
                source_segment_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(source_file_id, target_source_file_id,
                            source_segment_set_id, source_segment_key)
            )
            """
        )
        changed = True
    if not table_exists(connection, "document_group_suggestion_dismissals"):
        connection.execute(
            """
            CREATE TABLE document_group_suggestion_dismissals (
                suggestion_key TEXT PRIMARY KEY,
                source_file_ids_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        changed = True
    return changed


def install_zotero_sync_schema(connection: sqlite3.Connection) -> bool:
    """Install the v8 tables that link Zotero items to MEFinder documents.

    Zotero stays the source of truth for collections, items and files; these
    tables only remember what MEFinder last saw so a sync can diff against it.

    * ``zotero_items`` — one row per top-level Zotero item in scope: its local
      API version, a fingerprint of the bibliographic fields, the collection
      keys Zotero reported and which fingerprint was last written into the
      MEFinder metadata.
    * ``zotero_attachments`` — one row per PDF/EPUB attachment. Each attachment
      is its own MEFinder document; ``parent_item_key`` records the item.
      ``origin`` separates documents the sync imported from documents that were
      already in the library and were only linked by content hash.
    * ``zotero_sync_state`` — per-library bookkeeping (server id, last result,
      the attachment version index used for incremental reads).

    No foreign key points at ``source_files``: when a linked document goes
    missing the row must survive so the next sync can notice and repair it.
    """

    changed = False
    if not table_exists(connection, "zotero_items"):
        connection.execute(
            """
            CREATE TABLE zotero_items (
                library_id TEXT NOT NULL,
                item_key TEXT NOT NULL,
                item_version INTEGER,
                fingerprint TEXT NOT NULL,
                data_json TEXT NOT NULL,
                collections_json TEXT NOT NULL,
                metadata_fingerprint_applied TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(library_id, item_key)
            )
            """
        )
        changed = True
    if not table_exists(connection, "zotero_attachments"):
        connection.execute(
            """
            CREATE TABLE zotero_attachments (
                library_id TEXT NOT NULL,
                attachment_key TEXT NOT NULL,
                parent_item_key TEXT NOT NULL,
                attachment_version INTEGER,
                link_mode TEXT NOT NULL,
                content_type TEXT,
                file_name TEXT,
                file_signature TEXT,
                file_sha256 TEXT,
                source_file_id TEXT,
                origin TEXT NOT NULL DEFAULT 'imported'
                    CHECK(origin IN ('imported', 'linked_existing')),
                status TEXT NOT NULL
                    CHECK(status IN ('pending', 'linked', 'unavailable', 'failed')),
                import_job_id TEXT,
                replaces_source_file_id TEXT,
                status_message TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(library_id, attachment_key)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_zotero_attachments_source "
            "ON zotero_attachments(source_file_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_zotero_attachments_parent "
            "ON zotero_attachments(library_id, parent_item_key)"
        )
        changed = True
    if not table_exists(connection, "zotero_sync_state"):
        connection.execute(
            """
            CREATE TABLE zotero_sync_state (
                library_id TEXT PRIMARY KEY,
                server_id TEXT,
                synced_collections_json TEXT NOT NULL DEFAULT '[]',
                attachment_index_json TEXT NOT NULL DEFAULT '{}',
                last_attempt_at TEXT,
                last_success_at TEXT,
                last_result_json TEXT
            )
            """
        )
        changed = True
    return changed

"""SQLite writes for the translation-comparison workspace."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .connection import open_writable_index
from .schema_installers import (
    install_document_group_schema,
    install_text_alignment_schema,
    install_translation_workspace_schema,
)


@contextmanager
def workspace_write_transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Install workspace tables and keep each write in one immediate transaction."""

    connection = open_writable_index(Path(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        install_document_group_schema(connection)
        install_text_alignment_schema(connection)
        install_translation_workspace_schema(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def save_reading_position_row(
    connection: sqlite3.Connection,
    group_id: str,
    left_id: str,
    right_id: str | None,
    index: int,
    offset: int,
    timestamp: str,
) -> None:
    """Upsert the last reader position for a document group."""

    connection.execute(
        "INSERT INTO document_group_reading_positions(document_group_id, "
        "left_source_file_id, right_source_file_id, item_index, char_offset, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(document_group_id) DO UPDATE SET "
        "left_source_file_id = excluded.left_source_file_id, "
        "right_source_file_id = excluded.right_source_file_id, "
        "item_index = excluded.item_index, char_offset = excluded.char_offset, "
        "updated_at = excluded.updated_at",
        (group_id, left_id, right_id, index, offset, timestamp),
    )


def dismiss_suggestion_row(
    connection: sqlite3.Connection,
    key: str,
    source_file_ids_json: str,
    timestamp: str,
) -> None:
    """Store a same-title suggestion dismissal once."""

    connection.execute(
        "INSERT OR IGNORE INTO document_group_suggestion_dismissals("
        "suggestion_key, source_file_ids_json, created_at) VALUES (?, ?, ?)",
        (key, source_file_ids_json, timestamp),
    )


def defer_review_row(
    connection: sqlite3.Connection,
    source_id: str,
    target_id: str,
    source_set_id: str,
    key: str,
    timestamp: str,
) -> None:
    """Mark a translation link for later review."""

    connection.execute(
        "INSERT OR IGNORE INTO alignment_review_deferrals(source_file_id, "
        "target_source_file_id, source_segment_set_id, source_segment_key, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (source_id, target_id, source_set_id, key, timestamp),
    )


def clear_review_deferral_row(
    connection: sqlite3.Connection,
    source_id: str,
    target_id: str,
    source_set_id: str,
    key: str,
) -> None:
    """Clear a review deferral after a manual correction."""

    connection.execute(
        "DELETE FROM alignment_review_deferrals WHERE source_file_id = ? "
        "AND target_source_file_id = ? AND source_segment_set_id = ? "
        "AND source_segment_key = ?",
        (source_id, target_id, source_set_id, key),
    )

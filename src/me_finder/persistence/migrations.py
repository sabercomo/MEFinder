"""Transactional registry for additive index-schema migrations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .connection import open_writable_index
from .index_schema import DATABASE_SCHEMA_VERSION
from .schema_installers import (
    install_document_group_schema,
    install_text_alignment_schema,
)


MigrationStep = Callable[[sqlite3.Connection], bool]


@dataclass(frozen=True)
class Migration:
    target_version: int
    apply: MigrationStep


def _install_text_segment_paragraph_spans(
    connection: sqlite3.Connection,
) -> bool:
    table_name = "text_segment_paragraph_spans"
    index_name = "idx_segment_paragraph_spans_source_position"
    changed = not _table_exists(connection, table_name) or not _index_exists(
        connection, index_name
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS text_segment_paragraph_spans (
            segment_id TEXT NOT NULL REFERENCES text_segments(segment_id) ON DELETE CASCADE,
            source_file_id TEXT NOT NULL,
            paragraph_id TEXT NOT NULL,
            paragraph_index INTEGER NOT NULL,
            paragraph_char_start INTEGER NOT NULL,
            paragraph_char_end INTEGER NOT NULL,
            span_order INTEGER NOT NULL,
            PRIMARY KEY(segment_id, span_order)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_segment_paragraph_spans_source_position "
        "ON text_segment_paragraph_spans("
        "source_file_id, paragraph_index, paragraph_char_start, paragraph_char_end)"
    )
    return changed


# v1 -> v2 changed paragraph payload/search storage and is still performed by
# database.ensure_database_search_index because it publishes a replacement
# file atomically. v3 through v6 are pure additive DDL and belong here.
INDEX_MIGRATIONS: tuple[Migration, ...] = (
    Migration(target_version=3, apply=install_document_group_schema),
    Migration(target_version=4, apply=install_text_alignment_schema),
    Migration(target_version=5, apply=_install_text_segment_paragraph_spans),
    Migration(target_version=6, apply=install_text_alignment_schema),
)


def migrate_index_database(
    db_path: Path,
    *,
    migrations: Sequence[Migration] = INDEX_MIGRATIONS,
) -> bool:
    """Apply every pending additive migration in one transaction."""

    path = Path(db_path)
    if not path.exists():
        return False
    connection = open_writable_index(path)
    try:
        current_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if current_version > DATABASE_SCHEMA_VERSION:
            raise ValueError(
                "数据库版本高于当前应用支持的版本："
                f"{current_version} > {DATABASE_SCHEMA_VERSION}"
            )
        pending = sorted(
            (
                migration
                for migration in migrations
                if current_version <= migration.target_version
                <= DATABASE_SCHEMA_VERSION
            ),
            key=lambda migration: migration.target_version,
        )
        if not pending:
            return False

        connection.execute("BEGIN IMMEDIATE")
        changed = current_version != DATABASE_SCHEMA_VERSION
        for migration in pending:
            changed = migration.apply(connection) or changed
        if _table_exists(connection, "metadata"):
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
                (
                    "database_schema_version",
                    json.dumps(DATABASE_SCHEMA_VERSION),
                ),
            )
        connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
        connection.commit()
        return changed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _index_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        is not None
    )

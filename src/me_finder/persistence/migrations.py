"""Transactional registry for additive index-schema migrations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .connection import open_writable_index
from .index_schema import DATABASE_SCHEMA_VERSION


MigrationStep = Callable[[sqlite3.Connection], bool]


@dataclass(frozen=True)
class Migration:
    target_version: int
    apply: MigrationStep


def _install_document_groups(connection: sqlite3.Connection) -> bool:
    from ..document_groups import install_document_group_schema

    return install_document_group_schema(connection)


def _install_text_alignment(connection: sqlite3.Connection) -> bool:
    from ..text_alignment import install_text_alignment_schema

    return install_text_alignment_schema(connection)


# v1 -> v2 changed paragraph payload/search storage and is still performed by
# database.ensure_database_search_index because it publishes a replacement
# file atomically. v3 and v4 are pure additive DDL and belong here.
INDEX_MIGRATIONS: tuple[Migration, ...] = (
    Migration(target_version=3, apply=_install_document_groups),
    Migration(target_version=4, apply=_install_text_alignment),
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

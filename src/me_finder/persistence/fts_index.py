"""Versioned trigram FTS index storage operations."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable

from .connection import PROJECT_BUSY_TIMEOUT_MS, connect_index

from .index_schema import DATABASE_SCHEMA_VERSION, PARAGRAPH_FTS_VERSION


def _fts_objects_present(connection: sqlite3.Connection) -> bool:
    names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('paragraphs_fts', 'paragraphs_fts_ai', 'paragraphs_fts_ad', 'paragraphs_fts_au')"
        )
    }
    return names == {
        "paragraphs_fts",
        "paragraphs_fts_ai",
        "paragraphs_fts_ad",
        "paragraphs_fts_au",
    }


def database_has_fts5_search_index(connection: sqlite3.Connection) -> bool:
    """Return whether the versioned trigram FTS index is ready for queries."""

    if not _fts_objects_present(connection):
        return False
    row = connection.execute(
        "SELECT value_json FROM metadata WHERE key = 'paragraph_fts_version'"
    ).fetchone()
    if row is None:
        return False
    try:
        return int(json.loads(row[0])) == PARAGRAPH_FTS_VERSION
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _database_uses_sparse_paragraph_payload(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT value_json FROM metadata WHERE key = 'paragraph_payload_storage'"
    ).fetchone()
    if row is None:
        return False
    try:
        return json.loads(row[0]) == "sparse_text_v1"
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _install_fts5_search_index(
    connection: sqlite3.Connection,
    *,
    rebuild: bool,
) -> bool:
    """Install the external-content trigram index on an open write connection.

    ``detail=none`` and ``columnsize=0`` keep the index materially smaller than
    another stored copy of paragraph text.  Search code submits a bounded set
    of trigram terms and verifies every candidate against the canonical typed
    columns, so positional detail is unnecessary.
    """

    connection.execute("SAVEPOINT install_paragraphs_fts")
    try:
        statements = (
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS paragraphs_fts USING fts5(
                plain_text,
                content='paragraphs',
                content_rowid='rowid',
                tokenize='trigram',
                detail='none',
                columnsize=0
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS paragraphs_fts_ai
            AFTER INSERT ON paragraphs BEGIN
                INSERT INTO paragraphs_fts(rowid, plain_text)
                VALUES (new.rowid, new.plain_text);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS paragraphs_fts_ad
            AFTER DELETE ON paragraphs BEGIN
                INSERT INTO paragraphs_fts(paragraphs_fts, rowid, plain_text)
                VALUES ('delete', old.rowid, old.plain_text);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS paragraphs_fts_au
            AFTER UPDATE OF plain_text ON paragraphs BEGIN
                INSERT INTO paragraphs_fts(paragraphs_fts, rowid, plain_text)
                VALUES ('delete', old.rowid, old.plain_text);
                INSERT INTO paragraphs_fts(rowid, plain_text)
                VALUES (new.rowid, new.plain_text);
            END
            """,
        )
        for statement in statements:
            connection.execute(statement)
        if rebuild:
            connection.execute(
                "INSERT INTO paragraphs_fts(paragraphs_fts) VALUES ('rebuild')"
            )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            ("paragraph_fts_version", json.dumps(PARAGRAPH_FTS_VERSION)),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            ("database_schema_version", json.dumps(DATABASE_SCHEMA_VERSION)),
        )
        connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
        connection.execute("RELEASE SAVEPOINT install_paragraphs_fts")
        return True
    except sqlite3.OperationalError:
        # Some distributor-provided SQLite builds omit FTS5 or the trigram
        # tokenizer.  The caller keeps the legacy scan path available.
        connection.execute("ROLLBACK TO SAVEPOINT install_paragraphs_fts")
        connection.execute("RELEASE SAVEPOINT install_paragraphs_fts")
        return False


_FTS_INSTALL_LOCK = threading.Lock()


def ensure_database_search_index(
    db_path: Path, optimize_database_storage: Callable[[Path], bool]
) -> bool:
    """Upgrade paragraph storage and create FTS once, with scan fallback."""

    db_path = Path(db_path)
    with _FTS_INSTALL_LOCK:
        connection = connect_index(
            db_path, row_factory=None, busy_timeout_ms=PROJECT_BUSY_TIMEOUT_MS
        )
        try:
            fts_ready = database_has_fts5_search_index(connection)
            sparse_payload = _database_uses_sparse_paragraph_payload(connection)
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if fts_ready and sparse_payload:
                return True
        finally:
            connection.close()

        if not sparse_payload and user_version <= DATABASE_SCHEMA_VERSION:
            try:
                if optimize_database_storage(db_path):
                    return True
            except (OSError, sqlite3.Error, ValueError):
                # The old file is still authoritative until the final rename.
                # Insufficient space, an active Windows file handle, or an
                # unavailable tokenizer therefore degrades to the additive
                # migration below (or ultimately to the legacy scan path).
                pass

        if fts_ready:
            return True

        connection = connect_index(
            db_path,
            write=True,
            row_factory=None,
            busy_timeout_ms=PROJECT_BUSY_TIMEOUT_MS,
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            installed = _install_fts5_search_index(connection, rebuild=True)
            if installed:
                connection.commit()
            else:
                connection.rollback()
            return installed
        except sqlite3.Error:
            connection.rollback()
            return False
        finally:
            connection.close()

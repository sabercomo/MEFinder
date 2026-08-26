"""SQLite connection policies for the index database."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_readonly_index(db_path: Path) -> sqlite3.Connection:
    """Open the shared query connection used by the local HTTP runtime."""

    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    # Touch the schema before query_only so SQLite can recover a hot rollback
    # journal left by an interrupted importer.  Without this, desktop startup
    # can fail immediately at its first sqlite_master query.
    connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    connection.execute("PRAGMA query_only = ON")
    return connection


def open_writable_index(db_path: Path) -> sqlite3.Connection:
    """Open a transactional index connection with the project lock policy."""

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection

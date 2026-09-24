"""SQLite connection policies for the index database.

Every module outside ``persistence`` opens index connections through here so
the lock and integrity policy lives in one place:

* write connections always run with ``foreign_keys = ON``;
* ``busy_timeout_ms=None`` keeps Python's ``sqlite3`` default wait (5 s), which
  is what the migrated call sites used before; pass
  ``PROJECT_BUSY_TIMEOUT_MS`` to opt into the project-wide 30 s policy;
* ``row_factory`` defaults to ``sqlite3.Row`` but callers that relied on plain
  tuples pass ``row_factory=None``.

Bulk builders that write into a fresh temporary file (``build_database``,
storage optimisation) and file-level backups keep their own connections.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

PROJECT_BUSY_TIMEOUT_MS = 30000

RowFactory = Optional[Callable[..., object]]
_ROW = sqlite3.Row


def connect_index(
    db_path: Path | str,
    *,
    write: bool = False,
    row_factory: RowFactory = _ROW,
    busy_timeout_ms: int | None = None,
    check_same_thread: bool = True,
    readonly_uri: bool = False,
) -> sqlite3.Connection:
    """Open an index connection with the shared policy.

    ``readonly_uri`` opens ``file:...?mode=ro`` so SQLite itself refuses
    writes; it cannot be combined with ``write``.
    """

    if write and readonly_uri:
        raise ValueError("a read-only URI connection cannot be writable")
    if readonly_uri:
        uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(
            uri, uri=True, check_same_thread=check_same_thread
        )
    else:
        connection = sqlite3.connect(
            str(db_path), check_same_thread=check_same_thread
        )
    if row_factory is not None:
        connection.row_factory = row_factory
    if busy_timeout_ms is not None:
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    if write:
        connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def open_read(
    db_path: Path | str,
    *,
    row_factory: RowFactory = _ROW,
    busy_timeout_ms: int | None = None,
) -> Iterator[sqlite3.Connection]:
    """Yield a read connection and always close it."""

    connection = connect_index(
        db_path, row_factory=row_factory, busy_timeout_ms=busy_timeout_ms
    )
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def open_write(
    db_path: Path | str,
    *,
    immediate: bool = False,
    row_factory: RowFactory = _ROW,
    busy_timeout_ms: int | None = None,
) -> Iterator[sqlite3.Connection]:
    """Yield a ``foreign_keys = ON`` connection and always close it.

    The caller owns the transaction boundary and must ``commit()``; anything
    left uncommitted is rolled back.  ``immediate`` starts the transaction with
    ``BEGIN IMMEDIATE`` to take the write lock up front.
    """

    connection = connect_index(
        db_path,
        write=True,
        row_factory=row_factory,
        busy_timeout_ms=busy_timeout_ms,
    )
    try:
        if immediate:
            connection.execute("BEGIN IMMEDIATE")
        yield connection
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def open_readonly_snapshot(
    db_path: Path | str,
    *,
    row_factory: RowFactory = _ROW,
) -> Iterator[sqlite3.Connection]:
    """Yield a ``mode=ro`` URI connection that SQLite itself keeps read-only."""

    connection = connect_index(db_path, row_factory=row_factory, readonly_uri=True)
    try:
        yield connection
    finally:
        connection.close()


def open_readonly_index(db_path: Path) -> sqlite3.Connection:
    """Open the shared query connection used by the local HTTP runtime."""

    connection = connect_index(
        db_path, busy_timeout_ms=PROJECT_BUSY_TIMEOUT_MS, check_same_thread=False
    )
    # Touch the schema before query_only so SQLite can recover a hot rollback
    # journal left by an interrupted importer.  Without this, desktop startup
    # can fail immediately at its first sqlite_master query.
    connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    connection.execute("PRAGMA query_only = ON")
    return connection


def open_writable_index(db_path: Path) -> sqlite3.Connection:
    """Open a transactional index connection with the project lock policy."""

    return connect_index(db_path, write=True, busy_timeout_ms=PROJECT_BUSY_TIMEOUT_MS)


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    """Return whether a regular table called ``name`` exists."""

    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )

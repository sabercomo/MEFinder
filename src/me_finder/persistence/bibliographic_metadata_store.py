"""SQLite operations for updating one document's catalog payloads."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .connection import connect_index


@contextmanager
def metadata_write_transaction(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Keep source, volume, work, and paragraph updates atomic."""

    connection = connect_index(database_path, write=True, row_factory=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def source_payload_json(connection: sqlite3.Connection, source_file_id: str) -> str | None:
    """Load the source payload if the document exists."""

    row = connection.execute(
        "SELECT payload_json FROM source_files WHERE source_file_id = ?", (source_file_id,)
    ).fetchone()
    return row[0] if row else None


def write_source_payload(
    connection: sqlite3.Connection, source_file_id: str, payload_json: str
) -> None:
    """Store the source's canonical bibliographic metadata."""

    connection.execute(
        "UPDATE source_files SET payload_json = ? WHERE source_file_id = ?",
        (payload_json, source_file_id),
    )


def volume_payload_rows(
    connection: sqlite3.Connection, source_file_id: str
) -> list[tuple[int, str]]:
    """Load volume payloads belonging to the source."""

    return connection.execute(
        "SELECT rowid, payload_json FROM volumes WHERE source_file_id = ?",
        (source_file_id,),
    ).fetchall()


def write_volume_payload(
    connection: sqlite3.Connection, row_id: int, title: object, payload_json: str
) -> None:
    """Store a volume's display title and payload together."""

    connection.execute(
        "UPDATE volumes SET display_title = ?, payload_json = ? WHERE rowid = ?",
        (title, payload_json, row_id),
    )


def work_payload_rows(
    connection: sqlite3.Connection, source_file_id: str
) -> list[tuple[int, str]]:
    """Load works whose payload names this source."""

    return connection.execute(
        "SELECT rowid, payload_json FROM works WHERE payload_json LIKE ?",
        (f'%"source_file_id":"{source_file_id}"%',),
    ).fetchall()


def write_work_payload(
    connection: sqlite3.Connection, row_id: int, title: object, payload_json: str
) -> None:
    """Store a work's title and payload together."""

    connection.execute(
        "UPDATE works SET title = ?, payload_json = ? WHERE rowid = ?",
        (title, payload_json, row_id),
    )


def paragraph_payload_rows(
    connection: sqlite3.Connection, source_file_id: str
) -> list[tuple[str, str]]:
    """Load paragraph payloads belonging to the source."""

    return connection.execute(
        "SELECT paragraph_id, payload_json FROM paragraphs WHERE source_file_id = ?",
        (source_file_id,),
    ).fetchall()


def write_paragraph_payload(
    connection: sqlite3.Connection, paragraph_id: str, payload_json: str
) -> None:
    """Store one paragraph's bibliographic display fields."""

    connection.execute(
        "UPDATE paragraphs SET payload_json = ? WHERE paragraph_id = ?",
        (payload_json, paragraph_id),
    )

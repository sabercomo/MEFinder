"""SQLite reads and writes for applying a PDF's page mapping in place."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from .connection import connect_index
from .paragraph_payload import PARAGRAPH_SELECT_COLUMNS


@contextmanager
def page_mapping_transaction(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Keep page, paragraph, source, and mapping updates in one transaction."""

    connection = connect_index(database_path, write=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def pdf_page_rows(connection: sqlite3.Connection, source_file_id: str) -> list[sqlite3.Row]:
    """Return existing PDF page payloads for one document."""

    return connection.execute(
        "SELECT row_id, pdf_page_index, payload_json FROM pdf_pages WHERE source_file_id = ?",
        (source_file_id,),
    ).fetchall()


def write_pdf_page(connection: sqlite3.Connection, row_id: int, payload_json: str) -> None:
    """Store one mapped PDF page payload."""

    connection.execute(
        "UPDATE pdf_pages SET payload_json = ? WHERE row_id = ?", (payload_json, row_id)
    )


def paragraph_rows(connection: sqlite3.Connection, source_file_id: str) -> list[sqlite3.Row]:
    """Return hydrated paragraph columns for mapping propagation."""

    return connection.execute(
        f"SELECT {PARAGRAPH_SELECT_COLUMNS} FROM paragraphs p WHERE p.source_file_id = ?",
        (source_file_id,),
    ).fetchall()


def write_paragraph(
    connection: sqlite3.Connection,
    paragraph_id: str,
    paragraph: Mapping[str, object],
    payload_json: str,
) -> None:
    """Store a paragraph's mapped citation fields and payload."""

    connection.execute(
        """
        UPDATE paragraphs
           SET page_display = ?, page_source_type = ?, page_confidence = ?,
               citation_page_start = ?, citation_page_end = ?, payload_json = ?
         WHERE paragraph_id = ?
        """,
        (
            paragraph.get("page_display"),
            paragraph.get("page_source_type"),
            paragraph.get("page_confidence"),
            paragraph.get("citation_page_start"),
            paragraph.get("citation_page_end"),
            payload_json,
            paragraph_id,
        ),
    )


def source_payload_json(connection: sqlite3.Connection, source_file_id: str) -> str | None:
    """Return one source payload if the document still exists."""

    row = connection.execute(
        "SELECT payload_json FROM source_files WHERE source_file_id = ?", (source_file_id,)
    ).fetchone()
    return row[0] if row else None


def write_source_payload(
    connection: sqlite3.Connection, source_file_id: str, payload_json: str
) -> None:
    """Store the source's page-mapping profile."""

    connection.execute(
        "UPDATE source_files SET payload_json = ? WHERE source_file_id = ?",
        (payload_json, source_file_id),
    )


def write_page_mapping(
    connection: sqlite3.Connection, source_file_id: str, payload_json: str
) -> None:
    """Update the existing mapping row or insert its first row."""

    existing = connection.execute(
        "SELECT row_id FROM pdf_page_mappings WHERE source_file_id = ? LIMIT 1",
        (source_file_id,),
    ).fetchone()
    if existing:
        connection.execute(
            "UPDATE pdf_page_mappings SET payload_json = ? WHERE source_file_id = ?",
            (payload_json, source_file_id),
        )
    else:
        connection.execute(
            "INSERT INTO pdf_page_mappings(source_file_id, pdf_page_index, payload_json) "
            "VALUES (?, NULL, ?)",
            (source_file_id, payload_json),
        )

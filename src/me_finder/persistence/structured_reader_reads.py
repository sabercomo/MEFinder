"""Read-only SQLite queries behind the structured reader.

Every function takes the caller's connection so one reader request keeps a
single consistent snapshot.  Rows are returned as stored; page wording,
citation rules and pagination decisions stay in ``structured_reader``.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional

# Natural reading positions per reader kind.  Table and column names are
# internal constants; caller input never reaches the SQL text.
_POSITION_TABLES = {
    "pdf": ("pdf_pages", "pdf_page_index"),
    "word": ("paragraphs", "paragraph_index"),
}

_WORD_COLUMNS = (
    "paragraph_id, volume_id, work_id, paragraph_index, text_raw, "
    "page_display, page_source_type, payload_json"
)


def read_source_row(connection: sqlite3.Connection, source_id: str) -> Optional[sqlite3.Row]:
    """Return the ``source_files`` row the reader needs, or ``None``."""

    return connection.execute(
        """
        SELECT source_file_id, source_type, file_name, relative_path,
               volume_number, payload_json
        FROM source_files
        WHERE source_file_id = ?
        """,
        (source_id,),
    ).fetchone()


def read_first_volume(connection: sqlite3.Connection, source_id: str) -> Optional[sqlite3.Row]:
    """Return the first stored volume of a source (display title and payload)."""

    return connection.execute(
        """
        SELECT display_title, payload_json
        FROM volumes
        WHERE source_file_id = ?
        ORDER BY rowid
        LIMIT 1
        """,
        (source_id,),
    ).fetchone()


def read_volume_payloads(connection: sqlite3.Connection, source_id: str) -> List[sqlite3.Row]:
    """Return ``volume_id`` and payload for every volume of a source."""

    return connection.execute(
        """
        SELECT volume_id, payload_json
        FROM volumes
        WHERE source_file_id = ?
        ORDER BY rowid
        """,
        (source_id,),
    ).fetchall()


def read_work_payloads(connection: sqlite3.Connection, source_id: str) -> List[sqlite3.Row]:
    """Return ``work_id`` and payload for every work in the source's volumes."""

    return connection.execute(
        """
        SELECT works.work_id, works.payload_json
        FROM works
        INNER JOIN volumes ON volumes.volume_id = works.volume_id
        WHERE volumes.source_file_id = ?
        ORDER BY works.rowid
        """,
        (source_id,),
    ).fetchall()


def read_pdf_pages_at(
    connection: sqlite3.Connection, source_id: str, pdf_page_index: int
) -> List[sqlite3.Row]:
    """Return every stored page row at one PDF page index."""

    return connection.execute(
        """
        SELECT pdf_page_index, payload_json
        FROM pdf_pages
        WHERE source_file_id = ? AND pdf_page_index = ?
        ORDER BY rowid
        """,
        (source_id, pdf_page_index),
    ).fetchall()


def read_all_pdf_page_payloads(connection: sqlite3.Connection, source_id: str) -> List[sqlite3.Row]:
    """Return index and payload for every page of a PDF, in reading order."""

    return connection.execute(
        """
        SELECT pdf_page_index, payload_json
        FROM pdf_pages
        WHERE source_file_id = ?
        ORDER BY pdf_page_index, rowid
        """,
        (source_id,),
    ).fetchall()


def read_paragraph_positions(
    connection: sqlite3.Connection, source_id: str, paragraph_id: str
) -> List[sqlite3.Row]:
    """Return the ``paragraph_index`` of every row carrying a paragraph id."""

    return connection.execute(
        """
        SELECT paragraph_index
        FROM paragraphs
        WHERE source_file_id = ? AND paragraph_id = ?
        ORDER BY rowid
        """,
        (source_id, paragraph_id),
    ).fetchall()


def read_pdf_page_range(
    connection: sqlite3.Connection, source_id: str, start: int, end: int, limit: int
) -> List[sqlite3.Row]:
    """Return at most ``limit`` page rows with ``start <= index <= end``."""

    return connection.execute(
        """
        SELECT pdf_page_index, payload_json
        FROM pdf_pages
        WHERE source_file_id = ?
          AND pdf_page_index >= ?
          AND pdf_page_index <= ?
        ORDER BY pdf_page_index, rowid
        LIMIT ?
        """,
        (source_id, start, end, limit),
    ).fetchall()


def read_paragraph_range(
    connection: sqlite3.Connection, source_id: str, start: int, end: int, limit: int
) -> List[sqlite3.Row]:
    """Return at most ``limit`` paragraph rows with ``start <= index <= end``."""

    return connection.execute(
        f"""
        SELECT {_WORD_COLUMNS}
        FROM paragraphs
        WHERE source_file_id = ?
          AND paragraph_index >= ?
          AND paragraph_index <= ?
        ORDER BY paragraph_index, rowid
        LIMIT ?
        """,
        (source_id, start, end, limit),
    ).fetchall()


def read_position_summary(connection: sqlite3.Connection, kind: str, source_id: str) -> sqlite3.Row:
    """Return ``total`` and ``last_position`` for a PDF or Word source."""

    table, column = _POSITION_TABLES[kind]
    return connection.execute(
        f"""
        SELECT COUNT(*) AS total, MAX({column}) AS last_position
        FROM {table}
        WHERE source_file_id = ?
        """,
        (source_id,),
    ).fetchone()


def read_pdf_window_rows(
    connection: sqlite3.Connection, source_id: str, start: int, limit: int
) -> List[sqlite3.Row]:
    """Return up to ``limit`` page rows from natural position ``start``."""

    return connection.execute(
        """
        SELECT row_id, pdf_page_index, payload_json
        FROM pdf_pages
        WHERE source_file_id = ? AND pdf_page_index >= ?
        ORDER BY pdf_page_index, row_id
        LIMIT ?
        """,
        (source_id, start, limit),
    ).fetchall()


def read_word_window_rows(
    connection: sqlite3.Connection, source_id: str, start: int, limit: int
) -> List[sqlite3.Row]:
    """Return up to ``limit`` paragraph rows from natural position ``start``."""

    return connection.execute(
        f"""
        SELECT rowid, {_WORD_COLUMNS}
        FROM paragraphs
        WHERE source_file_id = ? AND paragraph_index >= ?
        ORDER BY paragraph_index, rowid
        LIMIT ?
        """,
        (source_id, start, limit),
    ).fetchall()


def read_paragraph_before(
    connection: sqlite3.Connection, source_id: str, position: int
) -> Optional[sqlite3.Row]:
    """Return the last paragraph row before natural position ``position``."""

    return connection.execute(
        f"""
        SELECT {_WORD_COLUMNS}
        FROM paragraphs
        WHERE source_file_id = ? AND paragraph_index < ?
        ORDER BY paragraph_index DESC, rowid DESC
        LIMIT 1
        """,
        (source_id, position),
    ).fetchone()


def read_positions_before(
    connection: sqlite3.Connection, kind: str, source_id: str, before: int, limit: int
) -> List[sqlite3.Row]:
    """Return up to ``limit`` natural positions before ``before``, nearest first."""

    table, column = _POSITION_TABLES[kind]
    return connection.execute(
        f"""
        SELECT {column}
        FROM {table}
        WHERE source_file_id = ? AND {column} < ?
        ORDER BY {column} DESC, rowid DESC
        LIMIT ?
        """,
        (source_id, before, limit),
    ).fetchall()

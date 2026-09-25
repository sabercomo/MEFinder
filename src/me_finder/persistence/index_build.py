"""Populate a new SQLite index while the caller owns publication and snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence

from .connection import connect_index
from .index_schema import ANCHOR_SPEC_VERSION, DATABASE_SCHEMA_VERSION, SCHEMA
from .paragraph_payload import paragraph_payload_for_storage


def _load_payload_rows(connection: sqlite3.Connection, table: str, order_by: str = "rowid") -> list[Dict[str, object]]:
    return [json.loads(row[0]) for row in connection.execute(f"SELECT payload_json FROM {table} ORDER BY {order_by}")]


def load_database_index(db_path: Path) -> Dict[str, object]:
    """Load the small metadata/catalog portion used by the Web UI."""

    connection = connect_index(db_path, row_factory=None)
    try:
        metadata = {str(row[0]): json.loads(row[1]) for row in connection.execute("SELECT key, value_json FROM metadata")}
        return {
            "metadata": metadata,
            "source_files": _load_payload_rows(connection, "source_files", "source_file_id"),
            "volumes": _load_payload_rows(connection, "volumes", "volume_id"),
            "works": _load_payload_rows(connection, "works", "rowid"),
        }
    finally:
        connection.close()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _insert_page_anchors(
    connection: sqlite3.Connection,
    anchors: Sequence[Dict[str, object]],
) -> None:
    """Store canonical page anchors in the legacy schema-v2 table.

    The v2 table called its typed lookup column ``paragraph_id``, while the
    page-anchor model has always called that relationship
    ``start_paragraph_id`` and also keeps ``end_paragraph_id`` and
    ``source_file_id`` in its payload.  Treat the legacy column as a typed
    alias for the start paragraph instead of silently writing NULL.
    """

    values = []
    for anchor in anchors:
        start_paragraph_id = anchor.get("start_paragraph_id")
        if start_paragraph_id in (None, ""):
            # Accept an old exported record that used the physical v2 column
            # name, while current extractors use the canonical field name.
            start_paragraph_id = anchor.get("paragraph_id")
        values.append(
            (
                str(start_paragraph_id)
                if start_paragraph_id not in (None, "")
                else None,
                _json(anchor),
            )
        )
    if values:
        connection.executemany(
            "INSERT INTO page_anchors(paragraph_id, payload_json) VALUES (?, ?)",
            values,
        )


def _int_or_none(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def insert_initial_index_rows(
    connection: sqlite3.Connection,
    index: Dict[str, object],
    source_files: Sequence[Dict[str, object]],
    paragraphs: Sequence[Dict[str, object]],
    deduplicated_rows: Dict[str, int],
) -> None:
    """Install schema and source rows before restoring document groups."""

    connection.executescript(SCHEMA)
    metadata = dict(index.get("metadata") or {})
    metadata["database_schema_version"] = DATABASE_SCHEMA_VERSION
    metadata["paragraph_payload_storage"] = "sparse_text_v1"
    metadata.setdefault("anchor_spec_version", ANCHOR_SPEC_VERSION)
    metadata["database_built_at"] = datetime.now(timezone.utc).isoformat()
    metadata["source_count"] = len(source_files)
    metadata["paragraph_count"] = len(paragraphs)
    metadata["eligible_paragraph_count"] = sum(
        1 for item in paragraphs if item.get("eligible_for_search")
    )
    if deduplicated_rows:
        metadata["database_deduplication"] = {
            "strategy": "first_record_wins_and_fills_missing_fields",
            "merged_rows": deduplicated_rows,
        }
    connection.executemany(
        "INSERT INTO metadata(key, value_json) VALUES (?, ?)",
        [(str(key), _json(value)) for key, value in metadata.items()],
    )

    connection.executemany(
        """
        INSERT INTO source_files(
            source_file_id, source_type, file_name, relative_path, volume_number, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                str(item.get("source_file_id") or ""),
                str(item.get("source_type") or "word"),
                item.get("file_name"),
                item.get("relative_path"),
                _int_or_none(item.get("volume_number")),
                _json(item),
            )
            for item in source_files
            if item.get("source_file_id")
        ],
    )


def insert_remaining_index_rows(
    connection: sqlite3.Connection,
    index: Dict[str, object],
    volumes: Sequence[Dict[str, object]],
    works: Sequence[Dict[str, object]],
    paragraphs: Sequence[Dict[str, object]],
    page_anchors: Sequence[Dict[str, object]],
    pdf_pages: Sequence[Dict[str, object]],
    pdf_page_mappings: Sequence[Dict[str, object]],
) -> None:
    """Write catalog, text, and page rows before restoring alignment recipes."""

    connection.executemany(
        """
        INSERT INTO volumes(volume_id, source_file_id, source_type, volume_number, display_title, payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                str(item.get("volume_id") or ""),
                item.get("source_file_id"),
                str(item.get("source_type") or "word"),
                _int_or_none(item.get("volume_number")),
                item.get("display_title"),
                _json(item),
            )
            for item in volumes
            if item.get("volume_id")
        ],
    )

    connection.executemany(
        """
        INSERT OR REPLACE INTO works(work_id, volume_id, source_type, work_order, title, payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                str(item.get("work_id") or ""),
                item.get("volume_id"),
                str(item.get("source_type") or "word"),
                _int_or_none(item.get("work_order")),
                item.get("title"),
                _json(item),
            )
            for item in works
            if item.get("work_id")
        ],
    )

    toc_entries = [item for item in index.get("toc_entries", []) if isinstance(item, dict)]
    connection.executemany(
        "INSERT INTO toc_entries(volume_id, work_id, title, payload_json) VALUES (?, ?, ?, ?)",
        [(item.get("volume_id"), item.get("work_id"), item.get("title"), _json(item)) for item in toc_entries],
    )

    paragraph_rows = []
    for item in paragraphs:
        paragraph_id = str(item.get("paragraph_id") or "")
        if not paragraph_id:
            continue
        paragraph_rows.append(
            (
                paragraph_id,
                item.get("volume_id"),
                item.get("work_id"),
                str(item.get("source_file_id") or ""),
                str(item.get("source_type") or "word"),
                int(item.get("paragraph_index") or 0),
                1 if item.get("eligible_for_search") else 0,
                str(item.get("text_raw") or ""),
                str(item.get("normalized_text") or ""),
                str(item.get("compact_text") or ""),
                str(item.get("plain_text") or ""),
                item.get("page_display"),
                item.get("page_source_type"),
                _float_or_none(item.get("page_confidence")),
                item.get("citation_page_start"),
                item.get("citation_page_end"),
                _int_or_none(item.get("pdf_page_start_index")),
                _int_or_none(item.get("pdf_page_end_index")),
                item.get("pdf_page_start_label"),
                item.get("pdf_page_end_label"),
                _json(paragraph_payload_for_storage(item)),
            )
        )
    connection.executemany(
        """
        INSERT INTO paragraphs(
            paragraph_id, volume_id, work_id, source_file_id, source_type, paragraph_index,
            eligible_for_search, text_raw, normalized_text, compact_text, plain_text,
            page_display, page_source_type, page_confidence, citation_page_start, citation_page_end,
            pdf_page_start_index, pdf_page_end_index, pdf_page_start_label, pdf_page_end_label, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        paragraph_rows,
    )
    _insert_page_anchors(connection, page_anchors)
    for table_name, key_fields in (
        ("pdf_pages", ("source_file_id", "pdf_page_index")),
        ("pdf_page_mappings", ("source_file_id", "pdf_page_index")),
        ("pdf_import_runs", ("source_file_id", "status")),
        ("audit_issues", ("source_file_id", "issue_type")),
    ):
        if table_name == "pdf_pages":
            rows = pdf_pages
        elif table_name == "pdf_page_mappings":
            rows = pdf_page_mappings
        else:
            rows = [
                item
                for item in index.get(table_name, [])
                if isinstance(item, dict)
            ]
        columns = ", ".join(key_fields) + ", payload_json"
        placeholders = ", ".join("?" for _ in key_fields) + ", ?"
        sql = f"INSERT INTO {table_name}({columns}) VALUES ({placeholders})"
        values = [tuple(item.get(field) for field in key_fields) + (_json(item),) for item in rows]
        if values:
            connection.executemany(sql, values)

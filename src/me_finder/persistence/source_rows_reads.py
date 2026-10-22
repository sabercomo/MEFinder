"""Read one source's own index rows in the shape ``source_replace`` writes them.

Read-only counterpart of ``replace_source_rows``: lets a caller write a source
back with selected tables unchanged (see ``pdf_page_refresh``).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List

from .connection import connect_index


# Typed columns of each source-owned table, keyed for ``read_source_rows``.
_SOURCE_ROW_COLUMNS = {
    "source_files": ("source_file_id", "source_type", "file_name", "relative_path", "volume_number"),
    "volumes": ("volume_id", "source_file_id", "source_type", "volume_number", "display_title"),
    "works": ("work_id", "volume_id", "source_type", "work_order", "title"),
    "toc_entries": ("volume_id", "work_id", "title"),
    "pdf_pages": ("source_file_id", "pdf_page_index"),
    "pdf_page_mappings": ("source_file_id", "pdf_page_index"),
    "pdf_import_runs": ("source_file_id", "status"),
    "audit_issues": ("source_file_id", "issue_type"),
}


def read_source_rows(db_path: Path, source_id: str) -> Dict[str, List[Dict[str, object]]] | None:
    """Read one source's own rows in the shape ``replace_source_rows`` writes.

    Each row is its stored payload with the typed columns laid over it, so
    writing the result back reproduces the same rows.  ``None`` when the
    source is not indexed.
    """

    connection = connect_index(db_path, row_factory=sqlite3.Row)
    try:
        if connection.execute(
            "SELECT 1 FROM source_files WHERE source_file_id = ?", (source_id,)
        ).fetchone() is None:
            return None
        volume_ids = [
            str(row[0]) for row in connection.execute(
                "SELECT volume_id FROM volumes WHERE source_file_id = ?", (source_id,)
            )
        ]
        volume_marks = ",".join("?" for _ in volume_ids) or "NULL"
        work_ids = [
            str(row[0]) for row in connection.execute(
                f"SELECT work_id FROM works WHERE volume_id IN ({volume_marks})", volume_ids
            )
        ]
        work_marks = ",".join("?" for _ in work_ids) or "NULL"
        filters = {
            "source_files": ("source_file_id = ?", [source_id], "source_file_id"),
            "volumes": ("source_file_id = ?", [source_id], "volume_id"),
            "works": (f"volume_id IN ({volume_marks})", volume_ids, "work_order, work_id"),
            "toc_entries": (
                f"volume_id IN ({volume_marks}) OR work_id IN ({work_marks})",
                volume_ids + work_ids,
                "row_id",
            ),
            "pdf_pages": ("source_file_id = ?", [source_id], "pdf_page_index"),
            "pdf_page_mappings": ("source_file_id = ?", [source_id], "row_id"),
            "pdf_import_runs": ("source_file_id = ?", [source_id], "row_id"),
            "audit_issues": ("source_file_id = ?", [source_id], "row_id"),
        }
        rows: Dict[str, List[Dict[str, object]]] = {}
        for table, (where, params, order) in filters.items():
            columns = _SOURCE_ROW_COLUMNS[table]
            rows[table] = []
            for row in connection.execute(
                f"SELECT {', '.join(columns)}, payload_json FROM {table} "
                f"WHERE {where} ORDER BY {order}",
                params,
            ):
                payload = json.loads(row["payload_json"] or "{}")
                item = payload if isinstance(payload, dict) else {}
                item.update({column: row[column] for column in columns})
                rows[table].append(item)
        return rows
    finally:
        connection.close()

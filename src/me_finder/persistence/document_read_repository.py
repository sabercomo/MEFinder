"""Read-only SQLite queries for document-facing application workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List

from .connection import open_readonly_index


class SQLiteDocumentReadRepository:
    """Keep document read models and table knowledge outside application code."""

    def front_matter_pages(
        self,
        database_path: Path,
        source_file_id: str,
        *,
        limit: int,
        tail: int,
    ) -> List[Dict[str, object]]:
        connection = open_readonly_index(database_path)
        try:
            total_row = connection.execute(
                "SELECT MAX(pdf_page_index) FROM pdf_pages "
                "WHERE source_file_id = ?",
                (source_file_id,),
            ).fetchone()
            total = (
                int(total_row[0]) + 1
                if total_row and total_row[0] is not None
                else 0
            )
            rows = connection.execute(
                "SELECT payload_json FROM pdf_pages "
                "WHERE source_file_id = ? "
                "AND (pdf_page_index < ? OR pdf_page_index >= ?) "
                "ORDER BY pdf_page_index",
                (source_file_id, limit, max(limit, total - tail)),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            connection.close()

    def latest_pdf_import_runs(
        self,
        database_path: Path,
    ) -> Dict[str, Dict[str, object]]:
        connection = open_readonly_index(database_path)
        try:
            rows = connection.execute(
                "SELECT source_file_id, payload_json "
                "FROM pdf_import_runs ORDER BY row_id"
            ).fetchall()
        finally:
            connection.close()

        result: Dict[str, Dict[str, object]] = {}
        for source_id, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            result[str(source_id)] = payload
        return result

    def language_samples(
        self,
        database_path: Path,
        source_file_ids: Iterable[str],
    ) -> Dict[str, str]:
        """Read a bounded opening sample from each indexed document."""

        connection = open_readonly_index(database_path)
        try:
            samples: Dict[str, str] = {}
            for source_id in source_file_ids:
                rows = connection.execute(
                    "SELECT substr(text_raw, 1, 1000) FROM paragraphs "
                    "WHERE source_file_id = ? AND eligible_for_search = 1 "
                    "AND text_raw <> '' ORDER BY paragraph_index LIMIT 16",
                    (source_id,),
                ).fetchall()
                text = "\n".join(str(row[0]) for row in rows if row[0])
                if text:
                    samples[source_id] = text
            return samples
        finally:
            connection.close()

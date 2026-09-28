"""Read-only SQLite queries for describing a document's stored page mapping.

Only the page-evidence fields are pulled out of each payload with
``json_extract`` so describing a long book never loads its full text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from .connection import open_readonly_index

# Payload keys consulted by ``page_display.resolve_citation_page``.
_PAGE_EVIDENCE_KEYS = (
    "citation_page",
    "citation_page_start",
    "citation_page_end",
    "original_page_start",
    "original_page_end",
    "page_source_type",
    "page_mapping_method",
    "mapping_method",
    "citation_page_verified",
    "page_mapping_verified",
    "page_verified",
)
_VERIFIED_KEYS = {"citation_page_verified", "page_mapping_verified", "page_verified"}


def _evidence_columns() -> str:
    return ", ".join(
        f"json_extract(payload_json, '$.{key}') AS \"{key}\""
        for key in _PAGE_EVIDENCE_KEYS
    )


def _evidence(row: object) -> Dict[str, object]:
    fields: Dict[str, object] = {}
    for key in _PAGE_EVIDENCE_KEYS:
        value = row[key]
        if value is None:
            continue
        # json_extract returns JSON booleans as 0/1.
        fields[key] = bool(value) if key in _VERIFIED_KEYS else value
    return fields


def read_page_mapping_evidence(
    database_path: Path, source_file_id: str
) -> Optional[Dict[str, object]]:
    """Return the source row, its per-unit page evidence and mapping record.

    ``None`` means the source does not exist. PDF units are pages ordered by
    ``pdf_page_index``; text units are paragraphs ordered by
    ``paragraph_index``.
    """

    connection = open_readonly_index(database_path)
    try:
        source = connection.execute(
            "SELECT source_file_id, source_type, file_name, payload_json "
            "FROM source_files WHERE source_file_id = ?",
            (source_file_id,),
        ).fetchone()
        if source is None:
            return None
        source_type = str(source["source_type"] or "").lower()
        units: List[Dict[str, object]] = []
        mapping_record: Optional[Dict[str, object]] = None
        if source_type == "pdf":
            for row in connection.execute(
                f"SELECT pdf_page_index AS position, {_evidence_columns()} "
                "FROM pdf_pages WHERE source_file_id = ? ORDER BY pdf_page_index",
                (source_file_id,),
            ):
                units.append({"position": int(row["position"]), **_evidence(row)})
            mapping_row = connection.execute(
                "SELECT json_extract(payload_json, '$.mapping_status') AS mapping_status, "
                "json_extract(payload_json, '$.validated_by') AS validated_by, "
                "json_extract(payload_json, '$.method') AS method "
                "FROM pdf_page_mappings WHERE source_file_id = ? "
                "ORDER BY row_id LIMIT 1",
                (source_file_id,),
            ).fetchone()
            if mapping_row is not None:
                mapping_record = {
                    key: mapping_row[key]
                    for key in ("mapping_status", "validated_by", "method")
                }
        else:
            for row in connection.execute(
                "SELECT paragraph_index AS position, "
                "page_source_type AS column_page_source_type, "
                "citation_page_start AS column_citation_page_start, "
                "citation_page_end AS column_citation_page_end, "
                "page_display AS column_page_display, "
                f"{_evidence_columns()} "
                "FROM paragraphs WHERE source_file_id = ? ORDER BY paragraph_index",
                (source_file_id,),
            ):
                unit = {"position": int(row["position"]), **_evidence(row)}
                # Indexed columns are authoritative when the payload omits them.
                for column, key in (
                    ("column_page_source_type", "page_source_type"),
                    ("column_citation_page_start", "citation_page_start"),
                    ("column_citation_page_end", "citation_page_end"),
                    ("column_page_display", "page_display"),
                ):
                    if unit.get(key) in (None, "") and row[column] not in (None, ""):
                        unit[key] = row[column]
                units.append(unit)
        payload = json.loads(source["payload_json"] or "{}")
        return {
            "source": {
                "source_file_id": str(source["source_file_id"]),
                "source_type": source_type,
                "file_name": source["file_name"],
                "payload": payload if isinstance(payload, dict) else {},
            },
            "units": units,
            "mapping_record": mapping_record,
        }
    finally:
        connection.close()

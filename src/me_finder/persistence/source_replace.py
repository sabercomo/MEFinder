"""Replace or remove source-owned index rows on one SQLite transaction."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .connection import connect_index
from .index_build import _float_or_none, _insert_page_anchors, _int_or_none, _json
from .index_schema import ANCHOR_SPEC_VERSION
from .paragraph_payload import paragraph_payload_for_storage


def _delete_page_anchors_for_source(
    connection: sqlite3.Connection,
    source_file_id: str,
) -> int:
    """Delete current and legacy-v2 anchors owned by one source.

    Older writers left ``page_anchors.paragraph_id`` NULL because of the
    field-name mismatch.  Their canonical ownership data is still present in
    payload_json, so source deletion must consult source/start/end there as
    well as the repaired typed start-paragraph alias.
    """

    # Keep current typed rows entirely inside SQLite.  Expanding every
    # paragraph id into an ``IN (?, ...)`` list crosses SQLite's variable
    # limit for large books (32,766 on the Windows build).
    typed_anchor_filter = (
        "paragraph_id IN ("
        "SELECT paragraph_id FROM paragraphs WHERE source_file_id = ?"
        ")"
    )
    deleted_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM page_anchors WHERE {typed_anchor_filter}",
            (source_file_id,),
        ).fetchone()[0]
    )
    connection.execute(
        f"DELETE FROM page_anchors WHERE {typed_anchor_filter}",
        (source_file_id,),
    )

    # Legacy NULL-typed rows still need payload inspection.  Materializing the
    # ids for Python membership tests is safe here because they are no longer
    # rebound as one SQL statement's parameters.
    paragraph_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT paragraph_id FROM paragraphs WHERE source_file_id = ?",
            (source_file_id,),
        )
    }

    # Only old buggy v2 rows require payload inspection.  Correctly written
    # anchors are handled by the typed-column delete above, avoiding a
    # full JSON scan for every source in ordinary batch removals.
    owned_row_ids: List[int] = []
    for row_id, raw_payload in connection.execute(
        "SELECT row_id, payload_json FROM page_anchors WHERE paragraph_id IS NULL"
    ):
        belongs_to_source = False
        try:
            payload = json.loads(raw_payload) if raw_payload else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            payload_source_id = str(payload.get("source_file_id") or "")
            start_paragraph_id = str(payload.get("start_paragraph_id") or "")
            end_paragraph_id = str(payload.get("end_paragraph_id") or "")
            belongs_to_source = belongs_to_source or (
                payload_source_id == source_file_id
                or start_paragraph_id in paragraph_ids
                or end_paragraph_id in paragraph_ids
            )
        if belongs_to_source:
            owned_row_ids.append(int(row_id))
    if owned_row_ids:
        connection.executemany(
            "DELETE FROM page_anchors WHERE row_id = ?",
            [(row_id,) for row_id in owned_row_ids],
        )
    return deleted_count + len(owned_row_ids)


# 每份快照都是整个索引的完整副本。真实语料下单份就有 3.5GB，不设上限时
# 一次批量删除就能在数据目录里堆出几百 GB。


def replace_source_rows(
    extracted: Dict[str, object],
    db_path: Path,
    source: Dict[str, object],
    source_id: str,
    backup_path: Path | None,
) -> Dict[str, object]:
    """Replace one validated source after its optional backup is complete."""

    connection = connect_index(db_path, write=True, row_factory=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        old_volume_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT volume_id FROM volumes WHERE source_file_id = ?", (source_id,)
            ).fetchall()
        ]
        old_work_ids: List[str] = []
        if old_volume_ids:
            placeholders = ",".join("?" for _ in old_volume_ids)
            old_work_ids = [
                str(row[0])
                for row in connection.execute(
                    f"SELECT work_id FROM works WHERE volume_id IN ({placeholders})", old_volume_ids
                ).fetchall()
            ]
        _delete_page_anchors_for_source(connection, source_id)
        if old_volume_ids:
            placeholders = ",".join("?" for _ in old_volume_ids)
            connection.execute(f"DELETE FROM toc_entries WHERE volume_id IN ({placeholders})", old_volume_ids)
        if old_work_ids:
            placeholders = ",".join("?" for _ in old_work_ids)
            connection.execute(f"DELETE FROM toc_entries WHERE work_id IN ({placeholders})", old_work_ids)
            connection.execute(f"DELETE FROM works WHERE work_id IN ({placeholders})", old_work_ids)
        connection.execute("DELETE FROM paragraphs WHERE source_file_id = ?", (source_id,))
        for table in ("pdf_pages", "pdf_page_mappings", "pdf_import_runs", "audit_issues"):
            connection.execute(f"DELETE FROM {table} WHERE source_file_id = ?", (source_id,))
        connection.execute("DELETE FROM volumes WHERE source_file_id = ?", (source_id,))
        connection.execute("DELETE FROM source_files WHERE source_file_id = ?", (source_id,))

        connection.execute(
            """
            INSERT INTO source_files(
                source_file_id, source_type, file_name, relative_path, volume_number, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                str(source.get("source_type") or "word"),
                source.get("file_name"),
                source.get("relative_path"),
                _int_or_none(source.get("volume_number")),
                _json(source),
            ),
        )
        volumes = [item for item in extracted.get("volumes", []) if isinstance(item, dict)]
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
        works = [item for item in extracted.get("works", []) if isinstance(item, dict)]
        connection.executemany(
            """
            INSERT INTO works(work_id, volume_id, source_type, work_order, title, payload_json)
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
        toc_entries = [item for item in extracted.get("toc_entries", []) if isinstance(item, dict)]
        connection.executemany(
            "INSERT INTO toc_entries(volume_id, work_id, title, payload_json) VALUES (?, ?, ?, ?)",
            [(item.get("volume_id"), item.get("work_id"), item.get("title"), _json(item)) for item in toc_entries],
        )
        paragraphs = [item for item in extracted.get("paragraphs", []) if isinstance(item, dict)]
        connection.executemany(
            """
            INSERT INTO paragraphs(
                paragraph_id, volume_id, work_id, source_file_id, source_type, paragraph_index,
                eligible_for_search, text_raw, normalized_text, compact_text, plain_text,
                page_display, page_source_type, page_confidence, citation_page_start, citation_page_end,
                pdf_page_start_index, pdf_page_end_index, pdf_page_start_label, pdf_page_end_label, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(item.get("paragraph_id") or ""),
                    item.get("volume_id"),
                    item.get("work_id"),
                    source_id,
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
                for item in paragraphs
                if item.get("paragraph_id")
            ],
        )
        _insert_page_anchors(
            connection,
            [
                item
                for item in extracted.get("page_anchors", [])
                if isinstance(item, dict)
            ],
        )
        for table_name, key_fields in (
            ("pdf_pages", ("source_file_id", "pdf_page_index")),
            ("pdf_page_mappings", ("source_file_id", "pdf_page_index")),
            ("pdf_import_runs", ("source_file_id", "status")),
            ("audit_issues", ("source_file_id", "issue_type")),
        ):
            rows = [item for item in extracted.get(table_name, []) if isinstance(item, dict)]
            if not rows:
                continue
            columns = ", ".join(key_fields) + ", payload_json"
            placeholders = ", ".join("?" for _ in key_fields) + ", ?"
            connection.executemany(
                f"INSERT INTO {table_name}({columns}) VALUES ({placeholders})",
                [tuple(item.get(field) for field in key_fields) + (_json(item),) for item in rows],
            )

        totals = {
            "source_count": connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
            "paragraph_count": connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0],
            "eligible_paragraph_count": connection.execute(
                "SELECT COUNT(*) FROM paragraphs WHERE eligible_for_search = 1"
            ).fetchone()[0],
            "anchor_spec_version": ANCHOR_SPEC_VERSION,
        }
        connection.executemany(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            [(key, _json(value)) for key, value in totals.items()],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "source_file_id": source_id,
        "paragraph_count": len(paragraphs),
        "eligible_paragraph_count": sum(1 for item in paragraphs if item.get("eligible_for_search")),
        "backup_path": str(backup_path) if backup_path else None,
        **totals,
    }


def _delete_one_source(connection: sqlite3.Connection, source_file_id: str) -> Dict[str, int]:
    """Delete one source's rows on an open transaction and report the counts."""

    source = connection.execute(
        "SELECT source_type FROM source_files WHERE source_file_id = ?", (source_file_id,)
    ).fetchone()
    if source is None:
        raise ValueError("文献不存在。")
    if str(source[0]) not in {"pdf", "word"}:
        raise ValueError("当前移除服务仅允许处理 PDF 或 Word 文献。")
    volume_ids = [
        str(row[0])
        for row in connection.execute(
            "SELECT volume_id FROM volumes WHERE source_file_id = ?", (source_file_id,)
        ).fetchall()
    ]
    work_ids: List[str] = []
    if volume_ids:
        placeholders = ",".join("?" for _ in volume_ids)
        work_ids = [
            str(row[0])
            for row in connection.execute(
                f"SELECT work_id FROM works WHERE volume_id IN ({placeholders})", volume_ids
            ).fetchall()
        ]
    counts: Dict[str, int] = {}
    counts["paragraphs"] = connection.execute(
        "SELECT COUNT(*) FROM paragraphs WHERE source_file_id = ?", (source_file_id,)
    ).fetchone()[0]
    counts["pdf_pages"] = connection.execute(
        "SELECT COUNT(*) FROM pdf_pages WHERE source_file_id = ?", (source_file_id,)
    ).fetchone()[0]
    counts["page_anchors"] = _delete_page_anchors_for_source(
        connection, source_file_id
    )
    if volume_ids:
        placeholders = ",".join("?" for _ in volume_ids)
        connection.execute(f"DELETE FROM toc_entries WHERE volume_id IN ({placeholders})", volume_ids)
    if work_ids:
        placeholders = ",".join("?" for _ in work_ids)
        connection.execute(f"DELETE FROM toc_entries WHERE work_id IN ({placeholders})", work_ids)
        connection.execute(f"DELETE FROM works WHERE work_id IN ({placeholders})", work_ids)
    timestamp = datetime.now(timezone.utc).isoformat()
    connection.execute(
        "UPDATE document_groups SET base_source_file_id = NULL, updated_at = ? "
        "WHERE base_source_file_id = ?",
        (timestamp, source_file_id),
    )
    connection.execute(
        "DELETE FROM document_group_members WHERE source_file_id = ?",
        (source_file_id,),
    )
    connection.execute("DELETE FROM paragraphs WHERE source_file_id = ?", (source_file_id,))
    for table in ("pdf_pages", "pdf_page_mappings", "pdf_import_runs", "audit_issues"):
        connection.execute(f"DELETE FROM {table} WHERE source_file_id = ?", (source_file_id,))
    connection.execute("DELETE FROM volumes WHERE source_file_id = ?", (source_file_id,))
    connection.execute("DELETE FROM source_files WHERE source_file_id = ?", (source_file_id,))
    return counts


def delete_source_rows(
    ids: List[str], db_path: Path, backup_path: Path | None
) -> Dict[str, object]:
    """Delete validated sources in one transaction after one optional backup."""

    connection = connect_index(db_path, write=True, row_factory=None)
    deleted: Dict[str, Dict[str, int]] = {}
    try:
        connection.execute("BEGIN IMMEDIATE")
        for source_file_id in ids:
            deleted[source_file_id] = _delete_one_source(connection, source_file_id)
        totals = {
            "source_count": connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
            "paragraph_count": connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0],
            "eligible_paragraph_count": connection.execute(
                "SELECT COUNT(*) FROM paragraphs WHERE eligible_for_search = 1"
            ).fetchone()[0],
        }
        connection.executemany(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            [(key, _json(value)) for key, value in totals.items()],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "source_file_ids": ids,
        "deleted": deleted,
        "backup_path": str(backup_path) if backup_path else None,
        **totals,
    }

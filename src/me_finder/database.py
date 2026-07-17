"""SQLite storage for the local literature index.

The JSON index remains available as an export and migration fallback. The
desktop application and the default search path use this SQLite database so
the full paragraph corpus is not loaded from one large JSON document.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


DEFAULT_DATABASE_PATH = Path("data/index.sqlite3")
DATABASE_SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE source_files (
    source_file_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    file_name TEXT,
    relative_path TEXT,
    volume_number INTEGER,
    payload_json TEXT NOT NULL
);

CREATE TABLE volumes (
    volume_id TEXT PRIMARY KEY,
    source_file_id TEXT,
    source_type TEXT NOT NULL,
    volume_number INTEGER,
    display_title TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE works (
    work_id TEXT PRIMARY KEY,
    volume_id TEXT,
    source_type TEXT NOT NULL,
    work_order INTEGER,
    title TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE toc_entries (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id TEXT,
    work_id TEXT,
    title TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE paragraphs (
    paragraph_id TEXT PRIMARY KEY,
    volume_id TEXT,
    work_id TEXT,
    source_file_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    paragraph_index INTEGER NOT NULL,
    eligible_for_search INTEGER NOT NULL,
    text_raw TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    compact_text TEXT NOT NULL,
    plain_text TEXT NOT NULL,
    page_display TEXT,
    page_source_type TEXT,
    page_confidence REAL,
    citation_page_start TEXT,
    citation_page_end TEXT,
    pdf_page_start_index INTEGER,
    pdf_page_end_index INTEGER,
    pdf_page_start_label TEXT,
    pdf_page_end_label TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE page_anchors (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    paragraph_id TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE pdf_pages (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id TEXT,
    pdf_page_index INTEGER,
    payload_json TEXT NOT NULL
);

CREATE TABLE pdf_page_mappings (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id TEXT,
    pdf_page_index INTEGER,
    payload_json TEXT NOT NULL
);

CREATE TABLE pdf_import_runs (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id TEXT,
    status TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE audit_issues (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id TEXT,
    issue_type TEXT,
    payload_json TEXT NOT NULL
);

CREATE INDEX idx_paragraphs_searchable ON paragraphs(eligible_for_search, source_type);
CREATE INDEX idx_paragraphs_volume_position ON paragraphs(volume_id, paragraph_index);
CREATE INDEX idx_paragraphs_source_position ON paragraphs(source_file_id, paragraph_index);
CREATE INDEX idx_pdf_pages_source_page ON pdf_pages(source_file_id, pdf_page_index);
"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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


def _backup_database(db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = backup_dir / f"{db_path.stem}-{stamp}{db_path.suffix}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def build_database(index: Dict[str, object], db_path: Path = DEFAULT_DATABASE_PATH, backup_existing: bool = False) -> Dict[str, object]:
    """Build a normalized SQLite database from an extracted index dictionary."""

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_existing and db_path.exists():
        _backup_database(db_path)

    temp_path = db_path.with_name(
        f".{db_path.name}.{os.getpid()}-{threading.get_ident()}.tmp"
    )
    if temp_path.exists():
        temp_path.unlink()
    connection = sqlite3.connect(str(temp_path))
    try:
        connection.executescript(SCHEMA)
        metadata = dict(index.get("metadata") or {})
        metadata["database_schema_version"] = DATABASE_SCHEMA_VERSION
        metadata["database_built_at"] = datetime.now(timezone.utc).isoformat()
        connection.executemany(
            "INSERT INTO metadata(key, value_json) VALUES (?, ?)",
            [(str(key), _json(value)) for key, value in metadata.items()],
        )

        source_files = [item for item in index.get("source_files", []) if isinstance(item, dict)]
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

        volumes = [item for item in index.get("volumes", []) if isinstance(item, dict)]
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

        works = [item for item in index.get("works", []) if isinstance(item, dict)]
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

        paragraphs = [item for item in index.get("paragraphs", []) if isinstance(item, dict)]
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
                    _json(item),
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
        for table_name, key_fields in (
            ("page_anchors", ("paragraph_id",)),
            ("pdf_pages", ("source_file_id", "pdf_page_index")),
            ("pdf_page_mappings", ("source_file_id", "pdf_page_index")),
            ("pdf_import_runs", ("source_file_id", "status")),
            ("audit_issues", ("source_file_id", "issue_type")),
        ):
            rows = [item for item in index.get(table_name, []) if isinstance(item, dict)]
            columns = ", ".join(key_fields) + ", payload_json"
            placeholders = ", ".join("?" for _ in key_fields) + ", ?"
            sql = f"INSERT INTO {table_name}({columns}) VALUES ({placeholders})"
            values = [tuple(item.get(field) for field in key_fields) + (_json(item),) for item in rows]
            if values:
                connection.executemany(sql, values)

        connection.commit()
        connection.execute("VACUUM")
        connection.close()
        _replace_database_file(temp_path, db_path)
    except Exception:
        connection.close()
        if temp_path.exists():
            temp_path.unlink()
        raise

    return {
        "path": str(db_path),
        "schema_version": DATABASE_SCHEMA_VERSION,
        "source_count": len(source_files),
        "paragraph_count": len(paragraphs),
        "eligible_paragraph_count": sum(1 for item in paragraphs if item.get("eligible_for_search")),
    }


def _replace_database_file(temp_path: Path, db_path: Path, attempts: int = 7) -> None:
    """Replace a live SQLite file after short-lived Windows locks clear."""

    for attempt in range(attempts):
        try:
            temp_path.replace(db_path)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.08 * (attempt + 1))


def _load_payload_rows(connection: sqlite3.Connection, table: str, order_by: str = "rowid") -> List[Dict[str, object]]:
    return [json.loads(row[0]) for row in connection.execute(f"SELECT payload_json FROM {table} ORDER BY {order_by}")]


def load_database_index(db_path: Path) -> Dict[str, object]:
    """Load the small metadata/catalog portion used by the Web UI."""

    connection = sqlite3.connect(str(db_path))
    try:
        metadata = {str(row[0]): json.loads(row[1]) for row in connection.execute("SELECT key, value_json FROM metadata")}
        result = {
            "metadata": metadata,
            "source_files": _load_payload_rows(connection, "source_files", "source_file_id"),
            "volumes": _load_payload_rows(connection, "volumes", "volume_id"),
            "works": _load_payload_rows(connection, "works", "rowid"),
        }
        return result
    finally:
        connection.close()


def replace_source_in_database(
    extracted: Dict[str, object],
    db_path: Path = DEFAULT_DATABASE_PATH,
    *,
    backup_existing: bool = True,
) -> Dict[str, object]:
    """Atomically replace one imported source without rebuilding the corpus."""

    sources = [item for item in extracted.get("source_files", []) if isinstance(item, dict)]
    if len(sources) != 1 or not sources[0].get("source_file_id"):
        raise ValueError("A targeted database update requires exactly one source file.")
    source = sources[0]
    source_id = str(source["source_file_id"])
    db_path = Path(db_path)
    backup_path = _backup_database(db_path) if backup_existing else None
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
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
        connection.execute(
            "DELETE FROM page_anchors WHERE paragraph_id IN "
            "(SELECT paragraph_id FROM paragraphs WHERE source_file_id = ?)",
            (source_id,),
        )
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
                    _json(item),
                )
                for item in paragraphs
                if item.get("paragraph_id")
            ],
        )
        for table_name, key_fields in (
            ("page_anchors", ("paragraph_id",)),
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


def delete_source_from_database(
    source_file_id: str,
    db_path: Path = DEFAULT_DATABASE_PATH,
    *,
    backup_existing: bool = True,
) -> Dict[str, object]:
    """Delete one source and all source-owned search rows in one transaction."""

    source_file_id = str(source_file_id or "").strip()
    if not source_file_id:
        raise ValueError("source_file_id is required")
    db_path = Path(db_path)
    backup_path = _backup_database(db_path) if backup_existing else None
    connection = sqlite3.connect(str(db_path))
    counts: Dict[str, int] = {}
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        source = connection.execute(
            "SELECT source_type FROM source_files WHERE source_file_id = ?", (source_file_id,)
        ).fetchone()
        if source is None:
            raise ValueError("文献不存在。")
        if str(source[0]) != "pdf":
            raise ValueError("当前移除服务仅允许处理 PDF 文献。")
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
        counts["paragraphs"] = connection.execute(
            "SELECT COUNT(*) FROM paragraphs WHERE source_file_id = ?", (source_file_id,)
        ).fetchone()[0]
        counts["pdf_pages"] = connection.execute(
            "SELECT COUNT(*) FROM pdf_pages WHERE source_file_id = ?", (source_file_id,)
        ).fetchone()[0]
        counts["page_anchors"] = connection.execute(
            "SELECT COUNT(*) FROM page_anchors WHERE paragraph_id IN "
            "(SELECT paragraph_id FROM paragraphs WHERE source_file_id = ?)",
            (source_file_id,),
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM page_anchors WHERE paragraph_id IN "
            "(SELECT paragraph_id FROM paragraphs WHERE source_file_id = ?)",
            (source_file_id,),
        )
        if volume_ids:
            placeholders = ",".join("?" for _ in volume_ids)
            connection.execute(f"DELETE FROM toc_entries WHERE volume_id IN ({placeholders})", volume_ids)
        if work_ids:
            placeholders = ",".join("?" for _ in work_ids)
            connection.execute(f"DELETE FROM toc_entries WHERE work_id IN ({placeholders})", work_ids)
            connection.execute(f"DELETE FROM works WHERE work_id IN ({placeholders})", work_ids)
        connection.execute("DELETE FROM paragraphs WHERE source_file_id = ?", (source_file_id,))
        for table in ("pdf_pages", "pdf_page_mappings", "pdf_import_runs", "audit_issues"):
            connection.execute(f"DELETE FROM {table} WHERE source_file_id = ?", (source_file_id,))
        connection.execute("DELETE FROM volumes WHERE source_file_id = ?", (source_file_id,))
        connection.execute("DELETE FROM source_files WHERE source_file_id = ?", (source_file_id,))
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
        "source_file_id": source_file_id,
        "deleted": counts,
        "backup_path": str(backup_path) if backup_path else None,
        **totals,
    }


def open_database(db_path: Path) -> sqlite3.Connection:
    # The local Web server handles requests in worker threads. SQLite's
    # serialized mode is safe for this read-only connection when the Python
    # thread check is disabled.
    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection

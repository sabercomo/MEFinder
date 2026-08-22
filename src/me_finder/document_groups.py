"""DocumentGroup + membership storage (additive v3 tables).

A DocumentGroup expresses only "these SourceFiles are versions / original /
translations of the same work". Membership is a dedicated join table with
``UNIQUE(source_file_id)`` so a SourceFile belongs to at most one group. Text
alignment between members is a separate layer and is intentionally absent here.

Storage is additive: the tables are created on demand and never require the
content index (paragraphs / OCR) to be rebuilt. Deleting a group or a member
never deletes the underlying SourceFile.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .database import DEFAULT_DATABASE_PATH, DATABASE_SCHEMA_VERSION
from .document_group_metadata import canonical_version_label, member_display_name

TITLE_MAX_LENGTH = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def install_document_group_schema(connection: sqlite3.Connection) -> bool:
    """Create the two group tables on an open connection if absent (idempotent)."""

    changed = False
    if not _table_exists(connection, "document_groups"):
        connection.execute(
            """
            CREATE TABLE document_groups (
                document_group_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                base_source_file_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        changed = True
    if not _table_exists(connection, "document_group_members"):
        connection.execute(
            """
            CREATE TABLE document_group_members (
                document_group_id TEXT NOT NULL
                    REFERENCES document_groups(document_group_id) ON DELETE CASCADE,
                source_file_id TEXT NOT NULL UNIQUE,
                version_label TEXT,
                member_order INTEGER NOT NULL DEFAULT 0,
                added_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_group_members_group "
            "ON document_group_members(document_group_id)"
        )
        changed = True
    return changed


def ensure_document_group_schema(db_path: Path = DEFAULT_DATABASE_PATH) -> bool:
    """Additive migration: install group tables and bump user_version to v3.

    Never rebuilds content; an existing index only gains two empty tables.
    """

    path = Path(db_path)
    if not path.exists():
        return False
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("BEGIN IMMEDIATE")
        changed = install_document_group_schema(connection)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < DATABASE_SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
            changed = True
        connection.commit()
        return changed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _connect_writable(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    return connection


def _clean_title(value: object) -> str:
    title = str(value or "").strip()
    if not title:
        raise ValueError("作品组标题不能为空。")
    if len(title) > TITLE_MAX_LENGTH:
        raise ValueError("作品组标题不能超过 200 个字符。")
    return title


def _require_source_exists(connection: sqlite3.Connection, source_id: str) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM source_files WHERE source_file_id = ?", (source_id,)
        ).fetchone()
        is None
    ):
        raise ValueError("文献不存在。")


def _require_group(connection: sqlite3.Connection, group_id: str) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM document_groups WHERE document_group_id = ?", (group_id,)
        ).fetchone()
        is None
    ):
        raise ValueError("作品组不存在。")


def create_document_group(
    title: object, db_path: Path = DEFAULT_DATABASE_PATH
) -> Dict[str, object]:
    group_title = _clean_title(title)
    group_id = f"document-group-{uuid.uuid4().hex}"
    timestamp = _now()
    ensure_document_group_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO document_groups"
            "(document_group_id, title, base_source_file_id, created_at, updated_at) "
            "VALUES (?, ?, NULL, ?, ?)",
            (group_id, group_title, timestamp, timestamp),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "document_group_id": group_id,
        "title": group_title,
        "base_source_file_id": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def rename_document_group(
    document_group_id: object,
    title: object,
    db_path: Path = DEFAULT_DATABASE_PATH,
) -> Dict[str, object]:
    group_id = str(document_group_id or "").strip()
    if not group_id:
        raise ValueError("document_group_id is required")
    group_title = _clean_title(title)
    timestamp = _now()
    ensure_document_group_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE document_groups SET title = ?, updated_at = ? "
            "WHERE document_group_id = ?",
            (group_title, timestamp, group_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("作品组不存在。")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "document_group_id": group_id,
        "title": group_title,
        "updated_at": timestamp,
    }


def delete_document_group(
    document_group_id: object, db_path: Path = DEFAULT_DATABASE_PATH
) -> Dict[str, object]:
    group_id = str(document_group_id or "").strip()
    if not group_id:
        raise ValueError("document_group_id is required")
    ensure_document_group_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_group(connection, group_id)
        unlinked = int(
            connection.execute(
                "SELECT COUNT(*) FROM document_group_members "
                "WHERE document_group_id = ?",
                (group_id,),
            ).fetchone()[0]
        )
        # Explicit member cleanup (independent of FK cascade); source_files untouched.
        connection.execute(
            "DELETE FROM document_group_members WHERE document_group_id = ?",
            (group_id,),
        )
        connection.execute(
            "DELETE FROM document_groups WHERE document_group_id = ?", (group_id,)
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"document_group_id": group_id, "unlinked_count": unlinked}


def add_group_member(
    document_group_id: object,
    source_file_id: object,
    db_path: Path = DEFAULT_DATABASE_PATH,
    version_label: object = None,
) -> Dict[str, object]:
    """Assign a SourceFile to a group.

    ``UNIQUE(source_file_id)`` means one group per SourceFile: if the file is
    already in another group it is moved here (its previous group's base pointer
    is cleared if it pointed at this file). An existing version_label is kept
    unless a new one is supplied.
    """

    group_id = str(document_group_id or "").strip()
    source_id = str(source_file_id or "").strip()
    if not group_id:
        raise ValueError("document_group_id is required")
    if not source_id:
        raise ValueError("source_file_id is required")
    supplied_label = canonical_version_label(version_label)
    timestamp = _now()
    ensure_document_group_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_group(connection, group_id)
        _require_source_exists(connection, source_id)
        existing = connection.execute(
            "SELECT document_group_id, version_label FROM document_group_members "
            "WHERE source_file_id = ?",
            (source_id,),
        ).fetchone()
        kept_label = supplied_label or (
            str(existing["version_label"] or "") if existing else ""
        )
        if existing is not None:
            previous_group = existing["document_group_id"]
            if previous_group != group_id:
                connection.execute(
                    "UPDATE document_groups SET base_source_file_id = NULL, "
                    "updated_at = ? WHERE document_group_id = ? "
                    "AND base_source_file_id = ?",
                    (timestamp, previous_group, source_id),
                )
            connection.execute(
                "DELETE FROM document_group_members WHERE source_file_id = ?",
                (source_id,),
            )
        order = int(
            connection.execute(
                "SELECT COALESCE(MAX(member_order), -1) + 1 "
                "FROM document_group_members WHERE document_group_id = ?",
                (group_id,),
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO document_group_members"
            "(document_group_id, source_file_id, version_label, member_order, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_id, source_id, kept_label or None, order, timestamp),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "document_group_id": group_id,
        "source_file_id": source_id,
        "version_label": kept_label,
        "member_order": order,
    }


def remove_group_member(
    source_file_id: object, db_path: Path = DEFAULT_DATABASE_PATH
) -> Dict[str, object]:
    source_id = str(source_file_id or "").strip()
    if not source_id:
        raise ValueError("source_file_id is required")
    timestamp = _now()
    ensure_document_group_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT document_group_id FROM document_group_members "
            "WHERE source_file_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise ValueError("该文献不在任何作品组中。")
        group_id = row["document_group_id"]
        connection.execute(
            "DELETE FROM document_group_members WHERE source_file_id = ?",
            (source_id,),
        )
        # If the removed member was the group's base version, clear the pointer.
        connection.execute(
            "UPDATE document_groups SET base_source_file_id = NULL, updated_at = ? "
            "WHERE document_group_id = ? AND base_source_file_id = ?",
            (timestamp, group_id, source_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"document_group_id": group_id, "source_file_id": source_id}


def set_document_group_base(
    document_group_id: object,
    base_source_file_id: object,
    db_path: Path = DEFAULT_DATABASE_PATH,
) -> Dict[str, object]:
    """Set (or clear, with an empty value) the group's default base version.

    A non-empty base must already be a member of the group. The base is only a
    default reading / computation anchor; it carries no alignment structure.
    """

    group_id = str(document_group_id or "").strip()
    base_id = str(base_source_file_id or "").strip()
    if not group_id:
        raise ValueError("document_group_id is required")
    timestamp = _now()
    ensure_document_group_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_group(connection, group_id)
        if base_id:
            member = connection.execute(
                "SELECT 1 FROM document_group_members "
                "WHERE document_group_id = ? AND source_file_id = ?",
                (group_id, base_id),
            ).fetchone()
            if member is None:
                raise ValueError("基准版本必须是该作品组的成员。")
        connection.execute(
            "UPDATE document_groups SET base_source_file_id = ?, updated_at = ? "
            "WHERE document_group_id = ?",
            (base_id or None, timestamp, group_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"document_group_id": group_id, "base_source_file_id": base_id or None}


def set_member_version_label(
    source_file_id: object,
    version_label: object,
    db_path: Path = DEFAULT_DATABASE_PATH,
) -> Dict[str, object]:
    source_id = str(source_file_id or "").strip()
    if not source_id:
        raise ValueError("source_file_id is required")
    label = canonical_version_label(version_label)
    ensure_document_group_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE document_group_members SET version_label = ? "
            "WHERE source_file_id = ?",
            (label or None, source_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("该文献不在任何作品组中。")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"source_file_id": source_id, "version_label": label}


def _member_source_payload(row: sqlite3.Row) -> Dict[str, object]:
    payload: Dict[str, object] = {}
    raw = row["payload_json"] if "payload_json" in row.keys() else None
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                payload = loaded
        except (TypeError, ValueError):
            payload = {}
    payload.setdefault("source_file_id", row["source_file_id"])
    if row["file_name"] is not None:
        payload.setdefault("file_name", row["file_name"])
    return payload


def list_document_groups(
    db_path: Path = DEFAULT_DATABASE_PATH,
) -> List[Dict[str, object]]:
    """Read all groups with members + resolved display names (read-only)."""

    path = Path(db_path)
    if not path.exists():
        return []
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        if not _table_exists(connection, "document_groups"):
            return []
        groups = connection.execute(
            "SELECT document_group_id, title, base_source_file_id, created_at, "
            "updated_at FROM document_groups ORDER BY title"
        ).fetchall()
        result: List[Dict[str, object]] = []
        for group in groups:
            members = connection.execute(
                "SELECT m.source_file_id AS source_file_id, m.version_label AS "
                "version_label, m.member_order AS member_order, "
                "s.file_name AS file_name, s.payload_json AS payload_json "
                "FROM document_group_members m "
                "LEFT JOIN source_files s ON s.source_file_id = m.source_file_id "
                "WHERE m.document_group_id = ? ORDER BY m.member_order, "
                "m.source_file_id",
                (group["document_group_id"],),
            ).fetchall()
            result.append(
                {
                    "document_group_id": group["document_group_id"],
                    "title": group["title"],
                    "base_source_file_id": group["base_source_file_id"],
                    "created_at": group["created_at"],
                    "updated_at": group["updated_at"],
                    "members": [
                        {
                            "source_file_id": member["source_file_id"],
                            "version_label": member["version_label"] or "",
                            "member_order": member["member_order"],
                            "display_name": member_display_name(
                                member["version_label"],
                                _member_source_payload(member),
                            ),
                            "is_base": member["source_file_id"]
                            == group["base_source_file_id"],
                        }
                        for member in members
                    ],
                }
            )
        return result
    finally:
        connection.close()


def document_group_for_source(
    source_file_id: object, db_path: Path = DEFAULT_DATABASE_PATH
) -> Optional[str]:
    """Return the group id a SourceFile belongs to, or None."""

    source_id = str(source_file_id or "").strip()
    path = Path(db_path)
    if not source_id or not path.exists():
        return None
    connection = sqlite3.connect(str(path))
    try:
        if not _table_exists(connection, "document_group_members"):
            return None
        row = connection.execute(
            "SELECT document_group_id FROM document_group_members "
            "WHERE source_file_id = ?",
            (source_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        connection.close()

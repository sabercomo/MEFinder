"""SQLite queue of agent-proposed bibliographic fills (schema v9).

The MCP sidecar writes ``pending``/``confirmed`` rows; the desktop runtime
reads ``confirmed`` rows and records ``applied``/``failed``. Tokens are kept
only as SHA-256 digests. Rows carry no foreign key to ``source_files`` and are
snapshotted across full rebuilds like the Zotero bookkeeping tables.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from .connection import open_readonly_index, open_writable_index, table_exists
from .schema_installers import install_bibliographic_update_schema

_TABLE = "bibliographic_update_requests"


class BibliographicUpdateNotFound(LookupError):
    """No request with that id, or the token does not match it."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_row(row: sqlite3.Row) -> Dict[str, object]:
    return {
        "request_id": row["request_id"],
        "source_file_id": row["source_file_id"],
        "status": row["status"],
        "fields": json.loads(row["fields_json"]),
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "applied_at": row["applied_at"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
    }


def insert_pending_request(
    database_path: Path,
    *,
    request_id: str,
    source_file_id: str,
    fields: List[Mapping[str, object]],
    token: str,
) -> None:
    """Record one pending proposal, installing the table on older indexes."""

    connection = open_writable_index(Path(database_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        install_bibliographic_update_schema(connection)
        connection.execute(
            f"INSERT INTO {_TABLE}(request_id, source_file_id, status, fields_json, "
            "token_sha256, created_at) VALUES (?, ?, 'pending', ?, ?, ?)",
            (
                request_id,
                source_file_id,
                json.dumps(list(fields), ensure_ascii=False),
                _digest(token),
                _now(),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def confirm_pending_request(
    database_path: Path, request_id: str, token: str
) -> Dict[str, object]:
    """Promote a pending request when the token matches; idempotent once confirmed."""

    connection = open_writable_index(Path(database_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = (
            connection.execute(
                f"SELECT * FROM {_TABLE} WHERE request_id = ?", (request_id,)
            ).fetchone()
            if table_exists(connection, _TABLE)
            else None
        )
        if row is None or row["token_sha256"] != _digest(token):
            raise BibliographicUpdateNotFound("补全请求不存在或确认码不匹配")
        if row["status"] == "pending":
            connection.execute(
                f"UPDATE {_TABLE} SET status = 'confirmed', confirmed_at = ? "
                "WHERE request_id = ?",
                (_now(), request_id),
            )
        connection.commit()
        confirmed = connection.execute(
            f"SELECT * FROM {_TABLE} WHERE request_id = ?", (request_id,)
        ).fetchone()
        return _public_row(confirmed)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_requests(
    database_path: Path,
    *,
    source_file_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, object]]:
    """Newest first; an index without the table simply has no requests."""

    path = Path(database_path)
    if not path.is_file():
        return []
    connection = open_readonly_index(path)
    try:
        if not table_exists(connection, _TABLE):
            return []
        clauses = []
        parameters: List[object] = []
        if source_file_id is not None:
            clauses.append("source_file_id = ?")
            parameters.append(source_file_id)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = connection.execute(
            f"SELECT * FROM {_TABLE} {where}ORDER BY created_at DESC, request_id "
            "LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        return [_public_row(row) for row in rows]
    finally:
        connection.close()


def record_request_result(
    database_path: Path,
    request_id: str,
    *,
    status: str,
    result: Mapping[str, object],
) -> bool:
    """Close a confirmed request as ``applied``/``failed``; False if already closed."""

    if status not in {"applied", "failed"}:
        raise ValueError("status must be applied or failed")
    connection = open_writable_index(Path(database_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            f"UPDATE {_TABLE} SET status = ?, applied_at = ?, result_json = ? "
            "WHERE request_id = ? AND status = 'confirmed'",
            (status, _now(), json.dumps(dict(result), ensure_ascii=False), request_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def read_bibliographic_update_snapshot(db_path: Path) -> List[Dict[str, object]]:
    """Copy every request row out of an index about to be replaced."""

    path = Path(db_path)
    if not path.exists():
        return []
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            return []
    connection = open_readonly_index(path)
    try:
        if not table_exists(connection, _TABLE):
            return []
        return [dict(row) for row in connection.execute(f"SELECT * FROM {_TABLE}")]
    finally:
        connection.close()


def restore_bibliographic_update_snapshot(
    connection: sqlite3.Connection, rows: List[Mapping[str, object]]
) -> None:
    """Re-insert preserved requests into a freshly built index (open transaction)."""

    if not rows:
        return
    install_bibliographic_update_schema(connection)
    for row in rows:
        columns = list(row)
        connection.execute(
            f"INSERT OR REPLACE INTO {_TABLE}({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [row[column] for column in columns],
        )

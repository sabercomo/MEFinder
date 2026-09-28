"""SQLite repository for the Zotero sync bookkeeping tables (schema v8).

The tables only cache what MEFinder last read from Zotero; Zotero stays the
source of truth. Every write opens its own short transaction on the index
database, the same way the translation-workspace tables are written.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional

from .connection import open_writable_index
from .schema_installers import install_zotero_sync_schema


MY_LIBRARY_ID = "users/0"

ITEM_COLUMNS = (
    "item_key",
    "item_version",
    "fingerprint",
    "data_json",
    "collections_json",
    "metadata_fingerprint_applied",
)
ATTACHMENT_COLUMNS = (
    "attachment_key",
    "parent_item_key",
    "attachment_version",
    "link_mode",
    "content_type",
    "file_name",
    "file_signature",
    "file_sha256",
    "source_file_id",
    "origin",
    "status",
    "import_job_id",
    "replaces_source_file_id",
    "status_message",
)
STATE_COLUMNS = (
    "server_id",
    "synced_collections_json",
    "attachment_index_json",
    "last_attempt_at",
    "last_success_at",
    "last_result_json",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StoredZoteroState:
    """Everything the sync diff needs from the previous run."""

    items: Dict[str, Dict[str, object]] = field(default_factory=dict)
    attachments: Dict[str, Dict[str, object]] = field(default_factory=dict)
    server_id: Optional[str] = None
    synced_collections: List[str] = field(default_factory=list)
    attachment_index: Dict[str, Dict[str, object]] = field(default_factory=dict)
    last_attempt_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_result: Optional[Dict[str, object]] = None


def _json_or(value: object, fallback):
    if not isinstance(value, str) or not value:
        return fallback
    try:
        decoded = json.loads(value)
    except ValueError:
        return fallback
    return decoded if isinstance(decoded, type(fallback)) else fallback


class ZoteroSyncStore:
    """Read and write the Zotero link tables of one index database."""

    def __init__(self, database_path: Path, library_id: str = MY_LIBRARY_ID) -> None:
        self._database_path = Path(database_path)
        self._library_id = library_id

    @property
    def library_id(self) -> str:
        return self._library_id

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = open_writable_index(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            install_zotero_sync_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read(self) -> StoredZoteroState:
        state = StoredZoteroState()
        if not self._database_path.exists():
            return state
        with self._transaction() as connection:
            for row in connection.execute(
                f"SELECT {', '.join(ITEM_COLUMNS)} FROM zotero_items WHERE library_id = ?",
                (self._library_id,),
            ):
                state.items[str(row["item_key"])] = dict(row)
            for row in connection.execute(
                f"SELECT {', '.join(ATTACHMENT_COLUMNS)} FROM zotero_attachments "
                "WHERE library_id = ?",
                (self._library_id,),
            ):
                state.attachments[str(row["attachment_key"])] = dict(row)
            row = connection.execute(
                f"SELECT {', '.join(STATE_COLUMNS)} FROM zotero_sync_state "
                "WHERE library_id = ?",
                (self._library_id,),
            ).fetchone()
        if row is not None:
            state.server_id = row["server_id"] or None
            state.synced_collections = [
                str(key) for key in _json_or(row["synced_collections_json"], [])
            ]
            state.attachment_index = _json_or(row["attachment_index_json"], {})
            state.last_attempt_at = row["last_attempt_at"]
            state.last_success_at = row["last_success_at"]
            state.last_result = _json_or(row["last_result_json"], {}) or None
        return state

    def write(
        self,
        *,
        items: Iterable[Mapping[str, object]] = (),
        deleted_items: Iterable[str] = (),
        attachments: Iterable[Mapping[str, object]] = (),
        deleted_attachments: Iterable[str] = (),
        state: Optional[Mapping[str, object]] = None,
    ) -> None:
        """Apply one batch of row changes atomically."""

        timestamp = _now()
        with self._transaction() as connection:
            for key in deleted_items:
                connection.execute(
                    "DELETE FROM zotero_items WHERE library_id = ? AND item_key = ?",
                    (self._library_id, str(key)),
                )
            for key in deleted_attachments:
                connection.execute(
                    "DELETE FROM zotero_attachments "
                    "WHERE library_id = ? AND attachment_key = ?",
                    (self._library_id, str(key)),
                )
            for item in items:
                values = [item.get(column) for column in ITEM_COLUMNS]
                connection.execute(
                    "INSERT OR REPLACE INTO zotero_items(library_id, "
                    f"{', '.join(ITEM_COLUMNS)}, updated_at) VALUES "
                    f"(?, {', '.join('?' for _ in ITEM_COLUMNS)}, ?)",
                    (self._library_id, *values, timestamp),
                )
            for attachment in attachments:
                values = [attachment.get(column) for column in ATTACHMENT_COLUMNS]
                connection.execute(
                    "INSERT OR REPLACE INTO zotero_attachments(library_id, "
                    f"{', '.join(ATTACHMENT_COLUMNS)}, updated_at) VALUES "
                    f"(?, {', '.join('?' for _ in ATTACHMENT_COLUMNS)}, ?)",
                    (self._library_id, *values, timestamp),
                )
            if state:
                connection.execute(
                    "INSERT OR IGNORE INTO zotero_sync_state(library_id) VALUES (?)",
                    (self._library_id,),
                )
                for column, value in state.items():
                    if column not in STATE_COLUMNS:
                        raise ValueError(f"unknown zotero_sync_state column: {column}")
                    connection.execute(
                        f"UPDATE zotero_sync_state SET {column} = ? WHERE library_id = ?",
                        (value, self._library_id),
                    )


# ── full-rebuild preservation ───────────────────────────────────────────────

_SNAPSHOT_TABLES = ("zotero_items", "zotero_attachments", "zotero_sync_state")


def read_zotero_sync_snapshot(db_path: Path) -> Dict[str, List[Dict[str, object]]]:
    """Copy every Zotero bookkeeping row out of an index about to be replaced."""

    snapshot: Dict[str, List[Dict[str, object]]] = {name: [] for name in _SNAPSHOT_TABLES}
    path = Path(db_path)
    if not path.exists():
        return snapshot
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            return snapshot
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        for table in _SNAPSHOT_TABLES:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                snapshot[table] = [
                    dict(row) for row in connection.execute(f"SELECT * FROM {table}")
                ]
    finally:
        connection.close()
    return snapshot


def restore_zotero_sync_snapshot(
    connection: sqlite3.Connection, snapshot: Mapping[str, List[Mapping[str, object]]]
) -> None:
    """Re-insert preserved rows into a freshly built index (open transaction).

    Links to documents the rebuild dropped are kept on purpose: the next sync
    sees the missing document and re-imports or re-links it.
    """

    if not any(snapshot.get(table) for table in _SNAPSHOT_TABLES):
        return
    install_zotero_sync_schema(connection)
    for table in _SNAPSHOT_TABLES:
        for row in snapshot.get(table) or []:
            columns = list(row)
            connection.execute(
                f"INSERT OR REPLACE INTO {table}({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [row[column] for column in columns],
            )

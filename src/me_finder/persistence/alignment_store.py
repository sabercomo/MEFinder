"""SQLite operations for manual alignment corrections.

The caller owns route and segment validation. This store keeps the proposal,
confirmation, and revocation SQL on the same connection and transaction as
those checks.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .connection import connect_index, open_writable_index, table_exists
from .schema_installers import install_text_alignment_schema


@contextmanager
def override_read_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Keep one read connection open during route and segment validation."""

    connection = connect_index(str(db_path))
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def override_write_transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open one immediate write transaction for a correction operation."""

    connection = open_writable_index(Path(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        connection.rollback()
        raise
    finally:
        connection.close()


def insert_pending_override(
    connection: sqlite3.Connection,
    context: dict[str, object],
    override_id: str,
    confirmation_token: str,
    evidence_json: str,
    timestamp: str,
) -> None:
    """Replace an earlier pending proposal for the same selection."""

    install_text_alignment_schema(connection)
    connection.execute(
        "DELETE FROM alignment_manual_overrides WHERE source_file_id = ? "
        "AND target_source_file_id = ? AND source_segment_set_id = ? "
        "AND source_segment_key = ? AND status = 'pending'",
        (
            context["source_file_id"],
            context["target_source_file_id"],
            context["source_segment_set_id"],
            context["source_segment_key"],
        ),
    )
    connection.execute(
        "INSERT INTO alignment_manual_overrides(override_id, "
        "document_group_id, source_file_id, target_source_file_id, "
        "source_segment_set_id, target_segment_set_id, source_segment_key, "
        "source_segment_ids_json, target_segment_ids_json, status, "
        "confirmation_token, evidence_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (
            override_id,
            context["document_group_id"],
            context["source_file_id"],
            context["target_source_file_id"],
            context["source_segment_set_id"],
            context["target_segment_set_id"],
            context["source_segment_key"],
            json.dumps(
                context["source_segment_ids"], ensure_ascii=False, separators=(",", ":")
            ),
            json.dumps(
                context["target_segment_ids"], ensure_ascii=False, separators=(",", ":")
            ),
            confirmation_token,
            evidence_json,
            timestamp,
        ),
    )


def load_override(connection: sqlite3.Connection, override_id: str) -> sqlite3.Row | None:
    """Load a proposal when the correction table exists."""

    if not table_exists(connection, "alignment_manual_overrides"):
        return None
    return connection.execute(
        "SELECT * FROM alignment_manual_overrides WHERE override_id = ?", (override_id,)
    ).fetchone()


def revoke_expired_override(
    connection: sqlite3.Connection, override_id: str, timestamp: str
) -> None:
    """Persist a stale proposal before its caller reports the conflict."""

    connection.execute(
        "UPDATE alignment_manual_overrides SET status = 'revoked', "
        "revoked_at = ? WHERE override_id = ?",
        (timestamp, override_id),
    )
    connection.commit()


def confirm_pending_override(
    connection: sqlite3.Connection, row: sqlite3.Row, override_id: str, timestamp: str
) -> None:
    """Retire the prior confirmation before activating this proposal."""

    connection.execute(
        "UPDATE alignment_manual_overrides SET status = 'revoked', "
        "revoked_at = ? WHERE status = 'confirmed' AND source_file_id = ? "
        "AND target_source_file_id = ? AND source_segment_set_id = ? "
        "AND source_segment_key = ?",
        (
            timestamp,
            str(row["source_file_id"]),
            str(row["target_source_file_id"]),
            str(row["source_segment_set_id"]),
            str(row["source_segment_key"]),
        ),
    )
    connection.execute(
        "UPDATE alignment_manual_overrides SET status = 'confirmed', "
        "confirmed_at = ? WHERE override_id = ?",
        (timestamp, override_id),
    )


def revoke_override_row(
    connection: sqlite3.Connection, override_id: str, timestamp: str
) -> None:
    """Persist revocation of a pending or confirmed proposal."""

    connection.execute(
        "UPDATE alignment_manual_overrides SET status = 'revoked', "
        "revoked_at = ? WHERE override_id = ?",
        (timestamp, override_id),
    )


def read_override_rows(
    db_path: Path,
    *,
    source_file_id: str | None,
    target_source_file_id: str | None,
    status: str | None,
    limit: int,
) -> list[sqlite3.Row]:
    """Read the requested audit rows, newest first."""

    filters: list[str] = []
    parameters: list[object] = []
    for column, value in (
        ("source_file_id", source_file_id),
        ("target_source_file_id", target_source_file_id),
        ("status", status),
    ):
        if value is not None:
            filters.append(f"{column} = ?")
            parameters.append(value)
    connection = connect_index(str(db_path))
    try:
        if not table_exists(connection, "alignment_manual_overrides"):
            return []
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        return connection.execute(
            "SELECT override_id, document_group_id, source_file_id, "
            "target_source_file_id, source_segment_ids_json, "
            "target_segment_ids_json, status, evidence_json, created_at, "
            "confirmed_at, revoked_at FROM alignment_manual_overrides "
            f"{where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
    finally:
        connection.close()

"""Alignment between DocumentGroup members (additive v4 tables).

An alignment expresses "this passage in version A corresponds to that passage in
version B (…and C, D)". It is a symmetric layer on top of the existing paragraph
model — a segment is only a thin reference to a contiguous paragraph range on one
version, never a copy of text or a page. Pages are used for locate/display only,
never as a structural unit; the DocumentGroup's base version is a reading/compute
default and is intentionally NOT stored in these tables.

Design invariants (validated in the service, not all expressible in SQL):
  * segment.source_file_id must be a member of the alignment_group's DocumentGroup;
  * a segment is a contiguous paragraph_index range; discontinuity = multiple
    segments ordered by segment_order;
  * within one (document_group, source_file), active (non-rejected) segments must
    not overlap across alignment_groups — so a paragraph resolves to at most one
    active alignment_group (deterministic locate);
  * review_status (proposed/confirmed/rejected) and is_stale (integrity) are
    orthogonal — detecting drift must never overwrite a confirmed/rejected review.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .database import DATABASE_SCHEMA_VERSION, DEFAULT_DATABASE_PATH

REVIEW_STATUSES = ("proposed", "confirmed", "rejected")
_FINGERPRINT_SEP = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def install_alignment_schema(connection: sqlite3.Connection) -> bool:
    """Create the alignment tables if absent (idempotent)."""

    changed = False
    if not _table_exists(connection, "alignment_groups"):
        connection.execute(
            """
            CREATE TABLE alignment_groups (
                alignment_group_id TEXT PRIMARY KEY,
                document_group_id  TEXT NOT NULL
                    REFERENCES document_groups(document_group_id) ON DELETE CASCADE,
                review_status      TEXT NOT NULL DEFAULT 'proposed',
                is_stale           INTEGER NOT NULL DEFAULT 0,
                provenance         TEXT NOT NULL DEFAULT 'manual',
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL,
                payload_json       TEXT NOT NULL DEFAULT '{}',
                CHECK (review_status IN ('proposed','confirmed','rejected')),
                CHECK (is_stale IN (0,1))
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_alignment_groups_docgroup "
            "ON alignment_groups(document_group_id)"
        )
        changed = True
    if not _table_exists(connection, "alignment_segments"):
        connection.execute(
            """
            CREATE TABLE alignment_segments (
                alignment_segment_id  TEXT PRIMARY KEY,
                alignment_group_id    TEXT NOT NULL
                    REFERENCES alignment_groups(alignment_group_id) ON DELETE CASCADE,
                source_file_id        TEXT NOT NULL,
                start_paragraph_id    TEXT NOT NULL,
                end_paragraph_id      TEXT NOT NULL,
                start_paragraph_index INTEGER NOT NULL,
                end_paragraph_index   INTEGER NOT NULL,
                segment_order         INTEGER NOT NULL DEFAULT 0,
                text_fingerprint      TEXT,
                created_at            TEXT NOT NULL,
                updated_at            TEXT NOT NULL,
                payload_json          TEXT NOT NULL DEFAULT '{}',
                UNIQUE (alignment_group_id, source_file_id, segment_order),
                CHECK (start_paragraph_index <= end_paragraph_index)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_alignment_segments_group "
            "ON alignment_segments(alignment_group_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_alignment_segments_locate "
            "ON alignment_segments(source_file_id, start_paragraph_index, end_paragraph_index)"
        )
        changed = True
    return changed


def ensure_alignment_schema(db_path: Path = DEFAULT_DATABASE_PATH) -> bool:
    """Additive migration: install alignment tables and bump user_version to v4."""

    path = Path(db_path)
    if not path.exists():
        return False
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("BEGIN IMMEDIATE")
        changed = install_alignment_schema(connection)
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


# ── validation / fingerprint helpers ──


def _document_group_exists(connection: sqlite3.Connection, group_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM document_groups WHERE document_group_id = ?", (group_id,)
        ).fetchone()
        is not None
    )


def _is_member(connection: sqlite3.Connection, group_id: str, source_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM document_group_members "
            "WHERE document_group_id = ? AND source_file_id = ?",
            (group_id, source_id),
        ).fetchone()
        is not None
    )


def _paragraph_index(
    connection: sqlite3.Connection, source_id: str, paragraph_id: str
) -> Optional[int]:
    row = connection.execute(
        "SELECT paragraph_index FROM paragraphs "
        "WHERE paragraph_id = ? AND source_file_id = ?",
        (paragraph_id, source_id),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _paragraph_span(
    connection: sqlite3.Connection, source_id: str, start_pid: str, end_pid: str
) -> "tuple[int, int]":
    start_index = _paragraph_index(connection, source_id, start_pid)
    if start_index is None:
        raise ValueError("起始段落不存在或不属于该文献。")
    end_index = _paragraph_index(connection, source_id, end_pid)
    if end_index is None:
        raise ValueError("结束段落不存在或不属于该文献。")
    if start_index > end_index:
        raise ValueError("段落范围起点不能晚于终点。")
    return start_index, end_index


def _fingerprint(
    connection: sqlite3.Connection, source_id: str, start_index: int, end_index: int
) -> str:
    """Stable drift signal: sha256 over the compact_text of the paragraph span.

    compact_text is NFKC-normalized with canonical punctuation kept and all
    whitespace removed — invariant to reflow / OCR space noise (common on reparse)
    yet sensitive to real character/punctuation change.
    """

    rows = connection.execute(
        "SELECT compact_text FROM paragraphs "
        "WHERE source_file_id = ? AND paragraph_index BETWEEN ? AND ? "
        "ORDER BY paragraph_index",
        (source_id, start_index, end_index),
    ).fetchall()
    joined = _FINGERPRINT_SEP.join(str(row[0] or "") for row in rows)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _active_overlap(
    connection: sqlite3.Connection,
    document_group_id: str,
    source_id: str,
    start_index: int,
    end_index: int,
    exclude_group_id: Optional[str] = None,
) -> bool:
    """Any active (non-rejected) segment of the same (docgroup, source) whose range
    overlaps [start_index, end_index] in a *different* alignment_group?"""

    sql = (
        "SELECT 1 FROM alignment_segments s "
        "JOIN alignment_groups g ON g.alignment_group_id = s.alignment_group_id "
        "WHERE g.document_group_id = ? AND g.review_status != 'rejected' "
        "AND s.source_file_id = ? "
        "AND s.start_paragraph_index <= ? AND ? <= s.end_paragraph_index"
    )
    args: List[object] = [document_group_id, source_id, end_index, start_index]
    if exclude_group_id is not None:
        sql += " AND s.alignment_group_id != ?"
        args.append(exclude_group_id)
    return connection.execute(sql + " LIMIT 1", args).fetchone() is not None


def _resolve_segments(
    connection: sqlite3.Connection,
    document_group_id: str,
    segments: Sequence[Dict[str, object]],
    exclude_group_id: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Validate membership / spans / overlap and attach index range + fingerprint."""

    resolved: List[Dict[str, object]] = []
    for segment in segments:
        source_id = str(segment.get("source_file_id") or "").strip()
        start_pid = str(segment.get("start_paragraph_id") or "").strip()
        end_pid = str(segment.get("end_paragraph_id") or start_pid).strip()
        if not source_id or not start_pid:
            raise ValueError("segment 缺少 source_file_id 或 start_paragraph_id。")
        if not _is_member(connection, document_group_id, source_id):
            raise ValueError("segment 的文献不属于该作品组。")
        start_index, end_index = _paragraph_span(connection, source_id, start_pid, end_pid)
        resolved.append(
            {
                "source_file_id": source_id,
                "start_paragraph_id": start_pid,
                "end_paragraph_id": end_pid,
                "start_index": start_index,
                "end_index": end_index,
                "fingerprint": _fingerprint(connection, source_id, start_index, end_index),
            }
        )
    # New siblings on the same source must not overlap each other.
    for i in range(len(resolved)):
        for j in range(i + 1, len(resolved)):
            a, b = resolved[i], resolved[j]
            if (
                a["source_file_id"] == b["source_file_id"]
                and a["start_index"] <= b["end_index"]
                and b["start_index"] <= a["end_index"]
            ):
                raise ValueError("同一版本的 segment 范围重叠。")
    # And must not overlap existing active segments across alignment_groups.
    for segment in resolved:
        if _active_overlap(
            connection,
            document_group_id,
            segment["source_file_id"],
            segment["start_index"],
            segment["end_index"],
            exclude_group_id,
        ):
            raise ValueError("该范围与同一版本的已有对齐重叠（跨对齐组不可重叠）。")
    return resolved


def _document_group_of(connection: sqlite3.Connection, alignment_group_id: str) -> str:
    row = connection.execute(
        "SELECT document_group_id FROM alignment_groups WHERE alignment_group_id = ?",
        (alignment_group_id,),
    ).fetchone()
    if row is None:
        raise ValueError("对齐组不存在。")
    return row[0]


def _insert_segments(
    connection: sqlite3.Connection,
    alignment_group_id: str,
    resolved: Sequence[Dict[str, object]],
    order_start: Optional[Dict[str, int]] = None,
) -> None:
    order_by_source: Dict[str, int] = dict(order_start or {})
    timestamp = _now()
    for segment in resolved:
        source_id = str(segment["source_file_id"])
        order = order_by_source.get(source_id, 0)
        order_by_source[source_id] = order + 1
        connection.execute(
            "INSERT INTO alignment_segments"
            "(alignment_segment_id, alignment_group_id, source_file_id, "
            "start_paragraph_id, end_paragraph_id, start_paragraph_index, "
            "end_paragraph_index, segment_order, text_fingerprint, created_at, "
            "updated_at, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?, '{}')",
            (
                f"alignment-segment-{uuid.uuid4().hex}",
                alignment_group_id,
                source_id,
                segment["start_paragraph_id"],
                segment["end_paragraph_id"],
                segment["start_index"],
                segment["end_index"],
                order,
                segment["fingerprint"],
                timestamp,
                timestamp,
            ),
        )


# ── CRUD ──


def create_alignment_group(
    document_group_id: object,
    segments: Sequence[Dict[str, object]],
    db_path: Path = DEFAULT_DATABASE_PATH,
    provenance: object = "manual",
    review_status: object = "proposed",
) -> Dict[str, object]:
    group_id = str(document_group_id or "").strip()
    if not group_id:
        raise ValueError("document_group_id is required")
    status = str(review_status or "proposed")
    if status not in REVIEW_STATUSES:
        raise ValueError("无效的 review_status。")
    prov = str(provenance or "manual").strip() or "manual"
    if not isinstance(segments, (list, tuple)) or not segments:
        raise ValueError("至少需要一个 segment。")
    ensure_alignment_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if not _document_group_exists(connection, group_id):
            raise ValueError("作品组不存在。")
        resolved = _resolve_segments(connection, group_id, segments)
        alignment_group_id = f"alignment-group-{uuid.uuid4().hex}"
        timestamp = _now()
        connection.execute(
            "INSERT INTO alignment_groups"
            "(alignment_group_id, document_group_id, review_status, is_stale, "
            "provenance, created_at, updated_at, payload_json) "
            "VALUES (?,?,?,0,?,?,?, '{}')",
            (alignment_group_id, group_id, status, prov, timestamp, timestamp),
        )
        _insert_segments(connection, alignment_group_id, resolved)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "alignment_group_id": alignment_group_id,
        "document_group_id": group_id,
        "review_status": status,
        "provenance": prov,
        "segment_count": len(resolved),
    }


def add_alignment_segment(
    alignment_group_id: object,
    source_file_id: object,
    start_paragraph_id: object,
    end_paragraph_id: object = None,
    db_path: Path = DEFAULT_DATABASE_PATH,
) -> Dict[str, object]:
    group_id = str(alignment_group_id or "").strip()
    if not group_id:
        raise ValueError("alignment_group_id is required")
    ensure_alignment_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        document_group_id = _document_group_of(connection, group_id)
        segment = {
            "source_file_id": source_file_id,
            "start_paragraph_id": start_paragraph_id,
            "end_paragraph_id": end_paragraph_id
            if end_paragraph_id
            else start_paragraph_id,
        }
        resolved = _resolve_segments(
            connection, document_group_id, [segment], exclude_group_id=None
        )
        # continue segment_order after this source's existing max
        row = connection.execute(
            "SELECT COALESCE(MAX(segment_order), -1) + 1 FROM alignment_segments "
            "WHERE alignment_group_id = ? AND source_file_id = ?",
            (group_id, resolved[0]["source_file_id"]),
        ).fetchone()
        _insert_segments(
            connection,
            group_id,
            resolved,
            order_start={resolved[0]["source_file_id"]: int(row[0])},
        )
        connection.execute(
            "UPDATE alignment_groups SET updated_at = ? WHERE alignment_group_id = ?",
            (_now(), group_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"alignment_group_id": group_id, "source_file_id": resolved[0]["source_file_id"]}


def remove_alignment_segment(
    alignment_segment_id: object, db_path: Path = DEFAULT_DATABASE_PATH
) -> Dict[str, object]:
    segment_id = str(alignment_segment_id or "").strip()
    if not segment_id:
        raise ValueError("alignment_segment_id is required")
    ensure_alignment_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "DELETE FROM alignment_segments WHERE alignment_segment_id = ?",
            (segment_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("对齐片段不存在。")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"alignment_segment_id": segment_id}


def delete_alignment_group(
    alignment_group_id: object, db_path: Path = DEFAULT_DATABASE_PATH
) -> Dict[str, object]:
    group_id = str(alignment_group_id or "").strip()
    if not group_id:
        raise ValueError("alignment_group_id is required")
    ensure_alignment_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM alignment_segments WHERE alignment_group_id = ?", (group_id,)
        )
        cursor = connection.execute(
            "DELETE FROM alignment_groups WHERE alignment_group_id = ?", (group_id,)
        )
        if cursor.rowcount != 1:
            raise ValueError("对齐组不存在。")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"alignment_group_id": group_id}


def set_alignment_review_status(
    alignment_group_id: object,
    review_status: object,
    db_path: Path = DEFAULT_DATABASE_PATH,
) -> Dict[str, object]:
    group_id = str(alignment_group_id or "").strip()
    status = str(review_status or "")
    if status not in REVIEW_STATUSES:
        raise ValueError("无效的 review_status。")
    ensure_alignment_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE alignment_groups SET review_status = ?, updated_at = ? "
            "WHERE alignment_group_id = ?",
            (status, _now(), group_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("对齐组不存在。")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"alignment_group_id": group_id, "review_status": status}


def prune_alignment_for_source(
    document_group_id: object,
    source_file_id: object,
    db_path: Path = DEFAULT_DATABASE_PATH,
) -> Dict[str, object]:
    """Remove a source's segments when it leaves the DocumentGroup / is deleted.

    Affected alignment_groups are marked is_stale (needs review) rather than judged
    by distinct-source count — a single-sided group is a legitimate gap/omission.
    Groups left with zero segments are deleted (nothing to reference).
    """

    group_id = str(document_group_id or "").strip()
    source_id = str(source_file_id or "").strip()
    if not group_id or not source_id:
        raise ValueError("document_group_id 与 source_file_id 均不能为空。")
    ensure_alignment_schema(db_path)
    connection = _connect_writable(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        affected = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT s.alignment_group_id FROM alignment_segments s "
                "JOIN alignment_groups g ON g.alignment_group_id = s.alignment_group_id "
                "WHERE g.document_group_id = ? AND s.source_file_id = ?",
                (group_id, source_id),
            )
        ]
        connection.execute(
            "DELETE FROM alignment_segments WHERE source_file_id = ? "
            "AND alignment_group_id IN "
            "(SELECT alignment_group_id FROM alignment_groups WHERE document_group_id = ?)",
            (source_id, group_id),
        )
        timestamp = _now()
        pruned_groups = 0
        stale_groups = 0
        for alignment_group_id in affected:
            remaining = int(
                connection.execute(
                    "SELECT COUNT(*) FROM alignment_segments WHERE alignment_group_id = ?",
                    (alignment_group_id,),
                ).fetchone()[0]
            )
            if remaining == 0:
                connection.execute(
                    "DELETE FROM alignment_groups WHERE alignment_group_id = ?",
                    (alignment_group_id,),
                )
                pruned_groups += 1
            else:
                connection.execute(
                    "UPDATE alignment_groups SET is_stale = 1, updated_at = ? "
                    "WHERE alignment_group_id = ?",
                    (timestamp, alignment_group_id),
                )
                stale_groups += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "document_group_id": group_id,
        "source_file_id": source_id,
        "deleted_groups": pruned_groups,
        "stale_groups": stale_groups,
    }


def list_alignment_groups(
    document_group_id: object, db_path: Path = DEFAULT_DATABASE_PATH
) -> List[Dict[str, object]]:
    group_id = str(document_group_id or "").strip()
    path = Path(db_path)
    if not group_id or not path.exists():
        return []
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        if not _table_exists(connection, "alignment_groups"):
            return []
        groups = connection.execute(
            "SELECT * FROM alignment_groups WHERE document_group_id = ? "
            "ORDER BY created_at, alignment_group_id",
            (group_id,),
        ).fetchall()
        result: List[Dict[str, object]] = []
        for group in groups:
            segments = connection.execute(
                "SELECT * FROM alignment_segments WHERE alignment_group_id = ? "
                "ORDER BY source_file_id, segment_order",
                (group["alignment_group_id"],),
            ).fetchall()
            result.append(
                {
                    "alignment_group_id": group["alignment_group_id"],
                    "document_group_id": group["document_group_id"],
                    "review_status": group["review_status"],
                    "is_stale": bool(group["is_stale"]),
                    "provenance": group["provenance"],
                    "segments": [
                        {
                            "alignment_segment_id": s["alignment_segment_id"],
                            "source_file_id": s["source_file_id"],
                            "start_paragraph_id": s["start_paragraph_id"],
                            "end_paragraph_id": s["end_paragraph_id"],
                            "start_paragraph_index": s["start_paragraph_index"],
                            "end_paragraph_index": s["end_paragraph_index"],
                            "segment_order": s["segment_order"],
                            "text_fingerprint": s["text_fingerprint"],
                        }
                        for s in segments
                    ],
                }
            )
        return result
    finally:
        connection.close()


# ── rebuild preservation ──


def read_alignment_snapshot(db_path: Path) -> Dict[str, list]:
    snapshot: Dict[str, list] = {"alignment_groups": [], "alignment_segments": []}
    path = Path(db_path)
    if not path.exists():
        return snapshot
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            return snapshot
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        if not _table_exists(connection, "alignment_groups"):
            return snapshot
        snapshot["alignment_groups"] = [
            dict(row)
            for row in connection.execute("SELECT * FROM alignment_groups")
        ]
        if _table_exists(connection, "alignment_segments"):
            snapshot["alignment_segments"] = [
                dict(row)
                for row in connection.execute("SELECT * FROM alignment_segments")
            ]
        return snapshot
    finally:
        connection.close()


def restore_alignment_snapshot(
    connection: sqlite3.Connection, snapshot: Dict[str, list]
) -> None:
    """Re-apply alignment into a freshly-built index (open transaction).

    paragraph_id is position-derived and drifts on reparse, so a missing/changed
    paragraph anchor NEVER drops a segment: as long as the source_file still
    exists the segment is kept and its group marked is_stale (no auto re-anchor).
    Only when the source_file itself is gone is that segment pruned; a group left
    with zero restorable segments is dropped. Single-sided groups are preserved.
    Requires source_files, document_groups and paragraphs to already be built.
    """

    if not snapshot:
        return
    groups = snapshot.get("alignment_groups") or []
    segments = snapshot.get("alignment_segments") or []
    if not groups:
        return
    install_alignment_schema(connection)

    valid_sources = {
        row[0]
        for row in connection.execute("SELECT source_file_id FROM source_files")
    }
    valid_document_groups = {
        row[0]
        for row in connection.execute("SELECT document_group_id FROM document_groups")
    }
    segments_by_group: Dict[str, list] = defaultdict(list)
    for segment in segments:
        segments_by_group[segment["alignment_group_id"]].append(segment)

    for group in groups:
        alignment_group_id = group["alignment_group_id"]
        if group["document_group_id"] not in valid_document_groups:
            continue  # its DocumentGroup is gone; nothing to hang the alignment on
        group_stale = bool(group.get("is_stale"))
        kept: List[tuple] = []
        for segment in sorted(
            segments_by_group.get(alignment_group_id, []),
            key=lambda item: (item["source_file_id"], item["segment_order"]),
        ):
            if segment["source_file_id"] not in valid_sources:
                # (2B) source_file gone → prune this segment; group is affected.
                group_stale = True
                continue
            # (2A) source exists → keep the user's segment no matter what.
            start_index = _paragraph_index(
                connection, segment["source_file_id"], segment["start_paragraph_id"]
            )
            end_index = _paragraph_index(
                connection, segment["source_file_id"], segment["end_paragraph_id"]
            )
            if start_index is None or end_index is None or start_index > end_index:
                # paragraph anchor drifted/gone: keep authored indices, mark stale.
                group_stale = True
                kept.append(
                    (segment, segment["start_paragraph_index"], segment["end_paragraph_index"])
                )
            else:
                current = _fingerprint(
                    connection, segment["source_file_id"], start_index, end_index
                )
                if current != (segment.get("text_fingerprint") or ""):
                    group_stale = True
                kept.append((segment, start_index, end_index))
        if not kept:
            continue  # every source gone → drop the empty group
        connection.execute(
            "INSERT INTO alignment_groups"
            "(alignment_group_id, document_group_id, review_status, is_stale, "
            "provenance, created_at, updated_at, payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                alignment_group_id,
                group["document_group_id"],
                group["review_status"],
                1 if group_stale else 0,
                group["provenance"],
                group["created_at"],
                group["updated_at"],
                group.get("payload_json") or "{}",
            ),
        )
        for segment, start_index, end_index in kept:
            connection.execute(
                "INSERT INTO alignment_segments"
                "(alignment_segment_id, alignment_group_id, source_file_id, "
                "start_paragraph_id, end_paragraph_id, start_paragraph_index, "
                "end_paragraph_index, segment_order, text_fingerprint, created_at, "
                "updated_at, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    segment["alignment_segment_id"],
                    alignment_group_id,
                    segment["source_file_id"],
                    segment["start_paragraph_id"],
                    segment["end_paragraph_id"],
                    start_index,
                    end_index,
                    segment["segment_order"],
                    segment.get("text_fingerprint"),
                    segment["created_at"],
                    segment["updated_at"],
                    segment.get("payload_json") or "{}",
                ),
            )

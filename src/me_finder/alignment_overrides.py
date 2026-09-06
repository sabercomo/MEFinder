"""人工对齐覆盖的提案、确认、撤销与查询。

覆盖是对已生成对齐的人工校正：提案写入待确认状态，确认后在读取路径上盖过
算法结果，撤销则退回算法结果。本模块位于 ``text_alignment`` 之上——它消费
对齐核心的定位与校验能力，核心不反向依赖它。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from .persistence.connection import open_writable_index
from .persistence.schema_installers import install_text_alignment_schema
from .text_alignment import (
    AlignmentNotFound,
    InvalidAlignmentRequest,
    WriteWindow,
    _now,
    _ordered_segments_in_set,
    _resolve_alignment_route,
    _segment_key,
    _segment_set_id_for_source,
    _source_row,
    _table_exists,
    _validate_source_id,
)


def resolve_override_context(
    db_path: Path,
    source_file_id: object,
    target_source_file_id: object,
    source_segment_ids: Sequence[object],
    target_segment_ids: Sequence[object],
) -> Dict[str, object]:
    """Validate a proposed correction against the current alignment run.

    Confirms both versions share a completed alignment route and that every
    referenced segment belongs to that route's current segment sets, so a
    correction can never point at segments the live alignment does not use.
    """

    source_id = _validate_source_id(source_file_id)
    target_id = _validate_source_id(target_source_file_id)
    if source_id == target_id:
        raise InvalidAlignmentRequest("源版本和目标版本不能相同。")
    source_ids = [str(value) for value in source_segment_ids]
    target_ids = [str(value) for value in target_segment_ids]
    if not source_ids or not target_ids:
        raise InvalidAlignmentRequest("源和目标 Segment 都不能为空。")
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        _source_row(connection, source_id)
        _source_row(connection, target_id)
        route_runs, _via = _resolve_alignment_route(connection, source_id, target_id)
        source_run = route_runs[0]
        final_run = route_runs[-1]
        source_set_id = _segment_set_id_for_source(source_run, source_id)
        target_set_id = _segment_set_id_for_source(final_run, target_id)
        source_rows = _ordered_segments_in_set(connection, source_set_id, source_ids)
        if len(source_rows) != len(set(source_ids)):
            raise InvalidAlignmentRequest(
                "部分源 Segment 不属于当前版本的对齐段落，请用最新查询结果重试。"
            )
        target_rows = _ordered_segments_in_set(connection, target_set_id, target_ids)
        if len(target_rows) != len(set(target_ids)):
            raise InvalidAlignmentRequest(
                "部分目标 Segment 不属于当前版本的对齐段落，请用最新查询结果重试。"
            )
        ordered_source_ids = [str(row["segment_id"]) for row in source_rows]
        ordered_target_ids = [str(row["segment_id"]) for row in target_rows]
        return {
            "document_group_id": str(source_run["document_group_id"]),
            "source_file_id": source_id,
            "target_source_file_id": target_id,
            "source_segment_set_id": source_set_id,
            "target_segment_set_id": target_set_id,
            "source_segment_key": _segment_key(ordered_source_ids),
            "source_segment_ids": ordered_source_ids,
            "target_segment_ids": ordered_target_ids,
            "source_segments": [
                {"segment_id": str(row["segment_id"]), "text": str(row["text_raw"])}
                for row in source_rows
            ],
            "target_segments": [
                {"segment_id": str(row["segment_id"]), "text": str(row["text_raw"])}
                for row in target_rows
            ],
        }
    finally:
        connection.close()


def create_override_proposal(
    db_path: Path,
    source_file_id: object,
    target_source_file_id: object,
    source_segment_ids: Sequence[object],
    target_segment_ids: Sequence[object],
    *,
    evidence: Mapping[str, object] | None = None,
    write_window: WriteWindow | None = None,
) -> Dict[str, object]:
    """Record a pending correction. It does not affect reads until confirmed."""

    context = resolve_override_context(
        db_path,
        source_file_id,
        target_source_file_id,
        source_segment_ids,
        target_segment_ids,
    )
    override_id = f"alignment-override-{uuid.uuid4().hex}"
    confirmation_token = uuid.uuid4().hex
    evidence_json = json.dumps(
        dict(evidence or {}), ensure_ascii=False, separators=(",", ":")
    )
    timestamp = _now()
    transaction_window = write_window or nullcontext
    with transaction_window():
        connection = open_writable_index(Path(db_path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            install_text_alignment_schema(connection)
            # A fresh proposal replaces any earlier unconfirmed one for the same
            # selection; confirmed corrections are only retired at confirm time.
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
                        context["source_segment_ids"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        context["target_segment_ids"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    confirmation_token,
                    evidence_json,
                    timestamp,
                ),
            )
            connection.commit()
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            connection.rollback()
            raise
        finally:
            connection.close()
    return {
        "override_id": override_id,
        "confirmation_token": confirmation_token,
        "status": "pending",
        "document_group_id": context["document_group_id"],
        "source_file_id": context["source_file_id"],
        "target_source_file_id": context["target_source_file_id"],
        "source_segments": context["source_segments"],
        "target_segments": context["target_segments"],
        "created_at": timestamp,
    }


def confirm_override(
    db_path: Path,
    override_id: object,
    confirmation_token: object,
    *,
    write_window: WriteWindow | None = None,
) -> Dict[str, object]:
    """Promote a pending correction to the authoritative alignment for reads."""

    override_id_value = str(override_id or "").strip()
    token_value = str(confirmation_token or "").strip()
    if not override_id_value or not token_value:
        raise InvalidAlignmentRequest("override_id 和 confirmation_token 都必填。")
    transaction_window = write_window or nullcontext
    timestamp = _now()
    with transaction_window():
        connection = open_writable_index(Path(db_path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not _table_exists(connection, "alignment_manual_overrides"):
                raise AlignmentNotFound("对齐修正提议不存在。")
            row = connection.execute(
                "SELECT * FROM alignment_manual_overrides WHERE override_id = ?",
                (override_id_value,),
            ).fetchone()
            if row is None:
                raise AlignmentNotFound("对齐修正提议不存在。")
            if str(row["status"]) != "pending":
                raise InvalidAlignmentRequest(
                    f"该提议状态为 {row['status']}，无法确认。"
                )
            if str(row["confirmation_token"]) != token_value:
                raise InvalidAlignmentRequest("确认令牌不匹配。")
            source_id = str(row["source_file_id"])
            target_id = str(row["target_source_file_id"])
            route_runs, _via = _resolve_alignment_route(
                connection, source_id, target_id
            )
            current_source_set = _segment_set_id_for_source(route_runs[0], source_id)
            current_target_set = _segment_set_id_for_source(route_runs[-1], target_id)
            stored_source_ids = json.loads(str(row["source_segment_ids_json"] or "[]"))
            stored_target_ids = json.loads(str(row["target_segment_ids_json"] or "[]"))
            still_valid = (
                current_source_set == str(row["source_segment_set_id"])
                and current_target_set == str(row["target_segment_set_id"])
                and len(
                    _ordered_segments_in_set(
                        connection, current_source_set, stored_source_ids
                    )
                )
                == len(set(stored_source_ids))
                and len(
                    _ordered_segments_in_set(
                        connection, current_target_set, stored_target_ids
                    )
                )
                == len(set(stored_target_ids))
            )
            if not still_valid:
                connection.execute(
                    "UPDATE alignment_manual_overrides SET status = 'revoked', "
                    "revoked_at = ? WHERE override_id = ?",
                    (timestamp, override_id_value),
                )
                connection.commit()
                raise InvalidAlignmentRequest(
                    "对齐或分段已更新，提议已过期，请用最新查询结果重新提议。"
                )
            # Retire the correction this one replaces so the partial unique index
            # on confirmed rows always sees a single active correction.
            connection.execute(
                "UPDATE alignment_manual_overrides SET status = 'revoked', "
                "revoked_at = ? WHERE status = 'confirmed' AND source_file_id = ? "
                "AND target_source_file_id = ? AND source_segment_set_id = ? "
                "AND source_segment_key = ?",
                (
                    timestamp,
                    source_id,
                    target_id,
                    str(row["source_segment_set_id"]),
                    str(row["source_segment_key"]),
                ),
            )
            connection.execute(
                "UPDATE alignment_manual_overrides SET status = 'confirmed', "
                "confirmed_at = ? WHERE override_id = ?",
                (timestamp, override_id_value),
            )
            connection.commit()
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            connection.rollback()
            raise
        finally:
            connection.close()
    return {
        "override_id": override_id_value,
        "status": "confirmed",
        "source_file_id": source_id,
        "target_source_file_id": target_id,
        "confirmed_at": timestamp,
    }


def revoke_override(
    db_path: Path,
    override_id: object,
    *,
    write_window: WriteWindow | None = None,
) -> Dict[str, object]:
    """Revert a pending or confirmed correction. Reversible by re-proposing."""

    override_id_value = str(override_id or "").strip()
    if not override_id_value:
        raise InvalidAlignmentRequest("override_id 必填。")
    transaction_window = write_window or nullcontext
    timestamp = _now()
    with transaction_window():
        connection = open_writable_index(Path(db_path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not _table_exists(connection, "alignment_manual_overrides"):
                raise AlignmentNotFound("对齐修正提议不存在。")
            row = connection.execute(
                "SELECT status, source_file_id, target_source_file_id "
                "FROM alignment_manual_overrides WHERE override_id = ?",
                (override_id_value,),
            ).fetchone()
            if row is None:
                raise AlignmentNotFound("对齐修正提议不存在。")
            previous_status = str(row["status"])
            if previous_status == "revoked":
                connection.commit()
            else:
                connection.execute(
                    "UPDATE alignment_manual_overrides SET status = 'revoked', "
                    "revoked_at = ? WHERE override_id = ?",
                    (timestamp, override_id_value),
                )
                connection.commit()
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            connection.rollback()
            raise
        finally:
            connection.close()
    return {
        "override_id": override_id_value,
        "status": "revoked",
        "previous_status": previous_status,
        "source_file_id": str(row["source_file_id"]),
        "target_source_file_id": str(row["target_source_file_id"]),
        "revoked_at": timestamp,
    }


def list_overrides(
    db_path: Path,
    *,
    source_file_id: object = None,
    target_source_file_id: object = None,
    status: object = None,
    limit: int = 50,
) -> Dict[str, object]:
    """Read stored corrections for review and audit."""

    filters: List[str] = []
    parameters: List[object] = []
    if source_file_id is not None:
        filters.append("source_file_id = ?")
        parameters.append(_validate_source_id(source_file_id))
    if target_source_file_id is not None:
        filters.append("target_source_file_id = ?")
        parameters.append(_validate_source_id(target_source_file_id))
    if status is not None:
        status_value = str(status)
        if status_value not in {"pending", "confirmed", "revoked"}:
            raise InvalidAlignmentRequest("status 取值无效。")
        filters.append("status = ?")
        parameters.append(status_value)
    bounded_limit = max(1, min(int(limit), 200))
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        if not _table_exists(connection, "alignment_manual_overrides"):
            return {"total": 0, "overrides": []}
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = connection.execute(
            "SELECT override_id, document_group_id, source_file_id, "
            "target_source_file_id, source_segment_ids_json, "
            "target_segment_ids_json, status, evidence_json, created_at, "
            "confirmed_at, revoked_at FROM alignment_manual_overrides "
            f"{where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*parameters, bounded_limit),
        ).fetchall()
        overrides = [
            {
                "override_id": str(row["override_id"]),
                "document_group_id": str(row["document_group_id"]),
                "source_file_id": str(row["source_file_id"]),
                "target_source_file_id": str(row["target_source_file_id"]),
                "source_segment_ids": json.loads(
                    str(row["source_segment_ids_json"] or "[]")
                ),
                "target_segment_ids": json.loads(
                    str(row["target_segment_ids_json"] or "[]")
                ),
                "status": str(row["status"]),
                "evidence": json.loads(str(row["evidence_json"] or "{}")),
                "created_at": str(row["created_at"]),
                "confirmed_at": (
                    str(row["confirmed_at"]) if row["confirmed_at"] else None
                ),
                "revoked_at": (
                    str(row["revoked_at"]) if row["revoked_at"] else None
                ),
            }
            for row in rows
        ]
        return {"total": len(overrides), "overrides": overrides}
    finally:
        connection.close()

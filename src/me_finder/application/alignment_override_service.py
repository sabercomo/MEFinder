"""Human-confirmed corrections to the automatic cross-version alignment.

MCP v1 stays read-only for *live* alignment: ``propose`` only records a pending
correction, which does not change any query until the user confirms it with the
token ``propose`` issued.  ``confirm`` promotes it, ``revoke`` reverts it, and
every step is reversible.  All persistence lives in ``text_alignment``; this
module only validates input and shapes the response.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping, Sequence

SCHEMA_VERSION = "1"
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_STATUSES = {"pending", "confirmed", "revoked"}


def propose_alignment_correction(
    index_path: Path,
    *,
    source_file_id: object,
    target_source_file_id: object,
    source_segment_ids: Sequence[object],
    target_segment_ids: Sequence[object],
    evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    from ..text_alignment import AlignmentNotFound, create_override_proposal

    source = _validate_id(source_file_id, name="source_file_id")
    target = _validate_id(target_source_file_id, name="target_source_file_id")
    validated_source = _validate_segment_ids(
        source_segment_ids, name="source_segment_ids"
    )
    validated_target = _validate_segment_ids(
        target_segment_ids, name="target_segment_ids"
    )
    validated_evidence = _validate_evidence(evidence)
    try:
        result = create_override_proposal(
            index_path,
            source,
            target,
            validated_source,
            validated_target,
            evidence=validated_evidence,
        )
    except AlignmentNotFound as exc:
        raise ValueError(str(exc)) from exc
    return {"schema_version": SCHEMA_VERSION, **result}


def confirm_alignment_correction(
    index_path: Path,
    *,
    override_id: object,
    confirmation_token: object,
) -> dict[str, object]:
    from ..text_alignment import AlignmentNotFound, confirm_override

    validated_override = _validate_id(override_id, name="override_id")
    validated_token = _validate_id(confirmation_token, name="confirmation_token")
    try:
        result = confirm_override(index_path, validated_override, validated_token)
    except AlignmentNotFound as exc:
        raise ValueError(str(exc)) from exc
    return {"schema_version": SCHEMA_VERSION, **result}


def revoke_alignment_correction(
    index_path: Path,
    *,
    override_id: object,
) -> dict[str, object]:
    from ..text_alignment import AlignmentNotFound, revoke_override

    validated_override = _validate_id(override_id, name="override_id")
    try:
        result = revoke_override(index_path, validated_override)
    except AlignmentNotFound as exc:
        raise ValueError(str(exc)) from exc
    return {"schema_version": SCHEMA_VERSION, **result}


def list_alignment_corrections(
    index_path: Path,
    *,
    source_file_id: object = None,
    target_source_file_id: object = None,
    status: object = None,
    limit: int = 50,
) -> dict[str, object]:
    from ..text_alignment import list_overrides

    validated_source = _validate_optional_id(source_file_id, name="source_file_id")
    validated_target = _validate_optional_id(
        target_source_file_id, name="target_source_file_id"
    )
    if status is not None and status not in _STATUSES:
        raise ValueError("status 取值无效")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit 必须是整数")
    if not 1 <= limit <= 200:
        raise ValueError("limit 必须在 1 到 200 之间")
    result = list_overrides(
        index_path,
        source_file_id=validated_source,
        target_source_file_id=validated_target,
        status=status,
        limit=limit,
    )
    return {"schema_version": SCHEMA_VERSION, **result}


def _validate_id(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ValueError(
            f"{name} 只能包含 ASCII 字母、数字、点、下划线和连字符，且长度不超过 128"
        )
    return value


def _validate_optional_id(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    return _validate_id(value, name=name)


def _validate_segment_ids(value: object, *, name: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{name} 必须是非空的 Segment ID 数组")
    if len(value) > 200:
        raise ValueError(f"{name} 不能超过 200 个 Segment")
    return [_validate_id(item, name=name) for item in value]


def _validate_evidence(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("evidence 必须是对象")
    if len(json.dumps(dict(value), ensure_ascii=False)) > 20_000:
        raise ValueError("evidence 太大，请精简判断依据")
    return dict(value)

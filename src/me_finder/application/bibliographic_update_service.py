"""Agent-proposed bibliographic fills: propose → user confirms → desktop applies.

``propose`` previews each field against the stored metadata and records a
pending request only when at least one empty field would be filled; it never
changes metadata. ``confirm`` needs the one-time token ``propose`` returned and
only marks the request confirmed. The desktop runtime performs the write (see
``bibliographic_update_applier``), so the sidecar never edits the import config.
"""

from __future__ import annotations

import re
import secrets
import uuid
from pathlib import Path
from typing import Mapping

from ..bibliographic_fill import normalize_proposals, plan_fill
from ..bibliographic_updates import (
    BibliographicUpdateNotFound,
    confirm_pending_request,
    insert_pending_request,
    list_requests,
)

SCHEMA_VERSION = "1"
_REQUEST_ID = re.compile(r"BIBFILL-[0-9a-f]{32}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,64}\Z")
APPLY_NOTE = (
    "已确认。MEFinder 桌面端运行时会在几秒内写入（未运行则下次启动时写入），"
    "只填空字段；写入结果可用 read_bibliographic_metadata 的 update_requests 查看。"
)


def propose_bibliographic_update(
    index_path: Path,
    *,
    source_file_id: str,
    current: Mapping[str, object],
    fields: object,
) -> dict[str, object]:
    proposals = normalize_proposals(fields)
    plan = plan_fill(current, proposals)
    preview = [
        {
            "field": item["field"],
            "current_value": item["current_value"],
            "proposed_value": item["value"],
            "action": item["action"],
            "source_page": item["source_page"],
        }
        for item in plan
    ]
    fillable = [item for item in plan if item["action"] == "fill"]
    if not fillable:
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": None,
            "confirmation_token": None,
            "fields": preview,
        }
    request_id = f"BIBFILL-{uuid.uuid4().hex}"
    token = secrets.token_urlsafe(24)
    insert_pending_request(
        index_path,
        request_id=request_id,
        source_file_id=source_file_id,
        fields=[
            {key: item[key] for key in ("field", "value", "evidence_text", "source_page")}
            for item in fillable
        ],
        token=token,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "confirmation_token": token,
        "fields": preview,
    }


def confirm_bibliographic_update(
    index_path: Path, *, request_id: object, confirmation_token: object
) -> dict[str, object]:
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise ValueError("request_id 格式无效")
    if not isinstance(confirmation_token, str) or not _TOKEN.fullmatch(confirmation_token):
        raise ValueError("confirmation_token 格式无效")
    try:
        request = confirm_pending_request(index_path, request_id, confirmation_token)
    except BibliographicUpdateNotFound as exc:
        raise ValueError(str(exc)) from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "status": request["status"],
        "fields": [item["field"] for item in request["fields"]],
        "note": APPLY_NOTE,
    }


def recent_update_requests(index_path: Path, source_file_id: str) -> list[dict[str, object]]:
    """The newest requests for one document, for status in metadata reads."""

    return [
        {
            "request_id": item["request_id"],
            "status": item["status"],
            "fields": [field["field"] for field in item["fields"]],
            "created_at": item["created_at"],
            "result": item["result"],
        }
        for item in list_requests(index_path, source_file_id=source_file_id, limit=5)
    ]

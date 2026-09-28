"""Plan agent-proposed bibliographic fills: only empty fields are ever written.

Pure logic shared by the MCP proposal preview and the desktop applier, so both
decide ``fill`` / ``same`` / ``conflict`` with the same rules. An existing
value is never replaced; a differing proposal is reported as a conflict and the
stored value is kept.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Sequence

from .bibliographic_metadata import invalid_metadata_fields
from .bibliographic_values import METADATA_FIELDS, is_valid_bibliographic_value
from .bibliographic_journal import normalize_doi, normalize_issn

EVIDENCE_SOURCE = "mcp_agent"
MAX_VALUE_CHARACTERS = 500
MAX_EVIDENCE_CHARACTERS = 500


def normalize_proposal(proposal: Mapping[str, object]) -> Dict[str, object]:
    """Validate one ``{field, value, evidence_text, source_page}`` proposal."""

    field = proposal.get("field")
    if field not in METADATA_FIELDS:
        raise ValueError(f"不支持的书目字段：{field}")
    value = " ".join(str(proposal.get("value") or "").split())
    if not value or len(value) > MAX_VALUE_CHARACTERS:
        raise ValueError(f"{field} 的值为空或过长")
    if field == "doi":
        value = normalize_doi(value) or ""
    elif field == "issn":
        value = normalize_issn(value) or ""
    if not value or invalid_metadata_fields({field: value}) or not is_valid_bibliographic_value(value):
        raise ValueError(f"{field} 的值无效")
    evidence = " ".join(str(proposal.get("evidence_text") or "").split())
    if not evidence:
        raise ValueError(f"{field} 必须附原书依据文本")
    source_page = proposal.get("source_page")
    if source_page is not None and (
        not isinstance(source_page, str) or not source_page.strip() or len(source_page) > 40
    ):
        raise ValueError("source_page 必须是简短页码文本")
    return {
        "field": field,
        "value": value,
        "evidence_text": evidence[:MAX_EVIDENCE_CHARACTERS],
        "source_page": source_page.strip() if source_page else None,
    }


def normalize_proposals(proposals: object) -> List[Dict[str, object]]:
    if not isinstance(proposals, (list, tuple)) or not 1 <= len(proposals) <= len(METADATA_FIELDS):
        raise ValueError("fields 必须是 1 到 14 项的数组")
    normalized = [
        normalize_proposal(item) if isinstance(item, Mapping) else _reject()
        for item in proposals
    ]
    names = [item["field"] for item in normalized]
    if len(set(names)) != len(names):
        raise ValueError("同一字段只能提议一次")
    return normalized


def plan_fill(
    current: Mapping[str, object], proposals: Sequence[Mapping[str, object]]
) -> List[Dict[str, object]]:
    """Decide each already-normalized proposal against current metadata."""

    plan = []
    for proposal in proposals:
        field = str(proposal["field"])
        stored = current.get(field)
        stored_text = " ".join(str(stored).split()) if stored not in (None, "") else None
        if stored_text is None or not is_valid_bibliographic_value(stored):
            action = "fill"
        elif stored_text.casefold() == str(proposal["value"]).casefold():
            action = "same"
        else:
            action = "conflict"
        plan.append({**proposal, "current_value": stored_text, "action": action})
    return plan


def manual_save_payload(
    current: Mapping[str, object], plan: Sequence[Mapping[str, object]]
) -> Dict[str, object]:
    """Build a ``manual_metadata`` payload that changes only the filled fields."""

    payload: Dict[str, object] = {
        field: current.get(field) for field in METADATA_FIELDS
    }
    document_type = current.get("document_type")
    if document_type:
        payload["document_type"] = document_type
    responsibility = current.get("responsibility_status")
    if responsibility:
        payload["responsibility_status"] = responsibility
    evidence = {}
    for item in plan:
        if item["action"] != "fill":
            continue
        payload[str(item["field"])] = item["value"]
        evidence[str(item["field"])] = {
            "source": EVIDENCE_SOURCE,
            "source_page": item.get("source_page"),
            "evidence_text": item["evidence_text"],
            "value": item["value"],
        }
    payload["metadata_evidence"] = evidence
    return payload


def _reject() -> Dict[str, object]:
    raise ValueError("fields 的每一项必须是对象")

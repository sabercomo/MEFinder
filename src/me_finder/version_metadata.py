"""User-facing metadata for one document-group member."""

from __future__ import annotations

from typing import Dict, Mapping


VERSION_LABEL_MAX_LENGTH = 200


def canonical_version_metadata(value: Mapping[str, object]) -> Dict[str, str]:
    nested = value.get("version_metadata")
    source = nested if isinstance(nested, Mapping) else value
    label = str(source.get("version_label") or "").strip()
    if len(label) > VERSION_LABEL_MAX_LENGTH:
        raise ValueError("版本名称不能超过 200 个字符。")
    return {"version_label": label} if label else {}

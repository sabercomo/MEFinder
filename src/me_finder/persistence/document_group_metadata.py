"""Version metadata for one document-group member.

A member's identity is the SourceFile it points at; its version-specific display
name is an optional per-membership label that falls back to the SourceFile's own
bibliographic metadata. This module only formats/validates that label — it never
copies the underlying bibliographic fields into the membership row.
"""

from __future__ import annotations

from typing import Mapping

VERSION_LABEL_MAX_LENGTH = 200

_LANGUAGE_LABELS = {
    "zh": "中文",
    "zh-hans": "简体中文",
    "zh-hant": "繁体中文",
    "en": "英文",
    "de": "德文",
    "fr": "法文",
    "ru": "俄文",
    "ja": "日文",
    "la": "拉丁文",
    "el": "希腊文",
}


def canonical_version_label(value: object) -> str:
    """Trim a version label and enforce its length ceiling (empty allowed)."""

    label = str(value or "").strip()
    if len(label) > VERSION_LABEL_MAX_LENGTH:
        raise ValueError("版本名称不能超过 200 个字符。")
    return label


def _language_label(code: object) -> str:
    key = str(code or "").strip().lower()
    if not key:
        return ""
    return _LANGUAGE_LABELS.get(key) or _LANGUAGE_LABELS.get(key.split("-")[0]) or key


def member_display_name(version_label: object, source: Mapping[str, object]) -> str:
    """Resolve a member's display name.

    Fallback order (per confirmed architecture):
    version_label → translator → language / edition / publish_year → file title.
    Bibliographic fields are read from the SourceFile payload, never duplicated.
    """

    label = str(version_label or "").strip()
    if label:
        return label

    def field(name: str) -> str:
        value = source.get(name)
        if value in (None, ""):
            bib = source.get("bibliographic_metadata")
            if isinstance(bib, Mapping):
                value = bib.get(name)
        return str(value or "").strip()

    translator = field("translator")
    if translator:
        return f"{translator} 译"

    parts = []
    language = _language_label(field("language_code") or field("language"))
    if language:
        parts.append(language)
    edition = field("edition")
    if edition:
        parts.append(edition)
    year = field("publish_year")
    if year:
        parts.append(f"{year} 年")
    if parts:
        return " · ".join(parts)

    return field("title") or field("file_name") or str(source.get("source_file_id") or "")

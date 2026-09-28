"""Turn the reader's indexed outline into bounded, readable MCP sections.

Pure shaping over ``document_outline.get_document_outline`` entries: no SQL and
no source-file parsing. A section runs from its heading to just before the
next heading of the same or a higher level; PDF headings sit mid-page, so a
PDF section's last page is the page holding the next heading.
"""

from __future__ import annotations

from typing import Mapping, Sequence

MAX_SECTIONS = 500
MAX_TITLE_CHARACTERS = 200


def build_sections(
    entries: Sequence[Mapping[str, object]],
    *,
    total_units: int,
    is_pdf: bool,
) -> list[dict[str, object]]:
    """Return every outline entry with its natural-position range."""

    ordered = sorted(
        (entry for entry in entries if isinstance(entry, Mapping)),
        key=lambda entry: (int(entry["item_index"]), int(entry.get("char_start") or 0)),
    )
    last_position = max(total_units - 1, 0)
    sections: list[dict[str, object]] = []
    for index, entry in enumerate(ordered):
        start = int(entry["item_index"])
        level = int(entry["level"])
        end = last_position
        for following in ordered[index + 1 :]:
            if int(following["level"]) <= level:
                next_start = int(following["item_index"])
                end = next_start if is_pdf else next_start - 1
                break
        title = clean_title(entry.get("title"))
        sections.append(
            {
                "section_index": index,
                "level": level,
                "title": title,
                "start": start,
                "end": max(start, min(end, last_position)),
                "anchor_id": entry.get("anchor_id"),
            }
        )
    return sections


def clean_title(value: object) -> str:
    """Collapse whitespace and bound over-long (misdetected) heading text."""

    title = " ".join(str(value or "").split())
    if len(title) > MAX_TITLE_CHARACTERS:
        title = title[: MAX_TITLE_CHARACTERS - 1] + "…"
    return title


def section_by_index(
    sections: Sequence[Mapping[str, object]], section_index: int
) -> Mapping[str, object]:
    """Return one section or reject an index the outline does not have."""

    if not 0 <= section_index < len(sections):
        raise ValueError(
            f"section_index 超出范围：该文献共有 {len(sections)} 个章节"
        )
    return sections[section_index]

"""Pure conversion of persisted MEFinder structured PDF pages to Markdown.

The module reads only data already stored by MEFinder (page payloads and
bibliographic metadata).  It never reparses a PDF, never OCRs, and never
consults MinerU output directories.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional, Sequence


SOURCE_NAME = "MEFinder"
MAX_HEADING_LEVEL = 6
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_YAML_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def safe_markdown_filename(title: object) -> str:
    """Return a Windows-safe ``{title}.md`` file name."""

    stem = _ILLEGAL_FILENAME_CHARS.sub("-", str(title or "")).strip(" .-")
    if not stem:
        stem = "MEFinder-document"
    encoded = stem.encode("utf-8")
    if len(encoded) > 120:
        stem = encoded[:120].decode("utf-8", errors="ignore").rstrip(" .-")
    return (stem or "MEFinder-document") + ".md"


def document_to_markdown(
    pages: Iterable[Mapping[str, object]],
    *,
    title: object = None,
    author: object = None,
) -> str:
    """Convert persisted pages into one UTF-8 Markdown document string."""

    chunks = [_frontmatter(title=title, author=author)]
    for page in pages:
        rendered = page_to_markdown(page)
        if rendered:
            chunks.append(rendered)
    return "\n\n".join(chunks).strip() + "\n"


def page_to_markdown(page: Mapping[str, object]) -> str:
    """Render one persisted page payload, page marker first, body after."""

    text_raw = str(page.get("text_raw") or "")
    blocks = page.get("blocks")
    body = _render_body(
        text_raw,
        blocks if isinstance(blocks, (list, tuple)) else None,
    )
    if not body:
        return ""
    marker = _page_marker(page)
    if marker:
        return f"{marker}\n\n{body}"
    return body


def _frontmatter(*, title: object, author: object) -> str:
    title_text = str(title or "").strip()
    author_text = str(author or "").strip()
    lines = ["---"]
    if title_text:
        lines.append(f"title: {_yaml_scalar(title_text)}")
    if author_text:
        lines.append(f"author: {_yaml_scalar(author_text)}")
    lines.append(f"source: {SOURCE_NAME}")
    lines.append("---")
    return "\n".join(lines)


def _yaml_scalar(value: object) -> str:
    text = (
        str(value or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    text = _YAML_CONTROL_CHARS.sub(
        lambda match: f"\\x{ord(match.group(0)):02x}",
        text,
    )
    return f'"{text}"'


def _render_body(
    text_raw: str,
    blocks: Optional[Sequence[object]],
) -> str:
    if blocks is not None and _blocks_aligned_with_text(blocks, text_raw):
        rendered: list[str] = []
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            block_text = str(block.get("text") or "").strip()
            if not block_text:
                continue
            level = _canonical_heading_level(block)
            if level is None:
                rendered.append(block_text)
            else:
                rendered.append(f"{'#' * level} {block_text}")
        return "\n\n".join(rendered)
    return text_raw.strip()


def _blocks_aligned_with_text(
    blocks: Sequence[object],
    text_raw: str,
) -> bool:
    usable = [
        block
        for block in blocks
        if isinstance(block, Mapping) and str(block.get("text") or "").strip()
    ]
    if not usable:
        return False
    page_text = str(text_raw or "").strip()
    if not page_text:
        return False
    if all(_has_valid_offsets(block, page_text) for block in usable):
        return True
    reconstructed = "\n".join(
        str(block.get("text") or "").strip() for block in usable
    )
    return reconstructed.strip() == page_text


def _has_valid_offsets(block: Mapping[str, object], page_text: str) -> bool:
    start = block.get("page_char_start")
    end = block.get("page_char_end")
    if start in (None, "") or end in (None, ""):
        return False
    try:
        start_index = int(start)
        end_index = int(end)
    except (TypeError, ValueError):
        return False
    if start_index < 0 or end_index < start_index or end_index > len(page_text):
        return False
    block_text = str(block.get("text") or "").strip()
    return bool(block_text) and page_text[start_index:end_index].strip() == block_text


def _canonical_heading_level(block: Mapping[str, object]) -> Optional[int]:
    """Prefer the canonical document heading level, fall back to raw text_level.

    Priority mirrors the import pipeline: ``document_heading_level`` (from a
    semantic PDF outline or MinerU v2 title) wins; otherwise the parser's raw
    ``text_level`` is used; otherwise the block renders as ordinary body text.
    """

    level = _heading_level(block.get("document_heading_level"))
    if level is not None:
        return level
    return _heading_level(block.get("text_level"))


def _heading_level(value: object) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        level = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= level <= MAX_HEADING_LEVEL:
        return level
    return None


def _page_marker(page: Mapping[str, object]) -> Optional[str]:
    physical = _first_nonempty(
        page.get("pdf_page_number_1based"),
        page.get("physical_pdf_page"),
    )
    if physical is None and page.get("pdf_page_index") not in (None, ""):
        try:
            physical = int(page["pdf_page_index"]) + 1
        except (TypeError, ValueError):
            physical = None
    if physical is None:
        return None
    try:
        physical_text = str(int(physical))
    except (TypeError, ValueError):
        return None
    printed = _first_nonempty(
        page.get("citation_page"),
        page.get("printed_page"),
        page.get("logical_page"),
        page.get("book_page"),
    )
    if printed is None:
        return f"<!-- pdf_page: {physical_text} -->"
    return f"<!-- pdf_page: {physical_text} | printed_page: {printed} -->"


def _first_nonempty(*values: object) -> Optional[object]:
    for value in values:
        if value not in (None, ""):
            return value
    return None

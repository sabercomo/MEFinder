"""Pure conversion of persisted MEFinder structured PDF pages to Markdown.

The module reads only data already stored by MEFinder (page payloads and
bibliographic metadata).  It never reparses a PDF, never OCRs, and never
consults MinerU output directories.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Mapping, Optional

from .markdown_export_normalize import (
    ExportOptions,
    PageArtifactProfile,
    PageMarker,
    build_page_artifact_profile,
    iter_export_page_blocks,
    resolve_page_marker,
)


SOURCE_NAME = "MEFinder"
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
    options: Optional[ExportOptions] = None,
) -> str:
    """Convert persisted pages into one UTF-8 Markdown document string.

    The export runs a normalization pass (see ``markdown_export_normalize``) that
    strips visible page numbers and repeated running headers/footers, and by
    default emits only the hidden ``<!-- printed_page: N -->`` anchor.  This view
    never mutates the persisted pages or the page-number anchors themselves.
    """

    options = options or ExportOptions()
    # Materialize once so the artifact profile (cross-page statistics) and the
    # per-page rendering can share the same page objects.  The Markdown builder
    # already holds the whole document in memory when joining chunks, so this is
    # not a new scaling constraint.
    materialized = [page for page in pages if isinstance(page, Mapping)]
    profile = build_page_artifact_profile(materialized, options)

    chunks = [_frontmatter(title=title, author=author)]
    for page in materialized:
        rendered = page_to_markdown(page, profile=profile, options=options)
        if rendered:
            chunks.append(rendered)
    return "\n\n".join(chunks).strip() + "\n"


def page_to_markdown(
    page: Mapping[str, object],
    *,
    profile: Optional[PageArtifactProfile] = None,
    options: Optional[ExportOptions] = None,
) -> str:
    """Render one persisted page payload, page marker first, body after."""

    options = options or ExportOptions()
    body = _render_body(page, profile=profile, options=options)
    if not body:
        return ""
    marker = render_markdown_page_marker(resolve_page_marker(page, options))
    if marker:
        return f"{marker}\n\n{body}"
    return body


def render_markdown_page_marker(marker: Optional[PageMarker]) -> Optional[str]:
    """Serialize a semantic :class:`PageMarker` as a hidden Markdown anchor.

    This is the *only* place the ``<!-- printed_page: N -->`` HTML-comment syntax
    lives.  A future EPUB renderer takes the same :class:`PageMarker` and emits an
    EPUB ``pagebreak`` anchor instead — the marker itself carries no format.
    Standard Markdown/Pandoc keeps the comment hidden, so the printed folio never
    shows up as body text.
    """

    if marker is None or marker.is_empty:
        return None
    printed = marker.printed_page
    physical = marker.physical_page
    if physical is None:
        return f"<!-- printed_page: {printed} -->"
    if printed is None:
        return f"<!-- pdf_page: {physical} -->"
    return f"<!-- pdf_page: {physical} | printed_page: {printed} -->"


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
    page: Mapping[str, object],
    *,
    profile: Optional[PageArtifactProfile],
    options: ExportOptions,
) -> str:
    rendered: List[str] = []
    for block in iter_export_page_blocks(page, profile=profile, options=options):
        if block.level is None:
            rendered.append(block.text)
        else:
            rendered.append(f"{'#' * block.level} {block.text}")
    return "\n\n".join(rendered)

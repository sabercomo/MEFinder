"""Pure conversion of persisted MEFinder document text to Markdown.

The module reads only data already stored by MEFinder (PDF page payloads, EPUB
paragraphs, and bibliographic metadata).  It never reparses a source file,
never OCRs, and never consults MinerU output directories.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Mapping, Optional

from .export_footnotes import (
    Footnote, FootnoteText, NormalizedDocument, normalize_document_export, normalize_document_footnotes,
)
from .markdown_export_normalize import (
    ExportOptions,
    PageArtifactProfile,
    PageMarker,
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
    normalized: Optional[NormalizedDocument] = None,
) -> str:
    """Convert persisted pages into one UTF-8 Markdown document string.

    The export runs a normalization pass (see ``markdown_export_normalize``) that
    strips visible page numbers and repeated running headers/footers, and by
    default emits only the hidden ``<!-- printed_page: N -->`` anchor.  This view
    never mutates the persisted pages or the page-number anchors themselves.
    """

    options = options or ExportOptions()
    # Materialize once for shared page cleanup and document-wide footnote recovery.
    # The Markdown builder
    # already holds the whole document in memory when joining chunks, so this is
    # not a new scaling constraint.
    if normalized is None:
        materialized = [page for page in pages if isinstance(page, Mapping)]
        normalized = normalize_document_export(materialized, options=options)
    chunks = [_frontmatter(title=title, author=author)]
    chunks.extend(_render_items(normalized.items))
    return "\n\n".join(chunks).strip() + "\n"


def epub_paragraphs_to_markdown(
    paragraphs: Iterable[Mapping[str, object]],
    *,
    title: object = None,
    author: object = None,
    options: Optional[ExportOptions] = None,
) -> str:
    """Convert indexed EPUB paragraphs into one UTF-8 Markdown document."""

    options = options or ExportOptions()
    chunks = [_frontmatter(title=title, author=author)]
    previous_page: Optional[str] = None
    for paragraph in paragraphs:
        text = str(paragraph.get("text_raw") or "").strip()
        if not text:
            continue
        printed_page = str(paragraph.get("original_page_start") or "").strip()
        if printed_page and printed_page != previous_page:
            previous_page = printed_page
            if options.page_marker_mode != "none":
                chunks.append(
                    render_markdown_page_marker(PageMarker(printed_page=printed_page))
                )
        style_name = str(paragraph.get("style_name") or "").lower()
        heading = re.fullmatch(r"h([1-6])", style_name)
        if heading:
            chunks.append(f"{'#' * int(heading.group(1))} {text}")
        elif style_name == "blockquote":
            chunks.append("\n".join(f"> {line}" for line in text.splitlines()))
        elif style_name == "li":
            chunks.append(f"- {text}")
        elif style_name == "pre":
            chunks.append("\n".join(f"    {line}" for line in text.splitlines()))
        else:
            chunks.append(text)
    return "\n\n".join(chunks).strip() + "\n"


def page_to_markdown(
    page: Mapping[str, object],
    *,
    profile: Optional[PageArtifactProfile] = None,
    options: Optional[ExportOptions] = None,
) -> str:
    """Render one persisted page payload, page marker first, body after."""

    options = options or ExportOptions()
    if not str(page.get("text_raw") or "").strip():
        return ""
    return "\n\n".join(_render_items(normalize_document_footnotes(
        [page], profile=profile, options=options,
    )))


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


def _render_items(items: Iterable[PageMarker | FootnoteText | Footnote]) -> List[str]:
    rendered: List[str] = []
    for item in items:
        if isinstance(item, PageMarker):
            rendered.append(render_markdown_page_marker(item))
        elif isinstance(item, Footnote):
            # Definitions stay with their chapter. Markdown viewers choose their
            # own displayed numbers; the identifier expresses the relationship.
            lines = item.text.splitlines()
            rendered.append(f"[^{item.note_id}]: {lines[0]}" + "".join(
                f"\n    {line}" for line in lines[1:]
            ))
        else:
            pieces = []
            offset = 0
            for ref in item.references:
                pieces.extend((item.text[offset:ref.start], f"[^{ref.note_id}]"))
                offset = ref.end
            pieces.append(item.text[offset:])
            text = "".join(pieces)
            rendered.append(f"{'#' * item.level} {text}" if item.level else text)
    return rendered

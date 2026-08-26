"""EPUB 3 renderer — a second renderer over the shared export normalization layer.

This module is deliberately a *sibling* of :mod:`markdown_export`, not a
Markdown-to-EPUB converter.  Both renderers consume the same format-neutral
primitives from :mod:`markdown_export_normalize`:

* :class:`ExportOptions`            — page-anchor policy + page cleanup flags
* :func:`build_page_artifact_profile` / :func:`iter_export_page_blocks`
                                     — cleaned ``(level, text)`` content stream
* :func:`resolve_page_marker` → :class:`PageMarker`
                                     — the *semantic* page anchor

The Markdown renderer turns a ``PageMarker`` into an ``<!-- printed_page: N -->``
comment; this renderer turns the identical ``PageMarker`` into an EPUB 3
``pagebreak`` anchor plus a ``page-list`` navigation entry.  That divergence is
the whole point: it proves the normalization layer is reusable across formats.

Scope (MVP, see task): EPUB 3, title/author metadata, heading hierarchy + nav
table of contents, basic body formatting, printed_page → EPUB pagebreak, and the
default page cleanup.  The persisted MEFinder page model is text-only (images,
tables, footnotes are not stored as blocks), so those are intentionally out of
scope — body text is preserved faithfully rather than guessed at.

The EPUB container is written by hand with :mod:`zipfile` (no new dependency);
EPUB 3 is a ZIP whose first entry is an uncompressed ``mimetype``.
"""

from __future__ import annotations

import os
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Mapping, Optional
from xml.sax.saxutils import escape, quoteattr

from .markdown_export_normalize import (
    ExportOptions,
    PageMarker,
    build_page_artifact_profile,
    iter_export_page_blocks,
    resolve_page_marker,
)


SOURCE_NAME = "MEFinder"
DEFAULT_LANGUAGE = "zh"
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

# Container layout.
_MIMETYPE = "application/epub+zip"
_OPF_PATH = "OEBPS/content.opf"
_NAV_PATH = "OEBPS/nav.xhtml"
_CONTENT_PATH = "OEBPS/content.xhtml"
_STYLE_PATH = "OEBPS/style.css"

_STYLESHEET = """\
@namespace epub "http://www.idpf.org/2007/ops";
html { font-size: 100%; }
body { margin: 0 5%; line-height: 1.5; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 1.4em 0 0.6em; }
p { margin: 0 0 0.9em; text-indent: 0; }
span[epub|type~="pagebreak"] { display: none; }
"""


def safe_epub_filename(title: object) -> str:
    """Return a Windows-safe ``{title}.epub`` file name."""

    stem = _ILLEGAL_FILENAME_CHARS.sub("-", str(title or "")).strip(" .-")
    if not stem:
        stem = "MEFinder-document"
    encoded = stem.encode("utf-8")
    if len(encoded) > 120:
        stem = encoded[:120].decode("utf-8", errors="ignore").rstrip(" .-")
    return (stem or "MEFinder-document") + ".epub"


# --------------------------------------------------------------------------- #
# Intermediate model built from the shared content stream
# --------------------------------------------------------------------------- #
@dataclass
class _HeadingEntry:
    level: int
    anchor_id: str
    text: str


@dataclass
class _PageEntry:
    anchor_id: str
    label: str  # printed folio (or physical page in 'full' mode)


@dataclass
class _RenderedContent:
    body_html: str
    headings: List[_HeadingEntry] = field(default_factory=list)
    pages: List[_PageEntry] = field(default_factory=list)


def _pagebreak_label(marker: PageMarker) -> Optional[str]:
    if marker.printed_page not in (None, ""):
        return str(marker.printed_page)
    if marker.physical_page is not None:
        return str(marker.physical_page)
    return None


def _render_content(
    pages: Iterable[Mapping[str, object]],
    *,
    options: ExportOptions,
) -> _RenderedContent:
    materialized = [page for page in pages if isinstance(page, Mapping)]
    profile = build_page_artifact_profile(materialized, options)

    lines: List[str] = []
    headings: List[_HeadingEntry] = []
    page_entries: List[_PageEntry] = []
    heading_seq = 0
    page_seq = 0

    for page in materialized:
        marker = resolve_page_marker(page, options)
        blocks = iter_export_page_blocks(page, profile=profile, options=options)
        if not blocks and marker is None:
            continue
        if marker is not None:
            label = _pagebreak_label(marker)
            if label is not None:
                page_seq += 1
                anchor = f"pagebreak-{page_seq:04d}"
                page_entries.append(_PageEntry(anchor, label))
                # EPUB 3 semantic page anchor; the reader exposes it as page N and
                # the page-list nav points here.  Hidden in body via CSS.
                lines.append(
                    f'<span epub:type="pagebreak" role="doc-pagebreak" '
                    f'id={quoteattr(anchor)} aria-label={quoteattr(label)}></span>'
                )
        for block in blocks:
            if block.level is None:
                lines.append(f"<p>{escape(block.text)}</p>")
            else:
                heading_seq += 1
                anchor = f"h-{heading_seq:04d}"
                headings.append(_HeadingEntry(block.level, anchor, block.text))
                lines.append(
                    f"<h{block.level} id={quoteattr(anchor)}>"
                    f"{escape(block.text)}</h{block.level}>"
                )
    return _RenderedContent("\n".join(lines), headings, page_entries)


# --------------------------------------------------------------------------- #
# XHTML documents
# --------------------------------------------------------------------------- #
def _content_xhtml(title: str, language: str, body_html: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" '
        f'lang={quoteattr(language)} xml:lang={quoteattr(language)}>\n'
        "<head>\n"
        f"<title>{escape(title)}</title>\n"
        '<meta charset="utf-8"/>\n'
        '<link rel="stylesheet" type="text/css" href="style.css"/>\n'
        "</head>\n"
        f"<body>\n{body_html}\n</body>\n</html>\n"
    )


def _heading_tree(headings: List[_HeadingEntry]) -> list:
    """Nest headings into a tree tolerant of skipped levels (e.g. h1→h3)."""

    root: dict = {"children": []}
    stack: list = [(0, root)]
    for entry in headings:
        node = {"entry": entry, "children": []}
        while stack and stack[-1][0] >= entry.level:
            stack.pop()
        if not stack:
            stack = [(0, root)]
        stack[-1][1]["children"].append(node)
        stack.append((entry.level, node))
    return root["children"]


def _render_toc_list(nodes: list) -> str:
    if not nodes:
        return ""
    items = []
    for node in nodes:
        entry: _HeadingEntry = node["entry"]
        child_html = _render_toc_list(node["children"])
        href = f"content.xhtml#{entry.anchor_id}"
        inner = f"<a href={quoteattr(href)}>{escape(entry.text)}</a>"
        if child_html:
            inner += "\n" + child_html
        items.append(f"<li>{inner}</li>")
    return "<ol>\n" + "\n".join(items) + "\n</ol>"


def _nav_xhtml(
    title: str,
    language: str,
    headings: List[_HeadingEntry],
    page_entries: List[_PageEntry],
) -> str:
    tree = _heading_tree(headings)
    toc_body = _render_toc_list(tree)
    if not toc_body:
        # EPUB requires a non-empty toc nav; fall back to the whole body.
        toc_body = (
            '<ol>\n<li><a href="content.xhtml">'
            f"{escape(title)}</a></li>\n</ol>"
        )
    toc_nav = (
        '<nav epub:type="toc" role="doc-toc" id="toc">\n'
        "<h1>目录</h1>\n"
        f"{toc_body}\n</nav>"
    )

    page_nav = ""
    if page_entries:
        page_items = "\n".join(
            f'<li><a href={quoteattr("content.xhtml#" + entry.anchor_id)}>'
            f"{escape(entry.label)}</a></li>"
            for entry in page_entries
        )
        page_nav = (
            '\n<nav epub:type="page-list" role="doc-pagelist" hidden="hidden">\n'
            "<h1>页码</h1>\n"
            f"<ol>\n{page_items}\n</ol>\n</nav>"
        )

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" '
        f'lang={quoteattr(language)} xml:lang={quoteattr(language)}>\n'
        "<head>\n"
        f"<title>{escape(title)}</title>\n"
        '<meta charset="utf-8"/>\n'
        "</head>\n"
        f"<body>\n{toc_nav}{page_nav}\n</body>\n</html>\n"
    )


def _container_xml() -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "<rootfiles>\n"
        f'<rootfile full-path="{_OPF_PATH}" '
        'media-type="application/oebps-package+xml"/>\n'
        "</rootfiles>\n</container>\n"
    )


def _content_opf(
    *,
    title: str,
    author: str,
    language: str,
    identifier: str,
    modified: str,
) -> str:
    meta = [
        f'<dc:identifier id="pub-id">{escape(identifier)}</dc:identifier>',
        f"<dc:title>{escape(title)}</dc:title>",
        f"<dc:language>{escape(language)}</dc:language>",
        f'<meta property="dcterms:modified">{escape(modified)}</meta>',
        f"<dc:source>{escape(SOURCE_NAME)}</dc:source>",
    ]
    if author:
        meta.insert(2, f'<dc:creator id="creator">{escape(author)}</dc:creator>')
    metadata = "\n".join(meta)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        f'unique-identifier="pub-id" xml:lang={quoteattr(language)}>\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/">\n'
        f"{metadata}\n"
        "</metadata>\n"
        "<manifest>\n"
        '<item id="nav" href="nav.xhtml" '
        'media-type="application/xhtml+xml" properties="nav"/>\n'
        '<item id="content" href="content.xhtml" '
        'media-type="application/xhtml+xml"/>\n'
        '<item id="style" href="style.css" media-type="text/css"/>\n'
        "</manifest>\n"
        '<spine>\n<itemref idref="content"/>\n</spine>\n'
        "</package>\n"
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_epub_bytes(
    pages: Iterable[Mapping[str, object]],
    *,
    title: object = None,
    author: object = None,
    language: object = None,
    options: Optional[ExportOptions] = None,
    identifier: Optional[str] = None,
    modified: Optional[str] = None,
) -> bytes:
    """Render persisted pages into one in-memory EPUB 3 container.

    Reads only already-persisted MEFinder data; never reparses, OCRs, or touches
    the library.  ``language`` defaults to ``zh`` when unknown.
    """

    import io

    options = options or ExportOptions()
    title_text = str(title or "").strip() or "MEFinder-document"
    author_text = str(author or "").strip()
    language_text = str(language or "").strip() or DEFAULT_LANGUAGE
    identifier = identifier or f"urn:uuid:{uuid.uuid4()}"
    modified = modified or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    content = _render_content(pages, options=options)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        # The mimetype MUST be the first entry and stored uncompressed.
        archive.writestr(
            zipfile.ZipInfo("mimetype"), _MIMETYPE, compress_type=zipfile.ZIP_STORED
        )
        archive.writestr("META-INF/container.xml", _container_xml())
        archive.writestr(
            _OPF_PATH,
            _content_opf(
                title=title_text,
                author=author_text,
                language=language_text,
                identifier=identifier,
                modified=modified,
            ),
        )
        archive.writestr(
            _NAV_PATH,
            _nav_xhtml(title_text, language_text, content.headings, content.pages),
        )
        archive.writestr(
            _CONTENT_PATH,
            _content_xhtml(title_text, language_text, content.body_html),
        )
        archive.writestr(_STYLE_PATH, _STYLESHEET)
    return buffer.getvalue()


def write_epub(
    output_path: Path,
    pages: Iterable[Mapping[str, object]],
    *,
    title: object = None,
    author: object = None,
    language: object = None,
    options: Optional[ExportOptions] = None,
) -> Path:
    """Atomically write one EPUB 3 file and return its path."""

    data = build_epub_bytes(
        pages,
        title=title,
        author=author,
        language=language,
        options=options,
    )
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    with partial.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(target)
    return target

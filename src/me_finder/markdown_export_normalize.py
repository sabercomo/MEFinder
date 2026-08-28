"""Export-only normalization layer for MEFinder document exports.

This module produces a *format-neutral view* of persisted pages that is cleaner
to read as an exported document.  It NEVER writes back to the database and NEVER
changes the data used by the reader, search, citation, highlight, or page-mapping
systems.  It only decides, at export time, which blocks are noise and what each
page's marker *means* — deliberately without committing to any output syntax.

Design:

    pages ─┬─> build_page_artifact_profile()  (cross-page header/footer stats)
           │
           page ──> normalize_export_blocks(page, profile, options)  (drop noise)
           page ──> resolve_page_marker(page, options)  → PageMarker | None

``ExportOptions`` and ``PageMarker`` are intentionally format-agnostic so a
future EPUB exporter can reuse the very same cleanup flags and page-anchor policy
and render ``PageMarker`` as an EPUB ``pagebreak`` anchor, while the Markdown
renderer renders it as an ``<!-- printed_page: N -->`` HTML comment.  The HTML
comment syntax lives in the Markdown renderer, not here.

The heavy lifting reuses existing, verified structure signals instead of broad
regexes:

* Heading metadata is evidence, not a trusted tree. ``trusted_heading`` combines
  located outline/TOC assignments, geometry and cross-page repetition before
  either the renderer or the numbering scope can treat a block as a heading.
* ``mineru_type``/``type`` == ``page_number`` marks a parser-detected folio.
* ``page_width``/``page_height`` + ``bbox`` give a normalized top/bottom region.
* ``printed_page``/``citation_page`` is the page's own known folio, used to
  delete a *visible* copy of that exact number with high confidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Sequence, Tuple


from .auto_page_mapping import _layout_bbox_scale, _normalized_page_bbox
from .document_heading import normalize_heading_text


# --------------------------------------------------------------------------- #
# Options (format-neutral; shared by every document exporter)
# --------------------------------------------------------------------------- #
PAGE_MARKER_NONE = "none"
PAGE_MARKER_PRINTED = "printed"
PAGE_MARKER_FULL = "full"
VALID_PAGE_MARKER_MODES = frozenset(
    {PAGE_MARKER_NONE, PAGE_MARKER_PRINTED, PAGE_MARKER_FULL}
)
DEFAULT_PAGE_MARKER_MODE = PAGE_MARKER_PRINTED

# Backwards-compatible private alias (kept for existing references).
_VALID_MARKER_MODES = VALID_PAGE_MARKER_MODES


@dataclass(frozen=True)
class ExportOptions:
    """Format-neutral page-cleanup + page-anchor policy for any exporter.

    Every field is a *document view* concern, not a Markdown concern, so an EPUB
    exporter can accept the identical options.  All cleanup is opt-out.
    """

    page_marker_mode: str = DEFAULT_PAGE_MARKER_MODE
    remove_visible_page_numbers: bool = True
    remove_running_headers: bool = True
    remove_running_footers: bool = True
    # Higher-risk; only acts on parser-identified TOC structure.  Off by default
    # because this export layer cannot yet reliably identify a TOC page from
    # page payloads alone (see task section 8).
    clean_toc_page_numbers: bool = False

    def __post_init__(self) -> None:
        if self.page_marker_mode not in VALID_PAGE_MARKER_MODES:
            object.__setattr__(
                self, "page_marker_mode", DEFAULT_PAGE_MARKER_MODE
            )

    @classmethod
    def from_mapping(
        cls, payload: Optional[Mapping[str, object]]
    ) -> "ExportOptions":
        """Build options from an untrusted request/preferences mapping.

        Unknown or malformed values fall back to the safe defaults; this is the
        single place transport layers (HTTP payloads, preferences.json) turn raw
        data into export options, so every exporter validates identically.
        """

        if not isinstance(payload, Mapping):
            return cls()
        mode = payload.get("page_marker_mode")
        if mode not in VALID_PAGE_MARKER_MODES:
            mode = DEFAULT_PAGE_MARKER_MODE

        def flag(key: str, default: bool) -> bool:
            value = payload.get(key)
            return bool(value) if isinstance(value, bool) else default

        return cls(
            page_marker_mode=str(mode),
            remove_visible_page_numbers=flag("remove_visible_page_numbers", True),
            remove_running_headers=flag("remove_running_headers", True),
            remove_running_footers=flag("remove_running_footers", True),
            clean_toc_page_numbers=flag("clean_toc_page_numbers", False),
        )

    def to_mapping(self) -> dict:
        """Serialize to a plain dict for preferences.json / API responses."""

        return {
            "page_marker_mode": self.page_marker_mode,
            "remove_visible_page_numbers": self.remove_visible_page_numbers,
            "remove_running_headers": self.remove_running_headers,
            "remove_running_footers": self.remove_running_footers,
            "clean_toc_page_numbers": self.clean_toc_page_numbers,
        }


# Deprecated alias retained so existing imports keep working.  The options are
# not Markdown-specific; prefer ``ExportOptions``.
MarkdownExportOptions = ExportOptions


# --------------------------------------------------------------------------- #
# Region / geometry helpers (reuse existing normalization)
# --------------------------------------------------------------------------- #
# A page number that MinerU tagged as its own block role.
_PAGE_NUMBER_ROLES = frozenset({"page_number"})
# Roles that are, by construction, page decoration rather than body text.
_DECORATION_ROLES = frozenset(
    {"page_number", "header", "footer", "page_header", "page_footer"}
)

# Explicit note markers only: ordinary digits may be page numbers, years or
# mathematical notation. Keep uncertain note-like text out of decoration cleanup.
_NOTE_MARKER = r"[①-⑳㉑-㉟㊱-㊿]|\[[1-9][0-9]{0,2}\]"
FOOTNOTE_MARKER_RE = re.compile(
    r"\$\s*\^\{\s*(?P<sup>" + _NOTE_MARKER + r")\s*\}\s*\$"
    r"|(?P<plain>" + _NOTE_MARKER + r")"
)
FOOTNOTE_ROLES = frozenset({"footnote", "page_footnote"})


def has_footnote_signal(block: Mapping[str, object]) -> bool:
    return _block_role(block) in FOOTNOTE_ROLES or bool(
        FOOTNOTE_MARKER_RE.search(str(block.get("text") or ""))
    )

# Top/bottom bands (fraction of page height) reused from edition_folio_anchors.
_TOP_BAND = 0.16
_BOTTOM_BAND = 0.84

# A block whose entire text is a bare folio (arabic, optionally zero padded) or
# a short roman numeral.  Years (1800-2099) are excluded by _looks_like_folio.
_BARE_ARABIC = re.compile(r"\s*0*([0-9]{1,4})\s*\Z")
_BARE_ROMAN = re.compile(r"\s*([ivxlcdm]{1,7})\s*\Z", re.IGNORECASE)

# Running-header/footer detection thresholds.  Kept explicit and conservative so
# small documents (and unit fixtures) never trigger accidental removal.
_MIN_ARTIFACT_COUNT = 3


def _block_role(block: Mapping[str, object]) -> str:
    return (
        str(
            block.get("mineru_type")
            or block.get("parser_type")
            or block.get("type")
            or ""
        )
        .strip()
        .casefold()
    )


def _has_heading_level(block: Mapping[str, object]) -> bool:
    return heading_level(block) is not None


def _page_scale(page: Mapping[str, object]) -> Tuple[float, float]:
    blocks = page.get("blocks")
    return _layout_bbox_scale(
        blocks if isinstance(blocks, list) else [],
        float(page.get("page_width") or 1000.0),
        float(page.get("page_height") or 1000.0),
    )


def _block_y_center(
    block: Mapping[str, object], width: float, height: float
) -> Optional[float]:
    bbox = _normalized_page_bbox(dict(block), width, height)
    if not bbox:
        return None
    return (float(bbox[1]) + float(bbox[3])) / 2.0


def _ordered_body_blocks(
    page: Mapping[str, object],
) -> List[Mapping[str, object]]:
    blocks = page.get("blocks")
    if not isinstance(blocks, (list, tuple)):
        return []
    return [
        block
        for block in blocks
        if isinstance(block, Mapping) and str(block.get("text") or "").strip()
    ]


def _region_of(
    index: int, total: int, y_center: Optional[float]
) -> Optional[str]:
    """Classify a block as living in the page 'top' or 'bottom' region.

    Prefers geometry (normalized bbox band); falls back to reading order (the
    first / last block) when a page has no usable coordinates.
    """

    if y_center is not None:
        if y_center <= _TOP_BAND:
            return "top"
        if y_center >= _BOTTOM_BAND:
            return "bottom"
        return None
    if total <= 0:
        return None
    if index == 0:
        return "top"
    if index == total - 1:
        return "bottom"
    return None


# --------------------------------------------------------------------------- #
# Visible-page-number detection
# --------------------------------------------------------------------------- #
def _folio_int(text: str) -> Optional[int]:
    """Return the integer value of a bare arabic folio, else None.

    Excludes 4-digit values in the year range so body years (e.g. 1848) are
    never mistaken for a page number.
    """

    match = _BARE_ARABIC.fullmatch(text)
    if match is None:
        return None
    value = int(match.group(1))
    if value <= 0 or 1800 <= value <= 2099:
        return None
    return value


def _printed_page_raw(page: Mapping[str, object]) -> Optional[str]:
    for key in ("citation_page", "printed_page", "logical_page", "book_page"):
        value = page.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _printed_matches_text(printed: Optional[str], text: str) -> bool:
    """Whether ``text`` is a standalone copy of the page's printed folio."""

    if printed is None:
        return False
    stripped = text.strip()
    printed = printed.strip()
    # Arabic comparison, tolerant of zero padding ("0135" == "135").
    text_int = _folio_int(stripped)
    printed_int = _folio_int(printed)
    if text_int is not None and printed_int is not None:
        return text_int == printed_int
    # Roman numeral comparison ("ix" == "IX"), only when both look roman.
    if _BARE_ROMAN.fullmatch(stripped) and _BARE_ROMAN.fullmatch(printed):
        return stripped.casefold() == printed.casefold()
    return False


def _is_visible_page_number(
    block: Mapping[str, object],
    *,
    printed: Optional[str],
    region: Optional[str],
) -> bool:
    """High-confidence 'this block is a visible folio, delete it' decision.

    Two independent, conservative signals:

    * the parser tagged the block role ``page_number`` (any position), or
    * the block sits in the top/bottom region and its whole text equals the
      page's own known printed folio (arabic tolerant of zero padding, or roman).
    """

    if _has_heading_level(block):
        return False
    text = str(block.get("text") or "").strip()
    if not text:
        return False
    if _block_role(block) in _PAGE_NUMBER_ROLES:
        # Parser-identified folio: only delete when it truly is a short number
        # (guards against a mislabeled body block).
        return _folio_int(text) is not None or bool(_BARE_ROMAN.fullmatch(text))
    if region in ("top", "bottom") and _printed_matches_text(printed, text):
        return True
    return False


def _strip_heading_folio_prefix(
    block: Mapping[str, object], printed: Optional[str]
) -> Optional[str]:
    """Return heading text with a leading folio prefix removed, else None.

    Only fires when the leading whitespace-delimited token is exactly the page's
    printed folio (e.g. heading "24 纯粹直观" on printed page 24).  Never touches
    "1844年经济学哲学手稿" because "1844年" is not a whitespace-delimited number
    and 1844 is not the printed folio.
    """

    if printed is None or not _has_heading_level(block):
        return None
    text = str(block.get("text") or "").strip()
    match = re.match(r"^(0*[0-9]{1,4}|[ivxlcdmIVXLCDM]{1,7})\s+(\S.*)$", text)
    if match is None:
        return None
    prefix, remainder = match.group(1), match.group(2).strip()
    if not remainder:
        return None
    if not _printed_matches_text(printed, prefix):
        return None
    return remainder


# --------------------------------------------------------------------------- #
# Page artifact profile (running headers / footers)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PageArtifactProfile:
    """Cross-page statistics identifying repeated page decoration."""

    running_headers: frozenset = field(default_factory=frozenset)
    running_footers: frozenset = field(default_factory=frozenset)

    def is_running_header(self, normalized_text: str) -> bool:
        return normalized_text in self.running_headers

    def is_running_footer(self, normalized_text: str) -> bool:
        return normalized_text in self.running_footers


def _page_region_texts(
    page: Mapping[str, object],
) -> Tuple[set, set]:
    """Normalized texts appearing in this page's top and bottom regions.

    Parser heading levels are NOT evidence against a running header. Footnote
    candidates are protected before they can contribute to these statistics.
    """

    width, height = _page_scale(page)
    ordered = _ordered_body_blocks(page)
    top: set = set()
    bottom: set = set()
    for index, block in enumerate(ordered):
        if has_footnote_signal(block):
            continue
        norm = normalize_heading_text(block.get("text"))
        if not norm:
            continue
        region = _region_of(index, len(ordered), _block_y_center(block, width, height))
        if region == "top":
            top.add(norm)
        elif region == "bottom":
            bottom.add(norm)
    return top, bottom


def build_page_artifact_profile(
    pages: Sequence[Mapping[str, object]],
    options: Optional[ExportOptions] = None,
) -> PageArtifactProfile:
    """Detect running headers/footers by cross-page repetition of region text."""

    header_counts: dict = {}
    footer_counts: dict = {}
    body_texts: set = set()
    total = 0
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        total += 1
        top, bottom = _page_region_texts(page)
        width, height = _page_scale(page)
        for block in _ordered_body_blocks(page):
            y = _block_y_center(block, width, height)
            if y is not None and _TOP_BAND < y < _BOTTOM_BAND and not has_footnote_signal(block):
                body_texts.add(normalize_heading_text(block.get("text")))
        for norm in top:
            header_counts[norm] = header_counts.get(norm, 0) + 1
        for norm in bottom:
            footer_counts[norm] = footer_counts.get(norm, 0) + 1

    def artifacts(counts: dict) -> frozenset:
        if total <= 0:
            return frozenset()
        # A running title may cover only a short chapter in a long book. Its
        # frequency must not be diluted by the length of unrelated chapters.
        return frozenset(
            text for text, count in counts.items()
            if count >= _MIN_ARTIFACT_COUNT or text in body_texts
        )

    # Evidence remains available even when a caller opts out of visible cleanup.
    running_headers = artifacts(header_counts)
    running_footers = artifacts(footer_counts)
    return PageArtifactProfile(
        running_headers=running_headers,
        running_footers=running_footers,
    )


# --------------------------------------------------------------------------- #
# Per-page block normalization
# --------------------------------------------------------------------------- #
def normalize_export_blocks(
    page: Mapping[str, object],
    *,
    profile: Optional[PageArtifactProfile] = None,
    options: Optional[ExportOptions] = None,
) -> List[Mapping[str, object]]:
    """Return the renderable blocks for one page with export noise removed.

    Never mutates the input blocks: a block whose heading text needs a folio
    prefix stripped is returned as a shallow copy with only ``text`` changed.
    """

    options = options or ExportOptions()
    profile = profile or PageArtifactProfile()
    ordered = _ordered_body_blocks(page)
    if not ordered:
        return []
    width, height = _page_scale(page)
    printed = _printed_page_raw(page)

    result: List[Mapping[str, object]] = []
    for index, block in enumerate(ordered):
        region = _region_of(index, len(ordered), _block_y_center(block, width, height))
        decision = trusted_heading(block, page=page, region=region, profile=profile)
        block = {**block, "_export_heading": decision}
        # Protect both note bodies and their references BEFORE any deletion,
        # including orphan notes and repeated bibliographic text such as 同上.
        if has_footnote_signal(block):
            result.append(block)
            continue
        if options.remove_visible_page_numbers and _is_visible_page_number(
            block, printed=printed, region=region
        ):
            continue

        # Running header / footer removal — never for real headings, and only in
        # the matching region so a mid-body sentence that happens to repeat is
        # left untouched.
        if not _has_heading_level(block):
            norm = normalize_heading_text(block.get("text"))
            if (
                options.remove_running_headers
                and region == "top"
                and profile.is_running_header(norm)
            ):
                continue
            if (
                options.remove_running_footers
                and region == "bottom"
                and profile.is_running_footer(norm)
            ):
                continue

        if options.remove_visible_page_numbers:
            cleaned = _strip_heading_folio_prefix(block, printed)
            if cleaned is not None:
                block = {**block, "text": cleaned}

        result.append(block)
    return result


# --------------------------------------------------------------------------- #
# Shared, format-neutral content primitives
#
# Both the Markdown renderer and the EPUB renderer consume these so the cleanup
# rules, heading-level resolution, and the block/raw-text trust gate stay in one
# place.  A renderer only decides how to serialize the (level, text) stream.
# --------------------------------------------------------------------------- #
MAX_HEADING_LEVEL = 6

_ORDINAL = r"[零〇一二三四五六七八九十百千万两兩0-9０-９]+"
_CHAPTER_TITLE = re.compile(
    rf"^(?:第\s*{_ORDINAL}\s*章|chapter\s+(?:[0-9]+|[ivxlcdm]+)\b)", re.IGNORECASE
)
_PART_TITLE = re.compile(
    rf"^(?:第\s*{_ORDINAL}\s*(?:篇|部(?:分)?)|part\s+(?:[0-9]+|[ivxlcdm]+)\b)", re.IGNORECASE
)
_SECTION_TITLE = re.compile(rf"^第\s*{_ORDINAL}\s*节")


@dataclass(frozen=True)
class HeadingDecision:
    level: Optional[int]
    kind: str
    reason: str


def trusted_heading(block, *, page, region, profile) -> HeadingDecision:
    """One decision shared by cleanup, both outlines and numbering scopes.

    Enrichment is evidence attached to a particular block, not a trusted tree.
    Repeated edge text defeats raw/v2 levels and unlocated TOC assignments.
    A bookmark, or TOC entry verified at its printed destination, can identify
    the real occurrence even when its title is repeated in running headers.
    """
    if "_export_heading" in block:
        return block["_export_heading"]
    text = str(block.get("text") or "").strip()
    structural_title = str(block.get("document_heading_title")
                           or _strip_heading_folio_prefix(block, _printed_page_raw(page)) or text)
    kind = "chapter" if _CHAPTER_TITLE.match(structural_title) else (
        "part" if _PART_TITLE.match(structural_title) else (
            "section" if _SECTION_TITLE.match(structural_title) else "heading"
        )
    )
    if re.match(r"^(?:过渡(?:\s|$)|索引$|index$|译名对照表)", structural_title, re.IGNORECASE):
        kind = "non_chapter_section"
    level = heading_level(block)
    source = str(block.get("document_heading_source") or "")
    located = source == "pdf_outline" or (
        source == "document_toc"
        and block.get("document_heading_printed_page") is not None
        and str(block["document_heading_printed_page"]) == _printed_page_raw(page)
    )
    norm = normalize_heading_text(text)
    repeated = (region == "top" and profile.is_running_header(norm)) or (
        region == "bottom" and profile.is_running_footer(norm)
    )
    if repeated and not located:
        return HeadingDecision(None, kind, "REPEATED_EDGE_TEXT")
    if _block_role(block) in _DECORATION_ROLES and not located:
        return HeadingDecision(None, kind, "PAGE_DECORATION")
    if _block_role(block) in FOOTNOTE_ROLES:
        return HeadingDecision(None, kind, "NOTE_BODY")
    if level is not None:
        return HeadingDecision(level, kind, source.upper() if source else "PARSER_TITLE_WITHOUT_DECORATION")
    # A standalone part divider is often split from its subtitle by the parser.
    # Only the explicit ordinal label, in the body region, is sufficient here.
    if kind == "part" and _PART_TITLE.fullmatch(text) and region is None:
        return HeadingDecision(1, kind, "EXPLICIT_PART_DIVIDER")
    return HeadingDecision(None, kind, "NO_HEADING_EVIDENCE")


@dataclass
class ExportStructure:
    pages: list[list[dict]]
    scopes: list[dict]
    heading_issues: list[dict]


def prepare_export_structure(pages, *, profile, options) -> ExportStructure:
    """Protect candidates, decide headings/scopes, then clean the export view.

    Numbering policy: explicit trusted chapters start scopes; parts supply the
    parent and close the preceding note output without resetting numbers;
    sections inherit their chapter. Content
    before the first chapter uses preface scope 0. With no trusted chapter the
    whole document uses scope 0, explicitly labelled document (not guessed h1).
    """
    prepared = []
    issues = []
    for page in pages:
        raw = str(page.get("text_raw") or "").strip()
        blocks = page.get("blocks")
        if not isinstance(blocks, (list, tuple)) or not blocks_aligned_with_text(blocks, raw):
            prepared.append([{"text": raw}] if raw else [])
            continue
        width, height = _page_scale(page)
        indexed = []
        for index, block in enumerate(blocks):
            if not isinstance(block, Mapping) or not str(block.get("text") or "").strip():
                continue
            region = _region_of(index, len(blocks), _block_y_center(block, width, height))
            decision = trusted_heading(block, page=page, region=region, profile=profile)
            if heading_level(block) is not None and decision.level is None:
                issues.append({
                    "reason": "UNTRUSTED_HEADING_BOUNDARY", "heading_reason": decision.reason,
                    "kind": decision.kind, "text": block["text"],
                    "source_file_id": page.get("source_file_id"),
                    "source_page_index": page.get("pdf_page_index"),
                    "source_physical_page": _physical_page(page),
                    "source_printed_page": _printed_page_raw(page), "source_block_index": index,
                })
            indexed.append({**block, "_export_index": index, "_export_heading": decision})
        # Parser arrays can append a discarded header after its own body. Move
        # only a trusted heading, and only across blocks proven below it in the
        # same horizontal flow. Never sort prose or change source block indices.
        for position in range(len(indexed)):
            block = indexed[position]
            if heading_level(block) is None:
                continue
            bbox = _normalized_page_bbox(block, width, height)
            if bbox is None:
                continue
            target = position
            while target > 0:
                previous = _normalized_page_bbox(indexed[target - 1], width, height)
                if previous is None or bbox[3] > previous[1]:
                    break
                if min(bbox[2], previous[2]) <= max(bbox[0], previous[0]):
                    break  # another column is not evidence of reading order
                target -= 1
            if target != position:
                indexed.insert(target, indexed.pop(position))
        prepared.append(indexed)

    has_parts = any(heading_level(b) is not None and b["_export_heading"].kind == "part"
                    for blocks in prepared for b in blocks)
    scopes = [{"scope_id": 0, "kind": "document", "title": None, "part_title": None,
               "source_physical_page": _physical_page(pages[0]) if pages else None,
               "source_printed_page": _printed_page_raw(pages[0]) if pages else None,
               "source_block_index": None, "boundary_reason": "NO_TRUSTED_CHAPTER"}]
    scope_id = 0
    part_title = None
    chapter_level = None
    for page, blocks in zip(pages, prepared):
        for block in blocks:
            if heading_level(block) is not None:
                decision = block["_export_heading"]
                if decision.kind == "part":
                    block["_export_scope_end_before"] = True
                    part_title = str(block["text"]).strip()
                    chapter_level = None
                    block["_export_heading"] = HeadingDecision(1, "part", decision.reason)
                elif decision.kind == "chapter":
                    scope_id += 1
                    scopes[0].update(kind="preface", boundary_reason="BEFORE_FIRST_CHAPTER")
                    scopes.append({"scope_id": scope_id, "kind": "chapter", "title": str(block["text"]).strip(),
                                   "part_title": part_title, "boundary_reason": decision.reason,
                                   "source_physical_page": _physical_page(page),
                                   "source_printed_page": _printed_page_raw(page),
                                   "source_block_index": block["_export_index"]})
                    chapter_level = 2 if has_parts else decision.level
                    block["_export_heading"] = HeadingDecision(chapter_level,
                                                               "chapter", decision.reason)
                elif decision.kind == "non_chapter_section":
                    chapter_level = None
                elif chapter_level is not None and has_parts:
                    # Flat OCR TOC indentation must not make chapter subsections
                    # siblings of parts. Non-chapter headings inherit the scope.
                    block["_export_heading"] = HeadingDecision(
                        min(MAX_HEADING_LEVEL, max(chapter_level + 1, decision.level)),
                        decision.kind, decision.reason,
                    )
            block["_export_scope_id"] = scope_id
    cleaned = []
    for page, blocks in zip(pages, prepared):
        if blocks and "_export_index" not in blocks[0]:
            cleaned.append(blocks)  # raw-text trust gate, never infer page structure
        else:
            cleaned.append(list(normalize_export_blocks({**page, "blocks": blocks}, profile=profile, options=options)))
    return ExportStructure(cleaned, scopes, issues)


@dataclass(frozen=True)
class ExportBlock:
    """One renderable unit of a page: a heading (level 1-6) or body text."""

    text: str
    level: Optional[int] = None  # 1..6 for a heading, None for a paragraph

    @property
    def is_heading(self) -> bool:
        return self.level is not None


def heading_level(block: Mapping[str, object]) -> Optional[int]:
    """Resolve a block's canonical heading level, else None.

    Priority mirrors the import pipeline: ``document_heading_level`` (semantic PDF
    outline or MinerU v2 title) wins; otherwise the parser's raw ``text_level``;
    otherwise the block is ordinary body text.  Never rewrites either field.
    """

    if "_export_heading" in block:
        return block["_export_heading"].level
    level = _coerce_heading_level(block.get("document_heading_level"))
    if level is not None:
        return level
    return _coerce_heading_level(block.get("text_level"))


def _coerce_heading_level(value: object) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        level = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= level <= MAX_HEADING_LEVEL:
        return level
    return None


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


def blocks_aligned_with_text(
    blocks: Sequence[object], text_raw: str
) -> bool:
    """Whether the persisted blocks faithfully reconstruct the page text.

    Per-block cleanup is only safe when this holds; otherwise a renderer should
    fall back to the raw page text untouched rather than risk dropping content.
    """

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


def iter_export_page_blocks(
    page: Mapping[str, object],
    *,
    profile: Optional[PageArtifactProfile] = None,
    options: Optional[ExportOptions] = None,
) -> List[ExportBlock]:
    """Yield the cleaned, format-neutral content of one page.

    When the blocks reconstruct the page text, they are normalized (noise
    removed) and mapped to ``(level, text)``.  Otherwise the whole raw page text
    is returned as a single body block, unmodified.  This is the single content
    primitive shared by every exporter.
    """

    options = options or ExportOptions()
    text_raw = str(page.get("text_raw") or "")
    blocks = page.get("blocks")
    blocks = blocks if isinstance(blocks, (list, tuple)) else None
    if blocks is not None and blocks_aligned_with_text(blocks, text_raw):
        result: List[ExportBlock] = []
        for block in normalize_export_blocks(page, profile=profile, options=options):
            if not isinstance(block, Mapping):
                continue
            block_text = str(block.get("text") or "").strip()
            if not block_text:
                continue
            result.append(ExportBlock(text=block_text, level=heading_level(block)))
        return result
    body = text_raw.strip()
    return [ExportBlock(text=body)] if body else []


# --------------------------------------------------------------------------- #
# Semantic page marker
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PageMarker:
    """A page anchor as *meaning*, not syntax.

    ``printed_page`` is the book's own printed folio (kept verbatim, so roman
    numerals such as ``"ix"`` survive).  ``physical_page`` is the 1-based PDF
    page.  A renderer decides how to serialize whichever fields are present — an
    HTML comment for Markdown, a ``pagebreak`` anchor for EPUB, etc.  A field
    left ``None`` must not be emitted; it is never fabricated from the other.
    """

    printed_page: Optional[str] = None
    physical_page: Optional[int] = None

    @property
    def is_empty(self) -> bool:
        return self.printed_page is None and self.physical_page is None


def _physical_page(page: Mapping[str, object]) -> Optional[int]:
    for key in ("pdf_page_number_1based", "physical_pdf_page"):
        value = page.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    index = page.get("pdf_page_index")
    if index not in (None, ""):
        try:
            return int(index) + 1
        except (TypeError, ValueError):
            return None
    return None


def resolve_page_marker(
    page: Mapping[str, object],
    options: Optional[ExportOptions] = None,
) -> Optional[PageMarker]:
    """Resolve the page's anchor *semantics* under the marker-mode policy.

    * ``none``    — no marker at all.
    * ``printed`` — only the printed folio, and only when it is actually known
      (never guessed from the PDF page).
    * ``full``    — the printed folio plus the physical PDF page (debug view).

    Returns ``None`` when nothing should be emitted, so a renderer never has to
    know the policy.
    """

    options = options or ExportOptions()
    mode = options.page_marker_mode
    if mode not in VALID_PAGE_MARKER_MODES:
        mode = DEFAULT_PAGE_MARKER_MODE
    if mode == PAGE_MARKER_NONE:
        return None

    printed = _printed_page_raw(page)

    if mode == PAGE_MARKER_PRINTED:
        if printed is None:
            return None
        return PageMarker(printed_page=printed)

    # full — expose the physical PDF page too.
    physical = _physical_page(page)
    marker = PageMarker(printed_page=printed, physical_page=physical)
    return None if marker.is_empty else marker

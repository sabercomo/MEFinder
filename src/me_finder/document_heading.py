"""Canonical document heading metadata for indexed PDFs.

This module derives an optional, canonical heading level for persisted
``pdf_pages`` blocks *without* touching the parser's raw ``text_level`` or any
page-number system.  Two independent structure sources are supported, in strict
priority order:

    semantic PDF outline (bookmarks)  >  MinerU content_list_v2 titles

The result is written onto each mapped block as two optional payload fields:

    document_heading_level : int | None
    document_heading_source: str | None  ("pdf_outline" | "mineru_v2" | "mineru_raw")

Design constraints (see task spec):

* Never overwrite ``text``/``text_level``/``bbox``/page mapping/visual page number.
* PDF page-navigation bookmarks never enter the heading tree.
* PDF PageLabels stay exclusively in the page-number system (handled elsewhere).
* No chapter regexes, no short-text guessing, no heuristic heading detection.
"""

from __future__ import annotations

import glob
import logging
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .auto_page_mapping import normalize_numeric_bookmark_title

logger = logging.getLogger(__name__)

# Bumped when the heading-derivation algorithm changes, so persisted documents
# can be lazily re-enriched (see document_heading_profile / ensure_document_headings).
DOCUMENT_HEADING_VERSION = 2

HEADING_SOURCE_PDF_OUTLINE = "pdf_outline"
HEADING_SOURCE_DOCUMENT_TOC = "document_toc"
HEADING_SOURCE_MINERU_V2 = "mineru_v2"
HEADING_SOURCE_MINERU_RAW = "mineru_raw"

# Rank used only to protect a higher-priority source from being overwritten by a
# lower-priority one.  Larger wins.
#   semantic PDF outline > document TOC hierarchy > MinerU v2 raw > MinerU text_level
_SOURCE_RANK = {
    HEADING_SOURCE_MINERU_RAW: 1,
    HEADING_SOURCE_MINERU_V2: 2,
    HEADING_SOURCE_DOCUMENT_TOC: 3,
    HEADING_SOURCE_PDF_OUTLINE: 4,
}

# Frontmatter bookmark titles that merely *locate* a table-of-contents page.
# They are used to find the TOC start; they never become chapter headings.
_TOC_LOCATOR_TITLES = {"目录", "目次", "contents", "tableofcontents"}

# End-matter TOC entries that open the index section; used to bound the body.
_INDEX_TITLES = {"索引", "index"}

# Leading chapter/section markers, stripped ONLY to match a TOC entry against a
# body title that MinerU split (e.g. "第二章 方法问题" -> body title "方法问题").
# This is a matching aid, never a hierarchy source, and never book-specific.
_CHAPTER_PREFIX_RE = re.compile(
    r"^(第[零一二三四五六七八九十百千0-9]+[章节節篇部卷回讲講])"
)

# x0 indentation clustering: tolerance is the larger of a page-width fraction
# and a small pixel floor, so it is never hard-coded to one book's coordinates.
_X0_CLUSTER_RATIO = 0.03
_X0_MIN_TOLERANCE = 6.0

# Trailing dotted page number on a TOC line ("导言 什么是批判理论？1").
_TOC_TRAILING_PAGE_RE = re.compile(r"[\s.·…]*\d+\s*$")

# Outline classification thresholds.  Kept explicit and small so they are easy
# to unit-test and reason about.
_MIN_SEMANTIC_ENTRIES = 3
_PAGE_NAVIGATION_RATIO = 0.8
_SEMANTIC_RATIO = 0.6

# How far from an outline's target page we are willing to look for a unique
# matching block before giving up (outline targets are sometimes off-by-one).
_NEARBY_PAGE_WINDOW = 1

_MAX_HEADING_LEVEL = 6


# --------------------------------------------------------------------------- #
# Text + level helpers
# --------------------------------------------------------------------------- #
def normalize_heading_text(value: object) -> str:
    """NFKC-normalize and strip all whitespace for robust title matching."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text).strip()


def _coerce_level(value: object) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        level = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= level <= _MAX_HEADING_LEVEL:
        return level
    return None


def is_page_navigation_title(title: object) -> bool:
    """Whether an outline title is a bare page-number navigation bookmark.

    Reuses the existing numeric-bookmark detector so page-mapping and heading
    classification agree on what counts as a page-number bookmark.
    """

    return normalize_numeric_bookmark_title(title) is not None


# --------------------------------------------------------------------------- #
# Outline classification
# --------------------------------------------------------------------------- #
def classify_outline_entries(entries: Sequence[Mapping[str, object]]) -> str:
    """Classify a PDF outline as semantic/page_navigation/mixed/none.

    Conservative by design: an outline is only ``semantic`` when real titles
    clearly dominate; a numeric-navigation majority is ``page_navigation``;
    anything ambiguous is ``mixed``; an empty outline is ``none``.
    """

    usable = [
        entry
        for entry in entries
        if isinstance(entry, Mapping) and str(entry.get("title") or "").strip()
    ]
    total = len(usable)
    if total == 0:
        return "none"
    numeric = sum(1 for entry in usable if is_page_navigation_title(entry.get("title")))
    semantic = total - numeric
    numeric_ratio = numeric / total
    semantic_ratio = semantic / total
    if numeric_ratio >= _PAGE_NAVIGATION_RATIO:
        return "page_navigation"
    if semantic_ratio >= _SEMANTIC_RATIO and semantic >= _MIN_SEMANTIC_ENTRIES:
        return "semantic"
    return "mixed"


def read_pdf_outline(path: Path) -> Dict[str, object]:
    """Read the raw PDF outline (bookmarks) and classify it.

    Returns ``{"classification": str, "entries": [{level, title, pdf_page}]}``.
    Never modifies the PDF.  Falls back to ``none`` when PyMuPDF is unavailable
    or the file has no usable outline.
    """

    empty: Dict[str, object] = {"classification": "none", "entries": []}
    try:
        import fitz  # type: ignore
    except Exception:
        return empty
    try:
        document = fitz.open(str(path))
    except Exception:
        return empty
    entries: List[Dict[str, object]] = []
    try:
        page_count = len(document)
        toc = document.get_toc(simple=True) or []
        for row in toc:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            try:
                level = int(row[0])
                target_1based = int(row[2])
            except (TypeError, ValueError):
                continue
            title = str(row[1] or "").strip()
            if not title:
                continue
            if target_1based < 1 or target_1based > page_count:
                continue
            entries.append(
                {"level": level, "title": title, "pdf_page": target_1based}
            )
    finally:
        document.close()
    return {"classification": classify_outline_entries(entries), "entries": entries}


# --------------------------------------------------------------------------- #
# Block indexing
# --------------------------------------------------------------------------- #
def _pages_by_index(
    pages: Sequence[Mapping[str, object]]
) -> Dict[int, List[dict]]:
    index: Dict[int, List[dict]] = {}
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        try:
            page_index = int(page.get("pdf_page_index"))
        except (TypeError, ValueError):
            continue
        blocks = page.get("blocks")
        index[page_index] = [b for b in blocks if isinstance(b, dict)] if isinstance(
            blocks, (list, tuple)
        ) else []
    return index


def _bbox_equal(a: object, b: object, tol: float = 2.0) -> bool:
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
        return False
    if len(a) < 4 or len(b) < 4:
        return False
    try:
        return all(abs(float(a[i]) - float(b[i])) <= tol for i in range(4))
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Semantic PDF outline -> block mapping
# --------------------------------------------------------------------------- #
def map_semantic_outline_to_blocks(
    outline: Mapping[str, object],
    pages: Sequence[Mapping[str, object]],
) -> Tuple[List[Dict[str, object]], List[str]]:
    """Map semantic outline entries to unique DB blocks.

    Only runs for ``classification == "semantic"``.  Matching order:
    target page + normalized title, then a small nearby-page window.  Numeric
    navigation entries are skipped.  Ambiguous or unmatched entries are not
    guessed; they are returned as diagnostics.

    Returns ``(assignments, diagnostics)`` where each assignment is
    ``{"pdf_page_index": int, "block_index": int, "level": int}`` (block_index
    is the position within that page's ``blocks`` list).
    """

    assignments: List[Dict[str, object]] = []
    diagnostics: List[str] = []
    if str(outline.get("classification") or "") != "semantic":
        return assignments, diagnostics
    by_index = _pages_by_index(pages)

    def unique_match(page_index: int, ntitle: str) -> Optional[int]:
        blocks = by_index.get(page_index) or []
        hits = [
            i
            for i, block in enumerate(blocks)
            if normalize_heading_text(block.get("text")) == ntitle
        ]
        return hits[0] if len(hits) == 1 else None

    for entry in outline.get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        title = str(entry.get("title") or "").strip()
        if not title or is_page_navigation_title(title):
            continue
        level = _coerce_level(entry.get("level"))
        if level is None:
            diagnostics.append(f"outline entry has no usable level: {title!r}")
            continue
        try:
            target_index = int(entry.get("pdf_page")) - 1
        except (TypeError, ValueError):
            diagnostics.append(f"outline entry has no target page: {title!r}")
            continue
        ntitle = normalize_heading_text(title)
        block_index = unique_match(target_index, ntitle)
        matched_page = target_index
        if block_index is None:
            # Small nearby-page window for off-by-one targets; require the match
            # to be unique across the whole window.
            window_hits: List[Tuple[int, int]] = []
            for delta in range(-_NEARBY_PAGE_WINDOW, _NEARBY_PAGE_WINDOW + 1):
                if delta == 0:
                    continue
                candidate = unique_match(target_index + delta, ntitle)
                if candidate is not None:
                    window_hits.append((target_index + delta, candidate))
            if len(window_hits) == 1:
                matched_page, block_index = window_hits[0]
        if block_index is None:
            diagnostics.append(
                f"outline entry not uniquely mapped (page {target_index}): {title!r}"
            )
            continue
        assignments.append(
            {
                "pdf_page_index": matched_page,
                "block_index": block_index,
                "level": level,
            }
        )
    return assignments, diagnostics


# --------------------------------------------------------------------------- #
# MinerU content_list_v2 -> block mapping
# --------------------------------------------------------------------------- #
def iter_v2_title_nodes(v2: Sequence[object]):
    """Yield ``(local_page_idx, level, text, bbox)`` for every v2 title node."""

    for page_idx, page in enumerate(v2):
        nodes = page if isinstance(page, list) else [page]
        for node in nodes:
            if not isinstance(node, Mapping) or node.get("type") != "title":
                continue
            content = node.get("content")
            level = None
            text = ""
            if isinstance(content, Mapping):
                level = content.get("level")
                parts = content.get("title_content")
                if isinstance(parts, (list, tuple)):
                    text = "".join(
                        str(part.get("content", ""))
                        for part in parts
                        if isinstance(part, Mapping)
                    )
            yield page_idx, level, text, node.get("bbox")


def map_v2_titles_to_blocks(
    v2: Sequence[object],
    pages: Sequence[Mapping[str, object]],
    *,
    page_index_offset: int = 0,
) -> Tuple[List[Dict[str, object]], List[str]]:
    """Map MinerU v2 title nodes to DB blocks by page + text (+ bbox).

    Uses the verified ``page_idx + normalized text + bbox`` strategy.  Returns
    ``(assignments, diagnostics)`` with the same assignment shape as
    :func:`map_semantic_outline_to_blocks`.
    """

    assignments: List[Dict[str, object]] = []
    diagnostics: List[str] = []
    by_index = _pages_by_index(pages)
    for local_idx, raw_level, text, bbox in iter_v2_title_nodes(v2):
        level = _coerce_level(raw_level)
        if level is None:
            continue
        ntitle = normalize_heading_text(text)
        if not ntitle:
            continue
        global_index = local_idx + page_index_offset
        blocks = by_index.get(global_index) or []
        text_hits = [
            i
            for i, block in enumerate(blocks)
            if normalize_heading_text(block.get("text")) == ntitle
        ]
        block_index: Optional[int]
        if len(text_hits) == 1:
            block_index = text_hits[0]
        elif len(text_hits) > 1:
            bbox_hits = [i for i in text_hits if _bbox_equal(blocks[i].get("bbox"), bbox)]
            block_index = bbox_hits[0] if len(bbox_hits) == 1 else None
        else:
            block_index = None
        if block_index is None:
            diagnostics.append(
                f"v2 title not uniquely mapped (page {global_index}): {text[:40]!r}"
            )
            continue
        assignments.append(
            {
                "pdf_page_index": global_index,
                "block_index": block_index,
                "level": level,
            }
        )
    return assignments, diagnostics


# --------------------------------------------------------------------------- #
# Applying assignments (priority-aware, additive only)
# --------------------------------------------------------------------------- #
def apply_heading_assignments(
    pages: Sequence[Mapping[str, object]],
    assignments: Sequence[Mapping[str, object]],
    source: str,
) -> int:
    """Write ``document_heading_level``/``document_heading_source`` onto blocks.

    Never overwrites a heading already provided by a higher-priority source.
    Only the two new keys are added; ``text``/``text_level``/``bbox`` are left
    untouched.  Returns the number of blocks actually written.
    """

    by_index = _pages_by_index(pages)
    new_rank = _SOURCE_RANK.get(source, 0)
    written = 0
    for assignment in assignments:
        try:
            page_index = int(assignment.get("pdf_page_index"))
            block_index = int(assignment.get("block_index"))
        except (TypeError, ValueError):
            continue
        level = _coerce_level(assignment.get("level"))
        if level is None:
            continue
        blocks = by_index.get(page_index) or []
        if not (0 <= block_index < len(blocks)):
            continue
        block = blocks[block_index]
        existing = block.get("document_heading_source")
        if existing and _SOURCE_RANK.get(str(existing), 0) >= new_rank:
            continue
        block["document_heading_level"] = level
        block["document_heading_source"] = source
        if assignment.get("printed_page") is not None:
            block["document_heading_printed_page"] = str(assignment["printed_page"])
        if assignment.get("title"):
            block["document_heading_title"] = str(assignment["title"])
        written += 1
    return written


# --------------------------------------------------------------------------- #
# v2 discovery
# --------------------------------------------------------------------------- #
def find_content_list_v2(
    result_dir: Optional[Path],
    *,
    root: Optional[Path] = None,
    document_job_id: Optional[str] = None,
) -> Optional[Path]:
    """Locate a MinerU ``content_list_v2.json`` for a segment or a job.

    Rich pipeline keeps it beside ``content_list.json`` in ``result_dir``; the
    engine pipeline keeps it under
    ``corpus/processed/parser_jobs/<job>/provider/**``.
    """

    if result_dir is not None:
        result_dir = Path(result_dir)
        direct = sorted(result_dir.glob("*_content_list_v2.json"))
        if direct:
            return direct[0]
        plain = result_dir / "content_list_v2.json"
        if plain.exists():
            return plain
    if root is not None and document_job_id:
        base = (
            Path(root)
            / "corpus"
            / "processed"
            / "parser_jobs"
            / str(document_job_id)
            / "provider"
        )
        matches = sorted(glob.glob(str(base / "**" / "*_content_list_v2.json"), recursive=True))
        if matches:
            return Path(matches[0])
    return None


def _load_v2(path: Path) -> Optional[List[object]]:
    try:
        import json

        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


# --------------------------------------------------------------------------- #
# Document TOC hierarchy
# --------------------------------------------------------------------------- #
def _strip_trailing_page_number(line: str) -> str:
    return _TOC_TRAILING_PAGE_RE.sub("", str(line or "")).strip()


def _strip_chapter_prefix(normalized: str) -> str:
    return _CHAPTER_PREFIX_RE.sub("", normalized).strip()


def _is_index_letter(normalized: str) -> bool:
    """A single-character index group heading (A/B/C…, incl. OCR digit/garble)."""

    return bool(re.fullmatch(r"[A-Za-z0-9|]", normalized))


def _page_width(pages: Sequence[Mapping[str, object]], page_index: int) -> float:
    for page in pages:
        if isinstance(page, Mapping):
            try:
                if int(page.get("pdf_page_index")) == page_index:
                    return float(page.get("page_width") or 0.0)
            except (TypeError, ValueError):
                continue
    return 0.0


def locate_toc_page(outline: Mapping[str, object]) -> Optional[int]:
    """Return the 0-based page index of a table-of-contents entry, if any.

    Uses non-numeric frontmatter bookmarks (e.g. "目录"/"Contents") even when the
    outline as a whole is classified ``page_navigation``.  The locator bookmark
    itself is never treated as a chapter heading.
    """

    for entry in outline.get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        if normalize_heading_text(entry.get("title")).lower() in _TOC_LOCATOR_TITLES:
            try:
                return int(entry.get("pdf_page")) - 1
            except (TypeError, ValueError):
                return None
    return None


def extract_toc_candidates(
    pages: Sequence[Mapping[str, object]], toc_page_index: int
) -> List[Dict[str, object]]:
    """Split a TOC page's blocks into per-line candidates with an x0 hint.

    TOC lines are frequently merged by MinerU into a single paragraph block; we
    split on newlines and carry the block's x0 so a single merged block yields
    one indentation cluster (a flat chapter list), while genuinely separate
    per-line blocks keep their own x0 for clustering.
    """

    by_index = _pages_by_index(pages)
    candidates: List[Dict[str, object]] = []
    page_index = toc_page_index
    while page_index in by_index:
        page_candidates = []
        for order, block in enumerate(by_index[page_index]):
            if str(block.get("mineru_type") or "") in {"page_number", "footer", "header"}:
                continue
            bbox = block.get("bbox")
            x0 = float(bbox[0]) if isinstance(bbox, (list, tuple)) and bbox else None
            for line in str(block.get("text") or "").split("\n"):
                entry = _strip_trailing_page_number(line)
                ntext = normalize_heading_text(entry)
                if not ntext or ntext.lower() in _TOC_LOCATOR_TITLES:
                    continue
                target = re.search(r"(\d+)\s*$", line)
                page_candidates.append({
                    "text": entry, "norm": ntext, "x0": x0, "reading_order": order,
                    "printed_page": target.group(1) if target else None,
                    "toc_page_index": page_index,
                })
        numbered = sum(c["printed_page"] is not None and len(c["text"]) <= 120 for c in page_candidates)
        if page_index != toc_page_index and (numbered < 2 or numbered < len(page_candidates) * 0.6):
            break
        candidates.extend(page_candidates)
        page_index += 1
    return candidates


def assign_toc_candidate_levels(
    candidates: Sequence[Dict[str, object]], page_width: float
) -> None:
    """Assign a ``level`` to each candidate by clustering left edges (x0).

    Levels only appear when there are multiple stable indentation clusters;
    otherwise every candidate is level 1.  Mutates candidates in place.
    """

    xs = sorted({c["x0"] for c in candidates if c.get("x0") is not None})
    tolerance = max((page_width or 0.0) * _X0_CLUSTER_RATIO, _X0_MIN_TOLERANCE)
    clusters: List[float] = []
    for x in xs:
        if not clusters or (x - clusters[-1]) > tolerance:
            clusters.append(x)

    def level_for(x0: object) -> int:
        if x0 is None or not clusters:
            return 1
        nearest = min(range(len(clusters)), key=lambda i: abs(clusters[i] - float(x0)))
        return nearest + 1

    for candidate in candidates:
        candidate["level"] = level_for(candidate.get("x0"))


def _collect_v2_titles(
    v2_sources: Sequence[Tuple[object, int]]
) -> List[Dict[str, object]]:
    titles: List[Dict[str, object]] = []
    for data, offset in v2_sources:
        for local_idx, raw_level, text, bbox in iter_v2_title_nodes(data):
            ntitle = normalize_heading_text(text)
            if not ntitle:
                continue
            titles.append(
                {
                    "page": local_idx + offset,
                    "norm": ntitle,
                    "text": text,
                    "level": _coerce_level(raw_level),
                    "bbox": bbox,
                }
            )
    return titles


def _locate_block(
    by_index: Mapping[int, List[dict]], page: int, ntitle: str, bbox: object
) -> Optional[int]:
    blocks = by_index.get(page) or []
    hits = [i for i, b in enumerate(blocks) if normalize_heading_text(b.get("text")) == ntitle]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        bbox_hits = [i for i in hits if _bbox_equal(blocks[i].get("bbox"), bbox)]
        if len(bbox_hits) == 1:
            return bbox_hits[0]
    return None


def derive_toc_headings(
    pages: Sequence[Mapping[str, object]],
    v2_titles: Sequence[Mapping[str, object]],
    candidates: Sequence[Mapping[str, object]],
    *,
    toc_page_index: int,
    page_count: int,
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], List[str]]:
    """Turn TOC candidates + body v2 titles into heading assignments.

    Returns ``(toc_assignments, section_assignments, diagnostics)``:

    * ``toc_assignments`` — TOC entries mapped to body v2 titles (chapter level
      from indentation clustering), tagged ``document_toc``.
    * ``section_assignments`` — remaining body v2 titles inside the chapter range
      (excluding book title / TOC page / index letters), tagged ``mineru_v2`` at
      their raw v2 level.
    """

    by_index = _pages_by_index(pages)
    by_printed: Dict[str, list] = {}
    for page in pages:
        printed = page.get("citation_page") or page.get("printed_page")
        if printed is not None:
            by_printed.setdefault(str(printed), []).append(int(page["pdf_page_index"]))
    # Index of body v2 titles by normalized text (unique ones only).
    v2_by_norm: Dict[str, List[Mapping[str, object]]] = {}
    for title in v2_titles:
        v2_by_norm.setdefault(str(title["norm"]), []).append(title)

    toc_assignments: List[Dict[str, object]] = []
    diagnostics: List[str] = []
    matched_v2: set = set()  # (page, norm) of v2 titles claimed by the TOC
    body_pages: List[int] = []
    index_start = page_count

    def match_v2(ntext: str) -> Optional[Mapping[str, object]]:
        for key in (ntext, _strip_chapter_prefix(ntext)):
            if key and key != "" and len(v2_by_norm.get(key, [])) == 1:
                return v2_by_norm[key][0]
        return None

    for candidate in candidates:
        ntext = str(candidate.get("norm") or "")
        level = int(candidate.get("level") or 1)
        destinations = by_printed.get(str(candidate.get("printed_page")), [])
        if destinations:
            # Verify the actual printed destination before trusting a unique v2
            # title elsewhere: that title may be a mislabeled running header.
            hits = [(p, i) for p in destinations for i, b in enumerate(by_index[p])
                    if normalize_heading_text(b.get("text")) in {ntext, _strip_chapter_prefix(ntext)}]
            if len(hits) == 1:
                page, block_index = hits[0]
                toc_assignments.append({"pdf_page_index": page, "block_index": block_index,
                                        "level": level, "printed_page": candidate["printed_page"],
                                        "title": candidate["text"]})
                matched_v2.add((page, normalize_heading_text(by_index[page][block_index].get("text"))))
                body_pages.append(page)
            else:
                diagnostics.append(f"toc destination not uniquely matched: {candidate.get('text')!r}")
            continue
        title = match_v2(ntext)
        if title is None:
            diagnostics.append(f"toc entry not matched to a body title: {candidate.get('text')!r}")
            continue
        block_index = _locate_block(by_index, int(title["page"]), str(title["norm"]), title.get("bbox"))
        if block_index is None:
            diagnostics.append(f"toc entry title not located in body block: {candidate.get('text')!r}")
            continue
        toc_assignments.append(
            {"pdf_page_index": int(title["page"]), "block_index": block_index, "level": level,
             "title": candidate["text"]}
        )
        matched_v2.add((int(title["page"]), str(title["norm"])))
        body_pages.append(int(title["page"]))
        if ntext.lower() in _INDEX_TITLES:
            index_start = min(index_start, int(title["page"]))

    body_start = min(body_pages) if body_pages else toc_page_index + 1
    cover_page = min(by_index) if by_index else 0

    section_assignments: List[Dict[str, object]] = []
    for title in v2_titles:
        page = int(title["page"])
        norm = str(title["norm"])
        key = (page, norm)
        if key in matched_v2:
            continue  # already a chapter from the TOC
        # Exclude only specific navigation/decoration items -- never a blanket
        # "before the first chapter" range, so legitimate pre-chapter sections
        # (e.g. 序言/前言/导言) are allowed into the outline.
        if (
            norm.lower() in _TOC_LOCATOR_TITLES  # 目录 / Contents itself
            or page == cover_page  # document / book title on the cover
            or page >= index_start  # index section (A/B/C... groups)
            or _is_index_letter(norm)  # stray single-letter index groups
        ):
            continue
        # Pre-chapter sections are top-level (level 1); in-body sections keep
        # their raw v2 level.
        level = 1 if page < body_start else title.get("level")
        if not isinstance(level, int):
            continue
        block_index = _locate_block(by_index, page, norm, title.get("bbox"))
        if block_index is None:
            continue
        section_assignments.append(
            {"pdf_page_index": page, "block_index": block_index, "level": level}
        )
    return toc_assignments, section_assignments, diagnostics


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def enrich_pdf_headings(
    pages: Sequence[Mapping[str, object]],
    pdf_path: Optional[Path],
    segments: Sequence[Mapping[str, object]],
    *,
    root: Optional[Path] = None,
) -> Dict[str, object]:
    """Read PDF outline + document TOC + MinerU v2, and enrich heading metadata.

    Returns the outline metadata dict (for ``source_files.payload_json``).
    Priority: semantic PDF outline > document TOC hierarchy > MinerU v2 raw >
    MinerU text_level.  The parser's raw ``text_level`` and v2 ``level`` are
    never rewritten; only the additive ``document_heading_*`` fields are set.
    """

    outline: Dict[str, object] = {"classification": "none", "entries": []}
    if pdf_path is not None:
        try:
            outline = read_pdf_outline(Path(pdf_path))
        except Exception:  # pragma: no cover - defensive; never fail an import
            logger.exception("failed to read PDF outline for headings")
            outline = {"classification": "none", "entries": []}

    # Load MinerU v2 (per-segment in result_dir, else whole-doc under parser_jobs).
    v2_sources: List[Tuple[object, int]] = []
    document_job_id: Optional[str] = None
    for segment in segments or []:
        if not isinstance(segment, Mapping):
            continue
        document_job_id = document_job_id or (
            str(segment.get("document_job_id")) if segment.get("document_job_id") else None
        )
        result_dir = segment.get("result_dir")
        try:
            offset = int(segment.get("page_index_offset"))
        except (TypeError, ValueError):
            offset = 0
        found = find_content_list_v2(Path(result_dir)) if result_dir else None
        if found is not None:
            data = _load_v2(found)
            if data is not None:
                v2_sources.append((data, offset))
    if not v2_sources and root is not None and document_job_id:
        whole = find_content_list_v2(None, root=root, document_job_id=document_job_id)
        if whole is not None:
            data = _load_v2(whole)
            if data is not None:
                v2_sources.append((data, 0))

    # Refresh derived assignments when evidence is available. Keep existing
    # metadata when both original PDF and parser caches are unavailable offline.
    if outline.get("classification") != "none" or v2_sources:
        for page in pages:
            for block in page.get("blocks") or []:
                if isinstance(block, dict) and block.get("document_heading_source") in _SOURCE_RANK:
                    for key in ("document_heading_level", "document_heading_source", "document_heading_printed_page", "document_heading_title"):
                        block.pop(key, None)

    page_count = 1 + max(
        (int(p.get("pdf_page_index")) for p in pages if isinstance(p, Mapping)
         and p.get("pdf_page_index") is not None),
        default=-1,
    )
    toc_page_index = locate_toc_page(outline)

    if outline.get("classification") == "semantic":
        # Highest priority; a document TOC must not override a semantic outline.
        assignments, diagnostics = map_semantic_outline_to_blocks(outline, pages)
        apply_heading_assignments(pages, assignments, HEADING_SOURCE_PDF_OUTLINE)
        for message in diagnostics:
            logger.warning("pdf_outline heading: %s", message)
        _apply_all_v2(pages, v2_sources)
    elif toc_page_index is not None:
        candidates = extract_toc_candidates(pages, toc_page_index)
        assign_toc_candidate_levels(candidates, _page_width(pages, toc_page_index))
        v2_titles = _collect_v2_titles(v2_sources)
        toc_assign, section_assign, diagnostics = derive_toc_headings(
            pages,
            v2_titles,
            candidates,
            toc_page_index=toc_page_index,
            page_count=page_count,
        )
        if toc_assign:
            apply_heading_assignments(pages, toc_assign, HEADING_SOURCE_DOCUMENT_TOC)
            apply_heading_assignments(pages, section_assign, HEADING_SOURCE_MINERU_V2)
            for message in diagnostics:
                logger.warning("document_toc heading: %s", message)
        else:
            # No usable TOC hierarchy: fall back to raw v2 (do not guess).
            _apply_all_v2(pages, v2_sources)
    else:
        _apply_all_v2(pages, v2_sources)

    return outline


def _apply_all_v2(
    pages: Sequence[Mapping[str, object]],
    v2_sources: Sequence[Tuple[object, int]],
) -> None:
    """Fallback: tag every mapped v2 title with its raw v2 level."""

    for data, offset in v2_sources:
        assignments, diagnostics = map_v2_titles_to_blocks(
            data, pages, page_index_offset=offset
        )
        apply_heading_assignments(pages, assignments, HEADING_SOURCE_MINERU_V2)
        for message in diagnostics:
            logger.warning("mineru_v2 heading: %s", message)

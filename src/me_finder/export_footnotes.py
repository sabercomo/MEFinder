"""Conservative, export-only recovery of page-local footnote relationships.

Only explicit circled/bracketed markers and independently identified note blocks
are paired. No neighbouring-page, plain-digit or OCR-error guessing. The output
is a shared stream of page markers, text blocks and chapter-end notes; source
pages are never changed. Unknown chapter boundaries use continuous numbering.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Mapping, Optional, Sequence

from .export_page_reconstruction import SourceFragment, reconstruct_export_pages

from .markdown_export_normalize import (
    ExportBlock,
    ExportOptions,
    FOOTNOTE_MARKER_RE,
    FOOTNOTE_ROLES,
    PageArtifactProfile,
    PageMarker,
    _block_role,
    _block_y_center,
    _page_scale,
    _physical_page,
    _printed_page_raw,
    build_page_artifact_profile,
    heading_level,
    prepare_export_structure,
    resolve_page_marker,
)

logger = logging.getLogger(__name__)

_NOTE_START = re.compile(r"^(?:" + FOOTNOTE_MARKER_RE.pattern + r")\s*", re.MULTILINE)
_MATH = re.compile(r"(?<!\\)\$(?:\\.|[^$])*\$")


@dataclass(frozen=True)
class FootnoteReference:
    start: int
    end: int
    note_id: str
    reference_id: str
    display_number: int


@dataclass(frozen=True)
class FootnoteText(ExportBlock):
    references: tuple[FootnoteReference, ...] = ()
    source_fragment: Optional[SourceFragment] = None


@dataclass(frozen=True)
class Footnote:
    note_id: str
    chapter_id: int
    display_number: int
    text: str
    source_marker: str
    source_physical_page: int
    source_printed_page: Optional[str]
    source_block_index: int
    reference_ids: tuple[str, ...]


@dataclass
class NormalizedDocument:
    items: list[PageMarker | FootnoteText | Footnote]
    footnote_report: dict
    reconstruction_report: dict = field(default_factory=dict)


def _is_unmarked_note(block: Mapping[str, object]) -> bool:
    return _block_role(block) in FOOTNOTE_ROLES and not _NOTE_START.match(
        str(block.get("text") or "").strip()
    )


def normalize_document_export(
    pages: Sequence[Mapping[str, object]],
    *,
    options: Optional[ExportOptions] = None,
    profile: Optional[PageArtifactProfile] = None,
) -> NormalizedDocument:
    """Pair reliable same-page notes, number by first reference, emit at chapter end.

    The model supports multiple backlinks, but raw repeated markers alone do not
    prove a shared target (MinerU may merge paragraphs across pages). Duplicate
    references/definitions, mixed chapter ownership, unknown layout and possible
    continuation fragments remain in place and are reported, never silently lost.
    """
    options = options or ExportOptions()
    reconstructed = reconstruct_export_pages(pages)
    pages = reconstructed.pages
    profile = profile or build_page_artifact_profile(pages, options)
    structure = prepare_export_structure(pages, profile=profile, options=options)
    prepared = structure.pages
    events: list[tuple[int, bool, PageMarker | FootnoteText]] = []
    notes_by_chapter: dict[int, list[Footnote]] = defaultdict(list)
    stats: Counter = Counter()
    chapter = 0
    records = []
    unstructured_pages = []

    def unresolved(reason, physical, block_index, marker=""):
        stats[reason] += 1
        logger.warning(
            "Footnote unresolved: reason=%s pdf_page=%s block=%s marker=%s",
            reason, physical, block_index, marker,
        )

    for page_position, (page, blocks) in enumerate(zip(pages, prepared)):
        physical = _physical_page(page)
        width, height = _page_scale(page)
        chapters = []
        for block in blocks:
            chapter = block["_export_scope_id"]
            chapters.append(chapter)

        candidates: dict[str, list[tuple[int, str]]] = defaultdict(list)
        refs: dict[str, list[tuple[int, re.Match]]] = defaultdict(list)
        note_positions = set()
        blocked_notes = {}
        note_records = {}
        ref_records = {}
        def candidate(kind, position, marker, start=None, end=None):
            block = blocks[position]
            record = {
                "kind": kind, "status": "unresolved",
                "reason": "NO_REFERENCE" if kind == "note" else "NO_NOTE_BODY",
                "source_file_id": page.get("source_file_id"),
                "source_page_index": page.get("pdf_page_index"),
                "source_physical_page": physical, "source_printed_page": _printed_page_raw(page),
                "source_block_index": block["_export_index"], "scope_id": chapters[position],
                "marker": marker, "text": str(block["text"]).strip(),
                "source_cross_page": bool(block.get("_export_source_cross_page") or block.get("cross_page")),
                "source_layout_available": bool(block.get("_export_layout_available")),
                "start": start, "end": end,
            }
            fragment = block.get("_export_source_fragment")
            if fragment is not None:
                # Original identities remain usable for search/citation. Logical
                # page ownership and local marker offsets are separate fields.
                source_start = fragment.source_char_start + len(str(block["text"])) - len(str(block["text"]).lstrip())
                record.update(
                    source_fragment=asdict(fragment),
                    source_page_index=fragment.source_page_index,
                    source_physical_page=fragment.source_physical_page,
                    source_printed_page=fragment.source_printed_page,
                    export_page_index=page.get("pdf_page_index"),
                    export_physical_page=physical,
                    export_printed_page=_printed_page_raw(page),
                    source_start=source_start + start if start is not None else None,
                    source_end=source_start + end if end is not None else None,
                )
            records.append(record)
            return record
        for position, block in enumerate(blocks):
            text = str(block["text"]).strip()
            if "_export_index" not in block:
                if FOOTNOTE_MARKER_RE.search(text):
                    unresolved("unstructured_page", physical, None)
                    unstructured_pages.append({"source_file_id": page.get("source_file_id"),
                                               "source_page_index": page.get("pdf_page_index"),
                                               "source_physical_page": physical,
                                               "source_printed_page": _printed_page_raw(page),
                                               "reason": "UNSTRUCTURED_PAGE", "text": text})
                continue
            start = _NOTE_START.match(text)
            tagged = _block_role(block) in FOOTNOTE_ROLES
            if tagged or (start and heading_level(block) is None):
                note_positions.add(position)
                stats["note_blocks"] += 1
                note_records[position] = candidate("note", position, (start.group("sup") or start.group("plain")) if start else None)
                if start:
                    marker = start.group("sup") or start.group("plain")
                    candidates[marker].append((position, text[start.end():]))
                    # A bundled or empty note is not one reconstructible note.
                    if FOOTNOTE_MARKER_RE.search(text[start.end():]) or not text[start.end():].strip():
                        blocked_notes[position] = "UNSUPPORTED_NOTE_BODY" if text[start.end():].strip() else "NO_NOTE_BODY"
                        unresolved("unsupported_note_body", physical, block["_export_index"], marker)
                    y = _block_y_center(block, width, height)
                    if not tagged and (y is None or y < 0.84):
                        blocked_notes.setdefault(position, "UNCONFIRMED_NOTE_LAYOUT")
                        unresolved("unconfirmed_note_layout", physical, block["_export_index"], marker)
                else:
                    unresolved("unmarked_note", physical, block["_export_index"])
                    note_records[position]["reason"] = "UNMARKED_NOTE"
                continue
            math_spans = [(m.start(), m.end()) for m in _MATH.finditer(text)]
            for match in FOOTNOTE_MARKER_RE.finditer(text):
                if match.group("plain") and any(start <= match.start() < end for start, end in math_spans):
                    continue
                # Leading list items are not inline footnote references.
                if not text[:match.start()].rsplit("\n", 1)[-1].strip():
                    continue
                marker = match.group("sup") or match.group("plain")
                refs[marker].append((position, match))
                ref_records[(position, match.start())] = candidate("ref", position, marker, match.start(), match.end())
                stats["refs"] += 1

        # An unnumbered note fragment may continue the preceding note. Withhold
        # relocation of that candidate rather than split the source further.
        previous_note = None
        for position in sorted(note_positions):
            if _is_unmarked_note(blocks[position]) and previous_note is not None:
                blocked_notes.setdefault(previous_note, "POSSIBLE_CROSS_PAGE_CONTINUATION")
                unresolved("possible_continuation", physical, blocks[previous_note]["_export_index"])
            previous_note = position
        if note_positions and page_position + 1 < len(pages):
            next_page = pages[page_position + 1]
            if physical is not None and _physical_page(next_page) == physical + 1:
                next_notes = [b for b in prepared[page_position + 1] if _block_role(b) in FOOTNOTE_ROLES]
                if next_notes and _is_unmarked_note(next_notes[0]):
                    last = max(note_positions)
                    blocked_notes.setdefault(last, "POSSIBLE_CROSS_PAGE_CONTINUATION")
                    unresolved("possible_continuation", physical, blocks[last]["_export_index"])
                if next_notes and not any(
                    _block_role(b) not in FOOTNOTE_ROLES | {"header", "page_header", "footer", "page_footer", "page_number"}
                    for b in prepared[page_position + 1]
                ):
                    # Observed MinerU output: a following page has only notes,
                    # while its prose was merged into the preceding page's block.
                    for position in note_positions:
                        blocked_notes.setdefault(position, "UNCERTAIN_PAGE_FLOW")
                    unresolved("uncertain_page_flow", physical, None)

        matched: list[tuple[str, int, str, list]] = []
        for marker, definitions in candidates.items():
            references = refs.get(marker, [])
            position, body = definitions[0]
            reason = None
            if len(definitions) != 1:
                reason = "ambiguous_definitions"
            elif not references:
                reason = "orphan_note"
            elif physical is None:
                reason = "missing_physical_page"
            elif any(blocks[pos].get("_export_source_cross_page") or blocks[pos].get("cross_page")
                     for pos in [position, *(p for p, _ in references)]):
                reason = "cross_page_source"
            elif position in blocked_notes:
                reason_code = blocked_notes[position]
                note_records[position]["reason"] = reason_code
                for pos, match in references:
                    ref_records[(pos, match.start())]["reason"] = reason_code
                continue
            elif len({chapters[pos] for pos, _ in references}) != 1:
                reason = "ambiguous_chapter"
            elif len(references) != 1:
                reason = "ambiguous_references"
            elif any(pos >= position for pos, _ in references):
                reason = "unconfirmed_reading_order"
            if reason:
                unresolved(reason, physical, blocks[position]["_export_index"], marker)
                reason_code = {
                    "ambiguous_definitions": "DUPLICATE_MARKER_ON_PAGE",
                    "ambiguous_references": "DUPLICATE_MARKER_ON_PAGE",
                    "orphan_note": "NO_REFERENCE", "missing_physical_page": "MISSING_PHYSICAL_PAGE",
                    "ambiguous_chapter": "AMBIGUOUS_CHAPTER_OWNERSHIP",
                    "unconfirmed_reading_order": "UNCONFIRMED_READING_ORDER",
                    "cross_page_source": "CROSS_PAGE_SOURCE_BLOCK",
                }[reason]
                for pos, _ in definitions:
                    note_records[pos]["reason"] = reason_code
                for pos, match in references:
                    ref_records[(pos, match.start())]["reason"] = reason_code
                continue
            matched.append((marker, position, body, references))
        matched.sort(key=lambda item: (item[3][0][0], item[3][0][1].start()))

        block_refs: dict[int, list[FootnoteReference]] = defaultdict(list)
        removed = set()
        matched_markers = set()
        for marker, position, body, references in matched:
            owner = chapters[references[0][0]]
            number = len(notes_by_chapter[owner]) + 1
            source_index = blocks[position]["_export_index"]
            # Source identity, not the new chapter number or note text. Repeated
            # bibliographic content remains distinct; renumbering doesn't shift IDs.
            identity = f"{page.get('source_file_id', '')}:{physical}:{source_index}"
            note_id = "fn-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            reference_ids = []
            note_records[position].update(status="matched", reason="SAME_PAGE_UNIQUE_MARKER",
                                          note_id=note_id, scope_id=owner, display_number=number)
            for ref_position, match in references:
                ref_record = ref_records[(ref_position, match.start())]
                source_start = ref_record.get("source_start", match.start())
                reference_id = f"{note_id}-ref-{blocks[ref_position]['_export_index']}-{source_start}"
                reference_ids.append(reference_id)
                ref_records[(ref_position, match.start())].update(
                    status="matched", reason="SAME_PAGE_UNIQUE_MARKER", note_id=note_id,
                    reference_id=reference_id, display_number=number,
                )
                block_refs[ref_position].append(FootnoteReference(
                    match.start(), match.end(), note_id, reference_id, number,
                ))
            notes_by_chapter[owner].append(Footnote(
                note_id, owner, number, body, marker, physical,
                _printed_page_raw(page), source_index, tuple(reference_ids),
            ))
            removed.add(position)
            matched_markers.add(marker)
            stats["matched_notes"] += 1
            stats["matched_refs"] += len(references)

        for position, reason_code in blocked_notes.items():
            if note_records[position]["status"] == "unresolved":
                note_records[position]["reason"] = reason_code

        for marker, references in refs.items():
            if marker not in matched_markers:
                stats["unresolved_refs"] += len(references)
                if marker not in candidates:
                    unresolved("orphan_ref", physical, blocks[references[0][0]]["_export_index"], marker)
        page_marker = resolve_page_marker(page, options)
        if page_marker is not None:
            events.append((chapter, False, page_marker))
        for position, block in enumerate(blocks):
            if position not in removed:
                events.append((chapters[position], block.get("_export_scope_end_before", False), FootnoteText(
                    str(block["text"]).strip(), heading_level(block),
                    tuple(sorted(block_refs[position], key=lambda ref: ref.start)),
                    block.get("_export_source_fragment"),
                )))

    result: list[PageMarker | FootnoteText | Footnote] = []
    active_chapter = 0
    notes_by_id = {note.note_id: note for notes in notes_by_chapter.values() for note in notes}
    pending_notes: dict[str, Footnote] = {}
    pending_markers: list[PageMarker] = []
    for owner, scope_end_before, item in events:
        if isinstance(item, PageMarker):
            pending_markers.append(item)
            continue
        if owner != active_chapter or scope_end_before:
            # The shared structure closes output at the parent title itself,
            # even if a subtitle or several pages precede the next chapter.
            # Flush only notes whose references have already been emitted.
            result.extend(pending_notes.values())
            pending_notes.clear()
            active_chapter = owner
        result.extend(pending_markers)
        pending_markers.clear()
        result.append(item)
        for ref in item.references:
            pending_notes.setdefault(ref.note_id, notes_by_id[ref.note_id])
    result.extend(pending_notes.values())
    result.extend(pending_markers)
    if stats["matched_notes"] and not chapter:
        logger.warning("Footnote chapter boundaries unavailable; numbering continuously for this document")
    if stats:
        logger.info("Footnote normalization: chapters=%s counts=%s", chapter, dict(stats))
    report = {"schema_version": 1, "candidates": records, "scopes": [],
              "match_reason": {}, "unresolved_reason": {},
              "heading_issues": structure.heading_issues, "unstructured_pages": unstructured_pages,
              "heading_issue_reason": dict(Counter(i["reason"] for i in structure.heading_issues)),
              "candidate_detection_scope": "aligned source blocks; unstructured pages are retained and listed separately",
              "source_provenance_policy": "exact native span reconstruction precedes matching; remaining cross_page blocks are vetoed; absent flags are not proof of PDF alignment",
              "counting_unit": "ref = explicit inline marker; note = candidate source block; reasons counted once per entity"}
    for kind in ("ref", "note"):
        candidates = [r for r in records if r["kind"] == kind]
        matched_records = [r for r in candidates if r["status"] == "matched"]
        unresolved_records = [r for r in candidates if r["status"] == "unresolved"]
        report[f"candidate_{kind}_count"] = len(candidates)
        report[f"matched_{kind}_count"] = len(matched_records)
        report[f"unresolved_{kind}_count"] = len(unresolved_records)
        report["match_reason"][kind] = dict(Counter(r["reason"] for r in matched_records))
        report["unresolved_reason"][kind] = dict(Counter(r["reason"] for r in unresolved_records))
    for scope in structure.scopes:
        scope_id = scope["scope_id"]
        scoped = [r for r in records if r["scope_id"] == scope_id]
        if scope_id == 0 and not scoped and len(structure.scopes) > 1:
            continue
        numbers = [n.display_number for n in notes_by_chapter[scope_id]]
        report["scopes"].append({**scope, "number_range": [min(numbers), max(numbers)] if numbers else None,
                                 **{f"{status}_{kind}_count": sum(r["kind"] == kind and r["status"] == status for r in scoped)
                                    for status in ("matched", "unresolved") for kind in ("ref", "note")}})
    report["numbering_scope_count"] = len(report["scopes"])
    return NormalizedDocument(result, report, reconstructed.report)


def normalize_document_footnotes(pages, *, options=None, profile=None):
    """Compatibility item-stream API; exporters also consume the report result."""
    return normalize_document_export(pages, options=options, profile=profile).items

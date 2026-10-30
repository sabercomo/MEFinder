"""Page reconstruction from exact, pre-merge MinerU span provenance.

The first prototype handles ordinary text split over two evidenced pages.
It does not split notes, media, headings, or blocks with unresolved provenance.
No marker recognition, neighbour-page search, or database writes live here.
Import (``pdf_page_split``) applies it once before indexing; export re-runs it
on private copies for pages indexed before that, skipping blocks already split.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Mapping, Optional

from ..pdf.auto_page_mapping import normalized_page_bbox
from .document import DocumentExportError
from .normalize import (
    block_role, blocks_aligned_with_text, heading_level, page_scale,
    physical_page_number, printed_page_raw,
)


@dataclass(frozen=True)
class SpanOrigin:
    local_page_index: int
    layout_block_index: int
    line_index: int
    span_index: int
    merged_line_index: int
    merged_span_index: int
    bbox: tuple[float, ...]


@dataclass(frozen=True)
class SourceFragment:
    source_block_id: str
    source_page_index: int
    source_physical_page: int
    source_printed_page: Optional[str]
    source_block_index: int
    parser_item_index: Optional[int]
    source_char_start: int
    source_char_end: int
    target_page_index: int
    target_physical_page: int
    target_printed_page: Optional[str]
    bbox_normalized: tuple[float, ...]
    spans: tuple[SpanOrigin, ...]


@dataclass
class ReconstructedPages:
    pages: list
    report: dict


def _span_key(span):
    bbox = span.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or not span.get("content"):
        return None
    return span.get("type"), span["content"], tuple(bbox)


def _span_text(span):
    return span["content"] if span["type"] == "text" else "$" + span["content"] + "$"


def reconstruction_invariant(source_block, logical_blocks) -> dict:
    """Compare actual fragments with the independent source-span snapshot.

    Identities are (merged line, merged span) within this source block. Layout
    provenance chooses ownership; this check cannot create or repair a split.
    """
    source_spans = source_block["_export_source_spans"]
    expected = [(li, si) for li, si, _ in source_spans]
    actual = [(s.merged_line_index, s.merged_span_index)
              for b in logical_blocks for s in b["_export_source_fragment"].spans]
    expected_counts, actual_counts = Counter(expected), Counter(actual)
    source_text = {(li, si): text for li, si, text in source_spans}
    unexpected = sum(count for key, count in actual_counts.items() if key not in expected_counts)
    missing = sum((expected_counts - actual_counts).values())
    duplicated = sum(count - 1 for count in actual_counts.values() if count > 1)
    original = str(source_block["text"])
    rebuilt = "".join(b["text"] for b in logical_blocks)
    normalized_equal = (
        "".join(original.split()) == "".join("".join(text.split()) for _, _, text in source_spans)
        == "".join(rebuilt.split())
    )
    fragment_text_equal = not unexpected and all(
        "".join(b["text"].split()) == "".join(
            "".join(source_text[(s.merged_line_index, s.merged_span_index)].split())
            for s in b["_export_source_fragment"].spans
        ) for b in logical_blocks
    )
    cursor = 0
    ranges_equal = True
    for b in logical_blocks:
        f = b["_export_source_fragment"]
        ranges_equal &= f.source_char_start == cursor and b["text"] == original[f.source_char_start:f.source_char_end]
        cursor = f.source_char_end
    ranges_equal &= cursor == len(original)
    passed = (not missing and not unexpected and not duplicated and expected == actual
              and original == rebuilt and normalized_equal and fragment_text_equal and ranges_equal)
    return {"checked_span_count": len(expected), "missing_span_count": missing,
            "duplicated_span_count": duplicated, "unexpected_span_count": unexpected,
            "span_order_preserved": expected == actual, "normalized_text_preserved": normalized_equal,
            "source_characters_preserved": original == rebuilt,
            "fragment_text_matches_spans": bool(fragment_text_equal),
            "source_ranges_contiguous": bool(ranges_equal),
            "content_order_invariant_failure_count": int(not passed)}


def _fragment_plan(page, block, source_index, merged, native, layout_pages, document_pages):
    if block_role(block) != "text" or merged.get("type") != "text":
        return (), "UNSUPPORTED_BLOCK_TYPE"
    if heading_level(block) is not None:
        return (), "HEADING_BLOCK"
    origins = []
    rendered = []
    for li, line in enumerate(merged.get("lines", [])):
        for si, span in enumerate(line.get("spans", [])):
            if span.get("type") not in {"text", "inline_equation"}:
                return (), "UNSUPPORTED_SPAN_TYPE"
            hits = native.get(_span_key(span), [])
            if len(hits) != 1:
                return (), "NATIVE_SPAN_NOT_UNIQUE"
            owner, order, native_li, native_si = hits[0]
            if (owner != block["local_page_idx"]) != (span.get("cross_page") is True):
                return (), "SOURCE_FLAG_CONFLICT"
            origins.append(SpanOrigin(owner, order, native_li, native_si, li, si, tuple(span["bbox"])))
            rendered.append(_span_text(span))
    owners = [origin.local_page_index for origin in origins]
    distinct = list(dict.fromkeys(owners))
    local = block["local_page_idx"]
    # A paragraph may fill a whole middle page, so any run of consecutive
    # pages starting at the block's own page is accepted.
    if len(distinct) < 2 or distinct != list(range(local, local + len(distinct))):
        return (), "NOT_CONSECUTIVE_EVIDENCED_PAGES"
    if owners != sorted(owners):
        return (), "NON_MONOTONIC_SPAN_ORIGINS"

    # Match every non-whitespace character exactly, including equation syntax.
    # Coordinates/native identities decide the cut; this only maps it back to
    # the original string. Never normalize punctuation, OCR or marker values.
    text = str(block["text"])
    positions = [i for i, char in enumerate(text) if not char.isspace()]
    compact = "".join(text[i] for i in positions)
    lengths = [len("".join(value.split())) for value in rendered]
    if compact != "".join("".join(value.split()) for value in rendered):
        return (), "SOURCE_TEXT_MISMATCH"
    bounds = [owners.index(owner) for owner in distinct] + [len(owners)]
    if any(not sum(lengths[a:b]) for a, b in zip(bounds, bounds[1:])):
        return (), "EMPTY_PAGE_FRAGMENT"
    cuts = [0] + [positions[sum(lengths[:b])] for b in bounds[1:-1]] + [len(text)]
    offset = page["pdf_page_index"] - local
    fragments = []
    for owner, start, end, a, b in zip(distinct, cuts, cuts[1:], bounds, bounds[1:]):
        part = origins[a:b]
        target_index = offset + owner
        target = document_pages.get(target_index)
        if target is None:
            return (), "TARGET_PAGE_UNAVAILABLE"
        orders = {span.layout_block_index for span in part}
        if len(orders) != 1:
            return (), "MULTIPLE_NATIVE_BLOCKS_ON_PAGE"
        native_blocks = [b for b in layout_pages[owner]["preproc_blocks"] if b.get("index") == part[0].layout_block_index]
        if len(native_blocks) != 1:
            return (), "NATIVE_BLOCK_INDEX_NOT_UNIQUE"
        native_block = native_blocks[0]
        native_positions = [(li, si) for li, line in enumerate(native_block.get("lines", []))
                            for si, _ in enumerate(line.get("spans", []))]
        if [(span.line_index, span.span_index) for span in part] != native_positions:
            return (), "INCOMPLETE_NATIVE_BLOCK"
        if owner != distinct[0] and not any(
            b.get("index") == part[0].layout_block_index and b.get("lines_deleted") is True
            and not b.get("lines") for b in layout_pages[owner].get("para_blocks", [])
        ):
            return (), "TARGET_NOT_DELETED_BY_MERGE"
        bbox = normalized_page_bbox(native_block, *layout_pages[owner]["page_size"])
        if bbox is None:
            return (), "MISSING_NATIVE_BLOCK_BBOX"
        fragments.append(SourceFragment(
            str(block.get("source_block_id") or f"{page.get('source_file_id', '')}:p{page['pdf_page_index']}:b{source_index}"),
            page["pdf_page_index"], physical_page_number(page), printed_page_raw(page), source_index,
            block.get("parser_item_index", block.get("mineru_item_index")), start, end,
            target_index, physical_page_number(target), printed_page_raw(target), tuple(bbox), tuple(part),
        ))
    return tuple(fragments), "EXACT_NATIVE_SPAN_ORIGINS"


def attach_export_layout(pages, runtime_root: Path, layout_paths=None) -> None:
    """Enrich freshly-read page copies, never persisted parser records.

    ``layout_paths`` maps a block's ``result_dir`` to its ``layout.json`` when
    the parser kept it elsewhere; the default is ``<result_dir>/layout.json``.
    """
    cache = {}
    document_pages = {page["pdf_page_index"]: page for page in pages}
    for page in pages:
        for source_index, block in enumerate(page.get("blocks") or []):
            directory = block.get("result_dir")
            if not directory or block.get("page_split"):
                continue
            mapped = (layout_paths or {}).get(str(directory))
            directory = Path(directory)
            if not directory.is_absolute():
                directory = runtime_root / directory
            if directory not in cache:
                path = Path(mapped) if mapped else directory / "layout.json"
                layout_pages = {}
                native = defaultdict(list)
                if path.is_file():
                    try:
                        layout = json.loads(path.read_text(encoding="utf-8-sig"))
                    except ValueError as exc:
                        raise DocumentExportError(f"解析器页面来源缓存不是有效 JSON：{path}") from exc
                    layout_pages = {p["page_idx"]: p for p in layout.get("pdf_info", [])}
                    for p in layout_pages.values():
                        for b in p.get("preproc_blocks", []):
                            if not isinstance(b.get("index"), int):
                                continue
                            for li, line in enumerate(b.get("lines", [])):
                                for si, span in enumerate(line.get("spans", [])):
                                    key = _span_key(span)
                                    if key is not None:
                                        native[key].append((p["page_idx"], b["index"], li, si))
                cache[directory] = layout_pages, native
            layout_pages, native = cache[directory]
            local = block.get("local_page_idx")
            block["_export_layout_available"] = local in layout_pages
            if local not in layout_pages:
                continue
            raw_page = layout_pages[local]
            bbox = normalized_page_bbox(block, *page_scale(page))
            if bbox is None:
                continue
            matches = [b for b in raw_page.get("para_blocks", []) + raw_page.get("discarded_blocks", [])
                       if (other := normalized_page_bbox(b, *raw_page["page_size"])) is not None
                       and all(abs(a - c) <= .003 for a, c in zip(bbox, other))]
            if len(matches) == 1 and isinstance(matches[0].get("index"), int):
                block["_export_layout_order"] = matches[0]["index"]
            if not any(s.get("cross_page") is True for b in matches
                       for line in b.get("lines", []) for s in line.get("spans", [])):
                continue
            block["_export_source_cross_page"] = True
            if len(matches) != 1:
                block["_export_reconstruction_reason"] = "LAYOUT_BLOCK_NOT_UNIQUE"
                continue
            fragments, reason = _fragment_plan(page, block, source_index, matches[0], native, layout_pages, document_pages)
            block["_export_fragments"] = fragments
            block["_export_reconstruction_reason"] = reason
            if fragments:
                block["_export_source_spans"] = tuple(
                    (li, si, _span_text(span)) for li, line in enumerate(matches[0]["lines"])
                    for si, span in enumerate(line["spans"])
                )


def reconstruct_export_pages(pages) -> ReconstructedPages:
    """Apply evidenced fragments before cleanup/scopes/matching on private copies."""
    result = [{**p, "blocks": [{**b, "_export_index": i} if isinstance(b, Mapping) else b
                              for i, b in enumerate(p.get("blocks") or [])]}
              for p in pages]
    by_index = {p["pdf_page_index"]: p for p in result if p.get("pdf_page_index") is not None}
    aligned = {p["pdf_page_index"]: blocks_aligned_with_text(p.get("blocks") or [], str(p.get("text_raw") or ""))
               for p in pages if p.get("pdf_page_index") is not None}
    records = []
    reconstructed_sources = []
    changed = set()
    for page in pages:
        for source_index, block in enumerate(page.get("blocks") or []):
            if not isinstance(block, Mapping):
                continue
            if not (block.get("_export_source_cross_page") or block.get("cross_page")):
                continue
            record = {"source_page_index": page.get("pdf_page_index"), "source_physical_page": physical_page_number(page),
                      "source_printed_page": printed_page_raw(page), "source_block_index": source_index,
                      "status": "retained", "reason": block.get("_export_reconstruction_reason", "NO_SPAN_EVIDENCE")}
            records.append(record)
            fragments = block.get("_export_fragments")
            if not fragments:
                continue
            first, incoming = fragments[0], fragments[1:]
            if not all(aligned.get(f.target_page_index) for f in fragments):
                record["reason"] = "SOURCE_OR_TARGET_TEXT_UNALIGNED"
                continue
            placements = []
            for fragment in incoming:
                target = by_index[fragment.target_page_index]
                # Preserve existing body/media/note order. Page decorations may be
                # appended by the parser; trusted-heading ordering handles them later.
                anchors = [b for b in target["blocks"] if block_role(b) not in {
                    "header", "page_header", "footer", "page_footer", "page_number",
                }]
                orders = [b.get("_export_layout_order") for b in anchors]
                order = fragment.spans[0].layout_block_index
                if any(o is None for o in orders) or orders != sorted(set(orders)) or order in orders:
                    break
                placements.append((target, anchors, order))
            if len(placements) != len(incoming):
                record["reason"] = "TARGET_BLOCK_ORDER_UNPROVEN"
                continue
            originals = by_index[first.target_page_index]["blocks"]
            original = next(b for b in originals if b["_export_index"] == source_index and "_export_source_fragment" not in b)
            logical = []
            for fragment in fragments:
                piece = {**original, "text": block["text"][fragment.source_char_start:fragment.source_char_end],
                         "bbox_normalized": list(fragment.bbox_normalized), "_export_source_fragment": fragment,
                         "_export_layout_order": fragment.spans[0].layout_block_index,
                         "_export_source_cross_page": False}
                piece.pop("cross_page", None)
                piece.pop("_export_fragments", None)
                piece.pop("_export_source_spans", None)
                logical.append(piece)
            originals[originals.index(original)] = logical[0]
            for (target, anchors, order), piece in zip(placements, logical[1:]):
                after = next((b for b in anchors if b["_export_layout_order"] > order), None)
                if after is not None:
                    insertion = target["blocks"].index(after)
                else:
                    # Native reading order places this after all existing body
                    # anchors, but before any evidenced later page decoration.
                    after = next((b for b in target["blocks"] if b.get("_export_layout_order", -1) > order), None)
                    insertion = target["blocks"].index(after) if after is not None else len(target["blocks"])
                target["blocks"].insert(insertion, piece)
            changed.update(f.target_page_index for f in fragments)
            record.update(status="reconstructed", reason="EXACT_NATIVE_SPAN_ORIGINS",
                          fragments=[asdict(f) for f in fragments])
            reconstructed_sources.append((record, block))
    # Audit the actual page/block stream, not just the partition plan. This
    # catches omission, duplication and reordering during fragment insertion.
    actual_fragments = defaultdict(list)
    for page in result:
        for block in page["blocks"]:
            if isinstance(block, Mapping) and (fragment := block.get("_export_source_fragment")) is not None:
                actual_fragments[(fragment.source_page_index, fragment.source_block_index)].append(block)
    invariant_totals = Counter()
    for record, block in reconstructed_sources:
        check = reconstruction_invariant(block, actual_fragments[(record["source_page_index"], record["source_block_index"])])
        record["content_invariant"] = check
        invariant_totals.update({k: v for k, v in check.items() if k.endswith("_count")})
        if check["content_order_invariant_failure_count"]:
            raise DocumentExportError(
                f"页面重建内容守恒失败：page_index={record['source_page_index']} "
                f"block_index={record['source_block_index']} {json.dumps(check)}"
            )
    for index in changed:
        page = by_index[index]
        page["text_raw"] = "\n".join(str(b.get("text") or "").strip() for b in page["blocks"] if str(b.get("text") or "").strip())
    return ReconstructedPages(result, {
        "schema_version": 1, "policy": "consecutive-page ordinary text; exact preproc span identities and deleted target blocks",
        "cross_page_block_count": len(records),
        "reconstructed_block_count": sum(r["status"] == "reconstructed" for r in records),
        "retained_block_count": sum(r["status"] == "retained" for r in records),
        "content_invariant": {"checked_block_count": len(reconstructed_sources), **{
            key: invariant_totals[key] for key in ("checked_span_count", "missing_span_count",
                "duplicated_span_count", "unexpected_span_count", "content_order_invariant_failure_count")}},
        "reason_counts": dict(Counter(r["reason"] for r in records)), "blocks": records,
    })

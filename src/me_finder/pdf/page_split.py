"""Return MinerU's cross-page paragraphs to the pages they are printed on.

MinerU joins a paragraph that runs over a page break into one block on the
earlier page, so the next page's opening lines would carry the earlier page's
citation number.  When the parser's pre-merge ``layout.json`` proves exactly
where the break falls, the block is cut there and the continuation moves to
the page it is printed on.  Blocks without that proof stay where MinerU put
them and are counted in the report, never guessed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set

from ..export.document import DocumentExportError
from ..export.page_reconstruction import attach_export_layout, reconstruct_export_pages
from ..export.normalize import page_scale
from ..persistence.parser_job_reads import read_slice_job_id

SPLIT_METHOD = "mineru_layout_spans"
_SLICE_DIR_RE = re.compile(r"pages-(\d+)-(\d+)")


def mineru_layout_paths(
    segments: Sequence[Mapping[str, object]], root: Optional[Path]
) -> Dict[str, Path]:
    """Map each segment ``result_dir`` to the ``layout.json`` that produced it.

    Older cloud results keep ``layout.json`` beside ``content_list.json``.  The
    slice engine keeps the provider's raw files under
    ``corpus/processed/parser_jobs/<job>/provider/<slice>``.
    """

    paths: Dict[str, Path] = {}
    for segment in segments:
        result_dir = str(segment.get("result_dir") or "").strip()
        if not result_dir:
            continue
        direct = Path(result_dir) / "layout.json"
        if direct.is_file():
            paths[result_dir] = direct
            continue
        provider = _provider_layout_path(segment, root)
        if provider is not None:
            paths[result_dir] = provider
    return paths


def _provider_layout_path(
    segment: Mapping[str, object], root: Optional[Path]
) -> Optional[Path]:
    job_id = str(segment.get("document_job_id") or "").strip()
    if root is None or not job_id:
        return None
    slice_id = str(segment.get("parser_slice_id") or "").strip() or _ledger_slice_id(
        Path(root), job_id, segment
    )
    if not slice_id:
        return None
    path = (
        Path(root) / "corpus" / "processed" / "parser_jobs" / job_id
        / "provider" / slice_id / "layout.json"
    )
    return path if path.is_file() else None


def _ledger_slice_id(
    root: Path, job_id: str, segment: Mapping[str, object]
) -> Optional[str]:
    """Read the slice id for manifests written before they recorded it."""

    try:
        start = int(segment["page_start"])
        end = int(segment["page_end"])
    except (KeyError, TypeError, ValueError):
        match = _SLICE_DIR_RE.fullmatch(Path(str(segment.get("result_dir") or "")).name)
        if match is None:
            return None
        start, end = int(match.group(1)), int(match.group(2))
    return read_slice_job_id(root / "data" / "parser_jobs.sqlite3", job_id, start, end)


def split_parser_merged_pages(
    pages: List[Dict[str, object]],
    layout_paths: Mapping[str, Path],
    checked_pages: Optional[Set[int]] = None,
) -> tuple[Dict[str, object], Set[int]]:
    """Move evidenced continuations to their printed page, in place.

    Returns the report and the ``pdf_page_index`` values whose blocks and
    ``text_raw`` changed; callers recompute offsets and hashes for those.
    Pages whose parser output was checked get ``page_text_boundary``:
    ``layout_checked`` (text is what the page prints) or ``parser_merged``
    (a merge could not be proven, so lines of the next page may remain).
    ``checked_pages`` lists every page index the layouts cover, so blank or
    image-only pages count as checked too; without it a page counts when one
    of its blocks comes from a covered result directory.
    """

    if not layout_paths:
        return {"status": "no_layout", "merged_block_count": 0,
                "split_block_count": 0, "unsplit_block_count": 0}, set()
    attach_export_layout(pages, Path("."), layout_paths=layout_paths)
    try:
        rebuilt = reconstruct_export_pages(pages)
    except DocumentExportError as exc:
        for page in pages:
            page["blocks"] = [_public_block(block) for block in page.get("blocks") or []]
        return {"status": "failed", "error": str(exc), "merged_block_count": 0,
                "split_block_count": 0, "unsplit_block_count": 0}, set()
    report = rebuilt.report
    changed: Set[int] = set()
    by_index = {page["pdf_page_index"]: page for page in rebuilt.pages}
    unsplit_pages = {
        record["source_page_index"] for record in report["blocks"]
        if record["status"] == "retained"
    }
    for page in pages:
        source = by_index[page["pdf_page_index"]]
        if page["pdf_page_index"] in unsplit_pages:
            page["page_text_boundary"] = "parser_merged"
        elif page["pdf_page_index"] in (checked_pages or ()) or any(
            str(block.get("result_dir")) in layout_paths for block in page.get("blocks") or []
        ):
            page["page_text_boundary"] = "layout_checked"
        blocks = []
        for block in source["blocks"]:
            fragment = block.get("_export_source_fragment")
            public = _public_block(block)
            if fragment is not None:
                changed.add(int(page["pdf_page_index"]))
                _mark_fragment(public, fragment, page)
            blocks.append(public)
        for index, block in enumerate(blocks):
            block["block_index"] = index
        page["blocks"] = blocks
        if int(page["pdf_page_index"]) in changed:
            page["text_raw"] = source["text_raw"]
    reasons = {
        reason: count
        for reason, count in report["reason_counts"].items()
        if reason != "EXACT_NATIVE_SPAN_ORIGINS"
    }
    return {
        "status": "split",
        "method": SPLIT_METHOD,
        "merged_block_count": report["cross_page_block_count"],
        "split_block_count": report["reconstructed_block_count"],
        "unsplit_block_count": report["retained_block_count"],
        "unsplit_reasons": reasons,
    }, changed


def _public_block(block: Mapping[str, object]) -> Dict[str, object]:
    return {key: value for key, value in block.items() if not str(key).startswith("_export")}


def _mark_fragment(block: Dict[str, object], fragment, page: Mapping[str, object]) -> None:
    moved = fragment.target_page_index != fragment.source_page_index
    width, height = page_scale(page)
    normalized = [float(value) for value in fragment.bbox_normalized]
    block["bbox_normalized"] = normalized
    block["bbox"] = [
        round(value * scale, 2)
        for value, scale in zip(normalized, (width, height, width, height))
    ]
    block["pdf_page_index"] = fragment.target_page_index
    block["local_page_idx"] = fragment.spans[0].local_page_index
    for key in ("page_char_start", "page_char_end", "offset_unit"):
        block.pop(key, None)
    block["page_split"] = {
        "method": SPLIT_METHOD,
        "part": "continuation" if moved else "head",
        "source_pdf_page_index": fragment.source_page_index,
    }

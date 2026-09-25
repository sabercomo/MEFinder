"""Read and locate persisted two-document alignments.

Generation and segmentation names remain available here for existing callers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from .alignment_generation import (
    SEGMENTER as SEGMENTER,
    SEGMENTER_VERSION as SEGMENTER_VERSION,
    ALIGNMENT_ALGORITHM as ALIGNMENT_ALGORITHM,
    ALIGNMENT_ALGORITHM_VERSION as ALIGNMENT_ALGORITHM_VERSION,
    READABLE_ALIGNMENT_VERSIONS as READABLE_ALIGNMENT_VERSIONS,
    RESTORABLE_ALIGNMENT_VERSIONS as RESTORABLE_ALIGNMENT_VERSIONS,
    AlignmentNotFound as AlignmentNotFound,
    InvalidAlignmentRequest as InvalidAlignmentRequest,
    TextAlignmentError as TextAlignmentError,
    WriteWindow as WriteWindow,
    AlignmentPreparation as AlignmentPreparation,
    _now as _now,
    _json_object as _json_object,
    _validate_source_id as _validate_source_id,
    _validate_nonnegative_integer as _validate_nonnegative_integer,
    _source_row as _source_row,
    _source_kind as _source_kind,
    _load_pages as _load_pages,
    _load_paragraphs as _load_paragraphs,
    _pdf_source_text_hash as _pdf_source_text_hash,
    _paragraph_source_text_hash as _paragraph_source_text_hash,
    _segment_set as _segment_set,
    _previous_segment_texts as _previous_segment_texts,
    _segment_set_language as _segment_set_language,
    _default_alignment_model_cache as _default_alignment_model_cache,
    _require_pair as _require_pair,
    _generate_alignment_on_connection as _generate_alignment_on_connection,
    latest_reviewed_body_ranges as latest_reviewed_body_ranges,
    validate_reviewed_body_ranges as validate_reviewed_body_ranges,
    generate_alignment as generate_alignment,
)
from .alignment_kernel import align_segment_sequences as align_segment_sequences
from .alignment_segmentation import (
    MAX_SEGMENT_LENGTH as MAX_SEGMENT_LENGTH,
    PageText as PageText,
    ParagraphText,
    SegmentDraft as SegmentDraft,
    ParagraphSegmentDraft as ParagraphSegmentDraft,
    segment_pdf_text as segment_pdf_text,
    segment_paragraph_text as segment_paragraph_text,
)
from .alignment_regions import alignment_body_bounds
from .document_group_metadata import member_display_name
from .embedding_models import DEFAULT_EMBEDDING_MODEL_ID, embedding_model_config
from .pdf_extractors import attach_page_block_offsets, pdf_page_text_hash
from .persistence.connection import connect_index, table_exists
from .semantic_alignment import cached_text_sequence_vectors, mutual_nearest_target_index


def _latest_pair_run(
    connection: sqlite3.Connection,
    document_group_id: str,
    left_source_id: str,
    right_source_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM alignment_runs WHERE document_group_id = ? "
        "AND status = 'completed' AND "
        "((pivot_source_file_id = ? AND target_source_file_id = ?) OR "
        "(pivot_source_file_id = ? AND target_source_file_id = ?)) "
        "ORDER BY completed_at DESC, rowid DESC LIMIT 1",
        (
            document_group_id,
            left_source_id,
            right_source_id,
            right_source_id,
            left_source_id,
        ),
    ).fetchone()


def _segment_set_id_for_source(run: Mapping[str, object], source_id: str) -> str:
    if str(run["pivot_source_file_id"]) == source_id:
        return str(run["pivot_segment_set_id"])
    if str(run["target_source_file_id"]) == source_id:
        return str(run["target_segment_set_id"])
    raise TextAlignmentError("对齐记录不包含指定版本。")


def list_alignment_targets(db_path: Path, source_file_id: object) -> Dict[str, object]:
    source_id = _validate_source_id(source_file_id)
    connection = connect_index(str(db_path))
    try:
        source = connection.execute(
            "SELECT source_type, payload_json FROM source_files WHERE source_file_id = ?",
            (source_id,),
        ).fetchone()
        if source is None:
            raise InvalidAlignmentRequest("文献不存在。")
        try:
            _source_kind(source)
        except InvalidAlignmentRequest:
            return {"source_file_id": source_id, "targets": []}
        if not table_exists(connection, "alignment_runs"):
            return {"source_file_id": source_id, "targets": []}
        group = connection.execute(
            "SELECT g.document_group_id, g.base_source_file_id "
            "FROM document_group_members m JOIN document_groups g "
            "ON g.document_group_id = m.document_group_id "
            "WHERE m.source_file_id = ?",
            (source_id,),
        ).fetchone()
        if group is None or not str(group["base_source_file_id"] or ""):
            return {"source_file_id": source_id, "targets": []}
        group_id = str(group["document_group_id"])
        pivot_id = str(group["base_source_file_id"])
        members = connection.execute(
            "SELECT m.source_file_id, m.version_label, s.source_type, "
            "s.file_name, s.payload_json "
            "FROM document_group_members m JOIN source_files s "
            "ON s.source_file_id = m.source_file_id "
            "WHERE m.document_group_id = ? ORDER BY m.member_order",
            (group_id,),
        ).fetchall()
        targets: List[Dict[str, object]] = []
        source_language = "und"
        for member in members:
            target_id = str(member["source_file_id"])
            if target_id == source_id:
                continue
            direct_run = _latest_pair_run(
                connection, group_id, source_id, target_id
            )
            route_runs = [direct_run] if direct_run is not None else []
            if not route_runs and source_id != pivot_id and target_id != pivot_id:
                source_run = _latest_pair_run(
                    connection, group_id, source_id, pivot_id
                )
                target_run = _latest_pair_run(
                    connection, group_id, pivot_id, target_id
                )
                if (
                    source_run is not None
                    and target_run is not None
                    and _segment_set_id_for_source(source_run, pivot_id)
                    == _segment_set_id_for_source(target_run, pivot_id)
                ):
                    route_runs = [source_run, target_run]
            if not route_runs:
                continue
            final_run = route_runs[-1]
            payload = _json_object(member["payload_json"])
            payload.setdefault("source_file_id", target_id)
            payload.setdefault("file_name", member["file_name"])
            source_language = _segment_set_language(
                connection, _segment_set_id_for_source(route_runs[0], source_id)
            )
            language_code = _segment_set_language(
                connection, _segment_set_id_for_source(final_run, target_id)
            )
            targets.append(
                {
                    "source_file_id": target_id,
                    "display_name": member_display_name(
                        member["version_label"], payload
                    ),
                    "alignment_run_id": final_run["alignment_run_id"],
                    "alignment_run_ids": [
                        str(route_run["alignment_run_id"])
                        for route_run in route_runs
                    ],
                    "via_source_file_id": (
                        pivot_id if len(route_runs) == 2 else None
                    ),
                    "algorithm": final_run["algorithm"],
                    "algorithm_version": final_run["algorithm_version"],
                    "language_code": language_code or "und",
                    "source_format": _source_kind(member),
                }
            )
        return {
            "source_file_id": source_id,
            "source_language_code": source_language or "und",
            "document_group_id": group_id,
            "targets": targets,
        }
    finally:
        connection.close()


def _selection_pdf_segment_ids(
    connection: sqlite3.Connection,
    segment_set_id: str,
    source_id: str,
    start_page: int,
    end_page: int,
    start_offset: int,
    end_offset: int,
) -> List[str]:
    if start_page == end_page:
        rows = connection.execute(
            "SELECT DISTINCT s.segment_id, s.order_index FROM text_segments s "
            "JOIN text_segment_spans p ON p.segment_id = s.segment_id "
            "WHERE s.segment_set_id = ? AND p.source_file_id = ? "
            "AND p.pdf_page_index = ? AND p.page_char_end > ? "
            "AND p.page_char_start < ? ORDER BY s.order_index",
            (
                segment_set_id,
                source_id,
                start_page,
                start_offset,
                end_offset,
            ),
        ).fetchall()
        return [str(row["segment_id"]) for row in rows]
    rows = connection.execute(
        "SELECT DISTINCT s.segment_id, s.order_index FROM text_segments s "
        "JOIN text_segment_spans p ON p.segment_id = s.segment_id "
        "WHERE s.segment_set_id = ? AND p.source_file_id = ? AND ("
        "(p.pdf_page_index = ? AND p.page_char_end > ?) OR "
        "(p.pdf_page_index > ? AND p.pdf_page_index < ?) OR "
        "(p.pdf_page_index = ? AND p.page_char_start < ?)) "
        "ORDER BY s.order_index",
        (
            segment_set_id,
            source_id,
            start_page,
            start_offset,
            start_page,
            end_page,
            end_page,
            end_offset,
        ),
    ).fetchall()
    return [str(row["segment_id"]) for row in rows]


def _selection_paragraph_segment_ids(
    connection: sqlite3.Connection,
    segment_set_id: str,
    source_id: str,
    start_paragraph: int,
    end_paragraph: int,
    start_offset: int,
    end_offset: int,
) -> List[str]:
    if start_paragraph == end_paragraph:
        rows = connection.execute(
            "SELECT DISTINCT s.segment_id, s.order_index FROM text_segments s "
            "JOIN text_segment_paragraph_spans p ON p.segment_id = s.segment_id "
            "WHERE s.segment_set_id = ? AND p.source_file_id = ? "
            "AND p.paragraph_index = ? AND p.paragraph_char_end > ? "
            "AND p.paragraph_char_start < ? ORDER BY s.order_index",
            (
                segment_set_id,
                source_id,
                start_paragraph,
                start_offset,
                end_offset,
            ),
        ).fetchall()
        return [str(row["segment_id"]) for row in rows]
    rows = connection.execute(
        "SELECT DISTINCT s.segment_id, s.order_index FROM text_segments s "
        "JOIN text_segment_paragraph_spans p ON p.segment_id = s.segment_id "
        "WHERE s.segment_set_id = ? AND p.source_file_id = ? AND ("
        "(p.paragraph_index = ? AND p.paragraph_char_end > ?) OR "
        "(p.paragraph_index > ? AND p.paragraph_index < ?) OR "
        "(p.paragraph_index = ? AND p.paragraph_char_start < ?)) "
        "ORDER BY s.order_index",
        (
            segment_set_id,
            source_id,
            start_paragraph,
            start_offset,
            start_paragraph,
            end_paragraph,
            end_paragraph,
            end_offset,
        ),
    ).fetchall()
    return [str(row["segment_id"]) for row in rows]


def _merge_page_spans(
    rows: Sequence[sqlite3.Row], page_payloads: Mapping[int, Dict[str, object]]
) -> List[Dict[str, object]]:
    grouped: Dict[int, List[Tuple[int, int]]] = {}
    for row in rows:
        grouped.setdefault(int(row["pdf_page_index"]), []).append(
            (int(row["page_char_start"]), int(row["page_char_end"]))
        )
    result: List[Dict[str, object]] = []
    for page_index in sorted(grouped):
        payload = page_payloads[page_index]
        text = str(payload.get("text_raw") or "")
        merged: List[List[int]] = []
        for start, end in sorted(grouped[page_index]):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            result.append(
                {
                    "pdf_page_id": str(
                        payload.get("pdf_page_id")
                        or f"PAGE-{page_index:06d}"
                    ),
                    "pdf_page_index": page_index,
                    "page_char_start": start,
                    "page_char_end": end,
                    "page_text_hash": str(
                        payload.get("page_text_hash")
                        or pdf_page_text_hash(text)
                    ),
                    "match_quote": text[start:end],
                }
            )
    return result


def _merge_paragraph_spans(
    rows: Sequence[sqlite3.Row],
    paragraphs: Mapping[int, ParagraphText],
) -> List[Dict[str, object]]:
    grouped: Dict[int, List[Tuple[int, int]]] = {}
    for row in rows:
        grouped.setdefault(int(row["paragraph_index"]), []).append(
            (int(row["paragraph_char_start"]), int(row["paragraph_char_end"]))
        )
    result: List[Dict[str, object]] = []
    for paragraph_index in sorted(grouped):
        paragraph = paragraphs[paragraph_index]
        merged: List[List[int]] = []
        for start, end in sorted(grouped[paragraph_index]):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            result.append(
                {
                    "anchor_id": paragraph.paragraph_id,
                    "paragraph_id": paragraph.paragraph_id,
                    "paragraph_index": paragraph_index,
                    "paragraph_char_start": start,
                    "paragraph_char_end": end,
                    "char_start": start,
                    "char_end": end,
                    "match_quote": paragraph.text[start:end],
                }
            )
    return result


def _bbox_refs(
    page_spans: Sequence[Mapping[str, object]],
    page_payloads: Mapping[int, Dict[str, object]],
) -> List[Dict[str, object]]:
    refs: List[Dict[str, object]] = []
    spans_by_page: Dict[int, List[Tuple[int, int]]] = {}
    for span in page_spans:
        spans_by_page.setdefault(int(span["pdf_page_index"]), []).append(
            (int(span["page_char_start"]), int(span["page_char_end"]))
        )
    for page_index, spans in spans_by_page.items():
        payload = page_payloads[page_index]
        blocks = [
            dict(block)
            for block in payload.get("blocks", [])
            if isinstance(block, dict)
        ]
        if blocks and not any("page_char_start" in block for block in blocks):
            attach_page_block_offsets(str(payload.get("text_raw") or ""), blocks)
        for block in blocks:
            if "page_char_start" not in block or "page_char_end" not in block:
                continue
            block_start = int(block["page_char_start"])
            block_end = int(block["page_char_end"])
            if not any(block_end > start and block_start < end for start, end in spans):
                continue
            refs.append(
                {
                    "pdf_page_id": str(
                        payload.get("pdf_page_id")
                        or f"PAGE-{page_index:06d}"
                    ),
                    "pdf_page_index": page_index,
                    "block_index": block.get("block_index"),
                    "bbox": block.get("bbox"),
                    "bbox_normalized": block.get("bbox_normalized"),
                    "page_char_start": block_start,
                    "page_char_end": block_end,
                }
            )
    return refs


def _map_segments_through_run(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    source_id: str,
    source_segments: Sequence[str],
    model_cache_dir: Path,
) -> List[str]:
    if str(run["pivot_source_file_id"]) == source_id:
        source_side = "pivot"
    elif str(run["target_source_file_id"]) == source_id:
        source_side = "target"
    else:
        raise TextAlignmentError("对齐记录不包含请求的源版本。")
    parameters = _json_object(run["parameters_json"])
    body_range = parameters.get("body_ranges", {}).get(source_side)
    if body_range is not None:
        selected_orders = connection.execute(
            "SELECT order_index FROM text_segments WHERE segment_id IN ("
            + ",".join("?" for _ in source_segments) + ")",
            tuple(source_segments),
        ).fetchall()
        if any(not body_range[0] <= row[0] < body_range[1] for row in selected_orders):
            if parameters.get("body_range_source") == "detected":
                texts = connection.execute(
                    "SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index",
                    (run[source_side + "_segment_set_id"],),
                ).fetchall()
                current_start, current_end = alignment_body_bounds([row[0] for row in texts])
                if all(current_start <= row[0] < current_end for row in selected_orders):
                    raise AlignmentNotFound("已保存对齐的正文范围已更新，请在作品组中重新生成对照")
            raise AlignmentNotFound("所选文字属于副文本区域，请通过人工修正指定对应段落。")
    placeholders = ",".join("?" for _ in source_segments)
    link_rows = connection.execute(
        "SELECT DISTINCT l.alignment_link_id, l.order_index, l.review_status "
        "FROM alignment_links l JOIN alignment_link_members m "
        "ON m.alignment_link_id = l.alignment_link_id "
        f"WHERE l.alignment_run_id = ? AND m.side = ? AND m.segment_id IN ({placeholders}) "
        "ORDER BY l.order_index",
        (run["alignment_run_id"], source_side, *source_segments),
    ).fetchall()
    structural_fallback = _paragraph_anchor_fallback(
        connection, run, source_id, source_segments
    )
    if not link_rows:
        if structural_fallback:
            return structural_fallback
        raise AlignmentNotFound("所选 Segment 没有对应的译文。")
    note_rows = [
        row for row in link_rows if str(row["review_status"]) == "note_automatic"
    ]
    if note_rows:
        link_rows = note_rows
    elif any(str(row["review_status"]) == "rejected" for row in link_rows):
        semantic_fallback = _semantic_paragraph_fallback(
            connection,
            run,
            source_id,
            source_segments,
            model_cache_dir,
        )
        if semantic_fallback:
            return semantic_fallback
        if structural_fallback:
            return structural_fallback
        raise AlignmentNotFound(
            "所选文字的跨语言对应关系置信度过低，已拒绝自动定位。"
        )
    elif any(str(row["review_status"]) == "unmatched" for row in link_rows):
        if structural_fallback:
            return structural_fallback
        raise AlignmentNotFound("所选文字在另一版本中没有可靠的对应段落。")
    link_ids = [str(row["alignment_link_id"]) for row in link_rows]
    link_placeholders = ",".join("?" for _ in link_ids)
    member_rows = connection.execute(
        "SELECT m.alignment_link_id, m.side, m.segment_id, s.order_index "
        "FROM alignment_link_members m "
        "JOIN text_segments s ON s.segment_id = m.segment_id "
        f"WHERE m.alignment_link_id IN ({link_placeholders}) "
        "ORDER BY m.alignment_link_id, s.order_index",
        link_ids,
    ).fetchall()
    source_selected = set(source_segments)
    members_by_link: Dict[str, Dict[str, List[Tuple[int, str]]]] = {}
    for member in member_rows:
        bucket = members_by_link.setdefault(
            str(member["alignment_link_id"]), {"source": [], "target": []}
        )
        side = "source" if str(member["side"]) == source_side else "target"
        bucket[side].append((int(member["order_index"]), str(member["segment_id"])))
    # A single alignment link may bundle several source and target segments
    # (an n:m block).  When only part of that block's source segments are
    # selected, restrict the result to the target segments that positionally
    # correspond to the selection, instead of returning the whole block.  This
    # keeps a selected Remark from dragging in the neighbouring paragraph that
    # is aligned to an unselected source segment (visible only for paragraph
    # targets such as EPUB; PDF targets are additionally narrowed by character
    # offset within the page).
    chosen_orders: Dict[str, int] = {}
    for link_id in link_ids:
        bucket = members_by_link.get(link_id)
        if bucket is None:
            continue
        source_members = bucket["source"]
        target_members = bucket["target"]
        if not target_members:
            continue
        selected_ranks = [
            rank
            for rank, (_order, segment_id) in enumerate(source_members)
            if segment_id in source_selected
        ]
        source_count = len(source_members)
        target_count = len(target_members)
        if not selected_ranks or source_count <= 1:
            narrowed = target_members
        else:
            low = selected_ranks[0]
            high = selected_ranks[-1]
            target_low = (low * target_count) // source_count
            target_high = -(-((high + 1) * target_count) // source_count) - 1
            target_high = max(target_low, min(target_high, target_count - 1))
            narrowed = target_members[target_low : target_high + 1]
        for order_index, segment_id in narrowed:
            chosen_orders.setdefault(segment_id, order_index)
    if not chosen_orders:
        raise AlignmentNotFound("所选 Segment 对应的是一个空译文区间。")
    return [
        segment_id
        for segment_id, _order in sorted(
            chosen_orders.items(), key=lambda item: item[1]
        )
    ]


def _semantic_paragraph_fallback(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    source_id: str,
    source_segments: Sequence[str],
    model_cache_dir: Path,
) -> List[str]:
    source_is_pivot = str(run["pivot_source_file_id"]) == source_id
    parameters = _json_object(run["parameters_json"])
    model_id = str(
        parameters.get("embedding_model_id") or DEFAULT_EMBEDDING_MODEL_ID
    )
    model_thresholds = embedding_model_config(model_id).thresholds
    low_confidence_threshold = float(
        parameters.get("low_confidence_threshold", model_thresholds.low)
    )
    source_order_key = "pivot_order_index" if source_is_pivot else "target_order_index"
    target_order_key = "target_order_index" if source_is_pivot else "pivot_order_index"
    anchors = sorted(
        (
            anchor
            for anchor in parameters.get("heading_anchors", [])
            if isinstance(anchor, dict)
            and str(anchor.get("key") or "").startswith("paragraph:")
        ),
        key=lambda anchor: int(anchor[source_order_key]),
    )
    if not anchors:
        return []
    placeholders = ",".join("?" for _ in source_segments)
    selected_rows = connection.execute(
        "SELECT order_index FROM text_segments "
        f"WHERE segment_id IN ({placeholders}) ORDER BY order_index",
        tuple(source_segments),
    ).fetchall()
    if not selected_rows:
        return []
    selected_orders = [int(row["order_index"]) for row in selected_rows]
    preceding = [
        anchor
        for anchor in anchors
        if int(anchor[source_order_key]) <= selected_orders[0]
    ]
    if not preceding:
        return []
    anchor = preceding[-1]
    anchor_index = anchors.index(anchor)
    next_anchor = anchors[anchor_index + 1] if anchor_index + 1 < len(anchors) else None
    source_set_id = str(
        run["pivot_segment_set_id"] if source_is_pivot else run["target_segment_set_id"]
    )
    target_set_id = str(
        run["target_segment_set_id"] if source_is_pivot else run["pivot_segment_set_id"]
    )
    source_rows = connection.execute(
        "SELECT segment_id, order_index, text_raw FROM text_segments "
        "WHERE segment_set_id = ? ORDER BY order_index",
        (source_set_id,),
    ).fetchall()
    target_rows = connection.execute(
        "SELECT segment_id, order_index, text_raw FROM text_segments "
        "WHERE segment_set_id = ? ORDER BY order_index",
        (target_set_id,),
    ).fetchall()
    source_start = int(anchor[source_order_key])
    target_start = int(anchor[target_order_key])
    source_end = (
        int(next_anchor[source_order_key]) if next_anchor is not None else len(source_rows)
    )
    target_end = (
        int(next_anchor[target_order_key]) if next_anchor is not None else len(target_rows)
    )
    ranges = parameters.get("body_ranges", {})
    source_range = ranges.get("pivot" if source_is_pivot else "target")
    target_range = ranges.get("target" if source_is_pivot else "pivot")
    if source_range is not None:
        source_end = min(source_end, source_range[1])
    if target_range is not None:
        target_end = min(target_end, target_range[1])
    if selected_orders[-1] >= source_end:
        return []
    source_vectors = cached_text_sequence_vectors(
        [str(row["text_raw"]) for row in source_rows],
        model_cache_dir,
        model_id=model_id,
    )
    target_vectors = cached_text_sequence_vectors(
        [str(row["text_raw"]) for row in target_rows],
        model_cache_dir,
        model_id=model_id,
    )
    if source_vectors is None or target_vectors is None:
        return []
    target_index = mutual_nearest_target_index(
        source_vectors[source_start:source_end],
        target_vectors[target_start:target_end],
        [order - source_start for order in selected_orders],
        low_confidence_threshold=low_confidence_threshold,
    )
    if target_index is None:
        return []
    return [str(target_rows[target_start + target_index]["segment_id"])]


def _paragraph_anchor_fallback(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    source_id: str,
    source_segments: Sequence[str],
) -> List[str]:
    source_is_pivot = str(run["pivot_source_file_id"]) == source_id
    source_order_key = "pivot_order_index" if source_is_pivot else "target_order_index"
    target_order_key = "target_order_index" if source_is_pivot else "pivot_order_index"
    target_set_id = str(
        run["target_segment_set_id"] if source_is_pivot else run["pivot_segment_set_id"]
    )
    placeholders = ",".join("?" for _ in source_segments)
    selected = connection.execute(
        "SELECT MIN(order_index) AS first_order FROM text_segments "
        f"WHERE segment_id IN ({placeholders})",
        tuple(source_segments),
    ).fetchone()
    if selected is None or selected["first_order"] is None:
        return []
    selected_order = int(selected["first_order"])
    anchors = [
        anchor
        for anchor in _json_object(run["parameters_json"]).get("heading_anchors", [])
        if isinstance(anchor, dict)
        and str(anchor.get("key") or "").startswith("paragraph:")
    ]
    preceding = [
        anchor
        for anchor in anchors
        if int(anchor[source_order_key]) <= selected_order
    ]
    if not preceding:
        return []
    anchor = max(preceding, key=lambda item: int(item[source_order_key]))
    next_orders = [
        int(item[source_order_key])
        for item in anchors
        if int(item[source_order_key]) > int(anchor[source_order_key])
    ]
    if next_orders and selected_order >= min(next_orders):
        return []
    row = connection.execute(
        "SELECT segment_id FROM text_segments WHERE segment_set_id = ? "
        "AND order_index = ?",
        (target_set_id, int(anchor[target_order_key])),
    ).fetchone()
    return [str(row["segment_id"])] if row is not None else []


def _alignment_candidate_segments(
    connection: sqlite3.Connection,
    segment_set_id: str,
    source_file_id: str,
    source_kind: str,
    aligned_segment_ids: Sequence[str],
    radius: int,
) -> List[Dict[str, object]]:
    placeholders = ",".join("?" for _ in aligned_segment_ids)
    aligned_rows = connection.execute(
        "SELECT segment_id, order_index FROM text_segments "
        f"WHERE segment_set_id = ? AND segment_id IN ({placeholders}) "
        "ORDER BY order_index",
        (segment_set_id, *aligned_segment_ids),
    ).fetchall()
    aligned_orders = {int(row["order_index"]) for row in aligned_rows}
    first_order = min(aligned_orders)
    last_order = max(aligned_orders)
    window_start = max(0, first_order - radius)
    window_end = last_order + radius
    rows = connection.execute(
        "SELECT segment_id, order_index, text_raw FROM text_segments "
        "WHERE segment_set_id = ? AND order_index BETWEEN ? AND ? "
        "ORDER BY order_index",
        (segment_set_id, max(0, window_start - 1), window_end + 1),
    ).fetchall()
    rows_by_order = {int(row["order_index"]): row for row in rows}
    candidates = [
        row
        for row in rows
        if window_start <= int(row["order_index"]) <= window_end
    ]
    candidate_ids = [str(row["segment_id"]) for row in candidates]
    candidate_placeholders = ",".join("?" for _ in candidate_ids)

    spans_by_segment: Dict[str, List[Dict[str, object]]] = {}
    if source_kind == "pdf":
        span_rows = connection.execute(
            "SELECT p.segment_id, p.pdf_page_index, p.page_char_start, "
            "p.page_char_end, s.order_index, p.span_order "
            "FROM text_segment_spans p JOIN text_segments s "
            "ON s.segment_id = p.segment_id "
            f"WHERE p.segment_id IN ({candidate_placeholders}) "
            "ORDER BY s.order_index, p.span_order",
            candidate_ids,
        ).fetchall()
        page_indices = sorted({int(row["pdf_page_index"]) for row in span_rows})
        page_placeholders = ",".join("?" for _ in page_indices)
        page_rows = connection.execute(
            "SELECT pdf_page_index, payload_json FROM pdf_pages "
            f"WHERE source_file_id = ? AND pdf_page_index IN ({page_placeholders})",
            (source_file_id, *page_indices),
        ).fetchall()
        page_payloads = {
            int(row["pdf_page_index"]): _json_object(row["payload_json"])
            for row in page_rows
        }
        for segment_id in candidate_ids:
            spans_by_segment[segment_id] = _merge_page_spans(
                [row for row in span_rows if str(row["segment_id"]) == segment_id],
                page_payloads,
            )
    else:
        span_rows = connection.execute(
            "SELECT p.segment_id, p.paragraph_id, p.paragraph_index, "
            "p.paragraph_char_start, p.paragraph_char_end, s.order_index, "
            "p.span_order FROM text_segment_paragraph_spans p "
            "JOIN text_segments s ON s.segment_id = p.segment_id "
            f"WHERE p.segment_id IN ({candidate_placeholders}) "
            "ORDER BY s.order_index, p.span_order",
            candidate_ids,
        ).fetchall()
        paragraph_indices = sorted(
            {int(row["paragraph_index"]) for row in span_rows}
        )
        paragraph_placeholders = ",".join("?" for _ in paragraph_indices)
        paragraph_rows = connection.execute(
            "SELECT paragraph_id, paragraph_index, text_raw, payload_json "
            "FROM paragraphs WHERE source_file_id = ? "
            f"AND paragraph_index IN ({paragraph_placeholders})",
            (source_file_id, *paragraph_indices),
        ).fetchall()
        paragraphs = {
            int(row["paragraph_index"]): ParagraphText(
                paragraph_id=str(row["paragraph_id"]),
                paragraph_index=int(row["paragraph_index"]),
                payload=_json_object(row["payload_json"]),
                text=str(row["text_raw"] or ""),
            )
            for row in paragraph_rows
        }
        for segment_id in candidate_ids:
            spans_by_segment[segment_id] = _merge_paragraph_spans(
                [row for row in span_rows if str(row["segment_id"]) == segment_id],
                paragraphs,
            )

    result: List[Dict[str, object]] = []
    for row in candidates:
        order = int(row["order_index"])
        if order < first_order:
            anchor_distance = order - first_order
        elif order > last_order:
            anchor_distance = order - last_order
        else:
            anchor_distance = 0
        before = rows_by_order.get(order - 1)
        after = rows_by_order.get(order + 1)
        result.append(
            {
                "segment_id": str(row["segment_id"]),
                "order_index": order,
                "anchor_distance": anchor_distance,
                "text": str(row["text_raw"]),
                "context_before": (
                    []
                    if before is None
                    else [
                        {
                            "segment_id": str(before["segment_id"]),
                            "text": str(before["text_raw"]),
                        }
                    ]
                ),
                "context_after": (
                    []
                    if after is None
                    else [
                        {
                            "segment_id": str(after["segment_id"]),
                            "text": str(after["text_raw"]),
                        }
                    ]
                ),
                "page_match_spans": spans_by_segment[str(row["segment_id"])],
            }
        )
    return result


def _resolve_alignment_route(
    connection: sqlite3.Connection,
    source_id: str,
    target_id: str,
) -> Tuple[List[sqlite3.Row], str | None]:
    """Return the completed run route (direct, or via the group pivot)."""

    direct_run = connection.execute(
        "SELECT * FROM alignment_runs WHERE status = 'completed' AND "
        "((pivot_source_file_id = ? AND target_source_file_id = ?) OR "
        "(pivot_source_file_id = ? AND target_source_file_id = ?)) "
        "ORDER BY completed_at DESC, rowid DESC LIMIT 1",
        (source_id, target_id, target_id, source_id),
    ).fetchone()
    if direct_run is not None:
        route_runs: List[sqlite3.Row] = [direct_run]
        via_source_id: str | None = None
    else:
        group = connection.execute(
            "SELECT g.document_group_id, g.base_source_file_id "
            "FROM document_groups g "
            "JOIN document_group_members source_member "
            "ON source_member.document_group_id = g.document_group_id "
            "JOIN document_group_members target_member "
            "ON target_member.document_group_id = g.document_group_id "
            "WHERE source_member.source_file_id = ? "
            "AND target_member.source_file_id = ?",
            (source_id, target_id),
        ).fetchone()
        if group is None or not str(group["base_source_file_id"] or ""):
            raise AlignmentNotFound("这两个版本还没有可用的自动对齐。")
        via_source_id = str(group["base_source_file_id"])
        if source_id == via_source_id or target_id == via_source_id:
            raise AlignmentNotFound("这两个版本还没有可用的自动对齐。")
        group_id = str(group["document_group_id"])
        source_run = _latest_pair_run(connection, group_id, source_id, via_source_id)
        target_run = _latest_pair_run(connection, group_id, via_source_id, target_id)
        if source_run is None or target_run is None:
            raise AlignmentNotFound("这两个版本还没有可用的自动对齐。")
        route_runs = [source_run, target_run]
        if _segment_set_id_for_source(
            route_runs[0], via_source_id
        ) != _segment_set_id_for_source(route_runs[1], via_source_id):
            raise AlignmentNotFound(
                "两个对齐使用的基准 Segment 版本不一致，请重新对齐后再定位。"
            )
    if any(
        run["algorithm"] != ALIGNMENT_ALGORITHM
        or run["algorithm_version"] not in READABLE_ALIGNMENT_VERSIONS
        for run in route_runs
    ):
        raise AlignmentNotFound(
            "对齐算法已更新，请在作品组中重新生成对照后再定位。"
        )
    return route_runs, via_source_id


def _segment_key(segment_ids: Sequence[str]) -> str:
    """Stable key for a set of source segments (order-independent)."""

    unique = sorted({str(segment_id) for segment_id in segment_ids})
    digest = hashlib.sha256("\n".join(unique).encode("utf-8"))
    return digest.hexdigest()


def _ordered_segments_in_set(
    connection: sqlite3.Connection,
    segment_set_id: str,
    segment_ids: Sequence[str],
) -> List[sqlite3.Row]:
    unique_ids = list(dict.fromkeys(str(segment_id) for segment_id in segment_ids))
    if not unique_ids:
        return []
    placeholders = ",".join("?" for _ in unique_ids)
    return connection.execute(
        "SELECT segment_id, order_index, text_raw FROM text_segments "
        f"WHERE segment_set_id = ? AND segment_id IN ({placeholders}) "
        "ORDER BY order_index",
        (segment_set_id, *unique_ids),
    ).fetchall()


def confirmed_overrides_for_pair(
    connection: sqlite3.Connection,
    source_id: str,
    target_id: str,
    source_set_id: str,
    target_set_id: str,
) -> Dict[str, Dict[str, object]]:
    """Confirmed corrections this alignment can still use, keyed by source key.

    The one reading path for every consumer (locate, link window, pair
    statistics), so a correction cannot look applied in one view and absent in
    another. A re-alignment or re-segmentation moves the confirmed target to a
    new segment set: such a correction is stale and left out rather than mapped
    onto segments the current alignment no longer uses. Most recent first.
    """

    if not table_exists(connection, "alignment_manual_overrides"):
        return {}
    overrides: Dict[str, Dict[str, object]] = {}
    for row in connection.execute(
        "SELECT override_id, source_segment_key, source_segment_ids_json, "
        "target_segment_ids_json, evidence_json FROM alignment_manual_overrides "
        "WHERE source_file_id = ? AND target_source_file_id = ? "
        "AND source_segment_set_id = ? AND target_segment_set_id = ? "
        "AND status = 'confirmed' ORDER BY confirmed_at DESC, override_id",
        (source_id, target_id, source_set_id, target_set_id),
    ):
        key = str(row["source_segment_key"])
        if key in overrides:
            continue
        stored_ids = json.loads(str(row["target_segment_ids_json"] or "[]"))
        ordered = _ordered_segments_in_set(connection, target_set_id, stored_ids)
        if len(ordered) != len({str(item) for item in stored_ids}):
            continue
        overrides[key] = {
            "override_id": str(row["override_id"]),
            "origin": str(_json_object(row["evidence_json"]).get("origin") or ""),
            "source_segment_ids": [
                str(item)
                for item in json.loads(str(row["source_segment_ids_json"] or "[]"))
            ],
            "target_segment_ids": [str(item["segment_id"]) for item in ordered],
        }
    return overrides


def override_for_selection(
    overrides: Mapping[str, Dict[str, object]],
    source_segments: Sequence[str],
) -> Dict[str, object] | None:
    """Pick the correction governing this selection out of a prepared mapping.

    Split from the query so a caller reading a whole window of selections
    applies the same rule as a single lookup, reading the corrections once.
    """

    exact = overrides.get(_segment_key(source_segments))
    if exact is not None:
        return exact
    # Reader corrections describe a whole alignment link. Scrolling locates
    # a single character, hence often only one of that link's segments.
    # Keep agent proposals selection-scoped; only reader link corrections
    # apply to a contained selection. Exact corrections above take priority.
    selection = {str(segment_id) for segment_id in source_segments}
    if not selection:
        return None
    contained = [
        override
        for override in overrides.values()
        if override["origin"] == "reader_review"
        and selection <= set(override["source_segment_ids"])
    ]
    if not contained:
        return None
    return min(contained, key=lambda item: len(item["source_segment_ids"]))


def locate_alignment(
    db_path: Path,
    source_file_id: object,
    target_source_file_id: object,
    *,
    start_page_index: object,
    end_page_index: object,
    start_offset: object,
    end_offset: object,
    candidate_radius: object = 0,
) -> Dict[str, object]:
    source_id = _validate_source_id(source_file_id)
    target_id = _validate_source_id(target_source_file_id)
    start_page = _validate_nonnegative_integer("start_page_index", start_page_index)
    end_page = _validate_nonnegative_integer("end_page_index", end_page_index)
    first_offset = _validate_nonnegative_integer("start_offset", start_offset)
    last_offset = _validate_nonnegative_integer("end_offset", end_offset)
    radius = _validate_nonnegative_integer("candidate_radius", candidate_radius)
    if radius > 5:
        raise InvalidAlignmentRequest("candidate_radius 不能大于 5。")
    if source_id == target_id:
        raise InvalidAlignmentRequest("源版本和目标版本不能相同。")
    if end_page < start_page or (end_page == start_page and last_offset <= first_offset):
        raise InvalidAlignmentRequest("选区范围无效。")

    connection = connect_index(str(db_path))
    try:
        source = _source_row(connection, source_id)
        source_kind = _source_kind(source)
        target_source = _source_row(connection, target_id)
        target_kind = _source_kind(target_source)
        endpoint_indices = [start_page]
        if end_page != start_page:
            endpoint_indices.append(end_page)
        endpoint_placeholders = ",".join("?" for _ in endpoint_indices)
        if source_kind == "pdf":
            endpoint_rows = connection.execute(
                "SELECT pdf_page_index, payload_json FROM pdf_pages "
                f"WHERE source_file_id = ? AND pdf_page_index IN ({endpoint_placeholders})",
                (source_id, *endpoint_indices),
            ).fetchall()
            endpoint_payloads = {
                int(row["pdf_page_index"]): _json_object(row["payload_json"])
                for row in endpoint_rows
            }
            missing_endpoint_message = "选区所在的 PDF 页不存在。"
            offset_boundary = "页文本"
        else:
            endpoint_rows = connection.execute(
                "SELECT paragraph_index, text_raw FROM paragraphs "
                f"WHERE source_file_id = ? AND paragraph_index IN ({endpoint_placeholders})",
                (source_id, *endpoint_indices),
            ).fetchall()
            endpoint_payloads = {
                int(row["paragraph_index"]): {"text_raw": str(row["text_raw"] or "")}
                for row in endpoint_rows
            }
            missing_endpoint_message = "选区所在的 EPUB 段落不存在。"
            offset_boundary = "段落文本"
        if set(endpoint_payloads) != set(endpoint_indices):
            raise InvalidAlignmentRequest(missing_endpoint_message)
        if first_offset > len(str(endpoint_payloads[start_page].get("text_raw") or "")):
            raise InvalidAlignmentRequest(f"start_offset 超出{offset_boundary}范围。")
        if last_offset > len(str(endpoint_payloads[end_page].get("text_raw") or "")):
            raise InvalidAlignmentRequest(f"end_offset 超出{offset_boundary}范围。")
        route_runs, via_source_id = _resolve_alignment_route(
            connection, source_id, target_id
        )
        source_run = route_runs[0]
        final_run = route_runs[-1]
        source_set_id = _segment_set_id_for_source(source_run, source_id)
        target_set_id = _segment_set_id_for_source(final_run, target_id)
        selection_function = (
            _selection_pdf_segment_ids
            if source_kind == "pdf"
            else _selection_paragraph_segment_ids
        )
        source_segments = selection_function(
            connection, source_set_id, source_id, start_page, end_page,
            first_offset, last_offset,
        )
        if not source_segments:
            raise AlignmentNotFound("所选文字没有落入可对齐的 Segment。")
        override = override_for_selection(
            confirmed_overrides_for_pair(
                connection, source_id, target_id, source_set_id, target_set_id
            ),
            source_segments,
        )
        if override is not None:
            target_segment_ids = override["target_segment_ids"]
            if not target_segment_ids:
                raise AlignmentNotFound("已人工确认：另一版本中没有对应段落。")
            alignment_source = "manual_review"
            manual_override_id: str | None = override["override_id"]
        else:
            model_cache_dir = _default_alignment_model_cache(Path(db_path))
            target_segment_ids = _map_segments_through_run(
                connection,
                source_run,
                source_id,
                source_segments,
                model_cache_dir,
            )
            if len(route_runs) == 2:
                target_segment_ids = _map_segments_through_run(
                    connection,
                    route_runs[1],
                    str(via_source_id),
                    target_segment_ids,
                    model_cache_dir,
                )
            alignment_source = "automatic"
            manual_override_id = None
        segment_placeholders = ",".join("?" for _ in target_segment_ids)
        if target_kind == "pdf":
            span_rows = connection.execute(
                "SELECT p.pdf_page_index, p.page_char_start, p.page_char_end, "
                "s.order_index, p.span_order FROM text_segment_spans p "
                "JOIN text_segments s ON s.segment_id = p.segment_id "
                f"WHERE p.segment_id IN ({segment_placeholders}) "
                "ORDER BY s.order_index, p.span_order",
                target_segment_ids,
            ).fetchall()
            target_indices = sorted({int(row["pdf_page_index"]) for row in span_rows})
            if not target_indices:
                raise AlignmentNotFound("对应 Segment 没有 PDF 位置信息。")
            target_placeholders = ",".join("?" for _ in target_indices)
            page_rows = connection.execute(
                "SELECT pdf_page_index, payload_json FROM pdf_pages "
                f"WHERE source_file_id = ? AND pdf_page_index IN ({target_placeholders})",
                (target_id, *target_indices),
            ).fetchall()
            page_payloads = {
                int(row["pdf_page_index"]): _json_object(row["payload_json"])
                for row in page_rows
            }
            match_spans = _merge_page_spans(span_rows, page_payloads)
            bbox_refs = _bbox_refs(match_spans, page_payloads)
            target_item_type = "pdf_page"
        else:
            span_rows = connection.execute(
                "SELECT p.paragraph_id, p.paragraph_index, "
                "p.paragraph_char_start, p.paragraph_char_end, "
                "s.order_index, p.span_order FROM text_segment_paragraph_spans p "
                "JOIN text_segments s ON s.segment_id = p.segment_id "
                f"WHERE p.segment_id IN ({segment_placeholders}) "
                "ORDER BY s.order_index, p.span_order",
                target_segment_ids,
            ).fetchall()
            target_indices = sorted({int(row["paragraph_index"]) for row in span_rows})
            if not target_indices:
                raise AlignmentNotFound("对应 Segment 没有 EPUB 段落位置信息。")
            target_placeholders = ",".join("?" for _ in target_indices)
            paragraph_rows = connection.execute(
                "SELECT paragraph_id, paragraph_index, text_raw, payload_json "
                "FROM paragraphs WHERE source_file_id = ? "
                f"AND paragraph_index IN ({target_placeholders})",
                (target_id, *target_indices),
            ).fetchall()
            paragraphs = {
                int(row["paragraph_index"]): ParagraphText(
                    paragraph_id=str(row["paragraph_id"]),
                    paragraph_index=int(row["paragraph_index"]),
                    payload=_json_object(row["payload_json"]),
                    text=str(row["text_raw"] or ""),
                )
                for row in paragraph_rows
            }
            match_spans = _merge_paragraph_spans(span_rows, paragraphs)
            bbox_refs = []
            target_item_type = "word_paragraph"
        target_payload = _json_object(target_source["payload_json"])
        title = str(
            target_payload.get("title")
            or target_payload.get("document_title")
            or target_source["file_name"]
            or target_id
        )
        result = {
            "alignment_run_id": final_run["alignment_run_id"],
            "alignment_run_ids": [
                str(route_run["alignment_run_id"]) for route_run in route_runs
            ],
            "via_source_file_id": via_source_id,
            "algorithm": final_run["algorithm"],
            "algorithm_version": final_run["algorithm_version"],
            "alignment_source": alignment_source,
            "manual_override_id": manual_override_id,
            "source_file_id": source_id,
            "source_segment_ids": list(source_segments),
            "target_source_file_id": target_id,
            "target_segment_ids": list(target_segment_ids),
            "target_title": title,
            "target_item_type": target_item_type,
            "target_index": target_indices[0],
            "page_match_spans": match_spans,
            "bbox_refs": bbox_refs,
            "match_offset_unit": "unicode_codepoint",
            "precise_highlight_available": True,
        }
        if radius:
            result["calibration_candidates"] = _alignment_candidate_segments(
                connection,
                target_set_id,
                target_id,
                target_kind,
                target_segment_ids,
                radius,
            )
        return result
    finally:
        connection.close()





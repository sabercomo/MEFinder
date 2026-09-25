"""Prepare, compute, and publish persisted document alignments."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Sequence, Tuple

from .alignment_segmentation import (
    MAX_SEGMENT_LENGTH as MAX_SEGMENT_LENGTH,
    PageText,
    ParagraphText,
    SegmentDraft,
    ParagraphSegmentDraft,
    segment_pdf_text,
    segment_paragraph_text,
)
from .runtime_location import component_runtime_root
from .calibration_library import _item_language_code
from .edition_folio_anchors import (
    FolioBoundaryCandidate,
    detect_folio_boundary_candidates,
)
from .embedding_models import (
    AlignmentThresholds,
    DEFAULT_EMBEDDING_MODEL_ID,
    embedding_model_config,
)
from .pdf_extractors import attach_page_block_offsets, pdf_page_text_hash
from .persistence.alignment_store import (
    completed_run_candidates,
    completed_run_status_counts,
    existing_segment_set,
    generation_group_exists,
    generation_group_members,
    generation_page_rows,
    generation_paragraph_rows,
    generation_segment_language,
    generation_source_row,
    generation_write_transaction,
    insert_alignment_links,
    insert_alignment_run,
    insert_segment_rows,
    insert_segment_set,
    previous_segment_set_id,
    reviewed_body_parameters,
    segment_rows as stored_segment_rows,
    segment_text_rows,
    supersede_completed_runs,
)
from .alignment_regions import alignment_body_bounds
from .alignment_kernel import align_segment_sequences
from .semantic_alignment import (
    ALIGNMENT_REGION_VERSION,
    EMBEDDING_RUNTIME_VERSION,
    HeadingAnchor,
    SEMANTIC_ALIGNMENT_VERSION,
    EmbeddingProvider,
    SemanticLink,
    alignment_transitions,
)


SEGMENTER = "me-finder-multilingual-sentence"
SEGMENTER_VERSION = "13"
ALIGNMENT_ALGORITHM = "chapter-anchored-semantic-dp"
ALIGNMENT_ALGORITHM_VERSION = "22"
# v22 changes anchor selection, not stored span semantics. Existing v21 results
# remain readable; generation only reuses runs of the current version.
READABLE_ALIGNMENT_VERSIONS = frozenset({"21", ALIGNMENT_ALGORITHM_VERSION})
RESTORABLE_ALIGNMENT_VERSIONS = frozenset(
    {"16", "17", "18", "19", "20", "21", ALIGNMENT_ALGORITHM_VERSION}
)
_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

WriteWindow = Callable[[], AbstractContextManager[None]]


class TextAlignmentError(RuntimeError):
    """Base failure in segmentation or alignment."""


class InvalidAlignmentRequest(TextAlignmentError, ValueError):
    """The requested group, pair, or selection is invalid."""


class AlignmentNotFound(TextAlignmentError, LookupError):
    """No completed alignment covers the requested pair or selection."""


@dataclass(frozen=True)
class AlignmentPreparation:
    pivot_set_id: str
    target_set_id: str
    pivot_segments: Tuple[Tuple[str, str], ...]
    target_segments: Tuple[Tuple[str, str], ...]
    pivot_reusable_texts: Tuple[str, ...] = ()
    target_reusable_texts: Tuple[str, ...] = ()
    folio_candidates: Tuple[FolioBoundaryCandidate, ...] = ()
    pivot_language: str = "und"
    target_language: str = "und"
    reviewed_body_ranges: Dict[str, List[int]] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(value: object) -> Dict[str, object]:
    try:
        loaded = json.loads(str(value or "{}"))
    except (TypeError, ValueError) as exc:
        raise TextAlignmentError("索引中的 JSON 记录损坏。") from exc
    if not isinstance(loaded, dict):
        raise TextAlignmentError("索引中的 JSON 记录必须是对象。")
    return loaded


def _validate_source_id(value: object) -> str:
    source_id = str(value or "").strip()
    if not _SOURCE_ID_PATTERN.fullmatch(source_id):
        raise InvalidAlignmentRequest("source_id 无效。")
    return source_id


def _validate_nonnegative_integer(name: str, value: object) -> int:
    if isinstance(value, bool):
        raise InvalidAlignmentRequest(f"{name} 必须是非负整数。")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidAlignmentRequest(f"{name} 必须是非负整数。") from exc
    if parsed < 0 or str(value).strip() not in {str(parsed), f"+{parsed}"}:
        raise InvalidAlignmentRequest(f"{name} 必须是非负整数。")
    return parsed


def _source_row(connection: sqlite3.Connection, source_id: str) -> sqlite3.Row:
    row = generation_source_row(connection, source_id)
    if row is None:
        raise InvalidAlignmentRequest("文献不存在。")
    _source_kind(row)
    return row


def _source_kind(row: Mapping[str, object]) -> str:
    source_type = str(row["source_type"] or "").casefold()
    if source_type == "pdf":
        return "pdf"
    payload = _json_object(row["payload_json"])
    source_format = str(
        payload.get("file_format") or payload.get("source_format") or ""
    ).casefold()
    if source_type == "epub" or (source_type == "word" and source_format == "epub"):
        return "epub"
    raise InvalidAlignmentRequest("自动对齐只支持 PDF 和 EPUB 文献。")


def _load_pages(
    connection: sqlite3.Connection, source_id: str
) -> Tuple[str, List[PageText]]:
    rows = generation_page_rows(connection, source_id)
    pieces: List[str] = []
    pages: List[PageText] = []
    cursor = 0
    for row in rows:
        payload = _json_object(row["payload_json"])
        text = str(payload.get("text_raw") or "")
        blocks = payload.get("blocks")
        if isinstance(blocks, list):
            attach_page_block_offsets(text, blocks)
        if pieces:
            previous_text = pages[-1].text
            toc_boundary = (
                re.search(
                    r"(?im)^\s*(?:(?:contents|table of contents|inhalt(?:sverzeichnis)?)\b|目录|目錄)",
                    previous_text,
                )
                or re.search(
                    r"(?im)^\s*(?:(?:contents|table of contents|inhalt(?:sverzeichnis)?)\b|目录|目錄)",
                    text,
                )
                or len(re.findall(r"(?:\.\s*){4,}", previous_text)) >= 2
            )
            required_newlines = 2 if toc_boundary else 1
            trailing_newlines = len(previous_text) - len(previous_text.rstrip("\n"))
            leading_newlines = len(text) - len(text.lstrip("\n"))
            separator = "\n" * max(
                0,
                required_newlines - trailing_newlines - leading_newlines,
            )
            pieces.append(separator)
            cursor += len(separator)
        start = cursor
        pieces.append(text)
        cursor += len(text)
        pages.append(
            PageText(
                page_index=int(row["pdf_page_index"]),
                payload=payload,
                text=text,
                global_start=start,
                global_end=cursor,
            )
        )
    return "".join(pieces), pages


def _load_paragraphs(
    connection: sqlite3.Connection, source_id: str
) -> List[ParagraphText]:
    rows = generation_paragraph_rows(connection, source_id)
    return [
        ParagraphText(
            paragraph_id=str(row["paragraph_id"]),
            paragraph_index=int(row["paragraph_index"]),
            payload=_json_object(row["payload_json"]),
            text=str(row["text_raw"] or ""),
        )
        for row in rows
    ]


def _pdf_source_text_hash(pages: Sequence[PageText]) -> str:
    digest = hashlib.sha256()
    for page in pages:
        digest.update(str(page.page_index).encode("ascii"))
        digest.update(b"\0")
        digest.update(
            str(page.payload.get("page_text_hash") or pdf_page_text_hash(page.text)).encode(
                "ascii"
            )
        )
        digest.update(b"\0")
    return digest.hexdigest()


def _paragraph_source_text_hash(paragraphs: Sequence[ParagraphText]) -> str:
    digest = hashlib.sha256()
    for paragraph in paragraphs:
        digest.update(paragraph.paragraph_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(paragraph.paragraph_index).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(paragraph.text.encode("utf-8")).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _segment_set(
    connection: sqlite3.Connection, source_id: str
) -> Tuple[str, List[Tuple[str, str]]]:
    source = _source_row(connection, source_id)
    source_kind = _source_kind(source)
    pages: List[PageText] = []
    paragraphs: List[ParagraphText] = []
    if source_kind == "pdf":
        full_text, pages = _load_pages(connection, source_id)
        text_hash = _pdf_source_text_hash(pages)
    else:
        paragraphs = _load_paragraphs(connection, source_id)
        full_text = "\n".join(paragraph.text for paragraph in paragraphs)
        text_hash = _paragraph_source_text_hash(paragraphs)
    if not full_text.strip():
        raise InvalidAlignmentRequest("文献没有可用于对齐的文本。")
    existing = existing_segment_set(
        connection, source_id, text_hash, SEGMENTER, SEGMENTER_VERSION
    )
    if existing is not None:
        segment_set_id = str(existing["segment_set_id"])
        segments = [
            (str(row["segment_id"]), str(row["text_raw"]))
            for row in stored_segment_rows(connection, segment_set_id)
        ]
        return segment_set_id, segments

    drafts: Sequence[SegmentDraft | ParagraphSegmentDraft]
    if source_kind == "pdf":
        drafts = segment_pdf_text(full_text, pages)
    else:
        drafts = segment_paragraph_text(paragraphs)
    payload = _json_object(source["payload_json"])
    language_code = str(payload.get("language_code") or "").strip()
    if not language_code:
        language_code = _item_language_code(
            full_text[:8000],
            payload.get("title"),
            payload.get("author"),
            source["file_name"],
        )
    set_digest = hashlib.sha256(
        f"{source_id}\0{text_hash}\0{SEGMENTER_VERSION}".encode("utf-8")
    ).hexdigest()[:24]
    segment_set_id = f"segment-set-{set_digest}"
    insert_segment_set(
        connection,
        segment_set_id,
        source_id,
        text_hash,
        SEGMENTER,
        SEGMENTER_VERSION,
        language_code or "und",
        _now(),
    )
    segments: List[Tuple[str, str]] = []
    segment_rows: List[Tuple[object, ...]] = []
    page_span_rows: List[Tuple[object, ...]] = []
    paragraph_span_rows: List[Tuple[object, ...]] = []
    for order_index, draft in enumerate(drafts):
        segment_digest = hashlib.sha256(
            f"{segment_set_id}\0{order_index}\0{draft.text}".encode("utf-8")
        ).hexdigest()[:24]
        segment_id = f"segment-{segment_digest}"
        segment_rows.append((segment_id, segment_set_id, order_index, draft.text))
        if source_kind == "pdf":
            page_span_rows.extend(
                (segment_id, source_id, page, start, end, span_order)
                for span_order, (page, start, end) in enumerate(draft.spans)
            )
        else:
            paragraph_span_rows.extend(
                (
                    segment_id,
                    source_id,
                    paragraph_id,
                    paragraph_index,
                    start,
                    end,
                    span_order,
                )
                for span_order, (
                    paragraph_id,
                    paragraph_index,
                    start,
                    end,
                ) in enumerate(draft.spans)
            )
        segments.append((segment_id, draft.text))
    if not segments:
        raise InvalidAlignmentRequest("文献没有可用于对齐的 Segment。")
    insert_segment_rows(connection, segment_rows, page_span_rows, paragraph_span_rows)
    return segment_set_id, segments


def _previous_segment_texts(
    connection: sqlite3.Connection,
    source_id: str,
    current_segment_set_id: str,
) -> Tuple[str, ...]:
    previous_id = previous_segment_set_id(
        connection, source_id, SEGMENTER, current_segment_set_id, SEGMENTER_VERSION
    )
    if previous_id is None:
        return ()
    return tuple(
        str(segment["text_raw"])
        for segment in segment_text_rows(connection, previous_id)
    )


def _segment_set_language(
    connection: sqlite3.Connection, segment_set_id: str
) -> str:
    row = generation_segment_language(connection, segment_set_id)
    if row is None:
        raise TextAlignmentError("对齐所需的 Segment 集不存在。")
    return str(row["language_code"] or "und")


def _default_alignment_model_cache(db_path: Path) -> Path:
    index_path = Path(db_path).resolve()
    runtime_root = (
        index_path.parent.parent
        if index_path.parent.name.casefold() == "data"
        else index_path.parent
    )
    return component_runtime_root(runtime_root) / "components" / "text-alignment" / "models"


def _require_pair(
    connection: sqlite3.Connection,
    document_group_id: str,
    pivot_source_id: str,
    target_source_id: str,
) -> None:
    if not generation_group_exists(connection, document_group_id):
        raise InvalidAlignmentRequest("作品组不存在。")
    if pivot_source_id == target_source_id:
        raise InvalidAlignmentRequest("两个对齐版本不能相同。")
    member_ids = generation_group_members(connection, document_group_id)
    if pivot_source_id not in member_ids or target_source_id not in member_ids:
        raise InvalidAlignmentRequest("两本文献都必须属于该作品组。")


def _generate_alignment_on_connection(
    connection: sqlite3.Connection,
    document_group_id: str,
    pivot_source_id: str,
    target_source_id: str,
    *,
    model_cache_dir: Path,
    embedding_provider: EmbeddingProvider | None = None,
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
    thresholds: AlignmentThresholds | None = None,
    preparation: AlignmentPreparation | None = None,
    computed: Tuple[List[SemanticLink], List[HeadingAnchor]] | None = None,
) -> Dict[str, object]:
    active_model = embedding_model_config(embedding_model_id)
    active_thresholds = thresholds or active_model.thresholds
    _require_pair(
        connection, document_group_id, pivot_source_id, target_source_id
    )
    if preparation is None:
        pivot_set_id, pivot_segments = _segment_set(connection, pivot_source_id)
        target_set_id, target_segments = _segment_set(connection, target_source_id)
        preparation = AlignmentPreparation(
            pivot_set_id,
            target_set_id,
            tuple(pivot_segments),
            tuple(target_segments),
            _previous_segment_texts(connection, pivot_source_id, pivot_set_id),
            _previous_segment_texts(connection, target_source_id, target_set_id),
            tuple(
                detect_folio_boundary_candidates(
                    connection,
                    pivot_source_id,
                    target_source_id,
                    pivot_set_id,
                    target_set_id,
                )
            ),
            pivot_language=_segment_set_language(connection, pivot_set_id),
            target_language=_segment_set_language(connection, target_set_id),
        )
    pivot_set_id = preparation.pivot_set_id
    target_set_id = preparation.target_set_id
    pivot_segments = preparation.pivot_segments
    target_segments = preparation.target_segments
    if computed is None:
        computed = align_segment_sequences(
            [text for _segment_id, text in pivot_segments],
            [text for _segment_id, text in target_segments],
            cache_dir=model_cache_dir,
            embedding_provider=embedding_provider,
            embedding_model_id=embedding_model_id,
            thresholds=active_thresholds,
            reusable_sequences=(
                preparation.pivot_reusable_texts,
                preparation.target_reusable_texts,
            ),
            folio_candidates=preparation.folio_candidates,
            source_language=preparation.pivot_language,
            target_language=preparation.target_language,
            reviewed_body_ranges=preparation.reviewed_body_ranges,
        )
    aligned, anchors = computed
    supersede_completed_runs(connection, document_group_id, pivot_source_id, target_source_id)
    run_id = f"alignment-run-{uuid.uuid4().hex}"
    timestamp = _now()
    candidate_by_key = {
        candidate.key: candidate for candidate in preparation.folio_candidates
    }
    folio_anchors = [anchor for anchor in anchors if anchor.key.startswith("folio:")]
    parameters = {
        "transitions": alignment_transitions(),
        "length_unit": "non_whitespace_unicode_codepoint",
        "embedding_model_id": active_model.id,
        "embedding_model_hf_name": active_model.hf_name,
        "embedding_runtime_version": EMBEDDING_RUNTIME_VERSION,
        "semantic_alignment_version": SEMANTIC_ALIGNMENT_VERSION,
        "alignment_region_version": ALIGNMENT_REGION_VERSION,
        "body_range_source": "reviewed" if preparation.reviewed_body_ranges is not None else "detected",
        "body_ranges": preparation.reviewed_body_ranges or {
            "pivot": list(alignment_body_bounds([text for _, text in pivot_segments])),
            "target": list(alignment_body_bounds([text for _, text in target_segments])),
        },
        "similarity": "cosine",
        "low_confidence_threshold": active_thresholds.low,
        "note_block_confidence_threshold": active_thresholds.note_block,
        "note_candidate_margin": active_thresholds.margin,
        "pivot_language": preparation.pivot_language,
        "target_language": preparation.target_language,
        "heading_anchors": [
            {
                "key": anchor.key,
                "pivot_order_index": anchor.source_index,
                "target_order_index": anchor.target_index,
            }
            for anchor in anchors
        ],
        "edition_folio_anchors": [
            {
                "key": anchor.key,
                "folio_number": candidate_by_key[anchor.key].folio_number,
                "pivot_order_index": anchor.source_index,
                "target_order_index": anchor.target_index,
                "target_pdf_page_index": candidate_by_key[
                    anchor.key
                ].target_pdf_page_index,
                "target_bbox": list(candidate_by_key[anchor.key].target_bbox),
                "semantic_verified": True,
            }
            for anchor in folio_anchors
        ],
    }
    insert_alignment_run(
        connection,
        (
            run_id,
            document_group_id,
            pivot_source_id,
            target_source_id,
            pivot_set_id,
            target_set_id,
            ALIGNMENT_ALGORITHM,
            ALIGNMENT_ALGORITHM_VERSION,
            json.dumps(parameters, ensure_ascii=False, separators=(",", ":")),
            "completed",
            timestamp,
            timestamp,
        ),
    )
    link_rows: List[Tuple[object, ...]] = []
    member_rows: List[Tuple[object, ...]] = []
    for order_index, link in enumerate(aligned):
        link_id = f"alignment-link-{uuid.uuid4().hex}"
        link_rows.append(
            (
                link_id,
                run_id,
                order_index,
                round(link.cost, 6),
                link.confidence,
                link.anchor_key or None,
                link.review_status,
            )
        )
        member_rows.extend(
            [
                (
                    link_id,
                    "pivot",
                    pivot_segments[index][0],
                    index - link.source_start,
                )
                for index in range(link.source_start, link.source_end)
            ]
            + [
                (
                    link_id,
                    "target",
                    target_segments[index][0],
                    index - link.target_start,
                )
                for index in range(link.target_start, link.target_end)
            ]
        )
    insert_alignment_links(connection, link_rows, member_rows)
    rejected_count = sum(link.review_status == "rejected" for link in aligned)
    unmatched_count = sum(link.review_status == "unmatched" for link in aligned)
    note_count = sum(link.review_status == "note_automatic" for link in aligned)
    return {
        "alignment_run_id": run_id,
        "document_group_id": document_group_id,
        "pivot_source_file_id": pivot_source_id,
        "target_source_file_id": target_source_id,
        "pivot_segment_count": len(pivot_segments),
        "target_segment_count": len(target_segments),
        "alignment_link_count": len(aligned),
        "accepted_link_count": len(aligned) - rejected_count - unmatched_count,
        "rejected_link_count": rejected_count,
        "unmatched_link_count": unmatched_count,
        "numbered_note_link_count": note_count,
        "heading_anchor_count": len(anchors) - len(folio_anchors),
        "folio_anchor_count": len(folio_anchors),
        "algorithm": ALIGNMENT_ALGORITHM,
        "algorithm_version": ALIGNMENT_ALGORITHM_VERSION,
        "embedding_model_id": active_model.id,
        "status": "completed",
        "reused": False,
    }


def latest_reviewed_body_ranges(
    connection: sqlite3.Connection,
    pivot_segment_set_id: str,
    target_segment_set_id: str,
) -> Dict[str, List[int]] | None:
    """Return the most recent human-reviewed body ranges for these two sets.

    Reviewed ranges are segment indices, so they only apply to the exact
    segmentation they were recorded against; a re-parse produces a new segment
    set and the review no longer matches, by design.
    """

    reviewed_json = reviewed_body_parameters(
        connection, pivot_segment_set_id, target_segment_set_id
    )
    if reviewed_json is None:
        return None
    ranges = json.loads(reviewed_json)["body_ranges"]
    return ranges if isinstance(ranges, dict) else None


def validate_reviewed_body_ranges(
    reviewed_body_ranges: Mapping[str, object],
    pivot_segment_count: int,
    target_segment_count: int,
) -> None:
    """Reject anything that is not a valid half-open segment interval per side."""

    for side, segment_count in (
        ("pivot", pivot_segment_count),
        ("target", target_segment_count),
    ):
        bounds = reviewed_body_ranges.get(side)
        if (
            not isinstance(bounds, list)
            or len(bounds) != 2
            or not all(type(value) is int for value in bounds)
            or not 0 <= bounds[0] < bounds[1] <= segment_count
        ):
            raise InvalidAlignmentRequest("复核正文范围必须是有效的半开 Segment 区间。")


def generate_alignment(
    db_path: Path,
    document_group_id: object,
    pivot_source_file_id: object,
    target_source_file_id: object,
    *,
    force: bool = False,
    model_cache_dir: Path | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
    alignment_thresholds: AlignmentThresholds | None = None,
    write_window: WriteWindow | None = None,
    reviewed_body_ranges: Dict[str, List[int]] | None = None,
    expected_segment_set_ids: Mapping[str, str] | None = None,
    compute_runner: Callable[..., Tuple[List[SemanticLink], list]] | None = None,
) -> Dict[str, object]:
    group_id = str(document_group_id or "").strip()
    if not group_id:
        raise InvalidAlignmentRequest("document_group_id is required")
    pivot_id = _validate_source_id(pivot_source_file_id)
    target_id = _validate_source_id(target_source_file_id)
    active_model = embedding_model_config(embedding_model_id)
    active_thresholds = alignment_thresholds or active_model.thresholds
    cache_dir = (
        Path(model_cache_dir)
        if model_cache_dir is not None
        else _default_alignment_model_cache(Path(db_path))
    )
    transaction_window = write_window or nullcontext
    with transaction_window():
        with generation_write_transaction(db_path, install_schema=True) as connection:
            _require_pair(connection, group_id, pivot_id, target_id)
            pivot_set_id, pivot_segments = _segment_set(connection, pivot_id)
            target_set_id, target_segments = _segment_set(connection, target_id)
            # Compare inside the preparation transaction, before interpreting
            # indices or creating a run. An in-range index can refer to new text.
            if expected_segment_set_ids is not None and expected_segment_set_ids != {
                "pivot": pivot_set_id, "target": target_set_id,
            }:
                raise InvalidAlignmentRequest("文献解析文本已更新，请重新加载正文范围后再提交")
            if reviewed_body_ranges is None:
                reviewed_body_ranges = latest_reviewed_body_ranges(
                    connection, pivot_set_id, target_set_id
                )
            if reviewed_body_ranges is not None:
                validate_reviewed_body_ranges(
                    reviewed_body_ranges, len(pivot_segments), len(target_segments)
                )
            preparation = AlignmentPreparation(
                pivot_set_id,
                target_set_id,
                tuple(pivot_segments),
                tuple(target_segments),
                _previous_segment_texts(connection, pivot_id, pivot_set_id),
                _previous_segment_texts(connection, target_id, target_set_id),
                tuple(
                    detect_folio_boundary_candidates(
                        connection,
                        pivot_id,
                        target_id,
                        pivot_set_id,
                        target_set_id,
                    )
                ),
                pivot_language=_segment_set_language(connection, pivot_set_id),
                target_language=_segment_set_language(connection, target_set_id),
                reviewed_body_ranges=reviewed_body_ranges,
            )
            existing = None
            if not force:
                candidates = completed_run_candidates(
                    connection,
                    group_id,
                    pivot_id,
                    target_id,
                    pivot_set_id,
                    target_set_id,
                    ALIGNMENT_ALGORITHM,
                    ALIGNMENT_ALGORITHM_VERSION,
                )
                existing = next(
                    (
                        row
                        for row in candidates
                        if (
                            parameters := _json_object(row["parameters_json"])
                        ).get("embedding_model_id")
                        == active_model.id
                        and parameters.get("alignment_region_version")
                        == ALIGNMENT_REGION_VERSION
                        and (reviewed_body_ranges is None or (
                            parameters.get("body_range_source") == "reviewed"
                            and parameters.get("body_ranges") == reviewed_body_ranges
                        ))
                        and parameters.get("low_confidence_threshold")
                        == active_thresholds.low
                        and parameters.get("note_block_confidence_threshold")
                        == active_thresholds.note_block
                        and parameters.get("note_candidate_margin")
                        == active_thresholds.margin
                    ),
                    None,
                )
            if existing is not None:
                counts = completed_run_status_counts(
                    connection, str(existing["alignment_run_id"])
                )
                parameters = _json_object(existing["parameters_json"])
                connection.commit()
                return {
                    "alignment_run_id": str(existing["alignment_run_id"]),
                    "document_group_id": group_id,
                    "pivot_source_file_id": pivot_id,
                    "target_source_file_id": target_id,
                    "pivot_segment_count": len(pivot_segments),
                    "target_segment_count": len(target_segments),
                    "alignment_link_count": sum(counts.values()),
                    "accepted_link_count": counts.get("automatic", 0)
                    + counts.get("note_automatic", 0),
                    "rejected_link_count": counts.get("rejected", 0),
                    "unmatched_link_count": counts.get("unmatched", 0),
                    "numbered_note_link_count": counts.get("note_automatic", 0),
                    "heading_anchor_count": len(
                        [
                            anchor
                            for anchor in parameters.get("heading_anchors", [])
                            if not str(anchor.get("key") or "").startswith("folio:")
                        ]
                    ),
                    "folio_anchor_count": len(
                        parameters.get("edition_folio_anchors", [])
                    ),
                    "algorithm": ALIGNMENT_ALGORITHM,
                    "algorithm_version": ALIGNMENT_ALGORITHM_VERSION,
                    "embedding_model_id": active_model.id,
                    "status": "completed",
                    "reused": True,
                }

    # Compute seam: the default runs in this process (imports NumPy); an
    # out-of-process runner may be injected so the main process never needs the
    # compute stack. Prepare (above) and publish (below) stay in the main
    # process with its identity checks and write coordination either way.
    compute = compute_runner if compute_runner is not None else align_segment_sequences
    computed = compute(
        [text for _segment_id, text in preparation.pivot_segments],
        [text for _segment_id, text in preparation.target_segments],
        cache_dir=cache_dir,
        embedding_provider=embedding_provider,
        embedding_model_id=active_model.id,
        thresholds=active_thresholds,
        reusable_sequences=(
            preparation.pivot_reusable_texts,
            preparation.target_reusable_texts,
        ),
        folio_candidates=preparation.folio_candidates,
        source_language=preparation.pivot_language,
        target_language=preparation.target_language,
        reviewed_body_ranges=preparation.reviewed_body_ranges,
    )

    with transaction_window():
        with generation_write_transaction(db_path) as connection:
            result = _generate_alignment_on_connection(
                connection,
                group_id,
                pivot_id,
                target_id,
                model_cache_dir=cache_dir,
                embedding_provider=embedding_provider,
                embedding_model_id=active_model.id,
                thresholds=active_thresholds,
                preparation=preparation,
                computed=computed,
            )
            return result

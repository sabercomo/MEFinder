"""Persisted document segments and monotonic two-document alignment."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np

from .auto_page_mapping import _layout_bbox_scale, _normalized_page_bbox
from .calibration_library import _item_language_code
from .document_group_metadata import member_display_name
from .edition_folio_anchors import (
    FolioBoundaryCandidate,
    detect_folio_boundary_candidates,
    verify_folio_boundary_candidates,
)
from .embedding_models import (
    AlignmentThresholds,
    DEFAULT_EMBEDDING_MODEL_ID,
    embedding_model_config,
)
from .pdf_extractors import attach_page_block_offsets, pdf_page_text_hash
from .persistence.connection import open_writable_index
from .persistence.schema_installers import install_text_alignment_schema
from .semantic_alignment import (
    ALIGNMENT_REGION_VERSION,
    EMBEDDING_RUNTIME_VERSION,
    HeadingAnchor,
    SEMANTIC_ALIGNMENT_VERSION,
    EmbeddingProvider,
    SemanticLink,
    align_semantic_sequences,
    alignment_body_bounds,
    alignment_transitions,
    cached_text_sequence_vectors,
    embed_text_sequences,
    mutual_nearest_target_index,
)


SEGMENTER = "me-finder-multilingual-sentence"
SEGMENTER_VERSION = "12"
ALIGNMENT_ALGORITHM = "chapter-anchored-semantic-dp"
ALIGNMENT_ALGORITHM_VERSION = "20"
# Anchor changes alter the alignment result even when stored span semantics match.
READABLE_ALIGNMENT_VERSIONS = frozenset({ALIGNMENT_ALGORITHM_VERSION})
RESTORABLE_ALIGNMENT_VERSIONS = frozenset(
    {"16", "17", "18", "19", ALIGNMENT_ALGORITHM_VERSION}
)
MAX_SEGMENT_LENGTH = 1200
_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SENTENCE_ENDINGS = frozenset("。！？!?；;")
_CLOSING_PUNCTUATION = frozenset("”’\"'）)]】》〉」』")
_STRUCTURAL_MARKER_LINE = re.compile(
    r"(?:"
    r"(?:§\s*[0-9IlOoSs]{1,4}|第\s*[零〇一二两三四五六七八九十百0-9IlOoSs]{1,5}\s*节)"
    r"\s*[.．、:]?"
    r"|第\s*[零〇一二两三四五六七八九十百0-9]{1,5}\s*(?:章|篇|部).*"
    r"|PART\s+(?:ONE|TWO|THREE|FOUR|[IVX]{1,4}|\d{1,2})"
    r")",
    re.IGNORECASE,
)
_ABBREVIATIONS = frozenset(
    {
        "dr",
        "mr",
        "mrs",
        "ms",
        "prof",
        "sr",
        "jr",
        "st",
        "vs",
        "etc",
        "bzw",
        "ca",
        "d.h",
        "u.a",
        "z.b",
    }
)
_NON_ALIGNMENT_BLOCK_ROLES = frozenset(
    {
        "discarded_block",
        "footer",
        "header",
        "page_footnote",
        "page_footer",
        "page_header",
        "page_number",
    }
)
_LAYOUT_NUMBER = re.compile(r"\s*[0-9]{1,4}\s*\Z")


def _is_confirmed_parser_placeholder(text: str) -> bool:
    normalized = " ".join(text.split()).casefold()
    return (
        normalized == "[no text detected]"
        or normalized == "the following table provides the information in english:"
        or normalized.startswith("the image contains no discernible text or characters")
        or (
            normalized.startswith("the ocr result ")
            and " is a hallucination" in normalized
        )
        or normalized.startswith(
            "therefore, the correct ocr output must reflect the absence"
        )
    )
WriteWindow = Callable[[], AbstractContextManager[None]]


class TextAlignmentError(RuntimeError):
    """Base failure in segmentation or alignment."""


class InvalidAlignmentRequest(TextAlignmentError, ValueError):
    """The requested group, pair, or selection is invalid."""


class AlignmentNotFound(TextAlignmentError, LookupError):
    """No completed alignment covers the requested pair or selection."""


@dataclass(frozen=True)
class PageText:
    page_index: int
    payload: Dict[str, object]
    text: str
    global_start: int
    global_end: int


@dataclass(frozen=True)
class ParagraphText:
    paragraph_id: str
    paragraph_index: int
    payload: Dict[str, object]
    text: str


@dataclass(frozen=True)
class SegmentDraft:
    text: str
    spans: Tuple[Tuple[int, int, int], ...]


@dataclass(frozen=True)
class ParagraphSegmentDraft:
    text: str
    spans: Tuple[Tuple[str, int, int, int], ...]


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


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone() is not None


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
    row = connection.execute(
        "SELECT source_file_id, source_type, file_name, payload_json "
        "FROM source_files WHERE source_file_id = ?",
        (source_id,),
    ).fetchone()
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
    rows = connection.execute(
        "SELECT pdf_page_index, payload_json FROM pdf_pages "
        "WHERE source_file_id = ? ORDER BY pdf_page_index, row_id",
        (source_id,),
    ).fetchall()
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
    rows = connection.execute(
        "SELECT paragraph_id, paragraph_index, text_raw, payload_json "
        "FROM paragraphs WHERE source_file_id = ? ORDER BY paragraph_index, rowid",
        (source_id,),
    ).fetchall()
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


def _period_ends_sentence(text: str, index: int) -> bool:
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    if before.isdigit() and after.isdigit():
        return False
    if re.search(r"\.\s*\Z", text[:index]) or re.match(r"\s*\.", text[index + 1 :]):
        return False
    word_match = re.search(r"([A-Za-zÀ-ÖØ-öø-ÿ.]+)\Z", text[:index])
    word = word_match.group(1).casefold() if word_match else ""
    if word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha()):
        return False
    return not after or after.isspace() or after in _CLOSING_PUNCTUATION


def _raw_segment_ranges(text: str) -> Iterable[Tuple[int, int]]:
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        boundary = char in _SENTENCE_ENDINGS or (
            char == "." and _period_ends_sentence(text, index)
        )
        if boundary:
            end = index + 1
            while end < len(text) and text[end] in _CLOSING_PUNCTUATION:
                end += 1
            yield start, end
            start = end
            index = end
            continue
        if char == "\n":
            line_start = text.rfind("\n", 0, index) + 1
            if _STRUCTURAL_MARKER_LINE.fullmatch(text[line_start:index].strip()):
                if start < line_start:
                    yield start, line_start
                yield max(start, line_start), index
                index += 1
                start = index
                continue
            match = re.match(r"\n[ \t]*\n+", text[index:])
            if match:
                yield start, index
                index += len(match.group(0))
                start = index
                continue
        index += 1
    yield start, len(text)


def _bounded_ranges(text: str, start: int, end: int) -> Iterable[Tuple[int, int]]:
    cursor = start
    while end - cursor > MAX_SEGMENT_LENGTH:
        limit = cursor + MAX_SEGMENT_LENGTH
        candidates = (
            text.rfind("\n", cursor + 1, limit + 1),
            text.rfind(" ", cursor + 1, limit + 1),
        )
        split = max(candidates)
        if split <= cursor:
            split = limit
        yield cursor, split
        cursor = split
    yield cursor, end


def segment_pdf_text(full_text: str, pages: Sequence[PageText]) -> List[SegmentDraft]:
    """Split reading-order PDF text and retain exact page-codepoint spans."""

    drafts: List[SegmentDraft] = []
    page_cursor = 0
    excluded_ranges: List[Tuple[int, int]] = []
    margin_text_counts: Dict[str, int] = {}
    page_metrics: Dict[int, Tuple[float, float]] = {}
    for page in pages:
        blocks = page.payload.get("blocks")
        if not isinstance(blocks, list):
            continue
        width, height = _layout_bbox_scale(
            blocks,
            float(page.payload.get("page_width") or 1000.0),
            float(page.payload.get("page_height") or 1000.0),
        )
        page_metrics[page.page_index] = (width, height)
        seen: set[str] = set()
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bbox = _normalized_page_bbox(block, width, height)
            text = " ".join(str(block.get("text") or "").split())
            if not text or bbox is None:
                continue
            y_center = (bbox[1] + bbox[3]) / 2.0
            if 0.14 < y_center < 0.86:
                continue
            key = re.sub(r"[0-9]+", "#", text.casefold())
            if len(key) <= 160:
                seen.add(key)
        for key in seen:
            margin_text_counts[key] = margin_text_counts.get(key, 0) + 1
    repeated_margin_texts = {
        key for key, count in margin_text_counts.items() if count >= 3
    }
    for page in pages:
        blocks = page.payload.get("blocks")
        if not isinstance(blocks, list):
            continue
        width, height = page_metrics.get(page.page_index, (1000.0, 1000.0))
        for block in blocks:
            if not isinstance(block, dict):
                continue
            role = str(
                block.get("mineru_type")
                or block.get("parser_type")
                or block.get("type")
                or ""
            ).strip().casefold()
            excluded = role in _NON_ALIGNMENT_BLOCK_ROLES
            text = " ".join(str(block.get("text") or "").split())
            bbox = _normalized_page_bbox(block, width, height)
            if bbox is not None and text:
                x_center = (bbox[0] + bbox[2]) / 2.0
                y_center = (bbox[1] + bbox[3]) / 2.0
                key = re.sub(r"[0-9]+", "#", text.casefold())
                excluded = excluded or bool(
                    key in repeated_margin_texts
                    and (y_center <= 0.14 or y_center >= 0.86)
                )
                excluded = excluded or bool(
                    _LAYOUT_NUMBER.fullmatch(text)
                    and (
                        x_center < 0.12
                        or x_center > 0.82
                        or y_center < 0.14
                        or y_center > 0.86
                    )
                )
            if not excluded:
                continue
            start = int(block.get("page_char_start") or 0)
            end = int(block.get("page_char_end") or start)
            if 0 <= start < end <= len(page.text):
                excluded_ranges.append(
                    (page.global_start + start, page.global_start + end)
                )
    for raw_start, raw_end in _raw_segment_ranges(full_text):
        clean_ranges = [(raw_start, raw_end)]
        for excluded_start, excluded_end in excluded_ranges:
            next_ranges: List[Tuple[int, int]] = []
            for clean_start, clean_end in clean_ranges:
                if excluded_end <= clean_start or excluded_start >= clean_end:
                    next_ranges.append((clean_start, clean_end))
                    continue
                if clean_start < excluded_start:
                    next_ranges.append((clean_start, excluded_start))
                if excluded_end < clean_end:
                    next_ranges.append((excluded_end, clean_end))
            clean_ranges = next_ranges
        for clean_start, clean_end in clean_ranges:
            for bounded_start, bounded_end in _bounded_ranges(
                full_text, clean_start, clean_end
            ):
                while bounded_start < bounded_end and full_text[bounded_start].isspace():
                    bounded_start += 1
                while bounded_end > bounded_start and full_text[bounded_end - 1].isspace():
                    bounded_end -= 1
                if bounded_start >= bounded_end:
                    continue
                segment_text = full_text[bounded_start:bounded_end]
                if _is_confirmed_parser_placeholder(segment_text):
                    continue
                spans: List[Tuple[int, int, int]] = []
                while (
                    page_cursor < len(pages)
                    and pages[page_cursor].global_end <= bounded_start
                ):
                    page_cursor += 1
                overlap_cursor = page_cursor
                while (
                    overlap_cursor < len(pages)
                    and pages[overlap_cursor].global_start < bounded_end
                ):
                    page = pages[overlap_cursor]
                    overlap_start = max(bounded_start, page.global_start)
                    overlap_end = min(bounded_end, page.global_end)
                    if overlap_end > overlap_start:
                        spans.append(
                            (
                                page.page_index,
                                overlap_start - page.global_start,
                                overlap_end - page.global_start,
                            )
                        )
                    overlap_cursor += 1
                if spans:
                    drafts.append(
                        SegmentDraft(
                            text=segment_text,
                            spans=tuple(spans),
                        )
                    )
    return drafts


def segment_paragraph_text(
    paragraphs: Sequence[ParagraphText],
) -> List[ParagraphSegmentDraft]:
    """Split EPUB paragraphs locally and retain exact paragraph-codepoint spans."""

    drafts: List[ParagraphSegmentDraft] = []
    for paragraph in paragraphs:
        for raw_start, raw_end in _raw_segment_ranges(paragraph.text):
            for start, end in _bounded_ranges(paragraph.text, raw_start, raw_end):
                while start < end and paragraph.text[start].isspace():
                    start += 1
                while end > start and paragraph.text[end - 1].isspace():
                    end -= 1
                if start >= end:
                    continue
                drafts.append(
                    ParagraphSegmentDraft(
                        text=paragraph.text[start:end],
                        spans=((
                            paragraph.paragraph_id,
                            paragraph.paragraph_index,
                            start,
                            end,
                        ),),
                    )
                )
    return drafts


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
    existing = connection.execute(
        "SELECT segment_set_id FROM segment_sets WHERE source_file_id = ? "
        "AND source_text_hash = ? AND segmenter = ? AND segmenter_version = ?",
        (source_id, text_hash, SEGMENTER, SEGMENTER_VERSION),
    ).fetchone()
    if existing is not None:
        segment_set_id = str(existing["segment_set_id"])
        segments = [
            (str(row["segment_id"]), str(row["text_raw"]))
            for row in connection.execute(
                "SELECT segment_id, text_raw FROM text_segments "
                "WHERE segment_set_id = ? ORDER BY order_index",
                (segment_set_id,),
            )
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
    connection.execute(
        "INSERT INTO segment_sets(segment_set_id, source_file_id, "
        "source_text_hash, segmenter, segmenter_version, language_code, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            segment_set_id,
            source_id,
            text_hash,
            SEGMENTER,
            SEGMENTER_VERSION,
            language_code or "und",
            _now(),
        ),
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
    connection.executemany(
        "INSERT INTO text_segments(segment_id, segment_set_id, order_index, text_raw) "
        "VALUES (?, ?, ?, ?)",
        segment_rows,
    )
    if page_span_rows:
        connection.executemany(
            "INSERT INTO text_segment_spans(segment_id, source_file_id, "
            "pdf_page_index, page_char_start, page_char_end, span_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            page_span_rows,
        )
    if paragraph_span_rows:
        connection.executemany(
            "INSERT INTO text_segment_paragraph_spans(segment_id, source_file_id, "
            "paragraph_id, paragraph_index, paragraph_char_start, "
            "paragraph_char_end, span_order) VALUES (?, ?, ?, ?, ?, ?, ?)",
            paragraph_span_rows,
        )
    return segment_set_id, segments


def _previous_segment_texts(
    connection: sqlite3.Connection,
    source_id: str,
    current_segment_set_id: str,
) -> Tuple[str, ...]:
    row = connection.execute(
        "SELECT segment_set_id FROM segment_sets "
        "WHERE source_file_id = ? AND segmenter = ? "
        "AND segment_set_id <> ? AND segmenter_version <> ? "
        "ORDER BY created_at DESC LIMIT 1",
        (source_id, SEGMENTER, current_segment_set_id, SEGMENTER_VERSION),
    ).fetchone()
    if row is None:
        return ()
    return tuple(
        str(segment["text_raw"])
        for segment in connection.execute(
            "SELECT text_raw FROM text_segments WHERE segment_set_id = ? "
            "ORDER BY order_index",
            (row["segment_set_id"],),
        )
    )


def _segment_set_language(
    connection: sqlite3.Connection, segment_set_id: str
) -> str:
    row = connection.execute(
        "SELECT language_code FROM segment_sets WHERE segment_set_id = ?",
        (segment_set_id,),
    ).fetchone()
    if row is None:
        raise TextAlignmentError("对齐所需的 Segment 集不存在。")
    return str(row["language_code"] or "und")


def align_segment_sequences(
    source_texts: Sequence[str],
    target_texts: Sequence[str],
    *,
    cache_dir: Path,
    embedding_provider: EmbeddingProvider | None = None,
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
    thresholds: AlignmentThresholds | None = None,
    reusable_sequences: Sequence[Sequence[str]] = (),
    folio_candidates: Sequence[FolioBoundaryCandidate] = (),
    source_language: str = "und",
    target_language: str = "und",
    reviewed_body_ranges: Dict[str, List[int]] | None = None,
) -> Tuple[List[SemanticLink], list]:
    """Return chapter-anchored semantic links and the anchors used."""

    active_thresholds = thresholds or embedding_model_config(
        embedding_model_id
    ).thresholds
    if embedding_provider is None:
        source_vectors, target_vectors = embed_text_sequences(
            [source_texts, target_texts],
            cache_dir,
            reusable_sequences=reusable_sequences,
            model_id=embedding_model_id,
        )
        embeddings = np.vstack([source_vectors, target_vectors])
    else:
        embeddings = embedding_provider([*source_texts, *target_texts], cache_dir)
        source_vectors = embeddings[: len(source_texts)]
        target_vectors = embeddings[len(source_texts) :]
    verified_folios = verify_folio_boundary_candidates(
        folio_candidates, source_vectors, target_vectors
    )
    source_start, source_end = (reviewed_body_ranges["pivot"] if reviewed_body_ranges is not None
                                else alignment_body_bounds(source_texts))
    target_start, target_end = (reviewed_body_ranges["target"] if reviewed_body_ranges is not None
                                else alignment_body_bounds(target_texts))
    aligned, anchors = align_semantic_sequences(
        source_texts[source_start:source_end],
        target_texts[target_start:target_end],
        np.vstack([
            source_vectors[source_start:source_end],
            target_vectors[target_start:target_end],
        ]),
        [
            HeadingAnchor(
                candidate.pivot_segment_index - source_start,
                candidate.target_segment_index - target_start,
                candidate.key,
            )
            for candidate in verified_folios
            if source_start <= candidate.pivot_segment_index < source_end
            and target_start <= candidate.target_segment_index < target_end
        ],
        source_language=source_language,
        target_language=target_language,
        thresholds=active_thresholds,
    )
    aligned = [
        replace(link,
                source_start=link.source_start + source_start,
                source_end=link.source_end + source_start,
                target_start=link.target_start + target_start,
                target_end=link.target_end + target_start)
        for link in aligned
    ]
    # Excluded segments remain inspectable as one-sided rejected rows. No
    # cross-book similarity exists for those rows, so confidence is zero.
    prefix = [
        SemanticLink(i, i + 1, 0, 0, 0.0, 0.0, "rejected")
        for i in range(source_start)
    ] + [
        SemanticLink(source_start, source_start, j, j + 1, 0.0, 0.0, "rejected")
        for j in range(target_start)
    ]
    suffix = [
        SemanticLink(i, i + 1, target_end, target_end, 0.0, 0.0, "rejected")
        for i in range(source_end, len(source_texts))
    ] + [
        SemanticLink(len(source_texts), len(source_texts), j, j + 1, 0.0, 0.0, "rejected")
        for j in range(target_end, len(target_texts))
    ]
    return prefix + aligned + suffix, [
        replace(anchor, source_index=anchor.source_index + source_start,
                target_index=anchor.target_index + target_start)
        for anchor in anchors
    ]


def _default_alignment_model_cache(db_path: Path) -> Path:
    index_path = Path(db_path).resolve()
    runtime_root = (
        index_path.parent.parent
        if index_path.parent.name.casefold() == "data"
        else index_path.parent
    )
    return runtime_root / "components" / "text-alignment" / "models"


def _require_pair(
    connection: sqlite3.Connection,
    document_group_id: str,
    pivot_source_id: str,
    target_source_id: str,
) -> None:
    group = connection.execute(
        "SELECT 1 FROM document_groups WHERE document_group_id = ?",
        (document_group_id,),
    ).fetchone()
    if group is None:
        raise InvalidAlignmentRequest("作品组不存在。")
    if pivot_source_id == target_source_id:
        raise InvalidAlignmentRequest("两个对齐版本不能相同。")
    member_ids = {
        str(row["source_file_id"])
        for row in connection.execute(
            "SELECT source_file_id FROM document_group_members "
            "WHERE document_group_id = ?",
            (document_group_id,),
        )
    }
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
    connection.execute(
        "UPDATE alignment_runs SET status = 'superseded' "
        "WHERE document_group_id = ? AND pivot_source_file_id = ? "
        "AND target_source_file_id = ? AND status = 'completed'",
        (document_group_id, pivot_source_id, target_source_id),
    )
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
    connection.execute(
        "INSERT INTO alignment_runs(alignment_run_id, document_group_id, "
        "pivot_source_file_id, target_source_file_id, pivot_segment_set_id, "
        "target_segment_set_id, algorithm, algorithm_version, parameters_json, "
        "status, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
    connection.executemany(
        "INSERT INTO alignment_links(alignment_link_id, alignment_run_id, "
        "order_index, cost, confidence, anchor_key, review_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        link_rows,
    )
    connection.executemany(
        "INSERT INTO alignment_link_members(alignment_link_id, side, "
        "segment_id, member_order) VALUES (?, ?, ?, ?)",
        member_rows,
    )
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
        connection = open_writable_index(Path(db_path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            install_text_alignment_schema(connection)
            _require_pair(connection, group_id, pivot_id, target_id)
            pivot_set_id, pivot_segments = _segment_set(connection, pivot_id)
            target_set_id, target_segments = _segment_set(connection, target_id)
            if reviewed_body_ranges is None:
                # Reuse reviewed annotations only for these exact segment sets.
                reviewed = connection.execute(
                    "SELECT parameters_json FROM alignment_runs WHERE pivot_segment_set_id=? "
                    "AND target_segment_set_id=? AND json_extract(parameters_json,'$.body_range_source')='reviewed' "
                    "ORDER BY created_at DESC LIMIT 1", (pivot_set_id, target_set_id),
                ).fetchone()
                if reviewed is not None:
                    reviewed_body_ranges = json.loads(reviewed[0])["body_ranges"]
            if reviewed_body_ranges is not None:
                for side, segments in (("pivot", pivot_segments), ("target", target_segments)):
                    bounds = reviewed_body_ranges.get(side)
                    if (not isinstance(bounds, list) or len(bounds) != 2
                            or not all(type(n) is int for n in bounds)
                            or not 0 <= bounds[0] < bounds[1] <= len(segments)):
                        raise InvalidAlignmentRequest("复核正文范围必须是有效的半开 Segment 区间。")
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
                candidates = connection.execute(
                    "SELECT alignment_run_id, parameters_json FROM alignment_runs "
                    "WHERE document_group_id = ? AND pivot_source_file_id = ? "
                    "AND target_source_file_id = ? AND pivot_segment_set_id = ? "
                    "AND target_segment_set_id = ? AND algorithm = ? "
                    "AND algorithm_version = ? AND status = 'completed' "
                    "AND NOT EXISTS (SELECT 1 FROM alignment_links l "
                    "WHERE l.alignment_run_id = alignment_runs.alignment_run_id "
                    "AND l.confidence IS NULL) "
                    "ORDER BY completed_at DESC, rowid DESC",
                    (
                        group_id,
                        pivot_id,
                        target_id,
                        pivot_set_id,
                        target_set_id,
                        ALIGNMENT_ALGORITHM,
                        ALIGNMENT_ALGORITHM_VERSION,
                    ),
                ).fetchall()
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
                counts = {
                    str(row["review_status"]): int(row["link_count"])
                    for row in connection.execute(
                        "SELECT review_status, COUNT(*) AS link_count "
                        "FROM alignment_links WHERE alignment_run_id = ? "
                        "GROUP BY review_status",
                        (existing["alignment_run_id"],),
                    )
                }
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
            connection.commit()
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            connection.rollback()
            raise
        finally:
            connection.close()

    computed = align_segment_sequences(
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
        connection = open_writable_index(Path(db_path))
        try:
            connection.execute("BEGIN IMMEDIATE")
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
            connection.commit()
            return result
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            connection.rollback()
            raise
        finally:
            connection.close()


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
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
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
        if not _table_exists(connection, "alignment_runs"):
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
            language_code = str(payload.get("language_code") or "").strip()
            if not language_code:
                language_code = _item_language_code(
                    "",
                    payload.get("title"),
                    payload.get("author"),
                    member["file_name"],
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
        return {"source_file_id": source_id, "targets": targets}
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
    body_range = _json_object(run["parameters_json"]).get("body_ranges", {}).get(source_side)
    if body_range is not None:
        selected_orders = connection.execute(
            "SELECT order_index FROM text_segments WHERE segment_id IN ("
            + ",".join("?" for _ in source_segments) + ")",
            tuple(source_segments),
        ).fetchall()
        if any(not body_range[0] <= row[0] < body_range[1] for row in selected_orders):
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


def _lookup_confirmed_override(
    connection: sqlite3.Connection,
    source_id: str,
    target_id: str,
    source_set_id: str,
    target_set_id: str,
    source_segments: Sequence[str],
) -> Dict[str, object] | None:
    """Return the human-confirmed target segments for this selection, if any."""

    if not _table_exists(connection, "alignment_manual_overrides"):
        return None
    row = connection.execute(
        "SELECT override_id, target_segment_ids_json, target_segment_set_id "
        "FROM alignment_manual_overrides WHERE source_file_id = ? "
        "AND target_source_file_id = ? AND source_segment_set_id = ? "
        "AND source_segment_key = ? AND status = 'confirmed' LIMIT 1",
        (source_id, target_id, source_set_id, _segment_key(source_segments)),
    ).fetchone()
    if row is None:
        return None
    # A re-alignment or re-segmentation would move the confirmed target to a new
    # segment set; treat the stored correction as stale rather than mapping to
    # segments the current alignment no longer uses.
    if str(row["target_segment_set_id"]) != target_set_id:
        return None
    stored_ids = json.loads(str(row["target_segment_ids_json"] or "[]"))
    ordered = _ordered_segments_in_set(connection, target_set_id, stored_ids)
    if len(ordered) != len({str(item) for item in stored_ids}):
        return None
    return {
        "override_id": str(row["override_id"]),
        "target_segment_ids": [str(item["segment_id"]) for item in ordered],
    }












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

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
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
        override = _lookup_confirmed_override(
            connection,
            source_id,
            target_id,
            source_set_id,
            target_set_id,
            source_segments,
        )
        if override is not None:
            target_segment_ids = override["target_segment_ids"]
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







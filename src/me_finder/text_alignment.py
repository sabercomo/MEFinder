"""Persisted PDF text segments and monotonic two-document alignment."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from .calibration_library import _item_language_code
from .document_group_metadata import member_display_name
from .pdf_extractors import attach_page_block_offsets, pdf_page_text_hash
from .persistence.connection import open_writable_index


SEGMENTER = "me-finder-multilingual-sentence"
SEGMENTER_VERSION = "1"
ALIGNMENT_ALGORITHM = "monotonic-length-dp"
ALIGNMENT_ALGORITHM_VERSION = "1"
MAX_SEGMENT_LENGTH = 1200
_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SENTENCE_ENDINGS = frozenset("。！？!?；;")
_CLOSING_PUNCTUATION = frozenset("”’\"'）)]】》〉」』")
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
_TRANSITIONS: Tuple[Tuple[int, int, float], ...] = (
    (1, 1, 0.0),
    (1, 2, 0.55),
    (2, 1, 0.55),
    (2, 2, 0.85),
    (1, 3, 1.1),
    (3, 1, 1.1),
    (2, 3, 1.25),
    (3, 2, 1.25),
    (3, 3, 1.45),
    (1, 0, 4.5),
    (0, 1, 4.5),
)


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
class SegmentDraft:
    text: str
    spans: Tuple[Tuple[int, int, int], ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone() is not None


def install_text_alignment_schema(connection: sqlite3.Connection) -> bool:
    """Install the additive v4 segmentation/alignment tables."""

    if _table_exists(connection, "segment_sets"):
        return False
    statements = (
        """
        CREATE TABLE segment_sets (
            segment_set_id TEXT PRIMARY KEY,
            source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id) ON DELETE CASCADE,
            source_text_hash TEXT NOT NULL,
            segmenter TEXT NOT NULL,
            segmenter_version TEXT NOT NULL,
            language_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_file_id, source_text_hash, segmenter, segmenter_version)
        )
        """,
        """
        CREATE TABLE text_segments (
            segment_id TEXT PRIMARY KEY,
            segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
            order_index INTEGER NOT NULL,
            text_raw TEXT NOT NULL,
            UNIQUE(segment_set_id, order_index)
        )
        """,
        """
        CREATE TABLE text_segment_spans (
            segment_id TEXT NOT NULL REFERENCES text_segments(segment_id) ON DELETE CASCADE,
            source_file_id TEXT NOT NULL,
            pdf_page_index INTEGER NOT NULL,
            page_char_start INTEGER NOT NULL,
            page_char_end INTEGER NOT NULL,
            span_order INTEGER NOT NULL,
            PRIMARY KEY(segment_id, span_order)
        )
        """,
        """
        CREATE TABLE alignment_runs (
            alignment_run_id TEXT PRIMARY KEY,
            document_group_id TEXT NOT NULL REFERENCES document_groups(document_group_id) ON DELETE CASCADE,
            pivot_source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id) ON DELETE CASCADE,
            target_source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id) ON DELETE CASCADE,
            pivot_segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
            target_segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
            algorithm TEXT NOT NULL,
            algorithm_version TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """,
        """
        CREATE TABLE alignment_links (
            alignment_link_id TEXT PRIMARY KEY,
            alignment_run_id TEXT NOT NULL REFERENCES alignment_runs(alignment_run_id) ON DELETE CASCADE,
            order_index INTEGER NOT NULL,
            cost REAL NOT NULL,
            review_status TEXT NOT NULL,
            UNIQUE(alignment_run_id, order_index)
        )
        """,
        """
        CREATE TABLE alignment_link_members (
            alignment_link_id TEXT NOT NULL REFERENCES alignment_links(alignment_link_id) ON DELETE CASCADE,
            side TEXT NOT NULL CHECK(side IN ('pivot', 'target')),
            segment_id TEXT NOT NULL REFERENCES text_segments(segment_id) ON DELETE CASCADE,
            member_order INTEGER NOT NULL,
            PRIMARY KEY(alignment_link_id, side, member_order),
            UNIQUE(alignment_link_id, segment_id)
        )
        """,
        "CREATE INDEX idx_segment_sets_source ON segment_sets(source_file_id)",
        "CREATE INDEX idx_segment_spans_source_page ON text_segment_spans(source_file_id, pdf_page_index, page_char_start, page_char_end)",
        "CREATE INDEX idx_alignment_runs_pair ON alignment_runs(document_group_id, pivot_source_file_id, target_source_file_id, status)",
        "CREATE INDEX idx_alignment_members_segment ON alignment_link_members(segment_id, side)",
    )
    for statement in statements:
        connection.execute(statement)
    return True


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
    if str(row["source_type"] or "").casefold() != "pdf":
        raise InvalidAlignmentRequest("0.4.9 的自动对齐只支持 PDF 文献。")
    return row


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
        if pieces:
            pieces.append("\n")
            cursor += 1
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


def _source_text_hash(pages: Sequence[PageText]) -> str:
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


def _period_ends_sentence(text: str, index: int) -> bool:
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    if before.isdigit() and after.isdigit():
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
    for raw_start, raw_end in _raw_segment_ranges(full_text):
        for bounded_start, bounded_end in _bounded_ranges(
            full_text, raw_start, raw_end
        ):
            while bounded_start < bounded_end and full_text[bounded_start].isspace():
                bounded_start += 1
            while bounded_end > bounded_start and full_text[bounded_end - 1].isspace():
                bounded_end -= 1
            if bounded_start >= bounded_end:
                continue
            spans: List[Tuple[int, int, int]] = []
            for page in pages:
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
            if spans:
                drafts.append(
                    SegmentDraft(
                        text=full_text[bounded_start:bounded_end],
                        spans=tuple(spans),
                    )
                )
    return drafts


def _segment_set(
    connection: sqlite3.Connection, source_id: str
) -> Tuple[str, List[Tuple[str, str]]]:
    source = _source_row(connection, source_id)
    full_text, pages = _load_pages(connection, source_id)
    if not full_text.strip():
        raise InvalidAlignmentRequest("文献没有可用于对齐的 PDF 文本。")
    text_hash = _source_text_hash(pages)
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

    connection.execute(
        "DELETE FROM segment_sets WHERE source_file_id = ?",
        (source_id,),
    )
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
    for order_index, draft in enumerate(segment_pdf_text(full_text, pages)):
        segment_digest = hashlib.sha256(
            f"{segment_set_id}\0{order_index}\0{draft.text}".encode("utf-8")
        ).hexdigest()[:24]
        segment_id = f"segment-{segment_digest}"
        connection.execute(
            "INSERT INTO text_segments(segment_id, segment_set_id, order_index, text_raw) "
            "VALUES (?, ?, ?, ?)",
            (segment_id, segment_set_id, order_index, draft.text),
        )
        connection.executemany(
            "INSERT INTO text_segment_spans(segment_id, source_file_id, "
            "pdf_page_index, page_char_start, page_char_end, span_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (segment_id, source_id, page, start, end, span_order)
                for span_order, (page, start, end) in enumerate(draft.spans)
            ],
        )
        segments.append((segment_id, draft.text))
    if not segments:
        raise InvalidAlignmentRequest("文献没有可用于对齐的 Segment。")
    return segment_set_id, segments


def _effective_length(text: str) -> int:
    return max(1, sum(not character.isspace() for character in text))


def _transition_cost(
    source_lengths: Sequence[int],
    target_lengths: Sequence[int],
    source_start: int,
    target_start: int,
    source_count: int,
    target_count: int,
    ratio: float,
    penalty: float,
) -> float:
    source_length = sum(
        source_lengths[source_start : source_start + source_count]
    )
    target_length = sum(
        target_lengths[target_start : target_start + target_count]
    )
    if source_count == 0 or target_count == 0:
        return penalty + math.log1p(source_length + target_length) / 2.0
    expected = max(ratio * source_length, 1.0)
    length_cost = abs(target_length - expected) / math.sqrt(
        target_length + expected
    )
    return penalty + length_cost


def align_segment_sequences(
    source_texts: Sequence[str], target_texts: Sequence[str]
) -> List[Tuple[int, int, int, int, float]]:
    """Return monotonic 0..3-by-0..3 links using a bounded length DP."""

    source_lengths = [_effective_length(text) for text in source_texts]
    target_lengths = [_effective_length(text) for text in target_texts]
    source_count = len(source_lengths)
    target_count = len(target_lengths)
    if not source_count or not target_count:
        raise InvalidAlignmentRequest("两本文献都必须至少包含一个 Segment。")
    ratio = sum(target_lengths) / max(sum(source_lengths), 1)
    full_matrix = source_count * target_count <= 400_000
    band = max(96, abs(target_count - source_count) // 20)
    row_bounds: List[Tuple[int, int]] = []
    back_rows: List[bytearray] = []
    recent_costs: Dict[int, Tuple[int, List[float]]] = {}

    def bounds(source_index: int) -> Tuple[int, int]:
        if full_matrix:
            return 0, target_count
        expected = round(source_index * target_count / source_count)
        return max(0, expected - band), min(target_count, expected + band)

    for source_index in range(source_count + 1):
        row_start, row_end = bounds(source_index)
        row_bounds.append((row_start, row_end))
        costs = [math.inf] * (row_end - row_start + 1)
        backs = bytearray([255]) * len(costs)
        if source_index == 0 and row_start == 0:
            costs[0] = 0.0
        recent_costs[source_index] = (row_start, costs)
        for target_index in range(row_start, row_end + 1):
            cell_offset = target_index - row_start
            if source_index == 0 and target_index == 0:
                continue
            best = math.inf
            best_transition = 255
            for transition_index, (di, dj, penalty) in enumerate(_TRANSITIONS):
                previous_source = source_index - di
                previous_target = target_index - dj
                if previous_source < 0 or previous_target < 0:
                    continue
                previous_row = recent_costs.get(previous_source)
                if previous_row is None:
                    continue
                previous_start, previous_costs = previous_row
                previous_offset = previous_target - previous_start
                if previous_offset < 0 or previous_offset >= len(previous_costs):
                    continue
                previous_cost = previous_costs[previous_offset]
                if math.isinf(previous_cost):
                    continue
                candidate = previous_cost + _transition_cost(
                    source_lengths,
                    target_lengths,
                    previous_source,
                    previous_target,
                    di,
                    dj,
                    ratio,
                    penalty,
                )
                if candidate < best:
                    best = candidate
                    best_transition = transition_index
            costs[cell_offset] = best
            backs[cell_offset] = best_transition
        back_rows.append(backs)
        for expired in tuple(recent_costs):
            if expired < source_index - 3:
                del recent_costs[expired]

    destination_start, destination_costs = recent_costs[source_count]
    destination_offset = target_count - destination_start
    if (
        destination_offset < 0
        or destination_offset >= len(destination_costs)
        or math.isinf(destination_costs[destination_offset])
    ):
        raise TextAlignmentError("对齐路径超出单调搜索带。")

    links: List[Tuple[int, int, int, int, float]] = []
    source_index = source_count
    target_index = target_count
    while source_index or target_index:
        row_start, _row_end = row_bounds[source_index]
        transition_index = back_rows[source_index][target_index - row_start]
        if transition_index == 255:
            raise TextAlignmentError("无法回溯完整的对齐路径。")
        di, dj, penalty = _TRANSITIONS[transition_index]
        previous_source = source_index - di
        previous_target = target_index - dj
        cost = _transition_cost(
            source_lengths,
            target_lengths,
            previous_source,
            previous_target,
            di,
            dj,
            ratio,
            penalty,
        )
        links.append(
            (previous_source, source_index, previous_target, target_index, cost)
        )
        source_index = previous_source
        target_index = previous_target
    links.reverse()
    return links


def _require_pair(
    connection: sqlite3.Connection,
    document_group_id: str,
    pivot_source_id: str,
    target_source_id: str,
) -> None:
    group = connection.execute(
        "SELECT base_source_file_id FROM document_groups WHERE document_group_id = ?",
        (document_group_id,),
    ).fetchone()
    if group is None:
        raise InvalidAlignmentRequest("作品组不存在。")
    if str(group["base_source_file_id"] or "") != pivot_source_id:
        raise InvalidAlignmentRequest("请先把基准文献设为作品组的基准版本。")
    if pivot_source_id == target_source_id:
        raise InvalidAlignmentRequest("基准版本和目标版本不能相同。")
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
) -> Dict[str, object]:
    _require_pair(
        connection, document_group_id, pivot_source_id, target_source_id
    )
    pivot_set_id, pivot_segments = _segment_set(connection, pivot_source_id)
    target_set_id, target_segments = _segment_set(connection, target_source_id)
    aligned = align_segment_sequences(
        [text for _segment_id, text in pivot_segments],
        [text for _segment_id, text in target_segments],
    )
    connection.execute(
        "DELETE FROM alignment_runs WHERE document_group_id = ? "
        "AND pivot_source_file_id = ? AND target_source_file_id = ?",
        (document_group_id, pivot_source_id, target_source_id),
    )
    run_id = f"alignment-run-{uuid.uuid4().hex}"
    timestamp = _now()
    parameters = {
        "transitions": [[di, dj] for di, dj, _penalty in _TRANSITIONS],
        "length_unit": "non_whitespace_unicode_codepoint",
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
    for order_index, (ps, pe, ts, te, cost) in enumerate(aligned):
        link_id = f"alignment-link-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO alignment_links(alignment_link_id, alignment_run_id, "
            "order_index, cost, review_status) VALUES (?, ?, ?, ?, ?)",
            (link_id, run_id, order_index, round(cost, 6), "automatic"),
        )
        connection.executemany(
            "INSERT INTO alignment_link_members(alignment_link_id, side, "
            "segment_id, member_order) VALUES (?, ?, ?, ?)",
            [
                (link_id, "pivot", pivot_segments[index][0], index - ps)
                for index in range(ps, pe)
            ]
            + [
                (link_id, "target", target_segments[index][0], index - ts)
                for index in range(ts, te)
            ],
        )
    return {
        "alignment_run_id": run_id,
        "document_group_id": document_group_id,
        "pivot_source_file_id": pivot_source_id,
        "target_source_file_id": target_source_id,
        "pivot_segment_count": len(pivot_segments),
        "target_segment_count": len(target_segments),
        "alignment_link_count": len(aligned),
        "algorithm": ALIGNMENT_ALGORITHM,
        "algorithm_version": ALIGNMENT_ALGORITHM_VERSION,
        "status": "completed",
    }


def generate_alignment(
    db_path: Path,
    document_group_id: object,
    pivot_source_file_id: object,
    target_source_file_id: object,
) -> Dict[str, object]:
    group_id = str(document_group_id or "").strip()
    if not group_id:
        raise InvalidAlignmentRequest("document_group_id is required")
    pivot_id = _validate_source_id(pivot_source_file_id)
    target_id = _validate_source_id(target_source_file_id)
    connection = open_writable_index(Path(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        install_text_alignment_schema(connection)
        result = _generate_alignment_on_connection(
            connection, group_id, pivot_id, target_id
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_alignment_targets(db_path: Path, source_file_id: object) -> Dict[str, object]:
    source_id = _validate_source_id(source_file_id)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        source = connection.execute(
            "SELECT source_type FROM source_files WHERE source_file_id = ?",
            (source_id,),
        ).fetchone()
        if source is None:
            raise InvalidAlignmentRequest("文献不存在。")
        if str(source["source_type"] or "").casefold() != "pdf":
            return {"source_file_id": source_id, "targets": []}
        if not _table_exists(connection, "alignment_runs"):
            return {"source_file_id": source_id, "targets": []}
        rows = connection.execute(
            "SELECT r.alignment_run_id, r.pivot_source_file_id, "
            "r.target_source_file_id, r.algorithm, r.algorithm_version, "
            "m.version_label, s.file_name, s.payload_json "
            "FROM alignment_runs r "
            "JOIN document_group_members m ON m.document_group_id = r.document_group_id "
            "AND m.source_file_id = CASE WHEN r.pivot_source_file_id = ? "
            "THEN r.target_source_file_id ELSE r.pivot_source_file_id END "
            "JOIN source_files s ON s.source_file_id = m.source_file_id "
            "WHERE r.status = 'completed' AND "
            "(r.pivot_source_file_id = ? OR r.target_source_file_id = ?) "
            "ORDER BY m.member_order",
            (source_id, source_id, source_id),
        ).fetchall()
        targets: List[Dict[str, object]] = []
        for row in rows:
            target_id = (
                str(row["target_source_file_id"])
                if str(row["pivot_source_file_id"]) == source_id
                else str(row["pivot_source_file_id"])
            )
            payload = _json_object(row["payload_json"])
            payload.setdefault("source_file_id", target_id)
            payload.setdefault("file_name", row["file_name"])
            targets.append(
                {
                    "source_file_id": target_id,
                    "display_name": member_display_name(
                        row["version_label"], payload
                    ),
                    "alignment_run_id": row["alignment_run_id"],
                    "algorithm": row["algorithm"],
                    "algorithm_version": row["algorithm_version"],
                }
            )
        return {"source_file_id": source_id, "targets": targets}
    finally:
        connection.close()


def _selection_segment_ids(
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


def locate_alignment(
    db_path: Path,
    source_file_id: object,
    target_source_file_id: object,
    *,
    start_page_index: object,
    end_page_index: object,
    start_offset: object,
    end_offset: object,
) -> Dict[str, object]:
    source_id = _validate_source_id(source_file_id)
    target_id = _validate_source_id(target_source_file_id)
    start_page = _validate_nonnegative_integer("start_page_index", start_page_index)
    end_page = _validate_nonnegative_integer("end_page_index", end_page_index)
    first_offset = _validate_nonnegative_integer("start_offset", start_offset)
    last_offset = _validate_nonnegative_integer("end_offset", end_offset)
    if source_id == target_id:
        raise InvalidAlignmentRequest("源版本和目标版本不能相同。")
    if end_page < start_page or (end_page == start_page and last_offset <= first_offset):
        raise InvalidAlignmentRequest("选区范围无效。")

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        _source_row(connection, source_id)
        endpoint_indices = [start_page]
        if end_page != start_page:
            endpoint_indices.append(end_page)
        endpoint_placeholders = ",".join("?" for _ in endpoint_indices)
        endpoint_rows = connection.execute(
            "SELECT pdf_page_index, payload_json FROM pdf_pages "
            f"WHERE source_file_id = ? AND pdf_page_index IN ({endpoint_placeholders})",
            (source_id, *endpoint_indices),
        ).fetchall()
        endpoint_payloads = {
            int(row["pdf_page_index"]): _json_object(row["payload_json"])
            for row in endpoint_rows
        }
        if set(endpoint_payloads) != set(endpoint_indices):
            raise InvalidAlignmentRequest("选区所在的 PDF 页不存在。")
        if first_offset > len(str(endpoint_payloads[start_page].get("text_raw") or "")):
            raise InvalidAlignmentRequest("start_offset 超出页文本范围。")
        if last_offset > len(str(endpoint_payloads[end_page].get("text_raw") or "")):
            raise InvalidAlignmentRequest("end_offset 超出页文本范围。")
        run = connection.execute(
            "SELECT * FROM alignment_runs WHERE status = 'completed' AND "
            "((pivot_source_file_id = ? AND target_source_file_id = ?) OR "
            "(pivot_source_file_id = ? AND target_source_file_id = ?)) "
            "ORDER BY completed_at DESC LIMIT 1",
            (source_id, target_id, target_id, source_id),
        ).fetchone()
        if run is None:
            raise AlignmentNotFound("这两个版本还没有可用的自动对齐。")
        source_is_pivot = str(run["pivot_source_file_id"]) == source_id
        source_side = "pivot" if source_is_pivot else "target"
        target_side = "target" if source_is_pivot else "pivot"
        source_set_id = str(
            run["pivot_segment_set_id"]
            if source_is_pivot
            else run["target_segment_set_id"]
        )
        source_segments = _selection_segment_ids(
            connection,
            source_set_id,
            source_id,
            start_page,
            end_page,
            first_offset,
            last_offset,
        )
        if not source_segments:
            raise AlignmentNotFound("所选文字没有落入可对齐的 Segment。")
        placeholders = ",".join("?" for _ in source_segments)
        link_rows = connection.execute(
            "SELECT DISTINCT l.alignment_link_id, l.order_index "
            "FROM alignment_links l JOIN alignment_link_members m "
            "ON m.alignment_link_id = l.alignment_link_id "
            f"WHERE l.alignment_run_id = ? AND m.side = ? AND m.segment_id IN ({placeholders}) "
            "ORDER BY l.order_index",
            (run["alignment_run_id"], source_side, *source_segments),
        ).fetchall()
        if not link_rows:
            raise AlignmentNotFound("所选 Segment 没有对应的译文。")
        link_ids = [str(row["alignment_link_id"]) for row in link_rows]
        link_placeholders = ",".join("?" for _ in link_ids)
        target_segments = connection.execute(
            "SELECT DISTINCT s.segment_id, s.order_index FROM text_segments s "
            "JOIN alignment_link_members m ON m.segment_id = s.segment_id "
            "JOIN alignment_links l ON l.alignment_link_id = m.alignment_link_id "
            f"WHERE m.side = ? AND l.alignment_link_id IN ({link_placeholders}) "
            "ORDER BY s.order_index",
            (target_side, *link_ids),
        ).fetchall()
        if not target_segments:
            raise AlignmentNotFound("所选 Segment 对应的是一个空译文区间。")
        target_segment_ids = [str(row["segment_id"]) for row in target_segments]
        segment_placeholders = ",".join("?" for _ in target_segment_ids)
        span_rows = connection.execute(
            "SELECT p.pdf_page_index, p.page_char_start, p.page_char_end, "
            "s.order_index, p.span_order FROM text_segment_spans p "
            "JOIN text_segments s ON s.segment_id = p.segment_id "
            f"WHERE p.segment_id IN ({segment_placeholders}) "
            "ORDER BY s.order_index, p.span_order",
            target_segment_ids,
        ).fetchall()
        page_indices = sorted({int(row["pdf_page_index"]) for row in span_rows})
        page_placeholders = ",".join("?" for _ in page_indices)
        page_rows = connection.execute(
            "SELECT pdf_page_index, payload_json FROM pdf_pages "
            f"WHERE source_file_id = ? AND pdf_page_index IN ({page_placeholders})",
            (target_id, *page_indices),
        ).fetchall()
        page_payloads = {
            int(row["pdf_page_index"]): _json_object(row["payload_json"])
            for row in page_rows
        }
        page_spans = _merge_page_spans(span_rows, page_payloads)
        target_source = _source_row(connection, target_id)
        target_payload = _json_object(target_source["payload_json"])
        title = str(
            target_payload.get("title")
            or target_payload.get("document_title")
            or target_source["file_name"]
            or target_id
        )
        return {
            "alignment_run_id": run["alignment_run_id"],
            "algorithm": run["algorithm"],
            "algorithm_version": run["algorithm_version"],
            "source_file_id": source_id,
            "target_source_file_id": target_id,
            "target_title": title,
            "target_index": page_indices[0],
            "page_match_spans": page_spans,
            "bbox_refs": _bbox_refs(page_spans, page_payloads),
            "match_offset_unit": "unicode_codepoint",
            "precise_highlight_available": True,
        }
    finally:
        connection.close()


def read_alignment_recipe_snapshot(db_path: Path) -> Dict[str, list]:
    path = Path(db_path)
    if not path.is_file():
        return {"alignment_pairs": []}
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            return {"alignment_pairs": []}
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        if not _table_exists(connection, "alignment_runs"):
            return {"alignment_pairs": []}
        return {
            "alignment_pairs": [
                {
                    "document_group_id": row["document_group_id"],
                    "pivot_source_file_id": row["pivot_source_file_id"],
                    "target_source_file_id": row["target_source_file_id"],
                    "algorithm": row["algorithm"],
                    "algorithm_version": row["algorithm_version"],
                }
                for row in connection.execute(
                    "SELECT document_group_id, pivot_source_file_id, "
                    "target_source_file_id, algorithm, algorithm_version "
                    "FROM alignment_runs WHERE status = 'completed' "
                    "ORDER BY document_group_id, pivot_source_file_id, target_source_file_id"
                )
            ]
        }
    finally:
        connection.close()


def restore_alignment_recipe_snapshot(
    connection: sqlite3.Connection, snapshot: Mapping[str, object]
) -> int:
    install_text_alignment_schema(connection)
    restored = 0
    for pair in snapshot.get("alignment_pairs", []):
        if not isinstance(pair, Mapping):
            continue
        if (
            pair.get("algorithm") != ALIGNMENT_ALGORITHM
            or pair.get("algorithm_version") != ALIGNMENT_ALGORITHM_VERSION
        ):
            continue
        group_id = str(pair.get("document_group_id") or "")
        pivot_id = str(pair.get("pivot_source_file_id") or "")
        target_id = str(pair.get("target_source_file_id") or "")
        present = connection.execute(
            "SELECT COUNT(*) FROM source_files WHERE source_file_id IN (?, ?)",
            (pivot_id, target_id),
        ).fetchone()[0]
        group_present = connection.execute(
            "SELECT 1 FROM document_groups WHERE document_group_id = ?",
            (group_id,),
        ).fetchone()
        if present != 2 or group_present is None:
            continue
        _generate_alignment_on_connection(
            connection, group_id, pivot_id, target_id
        )
        restored += 1
    return restored


def replace_alignment_recipe_snapshot(
    snapshot: Mapping[str, object], db_path: Path
) -> int:
    connection = open_writable_index(Path(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        install_text_alignment_schema(connection)
        connection.execute("DELETE FROM alignment_runs")
        restored = restore_alignment_recipe_snapshot(connection, snapshot)
        connection.commit()
        return restored
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

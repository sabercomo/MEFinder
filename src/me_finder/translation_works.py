"""Read models and small writes behind the translation-comparison workspace.

The workspace presents a DocumentGroup as a "work" with versions. Everything
here is either a read over existing alignment data (pair status overview, the
per-link window the reader draws, correction candidates) or a small additive
write into the v7 tables (reading position, review deferrals, dismissed
same-title suggestions). Alignment results themselves are never recomputed or
rewritten: human corrections go through ``alignment_overrides``.

Status facts are deliberately narrow. ``matched_segment_ratio`` counts base
segments whose link was accepted automatically; it says nothing about whether
that correspondence is correct, and callers must not present it as accuracy.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from .alignment_overrides import confirm_override, create_override_proposal
from .alignment_regions import alignment_body_bounds
from .persistence.connection import open_writable_index, table_exists
from .persistence.schema_installers import (
    install_document_group_schema,
    install_text_alignment_schema,
    install_translation_workspace_schema,
)
from .text_alignment import (
    ALIGNMENT_ALGORITHM,
    ALIGNMENT_ALGORITHM_VERSION,
    READABLE_ALIGNMENT_VERSIONS,
    AlignmentNotFound,
    InvalidAlignmentRequest,
    WriteWindow,
    _alignment_candidate_segments,
    _json_object,
    _latest_pair_run,
    _now,
    _ordered_segments_in_set,
    _resolve_alignment_route,
    _segment_key,
    _segment_set_id_for_source,
    _source_kind,
    _source_row,
    _validate_nonnegative_integer,
    _validate_source_id,
    confirmed_overrides_for_pair,
    override_for_selection,
)

UNMATCHED_STATUSES = frozenset({"rejected", "unmatched"})
MAX_LINK_WINDOW_ITEMS = 400
MAX_CANDIDATE_RADIUS = 5


def _read_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def _write(db_path: Path, write_window: WriteWindow | None, operation):
    transaction_window = write_window or nullcontext
    with transaction_window():
        connection = open_writable_index(Path(db_path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            install_document_group_schema(connection)
            install_text_alignment_schema(connection)
            install_translation_workspace_schema(connection)
            result = operation(connection)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _clean_ids(values: object, *, name: str, allow_empty: bool = False) -> List[str]:
    if not isinstance(values, (list, tuple)):
        raise InvalidAlignmentRequest(f"{name} 必须是数组。")
    cleaned = list(dict.fromkeys(str(value or "").strip() for value in values))
    if any(not value for value in cleaned):
        raise InvalidAlignmentRequest(f"{name} 不能包含空值。")
    if not cleaned and not allow_empty:
        raise InvalidAlignmentRequest(f"{name} 不能为空。")
    if len(cleaned) > 200:
        raise InvalidAlignmentRequest(f"{name} 不能超过 200 项。")
    return cleaned


# ── reading position ────────────────────────────────────────────────────────


def read_reading_position(db_path: Path, document_group_id: object) -> Dict[str, object]:
    group_id = str(document_group_id or "").strip()
    if not group_id:
        raise InvalidAlignmentRequest("document_group_id 必填。")
    connection = _read_connection(db_path)
    try:
        if not table_exists(connection, "document_group_reading_positions"):
            return {"document_group_id": group_id, "position": None}
        row = connection.execute(
            "SELECT left_source_file_id, right_source_file_id, item_index, "
            "char_offset, updated_at FROM document_group_reading_positions "
            "WHERE document_group_id = ?",
            (group_id,),
        ).fetchone()
        return {
            "document_group_id": group_id,
            "position": None if row is None else dict(row),
        }
    finally:
        connection.close()


def save_reading_position(
    db_path: Path,
    document_group_id: object,
    left_source_file_id: object,
    right_source_file_id: object,
    item_index: object,
    char_offset: object = 0,
    *,
    write_window: WriteWindow | None = None,
) -> Dict[str, object]:
    group_id = str(document_group_id or "").strip()
    if not group_id:
        raise InvalidAlignmentRequest("document_group_id 必填。")
    left_id = _validate_source_id(left_source_file_id)
    right_id = (
        _validate_source_id(right_source_file_id)
        if right_source_file_id not in (None, "")
        else None
    )
    if right_id == left_id:
        raise InvalidAlignmentRequest("左右两栏不能是同一个版本。")
    index = _validate_nonnegative_integer("item_index", item_index)
    offset = _validate_nonnegative_integer("char_offset", char_offset)
    timestamp = _now()

    def operation(connection: sqlite3.Connection) -> Dict[str, object]:
        members = {
            str(row[0])
            for row in connection.execute(
                "SELECT source_file_id FROM document_group_members "
                "WHERE document_group_id = ?",
                (group_id,),
            )
        }
        if left_id not in members or (right_id is not None and right_id not in members):
            raise InvalidAlignmentRequest("阅读位置中的版本不属于该作品。")
        connection.execute(
            "INSERT INTO document_group_reading_positions(document_group_id, "
            "left_source_file_id, right_source_file_id, item_index, char_offset, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(document_group_id) DO UPDATE SET "
            "left_source_file_id = excluded.left_source_file_id, "
            "right_source_file_id = excluded.right_source_file_id, "
            "item_index = excluded.item_index, char_offset = excluded.char_offset, "
            "updated_at = excluded.updated_at",
            (group_id, left_id, right_id, index, offset, timestamp),
        )
        return {
            "document_group_id": group_id,
            "left_source_file_id": left_id,
            "right_source_file_id": right_id,
            "item_index": index,
            "char_offset": offset,
            "updated_at": timestamp,
        }

    return _write(db_path, write_window, operation)


# ── same-title suggestion dismissals ───────────────────────────────────────


def suggestion_key(source_file_ids: Sequence[str]) -> str:
    return "\n".join(sorted({str(value) for value in source_file_ids}))


def list_suggestion_dismissals(db_path: Path) -> Dict[str, object]:
    connection = _read_connection(db_path)
    try:
        if not table_exists(connection, "document_group_suggestion_dismissals"):
            return {"dismissals": []}
        rows = connection.execute(
            "SELECT source_file_ids_json FROM document_group_suggestion_dismissals "
            "ORDER BY created_at"
        ).fetchall()
        return {
            "dismissals": [
                sorted(str(value) for value in json.loads(row[0] or "[]"))
                for row in rows
            ]
        }
    finally:
        connection.close()


def dismiss_suggestion(
    db_path: Path,
    source_file_ids: object,
    *,
    write_window: WriteWindow | None = None,
) -> Dict[str, object]:
    ids = sorted(_clean_ids(source_file_ids, name="source_file_ids"))
    if len(ids) < 2:
        raise InvalidAlignmentRequest("同名建议至少包含两份文献。")
    key = suggestion_key(ids)

    def operation(connection: sqlite3.Connection) -> Dict[str, object]:
        connection.execute(
            "INSERT OR IGNORE INTO document_group_suggestion_dismissals("
            "suggestion_key, source_file_ids_json, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(ids, ensure_ascii=False), _now()),
        )
        return {"source_file_ids": ids}

    return _write(db_path, write_window, operation)


# ── pair status overview ───────────────────────────────────────────────────


# Segment sets are immutable, so detected bounds are cached per set id.
_DETECTED_BOUNDS_CACHE: Dict[str, Tuple[int, int]] = {}
_DETECTED_BOUNDS_CACHE_LIMIT = 256


def _detected_body_bounds(
    connection: sqlite3.Connection, segment_set_id: str
) -> Tuple[int, int]:
    bounds = _DETECTED_BOUNDS_CACHE.get(segment_set_id)
    if bounds is None:
        bounds = alignment_body_bounds([
            str(row[0])
            for row in connection.execute(
                "SELECT text_raw FROM text_segments WHERE segment_set_id = ? "
                "ORDER BY order_index",
                (segment_set_id,),
            )
        ])
        if len(_DETECTED_BOUNDS_CACHE) >= _DETECTED_BOUNDS_CACHE_LIMIT:
            _DETECTED_BOUNDS_CACHE.clear()
        _DETECTED_BOUNDS_CACHE[segment_set_id] = bounds
    return bounds


def _body_range_changed(
    connection: sqlite3.Connection, run: Mapping[str, object], parameters: Mapping[str, object]
) -> bool:
    """True when a detected body range no longer matches current detection."""
    if parameters.get("body_range_source") != "detected":
        return False
    stored = parameters.get("body_ranges")
    if not isinstance(stored, dict):
        return False
    for side in ("pivot", "target"):
        bounds = stored.get(side)
        set_id = run[side + "_segment_set_id"]
        if isinstance(bounds, list) and set_id and (
            list(_detected_body_bounds(connection, str(set_id))) != bounds
        ):
            return True
    return False


def _run_staleness(
    run: Mapping[str, object],
    active_model_id: str,
    connection: sqlite3.Connection | None = None,
) -> str | None:
    if (
        run["algorithm"] != ALIGNMENT_ALGORITHM
        or run["algorithm_version"] not in READABLE_ALIGNMENT_VERSIONS
    ):
        return "algorithm_unreadable"
    parameters = _json_object(run["parameters_json"])
    if active_model_id and parameters.get("embedding_model_id") != active_model_id:
        return "model_changed"
    if connection is not None and _body_range_changed(connection, run, parameters):
        return "body_range_changed"
    if run["algorithm_version"] != ALIGNMENT_ALGORITHM_VERSION:
        return "algorithm_updated"
    return None


def _pair_corrections(
    connection: sqlite3.Connection,
    source_id: str,
    target_id: str,
    source_set_id: str,
    target_set_id: str,
) -> Tuple[Dict[str, Dict[str, object]], Dict[str, Dict[str, object]]]:
    """Confirmed corrections for this pair, read from both sides.

    A reviewer corrects a pair from whichever version they were reading, so a
    link counts as settled when either side carries a correction. Staleness is
    decided by ``confirmed_overrides_for_pair``: a correction left behind by a
    re-alignment or re-segmentation no longer counts.
    """

    return (
        confirmed_overrides_for_pair(
            connection, source_id, target_id, source_set_id, target_set_id
        ),
        confirmed_overrides_for_pair(
            connection, target_id, source_id, target_set_id, source_set_id
        ),
    )


def _link_needs_review(
    review_status: object,
    source_segment_ids: Sequence[str],
    target_segment_ids: Sequence[str],
    corrections: Tuple[Dict[str, Dict[str, object]], Dict[str, Dict[str, object]]],
) -> bool:
    """Whether a link is still waiting for a human to check it.

    Single rule behind the pair's「N 处待检查」and the reader's「!」marks: the
    algorithm proposed a counterpart but scored it below the threshold, and no
    confirmed correction settled it from either side. A link with one empty
    side means "no counterpart" (front matter or a missed segment), not a
    low-confidence guess, so it is not counted.
    """

    if str(review_status) != "rejected":
        return False
    if not source_segment_ids or not target_segment_ids:
        return False
    forward, backward = corrections
    return (
        override_for_selection(forward, source_segment_ids) is None
        and override_for_selection(backward, target_segment_ids) is None
    )


def _direct_run_statistics(
    connection: sqlite3.Connection, run: sqlite3.Row
) -> Dict[str, object]:
    pivot_id = str(run["pivot_source_file_id"])
    target_id = str(run["target_source_file_id"])
    segment_counts = {
        str(row["review_status"]): int(row["segment_count"])
        for row in connection.execute(
            "SELECT l.review_status, COUNT(DISTINCT m.segment_id) AS segment_count "
            "FROM alignment_links l JOIN alignment_link_members m "
            "ON m.alignment_link_id = l.alignment_link_id "
            "WHERE l.alignment_run_id = ? AND m.side = 'pivot' "
            "GROUP BY l.review_status",
            (run["alignment_run_id"],),
        )
    }
    total = sum(segment_counts.values())
    unmatched = sum(segment_counts.get(status, 0) for status in UNMATCHED_STATUSES)
    corrections = _pair_corrections(
        connection,
        pivot_id,
        target_id,
        str(run["pivot_segment_set_id"]),
        str(run["target_segment_set_id"]),
    )
    review_count = 0
    link_members: Dict[str, Dict[str, List[str]]] = {}
    for row in connection.execute(
        "SELECT l.alignment_link_id, m.side, m.segment_id "
        "FROM alignment_links l JOIN alignment_link_members m "
        "ON m.alignment_link_id = l.alignment_link_id "
        "WHERE l.alignment_run_id = ? AND l.review_status = 'rejected'",
        (run["alignment_run_id"],),
    ):
        bucket = link_members.setdefault(
            str(row["alignment_link_id"]), {"pivot": [], "target": []}
        )
        bucket[str(row["side"])].append(str(row["segment_id"]))
    for bucket in link_members.values():
        if _link_needs_review(
            "rejected", bucket["pivot"], bucket["target"], corrections
        ):
            review_count += 1
    return {
        "matched_segment_ratio": (
            round((total - unmatched) / total, 4) if total else None
        ),
        "review_count": review_count,
    }


def alignment_overview(
    db_path: Path, *, active_model_id: str = "", include_statistics: bool = True,
    source_id: str = "", target_id: str = "",
) -> Dict[str, object]:
    """Return the status of every version pair in every work.

    ``status`` is ``direct`` (a completed run joins the two versions),
    ``indirect`` (both versions are aligned to the work's base version on the
    same base segmentation, and reads are chained through it) or ``none``.
    ``review_count`` counts rejected links that still propose a counterpart
    (low confidence) and have no confirmed correction; links with an empty side
    mean "no counterpart" and are not counted.
    ``stale_reason`` is set when a run no longer matches the current model,
    body-range detection or algorithm: ``model_changed`` /
    ``body_range_changed`` / ``algorithm_updated`` are still readable,
    ``algorithm_unreadable`` is not.
    """

    if source_id:
        source_id = _validate_source_id(source_id)
    if target_id:
        target_id = _validate_source_id(target_id)
        if not source_id or source_id == target_id:
            raise InvalidAlignmentRequest("target_id 需要一个不同的 source_id。")
    connection = _read_connection(db_path)
    try:
        if not table_exists(connection, "document_groups"):
            return {"works": []}
        has_runs = table_exists(connection, "alignment_runs")
        works: List[Dict[str, object]] = []
        group_query = "SELECT document_group_id, base_source_file_id FROM document_groups"
        if source_id:
            group_query += (
                " WHERE document_group_id IN (SELECT document_group_id "
                "FROM document_group_members WHERE source_file_id = ?)"
            )
        groups = connection.execute(group_query, (source_id,) if source_id else ()).fetchall()
        for group in groups:
            group_id = str(group["document_group_id"])
            base_id = str(group["base_source_file_id"] or "")
            member_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT source_file_id FROM document_group_members "
                    "WHERE document_group_id = ? ORDER BY member_order, source_file_id",
                    (group_id,),
                )
            ]
            languages: Dict[str, str] = {}
            pairs: List[Dict[str, object]] = []
            for member_id in member_ids:
                if not table_exists(connection, "segment_sets"):
                    break
                row = connection.execute(
                    "SELECT language_code FROM segment_sets WHERE source_file_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (member_id,),
                ).fetchone()
                if row is not None and row[0] and str(row[0]) != "und":
                    languages[member_id] = str(row[0])
            for left_id, right_id in combinations(member_ids, 2):
                if target_id and {left_id, right_id} != {source_id, target_id}:
                    continue
                pair: Dict[str, object] = {
                    "source_file_ids": [left_id, right_id],
                    "status": "none",
                    "via_source_file_id": None,
                    "stale_reason": None,
                    "matched_segment_ratio": None,
                    "review_count": None,
                    "completed_at": None,
                }
                if has_runs:
                    direct = _latest_pair_run(connection, group_id, left_id, right_id)
                    if direct is not None:
                        pair.update(
                            status="direct",
                            stale_reason=_run_staleness(direct, active_model_id, connection),
                            completed_at=direct["completed_at"],
                        )
                        if include_statistics:
                            pair.update(_direct_run_statistics(connection, direct))
                    elif base_id and base_id not in (left_id, right_id):
                        first = _latest_pair_run(connection, group_id, left_id, base_id)
                        second = _latest_pair_run(connection, group_id, base_id, right_id)
                        if (
                            first is not None
                            and second is not None
                            and _segment_set_id_for_source(first, base_id)
                            == _segment_set_id_for_source(second, base_id)
                        ):
                            reasons = [
                                reason
                                for reason in (
                                    _run_staleness(first, active_model_id, connection),
                                    _run_staleness(second, active_model_id, connection),
                                )
                                if reason
                            ]
                            stale = None
                            if "algorithm_unreadable" in reasons:
                                stale = "algorithm_unreadable"
                            elif reasons:
                                stale = reasons[0]
                            pair.update(
                                status="indirect",
                                via_source_file_id=base_id,
                                stale_reason=stale,
                                completed_at=max(
                                    str(first["completed_at"] or ""),
                                    str(second["completed_at"] or ""),
                                ),
                            )
                pairs.append(pair)
            works.append(
                {
                    "document_group_id": group_id,
                    "languages": languages,
                    "pairs": pairs,
                }
            )
        return {"works": works, "active_model_id": active_model_id}
    finally:
        connection.close()


# ── link window for the parallel reader ────────────────────────────────────


def _segments_in_items(
    connection: sqlite3.Connection,
    segment_set_id: str,
    source_id: str,
    kind: str,
    start_index: int,
    end_index: int,
) -> List[str]:
    if kind == "pdf":
        query = (
            "SELECT DISTINCT s.segment_id, s.order_index FROM text_segment_spans p "
            "JOIN text_segments s ON s.segment_id = p.segment_id "
            "WHERE s.segment_set_id = ? AND p.source_file_id = ? "
            "AND p.pdf_page_index BETWEEN ? AND ? ORDER BY s.order_index"
        )
    else:
        query = (
            "SELECT DISTINCT s.segment_id, s.order_index "
            "FROM text_segment_paragraph_spans p "
            "JOIN text_segments s ON s.segment_id = p.segment_id "
            "WHERE s.segment_set_id = ? AND p.source_file_id = ? "
            "AND p.paragraph_index BETWEEN ? AND ? ORDER BY s.order_index"
        )
    return [
        str(row[0])
        for row in connection.execute(
            query, (segment_set_id, source_id, start_index, end_index)
        )
    ]


def _segment_spans(
    connection: sqlite3.Connection, kind: str, segment_ids: Sequence[str]
) -> Dict[str, List[Dict[str, int]]]:
    ids = list(dict.fromkeys(segment_ids))
    spans: Dict[str, List[Dict[str, int]]] = {segment_id: [] for segment_id in ids}
    if not ids:
        return spans
    placeholders = ",".join("?" for _ in ids)
    if kind == "pdf":
        query = (
            "SELECT segment_id, pdf_page_index AS item_index, page_char_start AS "
            "char_start, page_char_end AS char_end FROM text_segment_spans "
            f"WHERE segment_id IN ({placeholders}) ORDER BY span_order"
        )
    else:
        query = (
            "SELECT segment_id, paragraph_index AS item_index, paragraph_char_start "
            "AS char_start, paragraph_char_end AS char_end "
            f"FROM text_segment_paragraph_spans WHERE segment_id IN ({placeholders}) "
            "ORDER BY span_order"
        )
    for row in connection.execute(query, ids):
        spans[str(row["segment_id"])].append(
            {
                "item_index": int(row["item_index"]),
                "char_start": int(row["char_start"]),
                "char_end": int(row["char_end"]),
            }
        )
    return spans


def _run_links_for_segments(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    from_source_id: str,
    segment_ids: Sequence[str],
) -> List[Dict[str, object]]:
    if not segment_ids:
        return []
    from_side = "pivot" if str(run["pivot_source_file_id"]) == from_source_id else "target"
    placeholders = ",".join("?" for _ in segment_ids)
    link_rows = connection.execute(
        "SELECT DISTINCT l.alignment_link_id, l.order_index, l.review_status, "
        "l.confidence FROM alignment_links l JOIN alignment_link_members m "
        "ON m.alignment_link_id = l.alignment_link_id "
        f"WHERE l.alignment_run_id = ? AND m.side = ? AND m.segment_id IN ({placeholders}) "
        "ORDER BY l.order_index",
        (run["alignment_run_id"], from_side, *segment_ids),
    ).fetchall()
    if not link_rows:
        return []
    link_ids = [str(row["alignment_link_id"]) for row in link_rows]
    link_placeholders = ",".join("?" for _ in link_ids)
    members: Dict[str, Dict[str, List[str]]] = {
        link_id: {"from": [], "to": []} for link_id in link_ids
    }
    for row in connection.execute(
        "SELECT m.alignment_link_id, m.side, m.segment_id FROM alignment_link_members m "
        "JOIN text_segments s ON s.segment_id = m.segment_id "
        f"WHERE m.alignment_link_id IN ({link_placeholders}) ORDER BY s.order_index",
        link_ids,
    ):
        side = "from" if str(row["side"]) == from_side else "to"
        members[str(row["alignment_link_id"])][side].append(str(row["segment_id"]))
    return [
        {
            "order_index": int(row["order_index"]),
            "review_status": str(row["review_status"]),
            "confidence": row["confidence"],
            "from_segment_ids": members[str(row["alignment_link_id"])]["from"],
            "to_segment_ids": members[str(row["alignment_link_id"])]["to"],
        }
        for row in link_rows
    ]


def _worse_status(first: str, second: str) -> str:
    rank = {"unmatched": 3, "rejected": 2}
    return first if rank.get(first, 0) >= rank.get(second, 0) else second


def alignment_link_window(
    db_path: Path,
    source_file_id: object,
    target_source_file_id: object,
    start_index: object,
    end_index: object,
) -> Dict[str, object]:
    """Links touching source items ``start_index..end_index`` (inclusive).

    Each link carries both sides' segment ids and raw spans (item index plus
    code-point range), its automatic review status and confidence, and whether
    a human confirmed a correction ("corrected" / "no_counterpart") or deferred
    it. An indirect route composes the two runs through the base version; its
    status is the worse of the two legs and its confidence the lower one.
    """

    source_id = _validate_source_id(source_file_id)
    target_id = _validate_source_id(target_source_file_id)
    first = _validate_nonnegative_integer("start_index", start_index)
    last = _validate_nonnegative_integer("end_index", end_index)
    if last < first:
        raise InvalidAlignmentRequest("end_index 不能小于 start_index。")
    if last - first + 1 > MAX_LINK_WINDOW_ITEMS:
        raise InvalidAlignmentRequest(
            f"一次最多读取 {MAX_LINK_WINDOW_ITEMS} 个页面或段落的对应关系。"
        )
    connection = _read_connection(db_path)
    try:
        source_kind = _source_kind(_source_row(connection, source_id))
        target_kind = _source_kind(_source_row(connection, target_id))
        route_runs, via_id = _resolve_alignment_route(connection, source_id, target_id)
        source_set_id = _segment_set_id_for_source(route_runs[0], source_id)
        target_set_id = _segment_set_id_for_source(route_runs[-1], target_id)
        window_segments = _segments_in_items(
            connection, source_set_id, source_id, source_kind, first, last
        )
        links = _run_links_for_segments(
            connection, route_runs[0], source_id, window_segments
        )
        if via_id is not None:
            for link in links:
                second_leg = _run_links_for_segments(
                    connection, route_runs[1], via_id, link["to_segment_ids"]
                )
                target_ids: List[str] = []
                status = str(link["review_status"])
                confidence = link["confidence"]
                for leg in second_leg:
                    target_ids.extend(leg["to_segment_ids"])
                    status = _worse_status(status, str(leg["review_status"]))
                    if leg["confidence"] is not None:
                        confidence = (
                            leg["confidence"]
                            if confidence is None
                            else min(float(confidence), float(leg["confidence"]))
                        )
                if not second_leg:
                    status = _worse_status(status, "unmatched")
                link["to_segment_ids"] = list(dict.fromkeys(target_ids))
                link["review_status"] = status
                link["confidence"] = confidence

        corrections = _pair_corrections(
            connection, source_id, target_id, source_set_id, target_set_id
        )
        overrides = corrections[0]
        deferred: set[str] = set()
        if table_exists(connection, "alignment_review_deferrals"):
            deferred = {
                str(row[0])
                for row in connection.execute(
                    "SELECT source_segment_key FROM alignment_review_deferrals "
                    "WHERE source_file_id = ? AND target_source_file_id = ? "
                    "AND source_segment_set_id = ?",
                    (source_id, target_id, source_set_id),
                )
            }

        result_links: List[Dict[str, object]] = []
        for link in links:
            key = _segment_key(link["from_segment_ids"])
            manual = None
            target_ids = list(link["to_segment_ids"])
            # Same correction rule as locate: the exact selection first, then a
            # reader correction that covers this link.
            override = override_for_selection(overrides, link["from_segment_ids"])
            if override is not None:
                target_ids = list(override["target_segment_ids"])
                manual = "corrected" if target_ids else "no_counterpart"
            result_links.append(
                {
                    "order_index": link["order_index"],
                    "review_status": link["review_status"],
                    "confidence": link["confidence"],
                    "manual": manual,
                    "needs_review": _link_needs_review(
                        link["review_status"],
                        link["from_segment_ids"],
                        link["to_segment_ids"],
                        corrections,
                    ),
                    "deferred": key in deferred and manual is None,
                    "source_segment_ids": link["from_segment_ids"],
                    "target_segment_ids": target_ids,
                }
            )
        source_spans = _segment_spans(
            connection,
            source_kind,
            [sid for link in result_links for sid in link["source_segment_ids"]],
        )
        target_spans = _segment_spans(
            connection,
            target_kind,
            [sid for link in result_links for sid in link["target_segment_ids"]],
        )
        for link in result_links:
            link["source_spans"] = [
                span for sid in link["source_segment_ids"] for span in source_spans[sid]
            ]
            link["target_spans"] = [
                span for sid in link["target_segment_ids"] for span in target_spans[sid]
            ]
        return {
            "source_file_id": source_id,
            "target_source_file_id": target_id,
            "via_source_file_id": via_id,
            "source_item_type": "pdf_page" if source_kind == "pdf" else "word_paragraph",
            "target_item_type": "pdf_page" if target_kind == "pdf" else "word_paragraph",
            "start_index": first,
            "end_index": last,
            "links": result_links,
        }
    finally:
        connection.close()


# ── human review ───────────────────────────────────────────────────────────


def review_candidates(
    db_path: Path,
    source_file_id: object,
    target_source_file_id: object,
    source_segment_ids: object,
    near_target_segment_ids: object,
    radius: object = 4,
) -> Dict[str, object]:
    """Target segments around ``near_target_segment_ids`` for a manual choice."""

    source_id = _validate_source_id(source_file_id)
    target_id = _validate_source_id(target_source_file_id)
    source_ids = _clean_ids(source_segment_ids, name="source_segment_ids")
    near_ids = _clean_ids(
        near_target_segment_ids or [], name="near_target_segment_ids", allow_empty=True
    )
    width = _validate_nonnegative_integer("radius", radius)
    if not 1 <= width <= MAX_CANDIDATE_RADIUS:
        raise InvalidAlignmentRequest(f"radius 必须在 1 到 {MAX_CANDIDATE_RADIUS} 之间。")
    connection = _read_connection(db_path)
    try:
        _source_row(connection, source_id)
        target_kind = _source_kind(_source_row(connection, target_id))
        route_runs, _via = _resolve_alignment_route(connection, source_id, target_id)
        source_set_id = _segment_set_id_for_source(route_runs[0], source_id)
        target_set_id = _segment_set_id_for_source(route_runs[-1], target_id)
        source_rows = _ordered_segments_in_set(connection, source_set_id, source_ids)
        if len(source_rows) != len(source_ids):
            raise InvalidAlignmentRequest("源段落不属于当前对齐，请刷新后重试。")
        if not near_ids:
            near_ids = _nearest_aligned_targets(
                connection, route_runs, source_id, source_rows
            )
            if not near_ids:
                return {
                    "source_file_id": source_id,
                    "target_source_file_id": target_id,
                    "source_segments": [
                        {"segment_id": str(row["segment_id"]), "text": str(row["text_raw"])}
                        for row in source_rows
                    ],
                    "candidates": [],
                }
        near_rows = _ordered_segments_in_set(connection, target_set_id, near_ids)
        if len(near_rows) != len(near_ids):
            raise InvalidAlignmentRequest("参考译文段落不属于当前对齐，请刷新后重试。")
        candidates = _alignment_candidate_segments(
            connection,
            target_set_id,
            target_id,
            target_kind,
            [str(row["segment_id"]) for row in near_rows],
            width,
        )
        return {
            "source_file_id": source_id,
            "target_source_file_id": target_id,
            "source_segments": [
                {"segment_id": str(row["segment_id"]), "text": str(row["text_raw"])}
                for row in source_rows
            ],
            "candidates": candidates,
        }
    finally:
        connection.close()


def _nearest_aligned_targets(
    connection: sqlite3.Connection,
    route_runs: Sequence[sqlite3.Row],
    source_id: str,
    source_rows: Sequence[sqlite3.Row],
) -> List[str]:
    """Target segments of the direct link nearest to the source selection.

    Used when the selection's own link has no target (unmatched, or front
    matter): candidates are centred on the closest link that does have one.
    Indirect routes return nothing; their corrections are not offered.
    """

    if len(route_runs) != 1 or not source_rows:
        return []
    run = route_runs[0]
    side = "pivot" if str(run["pivot_source_file_id"]) == source_id else "target"
    other = "target" if side == "pivot" else "pivot"
    order = int(source_rows[0]["order_index"])
    row = connection.execute(
        "SELECT l.alignment_link_id FROM alignment_links l "
        "JOIN alignment_link_members sm ON sm.alignment_link_id = l.alignment_link_id "
        "AND sm.side = ? JOIN text_segments s ON s.segment_id = sm.segment_id "
        "WHERE l.alignment_run_id = ? AND EXISTS (SELECT 1 FROM alignment_link_members tm "
        "WHERE tm.alignment_link_id = l.alignment_link_id AND tm.side = ?) "
        "ORDER BY ABS(s.order_index - ?) LIMIT 1",
        (side, run["alignment_run_id"], other, order),
    ).fetchone()
    if row is None:
        return []
    return [
        str(member[0])
        for member in connection.execute(
            "SELECT m.segment_id FROM alignment_link_members m "
            "JOIN text_segments s ON s.segment_id = m.segment_id "
            "WHERE m.alignment_link_id = ? AND m.side = ? ORDER BY s.order_index",
            (row[0], other),
        )
    ]


def save_correction(
    db_path: Path,
    source_file_id: object,
    target_source_file_id: object,
    source_segment_ids: object,
    target_segment_ids: object,
    *,
    write_window: WriteWindow | None = None,
) -> Dict[str, object]:
    """Record a reviewer's correction and make it authoritative at once.

    The in-app reviewer is the user acting directly, so the proposal is
    confirmed in the same call. An empty target list means "no counterpart".
    Any deferral on the same selection is cleared.
    """

    source_ids = _clean_ids(source_segment_ids, name="source_segment_ids")
    target_ids = _clean_ids(
        target_segment_ids, name="target_segment_ids", allow_empty=True
    )
    proposal = create_override_proposal(
        db_path,
        source_file_id,
        target_source_file_id,
        source_ids,
        target_ids,
        evidence={"origin": "reader_review"},
        write_window=write_window,
        allow_empty_target=True,
    )
    confirmed = confirm_override(
        db_path,
        proposal["override_id"],
        proposal["confirmation_token"],
        write_window=write_window,
    )
    _clear_deferral(
        db_path,
        str(proposal["source_file_id"]),
        str(proposal["target_source_file_id"]),
        source_ids,
        write_window=write_window,
    )
    return {
        "override_id": confirmed["override_id"],
        "status": confirmed["status"],
        "manual": "corrected" if target_ids else "no_counterpart",
    }


def _deferral_context(
    connection: sqlite3.Connection, source_id: str, target_id: str, source_ids: List[str]
) -> Tuple[str, str]:
    route_runs, _via = _resolve_alignment_route(connection, source_id, target_id)
    source_set_id = _segment_set_id_for_source(route_runs[0], source_id)
    rows = _ordered_segments_in_set(connection, source_set_id, source_ids)
    if len(rows) != len(source_ids):
        raise InvalidAlignmentRequest("源段落不属于当前对齐，请刷新后重试。")
    return source_set_id, _segment_key(source_ids)


def defer_review(
    db_path: Path,
    source_file_id: object,
    target_source_file_id: object,
    source_segment_ids: object,
    *,
    write_window: WriteWindow | None = None,
) -> Dict[str, object]:
    source_id = _validate_source_id(source_file_id)
    target_id = _validate_source_id(target_source_file_id)
    source_ids = _clean_ids(source_segment_ids, name="source_segment_ids")

    def operation(connection: sqlite3.Connection) -> Dict[str, object]:
        connection.row_factory = sqlite3.Row
        source_set_id, key = _deferral_context(
            connection, source_id, target_id, source_ids
        )
        connection.execute(
            "INSERT OR IGNORE INTO alignment_review_deferrals(source_file_id, "
            "target_source_file_id, source_segment_set_id, source_segment_key, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (source_id, target_id, source_set_id, key, _now()),
        )
        return {"deferred": True}

    try:
        return _write(db_path, write_window, operation)
    except AlignmentNotFound as exc:
        raise InvalidAlignmentRequest(str(exc)) from exc


def _clear_deferral(
    db_path: Path,
    source_id: str,
    target_id: str,
    source_ids: List[str],
    *,
    write_window: WriteWindow | None = None,
) -> None:
    def operation(connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        source_set_id, key = _deferral_context(
            connection, source_id, target_id, source_ids
        )
        connection.execute(
            "DELETE FROM alignment_review_deferrals WHERE source_file_id = ? "
            "AND target_source_file_id = ? AND source_segment_set_id = ? "
            "AND source_segment_key = ?",
            (source_id, target_id, source_set_id, key),
        )

    _write(db_path, write_window, operation)

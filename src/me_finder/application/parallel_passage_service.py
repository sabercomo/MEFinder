"""Read persisted cross-version passages for one located quote."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from .search_service import SearchRequest, SearchService


def find_parallel_passages(
    index_path: Path,
    engine: object,
    quote: str,
    *,
    mode: str,
    source_file_id: str | None,
    target_source_file_id: str | None,
    target_language_code: str | None,
    limit: int,
    schema_version: str,
    source_formatter: Callable[[Mapping[str, object]], dict[str, object]],
) -> dict[str, object]:
    from ..structured_reader import SourceNotFound
    from ..text_alignment import (
        AlignmentNotFound,
        list_alignment_targets,
        locate_alignment,
    )

    if (
        target_source_file_id is not None
        and target_source_file_id not in engine.sources_by_id
    ):
        raise SourceNotFound(f"未找到文献：{target_source_file_id}")
    raw_result = SearchService.execute(
        engine,
        SearchRequest(
            query=quote,
            mode=mode,
            limit=200,
            source_type="all",
            source_file_id=source_file_id,
        ),
    )

    correspondences: list[dict[str, object]] = []
    for raw_match in raw_result["results"]:
        if not isinstance(raw_match, Mapping):
            continue
        source_id = str(raw_match["source_file_id"])
        targets = list_alignment_targets(index_path, source_id)["targets"]
        selected_targets = [
            target
            for target in targets
            if isinstance(target, Mapping)
            and (
                target_source_file_id is None
                or str(target["source_file_id"]) == target_source_file_id
            )
            and (
                target_language_code is None
                or _language_matches(
                    str(target["language_code"]), target_language_code
                )
            )
        ]
        if not selected_targets:
            continue

        source = _parallel_source(raw_match, source_formatter)
        selection = _source_selection(raw_match)
        for target in selected_targets:
            target_summary = _parallel_target(target)
            if selection is None:
                correspondences.append(
                    {
                        "source": source,
                        "target": target_summary,
                        "status": "unavailable",
                        "via_source_file_id": target["via_source_file_id"],
                        "source_segment_ids": [],
                        "manual_override_id": None,
                        "candidates": [],
                        "note": "原句命中缺少可用于对照定位的精确字符偏移。",
                    }
                )
                continue
            try:
                located = locate_alignment(
                    index_path,
                    source_id,
                    target["source_file_id"],
                    **selection,
                    candidate_radius=3,
                )
            except AlignmentNotFound as exc:
                correspondences.append(
                    {
                        "source": source,
                        "target": target_summary,
                        "status": "unavailable",
                        "via_source_file_id": target["via_source_file_id"],
                        "source_segment_ids": [],
                        "manual_override_id": None,
                        "candidates": [],
                        "note": str(exc),
                    }
                )
                continue
            if str(located.get("alignment_source")) == "manual_review":
                status = "confirmed"
                note = "该对应已由人工复核确认，普通双栏阅读与 MCP 查询都优先采用它。"
            else:
                status = "needs_agent_review"
                note = (
                    "anchor_distance 只表示相对既有对齐中心的位置，不代表语义正确率。"
                )
            correspondences.append(
                {
                    "source": source,
                    "target": target_summary,
                    "status": status,
                    "via_source_file_id": located["via_source_file_id"],
                    "source_segment_ids": list(
                        located.get("source_segment_ids", [])
                    ),
                    "manual_override_id": located.get("manual_override_id"),
                    "candidates": _target_candidates(located),
                    "note": note,
                }
            )

    total = len(correspondences)
    return {
        "schema_version": schema_version,
        "query": str(raw_result["query"]),
        "source_match_count": int(raw_result["total"]),
        "source_match_count_is_exact": bool(raw_result["total_is_exact"]),
        "total": total,
        "candidate_set_count": sum(
            item["status"] == "needs_agent_review" for item in correspondences
        ),
        "has_more": bool(raw_result["has_more"]) or total > limit,
        "correspondences": correspondences[:limit],
    }


def _language_matches(candidate: str, requested: str) -> bool:
    normalized_candidate = candidate.casefold()
    normalized_requested = requested.casefold()
    return normalized_candidate == normalized_requested or normalized_candidate.startswith(
        normalized_requested + "-"
    )


def _parallel_source(
    fields: Mapping[str, object],
    formatter: Callable[[Mapping[str, object]], dict[str, object]],
) -> dict[str, object]:
    match = formatter(fields)
    return {
        "source_file_id": match["source_file_id"],
        "source_type": match["source_type"],
        "document_title": match["document_title"],
        "matched_text": match["matched_text"],
        "paragraph_text": match["paragraph_text"],
        "context_before": match["context_before"],
        "context_after": match["context_after"],
        "match_type": match["match_type"],
        "match_score": match["match_score"],
        "physical_page": match["physical_page"],
        "citation_page": match["citation_page"],
        "reader": match["reader"],
    }


def _parallel_target(fields: Mapping[str, object]) -> dict[str, object]:
    return {
        "source_file_id": str(fields["source_file_id"]),
        "display_name": str(fields["display_name"]),
        "language_code": str(fields["language_code"]),
        "source_format": str(fields["source_format"]),
    }


def _source_selection(fields: Mapping[str, object]) -> dict[str, object] | None:
    if str(fields["source_type"]) == "pdf":
        spans = fields["page_match_spans"]
        if not isinstance(spans, list) or not spans:
            return None
        first = spans[0]
        last = spans[-1]
        if not isinstance(first, Mapping) or not isinstance(last, Mapping):
            return None
        return {
            "start_page_index": first["pdf_page_index"],
            "end_page_index": last["pdf_page_index"],
            "start_offset": first["page_char_start"],
            "end_offset": last["page_char_end"],
        }
    return {
        "start_page_index": fields["paragraph_index"],
        "end_page_index": fields["paragraph_index"],
        "start_offset": fields["match_start"],
        "end_offset": fields["match_end"],
    }


def _target_passages(
    located: Mapping[str, object],
) -> list[dict[str, object]]:
    item_type = str(located["target_item_type"])
    passages = []
    for span in located["page_match_spans"]:
        if not isinstance(span, Mapping):
            continue
        if item_type == "pdf_page":
            position = span["pdf_page_index"]
            char_start = span["page_char_start"]
            char_end = span["page_char_end"]
        else:
            position = span["paragraph_index"]
            char_start = span["paragraph_char_start"]
            char_end = span["paragraph_char_end"]
        passages.append(
            {
                "item_type": item_type,
                "position": int(position),
                "char_start": int(char_start),
                "char_end": int(char_end),
                "text": str(span["match_quote"]),
            }
        )
    return passages


def _target_candidates(
    located: Mapping[str, object],
) -> list[dict[str, object]]:
    candidates = []
    for fields in located["calibration_candidates"]:
        if not isinstance(fields, Mapping):
            continue
        candidates.append(
            {
                "candidate_id": str(fields["segment_id"]),
                "order_index": int(fields["order_index"]),
                "anchor_distance": int(fields["anchor_distance"]),
                "text": str(fields["text"]),
                "passages": _target_passages(
                    {
                        "target_item_type": located["target_item_type"],
                        "page_match_spans": fields["page_match_spans"],
                    }
                ),
                "context_before": fields["context_before"],
                "context_after": fields["context_after"],
            }
        )
    return candidates

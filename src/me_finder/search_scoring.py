"""Candidate scoring: ranking order, deduplication and fuzzy window search."""

from __future__ import annotations

import difflib
from typing import Dict, List, Sequence, Tuple

from .normalization import punctuationless_text
from .search_anchors import page_match_spans


def rank_key(item: Dict[str, object]) -> Tuple[float, int, int, str, int, str, int]:
    nested = item.get("paragraph")
    record = nested if isinstance(nested, dict) else item
    volume_number = record.get("volume_number")
    try:
        volume_sort = int(volume_number) if volume_number is not None else 9999
    except (TypeError, ValueError):
        volume_sort = 9999
    paragraph_index = record.get("paragraph_index")
    try:
        paragraph_sort = int(paragraph_index) if paragraph_index is not None else 0
    except (TypeError, ValueError):
        paragraph_sort = 0
    uncalibrated_sort = 1 if record.get("source_type") == "pdf" and not record.get("citation_page_start") else 0
    cross_sort = 1 if record.get("is_cross_page") else 0
    return (
        -float(item["match_score"]),
        uncalibrated_sort,
        cross_sort,
        str(record.get("source_type") or "word"),
        volume_sort,
        str(record.get("original_file_name") or ""),
        paragraph_sort,
    )


def merge_candidate_specs(ranked: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    """Deduplicate lightweight candidates before expensive formatting."""

    merged: List[Dict[str, object]] = []
    seen = set()
    for item in ranked:
        paragraph = item.get("paragraph")
        if not isinstance(paragraph, dict):
            continue
        if paragraph.get("is_cross_page") and cross_candidate_duplicate(item, ranked):
            continue
        # A result is an occurrence, not a unique text fragment.  The same
        # sentence can legitimately appear in multiple paragraphs (for
        # example in a preface and again in the main text), so text-based
        # deduplication silently discarded valid locations.
        key = (
            paragraph.get("source_file_id"),
            paragraph.get("paragraph_id"),
            item.get("match_start"),
            item.get("match_end"),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def physical_match_ranges(
    page_match_spans: object,
) -> Tuple[Tuple[str, int, int], ...]:
    """Return canonical physical-page ranges for one paragraph match."""

    if not isinstance(page_match_spans, list):
        return ()
    grouped: Dict[str, List[Tuple[int, int]]] = {}
    for span in page_match_spans:
        if not isinstance(span, dict):
            continue
        page_id = span.get("pdf_page_id")
        start = span.get("page_char_start")
        end = span.get("page_char_end")
        if (
            not page_id
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
        ):
            continue
        grouped.setdefault(str(page_id), []).append((start, end))

    canonical: List[Tuple[str, int, int]] = []
    for page_id, ranges in grouped.items():
        merged: List[List[int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        canonical.extend(
            (page_id, start, end) for start, end in merged
        )
    return tuple(sorted(canonical))


def cross_candidate_duplicate(
    cross_item: Dict[str, object], ranked: Sequence[Dict[str, object]]
) -> bool:
    cross = cross_item.get("paragraph")
    if not isinstance(cross, dict):
        return False
    raw = str(cross.get("text_raw") or "")
    start = int(cross_item.get("match_start") or 0)
    end = int(cross_item.get("match_end") or 0)
    matched = punctuationless_text(raw[start:end])
    if not matched:
        return False
    cross_ranges = physical_match_ranges(
        page_match_spans(
            cross,
            str(cross.get("source_type") or "word"),
            start,
            end,
            len(raw),
        )
    )
    # A match touching two physical pages is the reason the CROSS helper
    # exists.  Text repeated elsewhere on either full-page paragraph is a
    # different occurrence and must not suppress it.
    if len({page_id for page_id, _, _ in cross_ranges}) > 1:
        return False
    start_page = cross.get("pdf_page_start_index")
    end_page = cross.get("pdf_page_end_index")
    for item in ranked:
        if item is cross_item:
            continue
        paragraph = item.get("paragraph")
        if not isinstance(paragraph, dict) or paragraph.get("is_cross_page"):
            continue
        if paragraph.get("source_file_id") != cross.get("source_file_id"):
            continue
        if cross_ranges:
            page_raw = str(paragraph.get("text_raw") or "")
            page_match_start = int(item.get("match_start") or 0)
            page_match_end = int(item.get("match_end") or 0)
            page_ranges = physical_match_ranges(
                page_match_spans(
                    paragraph,
                    str(paragraph.get("source_type") or "word"),
                    page_match_start,
                    page_match_end,
                    len(page_raw),
                )
            )
            # Precise anchor data proves duplication only when both hits
            # resolve to the same source-page character interval.  Merely
            # finding the same text elsewhere on that page is insufficient.
            if page_ranges:
                if page_ranges == cross_ranges:
                    return True
                continue
            # One precise side and one unanchored side cannot establish
            # occurrence identity; prefer retaining the result.
            continue
        page = paragraph.get("pdf_page_start_index")
        if page is None or start_page is None or end_page is None:
            continue
        if not (int(start_page) <= int(page) <= int(end_page)):
            continue
        page_text = punctuationless_text(str(paragraph.get("text_raw") or ""))
        if matched in page_text:
            return True
    return False


def best_window_ratio(query_plain: str, plain: str) -> Tuple[float, int, int]:
    if not query_plain or not plain:
        return 0.0, 0, 0
    if query_plain in plain:
        start = plain.find(query_plain)
        return 0.91, start, start + len(query_plain) - 1
    q_len = len(query_plain)
    if len(plain) <= q_len + 8:
        return difflib.SequenceMatcher(None, query_plain, plain).ratio(), 0, max(0, len(plain) - 1)
    window_sizes = sorted(set([q_len, int(q_len * 1.25) + 1, int(q_len * 1.6) + 1, q_len + 8]))
    step = max(1, q_len // 3)
    # A fine window scan across a whole book page (footnote-dense, 2000+ chars)
    # is O(len) SequenceMatcher calls and dominates fuzzy search time.  For long
    # paragraphs, first locate the promising region with a coarse stride, then
    # refine only around it.  Short paragraphs keep the exhaustive scan so their
    # behaviour (and the regression fixtures) is unchanged.
    if len(plain) > 600:
        primary = window_sizes[0]
        coarse_step = max(step, q_len)
        anchor = 0
        anchor_ratio = -1.0
        for start in range(0, max(1, len(plain) - primary + 1), coarse_step):
            ratio = difflib.SequenceMatcher(
                None, query_plain, plain[start : start + primary]
            ).ratio()
            if ratio > anchor_ratio:
                anchor_ratio = ratio
                anchor = start
        region_lo = max(0, anchor - q_len)
        region_hi = min(len(plain), anchor + primary + q_len)
    else:
        region_lo = 0
        region_hi = len(plain)
    best = (0.0, 0, min(len(plain) - 1, q_len))
    for size in window_sizes:
        if size <= 0:
            continue
        for start in range(region_lo, max(region_lo + 1, region_hi - size + 1), step):
            window = plain[start : start + size]
            ratio = difflib.SequenceMatcher(None, query_plain, window).ratio()
            if ratio > best[0]:
                best = (ratio, start, start + len(window) - 1)
        tail_start = max(0, len(plain) - size)
        window = plain[tail_start:]
        ratio = difflib.SequenceMatcher(None, query_plain, window).ratio()
        if ratio > best[0]:
            best = (ratio, tail_start, len(plain) - 1)
    return best


def relative_relevance(raw_score: float, raw_scores: Sequence[float]) -> float:
    """Normalize one raw relevance to [0, 1] within the current result set.

    Lower raw scores are more relevant, so the best hit in the set maps to 1.0.
    This is a within-response relative signal only — it is not comparable across
    queries. Callers should order results by ``rank``, not by this score.
    """

    if not raw_scores:
        return 1.0
    best = min(raw_scores)
    worst = max(raw_scores)
    if worst <= best:
        return 1.0
    return round((worst - raw_score) / (worst - best), 4)

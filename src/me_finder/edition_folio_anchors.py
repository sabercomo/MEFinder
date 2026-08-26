"""Detect original-edition margin folios as candidate alignment boundaries."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence, Tuple

import numpy as np

from .auto_page_mapping import _layout_bbox_scale, _normalized_page_bbox


MIN_FOLIO_CHAIN = 4
MIN_FOLIO_SPAN = 5
MIN_PIVOT_PAGE_CONFIDENCE = 0.8
MIN_INDIVIDUAL_FOLIO_SIMILARITY = 0.35
MIN_MEDIAN_FOLIO_SIMILARITY = 0.5
MIN_FOLIO_PASS_RATE = 0.6
_ARABIC_FOLIO = re.compile(r"\s*([0-9]{1,4})\s*\Z")
_NON_BODY_ROLES = frozenset(
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


@dataclass(frozen=True)
class FolioBoundaryCandidate:
    folio_number: int
    pivot_segment_index: int
    target_segment_index: int
    target_pdf_page_index: int
    target_bbox: Tuple[float, float, float, float]
    similarity: float | None = None

    @property
    def key(self) -> str:
        return f"folio:{self.folio_number}"


def _block_role(block: Mapping[str, object]) -> str:
    return str(
        block.get("mineru_type")
        or block.get("parser_type")
        or block.get("type")
        or ""
    ).strip().casefold()


def _body_blocks(
    payload: Mapping[str, object],
) -> List[Tuple[Mapping[str, object], List[float]]]:
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        return []
    width, height = _layout_bbox_scale(
        blocks,
        float(payload.get("page_width") or 1000.0),
        float(payload.get("page_height") or 1000.0),
    )
    result: List[Tuple[Mapping[str, object], List[float]]] = []
    for block in blocks:
        if not isinstance(block, dict) or _block_role(block) in _NON_BODY_ROLES:
            continue
        text = str(block.get("text") or "").strip()
        bbox = _normalized_page_bbox(block, width, height)
        if not text or bbox is None or _ARABIC_FOLIO.fullmatch(text):
            continue
        result.append((block, bbox))
    return result


def _margin_folios(
    payload: Mapping[str, object],
) -> List[Tuple[int, Tuple[float, float, float, float], int]]:
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        return []
    body = _body_blocks(payload)
    if not body:
        return []
    width, height = _layout_bbox_scale(
        blocks,
        float(payload.get("page_width") or 1000.0),
        float(payload.get("page_height") or 1000.0),
    )
    candidates: List[
        Tuple[Tuple[int, Tuple[float, float, float, float], int], float]
    ] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        role = _block_role(block)
        if role and role != "page_number":
            continue
        match = _ARABIC_FOLIO.fullmatch(str(block.get("text") or ""))
        bbox = _normalized_page_bbox(block, width, height)
        if match is None or bbox is None:
            continue
        number = int(match.group(1))
        if number <= 0 or 1800 <= number <= 2099:
            continue
        x_center = (bbox[0] + bbox[2]) / 2.0
        y_center = (bbox[1] + bbox[3]) / 2.0
        if y_center >= 0.86:
            continue
        nearby = [
            body_bbox
            for _body_block, body_bbox in body
            if body_bbox[1] - 0.025 <= y_center <= body_bbox[3] + 0.025
        ]
        reference = nearby or [body_bbox for _body_block, body_bbox in body]
        left_edge = min(body_bbox[0] for body_bbox in reference)
        right_edge = max(body_bbox[2] for body_bbox in reference)
        if not (x_center < left_edge - 0.005 or x_center > right_edge + 0.005):
            continue
        marker_offset = int(block.get("page_char_start") or 0)
        candidates.append(
            (
                (number, tuple(float(value) for value in bbox[:4]), marker_offset),
                y_center,
            )
        )
    top_margin = [candidate for candidate, y_center in candidates if y_center <= 0.16]
    return top_margin or [candidate for candidate, _y_center in candidates]


def _page_segment_spans(
    connection: sqlite3.Connection,
    segment_set_id: str,
    source_file_id: str,
) -> Dict[int, List[Tuple[int, int, int]]]:
    result: Dict[int, List[Tuple[int, int, int]]] = {}
    for row in connection.execute(
        "SELECT p.pdf_page_index, p.page_char_start, p.page_char_end, s.order_index "
        "FROM text_segment_spans p JOIN text_segments s ON s.segment_id = p.segment_id "
        "WHERE s.segment_set_id = ? AND p.source_file_id = ? "
        "ORDER BY p.pdf_page_index, p.page_char_start, s.order_index",
        (segment_set_id, source_file_id),
    ):
        result.setdefault(int(row["pdf_page_index"]), []).append(
            (
                int(row["page_char_start"]),
                int(row["page_char_end"]),
                int(row["order_index"]),
            )
        )
    return result


def _segment_at_offset(
    spans: Sequence[Tuple[int, int, int]], offset: int
) -> int | None:
    for start, end, order_index in spans:
        if start <= offset < end:
            return order_index
    if not spans:
        return None
    return min(
        spans,
        key=lambda span: min(abs(offset - span[0]), abs(offset - span[1])),
    )[2]


def _body_boundary_offset(
    payload: Mapping[str, object], marker_bbox: Sequence[float]
) -> int | None:
    body = _body_blocks(payload)
    if not body:
        return None
    marker_y = (float(marker_bbox[1]) + float(marker_bbox[3])) / 2.0
    containing = [
        item
        for item in body
        if item[1][1] - 0.025 <= marker_y <= item[1][3] + 0.025
    ]
    if containing:
        block, bbox = min(
            containing,
            key=lambda item: abs((item[1][1] + item[1][3]) / 2.0 - marker_y),
        )
    else:
        following = [item for item in body if item[1][1] > marker_y]
        block, bbox = (
            min(following, key=lambda item: item[1][1])
            if following
            else max(body, key=lambda item: item[1][3])
        )
    start = int(block.get("page_char_start") or 0)
    end = int(block.get("page_char_end") or start)
    if end <= start or bbox[3] <= bbox[1]:
        return start
    ratio = max(0.0, min(1.0, (marker_y - bbox[1]) / (bbox[3] - bbox[1])))
    return start + int((end - start) * ratio)


def _numeric_page_label(payload: Mapping[str, object]) -> int | None:
    confidence = payload.get("page_mapping_confidence")
    if confidence is not None and float(confidence) < MIN_PIVOT_PAGE_CONFIDENCE:
        return None
    for key in ("citation_page_number", "printed_page", "citation_page"):
        match = _ARABIC_FOLIO.fullmatch(str(payload.get(key) or ""))
        if match is not None:
            return int(match.group(1))
    return None


def _longest_monotonic_chain(
    candidates: Sequence[FolioBoundaryCandidate],
) -> List[FolioBoundaryCandidate]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.folio_number,
            item.pivot_segment_index,
            item.target_segment_index,
        ),
    )
    paths: List[Tuple[int, ...]] = []
    for index, candidate in enumerate(ordered):
        path = (index,)
        for previous_index, previous in enumerate(ordered[:index]):
            if (
                previous.folio_number >= candidate.folio_number
                or previous.pivot_segment_index >= candidate.pivot_segment_index
                or previous.target_segment_index >= candidate.target_segment_index
                or previous.target_pdf_page_index >= candidate.target_pdf_page_index
            ):
                continue
            proposed = paths[previous_index] + (index,)
            if len(proposed) > len(path):
                path = proposed
        paths.append(path)
    if not paths:
        return []
    best = paths[max(range(len(paths)), key=lambda index: len(paths[index]))]
    return [ordered[index] for index in best]


def detect_folio_boundary_candidates(
    connection: sqlite3.Connection,
    pivot_source_file_id: str,
    target_source_file_id: str,
    pivot_segment_set_id: str,
    target_segment_set_id: str,
) -> List[FolioBoundaryCandidate]:
    """Return layout-supported exact-folio pairs; semantic checks happen later."""

    source_types = {
        str(row["source_file_id"]): str(row["source_type"] or "").casefold()
        for row in connection.execute(
            "SELECT source_file_id, source_type FROM source_files "
            "WHERE source_file_id IN (?, ?)",
            (pivot_source_file_id, target_source_file_id),
        )
    }
    if source_types != {
        pivot_source_file_id: "pdf",
        target_source_file_id: "pdf",
    }:
        return []
    pivot_spans = _page_segment_spans(
        connection, pivot_segment_set_id, pivot_source_file_id
    )
    target_spans = _page_segment_spans(
        connection, target_segment_set_id, target_source_file_id
    )
    pivot_by_folio: Dict[int, int] = {}
    duplicate_folios: set[int] = set()
    for row in connection.execute(
        "SELECT pdf_page_index, payload_json FROM pdf_pages "
        "WHERE source_file_id = ? ORDER BY pdf_page_index",
        (pivot_source_file_id,),
    ):
        payload = json.loads(str(row["payload_json"] or "{}"))
        folio = _numeric_page_label(payload)
        page_index = int(row["pdf_page_index"])
        body = _body_blocks(payload)
        if folio is None or not body:
            continue
        first_body_offset = min(int(block.get("page_char_start") or 0) for block, _ in body)
        segment_index = _segment_at_offset(pivot_spans.get(page_index, ()), first_body_offset)
        if segment_index is None:
            continue
        if folio in pivot_by_folio:
            duplicate_folios.add(folio)
        else:
            pivot_by_folio[folio] = segment_index
    for folio in duplicate_folios:
        pivot_by_folio.pop(folio, None)

    candidates: List[FolioBoundaryCandidate] = []
    for row in connection.execute(
        "SELECT pdf_page_index, payload_json FROM pdf_pages "
        "WHERE source_file_id = ? ORDER BY pdf_page_index",
        (target_source_file_id,),
    ):
        page_index = int(row["pdf_page_index"])
        payload = json.loads(str(row["payload_json"] or "{}"))
        for folio, bbox, _marker_offset in _margin_folios(payload):
            pivot_index = pivot_by_folio.get(folio)
            body_offset = _body_boundary_offset(payload, bbox)
            if pivot_index is None or body_offset is None:
                continue
            target_index = _segment_at_offset(
                target_spans.get(page_index, ()), body_offset
            )
            if target_index is None:
                continue
            candidates.append(
                FolioBoundaryCandidate(
                    folio,
                    pivot_index,
                    target_index,
                    page_index,
                    bbox,
                )
            )
    chain = _longest_monotonic_chain(candidates)
    if (
        len(chain) < MIN_FOLIO_CHAIN
        or chain[-1].folio_number - chain[0].folio_number < MIN_FOLIO_SPAN
    ):
        return []
    return chain


def verify_folio_boundary_candidates(
    candidates: Sequence[FolioBoundaryCandidate],
    pivot_vectors: np.ndarray,
    target_vectors: np.ndarray,
) -> List[FolioBoundaryCandidate]:
    """Confirm an edition-page stream from the bilingual text around each marker."""

    if len(candidates) < MIN_FOLIO_CHAIN:
        return []
    source = np.asarray(pivot_vectors, dtype=np.float32)
    target = np.asarray(target_vectors, dtype=np.float32)
    similarities: List[float] = []
    for candidate in candidates:
        source_window = source[
            candidate.pivot_segment_index : candidate.pivot_segment_index + 2
        ].sum(axis=0)
        target_window = target[
            candidate.target_segment_index : candidate.target_segment_index + 2
        ].sum(axis=0)
        denominator = float(np.linalg.norm(source_window) * np.linalg.norm(target_window))
        similarities.append(
            float(np.dot(source_window, target_window) / denominator)
            if denominator
            else -1.0
        )
    accepted_indices = [
        index
        for index, similarity in enumerate(similarities)
        if similarity >= MIN_INDIVIDUAL_FOLIO_SIMILARITY
    ]
    if (
        len(accepted_indices) < MIN_FOLIO_CHAIN
        or float(np.median(similarities)) < MIN_MEDIAN_FOLIO_SIMILARITY
        or len(accepted_indices) / len(candidates) < MIN_FOLIO_PASS_RATE
    ):
        return []
    return [
        FolioBoundaryCandidate(
            candidate.folio_number,
            candidate.pivot_segment_index,
            candidate.target_segment_index,
            candidate.target_pdf_page_index,
            candidate.target_bbox,
            similarities[index],
        )
        for index, candidate in enumerate(candidates)
        if index in accepted_indices
    ]

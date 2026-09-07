"""Leaf helpers for the D corridor re-alignment experiment (issue #18).

Pure, duck-typed scoring over cached E5 vectors that reproduces the production
DP's link cost exactly, so the experiment can ask — for any candidate grouping
inside an anchor corridor — whether it is cheaper than the link the frozen run
chose.  This module never imports SemanticLink and never touches the database;
callers pass plain arrays and integer spans.

Cost model mirrors ``semantic_alignment._transition_cost`` /
``_align_partition``:

    matched (di>0, dj>0):  penalty + (1 - cos) * 3 + 0.18 * |log(tlen / (ratio*slen))|
    gap     (di==0|dj==0): penalty + log1p(slen + tlen) / 12

where ``cos`` is the cosine between the unit-normalised sums of the grouped rows
and ``ratio`` is the corridor's target/source non-space character ratio.
"""
from __future__ import annotations

import math
from typing import Dict, Sequence, Tuple

import numpy as np

# (di, dj, penalty) — identical to semantic_alignment._TRANSITIONS.
TRANSITIONS: Tuple[Tuple[int, int, float], ...] = (
    (1, 1, 0.0), (1, 2, 0.15), (2, 1, 0.15), (2, 2, 0.25),
    (1, 3, 0.35), (3, 1, 0.35), (2, 3, 0.45), (3, 2, 0.45), (3, 3, 0.55),
    (1, 0, 2.2), (0, 1, 2.2),
)
_PENALTY: Dict[Tuple[int, int], float] = {(di, dj): p for di, dj, p in TRANSITIONS}


def prefix_sums(vectors: np.ndarray) -> np.ndarray:
    """Cumulative row sums with a leading zero row (matches the production DP)."""
    return np.vstack(
        [np.zeros((1, vectors.shape[1]), dtype=np.float32), np.cumsum(vectors, axis=0)]
    )


def non_space_lengths(texts: Sequence[str]) -> list[int]:
    return [max(1, sum(not ch.isspace() for ch in text)) for text in texts]


def group_vector(prefix: np.ndarray, start: int, end: int) -> np.ndarray:
    vector = prefix[end] - prefix[start]
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def group_similarity(
    source_prefix: np.ndarray, target_prefix: np.ndarray,
    s0: int, s1: int, t0: int, t1: int,
) -> float:
    if s0 == s1 or t0 == t1:
        return 0.0
    return float(np.clip(group_vector(source_prefix, s0, s1) @ group_vector(target_prefix, t0, t1), -1.0, 1.0))


def link_cost(
    source_prefix: np.ndarray, target_prefix: np.ndarray,
    source_lengths: Sequence[int], target_lengths: Sequence[int],
    s0: int, s1: int, t0: int, t1: int, ratio: float,
) -> Tuple[float, float]:
    """Return (cost, similarity) for one link spanning [s0,s1) x [t0,t1)."""
    di, dj = s1 - s0, t1 - t0
    penalty = _PENALTY.get((di, dj))
    slen = sum(source_lengths[s0:s1])
    tlen = sum(target_lengths[t0:t1])
    if penalty is None:  # spans wider than the transition table are not admissible
        penalty = _PENALTY[(min(di, 3) or 1, min(dj, 3) or 1)]
    if di == 0 or dj == 0:
        return penalty + math.log1p(slen + tlen) / 12.0, 0.0
    sim = group_similarity(source_prefix, target_prefix, s0, s1, t0, t1)
    expected = max(ratio * slen, 1.0)
    length_cost = 0.18 * abs(math.log(max(tlen, 1) / expected))
    return penalty + (1.0 - sim) * 3.0 + length_cost, sim


def path_cost(
    source_prefix: np.ndarray, target_prefix: np.ndarray,
    source_lengths: Sequence[int], target_lengths: Sequence[int],
    links: Sequence[Tuple[int, int, int, int]], ratio: float,
) -> float:
    return sum(
        link_cost(source_prefix, target_prefix, source_lengths, target_lengths, s0, s1, t0, t1, ratio)[0]
        for s0, s1, t0, t1 in links
    )


def gap_to_reach_correct(
    source_lengths: Sequence[int], target_lengths: Sequence[int],
    frozen_target: Tuple[int, int], correct_target: Tuple[int, int],
    *, gap_penalty: float = 2.2,
) -> Dict[str, float]:
    """Cost of gapping the target segments between the frozen and correct spans.

    Reaching the correct target for a phase-shifted pivot means gapping every
    edition-only / OCR-noise target segment in between at the flat gap penalty.
    This quantifies why the frozen (wrong) path is globally cheaper even when the
    correct link is locally cheaper.
    """
    lo, hi = sorted([frozen_target, correct_target], key=lambda s: s[0])
    span = range(lo[1], hi[0]) if lo[1] <= hi[0] else range(0, 0)
    count = len(span)
    cost = sum(gap_penalty + math.log1p(target_lengths[o]) / 12.0 for o in span if o < len(target_lengths))
    return {"gapped_segments": count, "gap_cost": round(cost, 3)}


def best_target_grouping(
    source_prefix: np.ndarray, target_prefix: np.ndarray,
    source_lengths: Sequence[int], target_lengths: Sequence[int],
    s0: int, s1: int, target_center: int, ratio: float,
    *, radius: int = 4, max_span: int = 3,
) -> Dict[str, object]:
    """Cheapest matched link for a fixed source group over target windows near a centre.

    Scans target windows [t0, t0+span) with t0 within ``radius`` of
    ``target_center`` and span up to ``max_span``; returns the minimum-cost one.
    """
    best: Dict[str, object] | None = None
    for t0 in range(max(0, target_center - radius), target_center + radius + 1):
        for span in range(1, max_span + 1):
            t1 = t0 + span
            if t1 > len(target_lengths):
                break
            cost, sim = link_cost(source_prefix, target_prefix, source_lengths, target_lengths, s0, s1, t0, t1, ratio)
            if best is None or cost < best["cost"]:
                best = {"t0": t0, "t1": t1, "cost": cost, "similarity": sim}
    return best or {"t0": target_center, "t1": target_center, "cost": math.inf, "similarity": 0.0}

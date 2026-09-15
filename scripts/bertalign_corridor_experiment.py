"""Two-stage corridor recovery in the Bertalign manner (issue #18, round 2).

This module reconstructs the lost 2026-09-06 experimental driver of the same
name; the original was never committed and is no longer recoverable, but its
public interface is pinned by ``tests/test_bertalign_corridor_experiment.py``.
The reconstruction follows the documented method (``docs/issues/
d-bertalign-body-corridor-experiment.md``): a 1:1 skeleton first, then
constrained many-to-many recovery inside the open intervals between reliable
anchors.  Differences from upstream Bertalign (commit ``df8c63f``) are stated
in the round-2 report; this is an E5 adaptation, not a full reproduction.

Experiment path only: pure numpy over cached vectors, no database, no I/O.
"""
from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

import numpy as np

# Production transition table (semantic_alignment._TRANSITIONS).
PRODUCTION_TRANSITIONS: Tuple[Tuple[int, int, float], ...] = (
    (1, 1, 0.0), (1, 2, 0.15), (2, 1, 0.15), (2, 2, 0.25),
    (1, 3, 0.35), (3, 1, 0.35), (2, 3, 0.45), (3, 2, 0.45), (3, 3, 0.55),
    (1, 0, 2.2), (0, 1, 2.2),
)
# Frozen round-2 extension: Bertalign max_align=5 adds the 1:4 / 4:1 shapes the
# production table cannot express.  They inherit the largest matched penalty
# (0.55, the 3:3 level) — no open grid search; see notes-materials.md §三.
EXTENDED_TRANSITIONS: Tuple[Tuple[int, int, float], ...] = PRODUCTION_TRANSITIONS + (
    (1, 4, 0.55), (4, 1, 0.55),
)

SEARCH_BAND = 96  # semantic_alignment._SEARCH_BAND / alignment_corridor_refine


def _penalty(transitions, di: int, dj: int) -> float | None:
    for td, tj, penalty in transitions:
        if td == di and tj == dj:
            return penalty
    return None


def _group_vector(prefix: np.ndarray, start: int, end: int) -> np.ndarray:
    vector = prefix[end] - prefix[start]
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def prefix_sums(vectors: np.ndarray) -> np.ndarray:
    return np.vstack(
        [np.zeros((1, vectors.shape[1]), dtype=np.float32),
         np.cumsum(vectors, axis=0)]
    )


def group_similarity(source_prefix, target_prefix, s0, s1, t0, t1) -> float:
    if s0 == s1 or t0 == t1:
        return 0.0
    sim = float(_group_vector(source_prefix, s0, s1) @ _group_vector(target_prefix, t0, t1))
    return float(np.clip(sim, -1.0, 1.0))


def link_cost(source_prefix, target_prefix, source_lengths, target_lengths,
              s0, s1, t0, t1, ratio, transitions=PRODUCTION_TRANSITIONS):
    di, dj = s1 - s0, t1 - t0
    penalty = _penalty(transitions, di, dj)
    if penalty is None:
        raise ValueError(f"transition {di}:{dj} not admissible")
    slen = sum(source_lengths[s0:s1])
    tlen = sum(target_lengths[t0:t1])
    if di == 0 or dj == 0:
        return penalty + math.log1p(slen + tlen) / 12.0, 0.0
    sim = group_similarity(source_prefix, target_prefix, s0, s1, t0, t1)
    expected = max(ratio * slen, 1.0)
    length_cost = 0.18 * abs(math.log(max(tlen, 1) / expected))
    return penalty + (1.0 - sim) * 3.0 + length_cost, sim


def align_pass(
    source_vectors: np.ndarray,
    target_vectors: np.ndarray,
    max_align: int = 5,
    threshold: float = 0.83,
    transitions: Tuple[Tuple[int, int, float], ...] | None = None,
    *,
    source_lengths: Sequence[int] | None = None,
    target_lengths: Sequence[int] | None = None,
    ratio: float | None = None,
) -> List[Tuple[int, int, int, int, float]]:
    """Unconstrained two-stage second pass over the whole pair (small inputs).

    Returns matched links ``(s0, s1, t0, t1, confidence)`` whose group
    similarity clears ``threshold``; confidence is the group cosine.  Only the
    matched transitions with ``x + y <= max_align`` are admissible; gaps are not
    emitted (callers represent them implicitly between links).  Lengths default
    to unit (the pinned-test shape); real callers pass non-space char lengths
    and the corridor's own target/source ratio.
    """
    transitions = transitions or EXTENDED_TRANSITIONS
    sc, tc = len(source_vectors), len(target_vectors)
    sp = prefix_sums(np.asarray(source_vectors, dtype=np.float32))
    tp = prefix_sums(np.asarray(target_vectors, dtype=np.float32))
    types = [
        (di, dj, p) for di, dj, p in transitions if di > 0 and dj > 0
        and di + dj <= max_align
    ]
    sl = list(source_lengths) if source_lengths is not None else [1] * sc
    tl = list(target_lengths) if target_lengths is not None else [1] * tc
    if ratio is None:
        ratio = sum(tl) / max(sum(sl), 1)
    band = max(SEARCH_BAND, math.ceil(tc / max(sc, 1)) + 3)

    def bounds(i: int) -> Tuple[int, int]:
        expected = round(i * tc / max(sc, 1))
        return max(0, expected - band), min(tc, expected + band)

    cost: List[Dict[int, float]] = [dict() for _ in range(sc + 1)]
    back: List[Dict[int, Tuple[int, int]]] = [dict() for _ in range(sc + 1)]
    cost[0][0] = 0.0
    for i in range(sc + 1):
        lo, hi = bounds(i)
        for j in range(lo, hi + 1):
            if i == 0 and j == 0:
                continue
            best, bdi, bdj = math.inf, 0, 0
            for di, dj, _penalty in types:
                pi, pj = i - di, j - dj
                if pi < 0 or pj < 0:
                    continue
                previous = cost[pi].get(pj)
                if previous is None or previous == math.inf:
                    continue
                candidate = previous + link_cost(
                    sp, tp, sl, tl, i - di, i, j - dj, j, ratio, transitions,
                )[0]
                if candidate < best:
                    best, bdi, bdj = candidate, di, dj
            if best < math.inf:
                cost[i][j] = best
                back[i][j] = (bdi, bdj)
    links: List[Tuple[int, int, int, int, float]] = []
    i, j = sc, tc
    while i > 0 or j > 0:
        step = back[i].get(j)
        if step is None:
            return []
        di, dj = step
        confidence = group_similarity(sp, tp, i - di, i, j - dj, j)
        if confidence >= threshold:
            links.append((i - di, i, j - dj, j, confidence))
        i, j = i - di, j - dj
    links.reverse()
    return links


def _validated_anchors(
    anchors: Sequence[Tuple[int, int]],
    source_count: int,
    target_count: int,
    body: Dict[str, Sequence[int]],
) -> List[Tuple[int, int]]:
    """Sort anchors, reject crossed pairs, keep only in-body coordinates."""
    (bs0, bs1), (bt0, bt1) = body["pivot"], body["target"]
    kept: List[Tuple[int, int]] = []
    for source, target in anchors:
        if not (bs0 <= source <= bs1 and bt0 <= target <= bt1):
            continue
        if not (0 <= source <= source_count and 0 <= target <= target_count):
            continue
        kept.append((source, target))
    kept.sort()
    for (s0, t0), (s1, t1) in zip(kept, kept[1:]):
        if (s1 - s0 > 0) != (t1 - t0 > 0):
            raise ValueError("crossed anchors")
        if s0 == s1 and t0 == t1:
            raise ValueError("duplicate anchor coordinates")
    return kept


def two_pass_corridors(
    vectors: np.ndarray,
    other_vectors: np.ndarray,
    anchors: Sequence[Tuple[int, int]],
    body_ranges: Dict[str, Sequence[int]],
    *,
    max_align: int = 5,
    threshold: float = 0.83,
    transitions: Tuple[Tuple[int, int, float], ...] | None = None,
    source_lengths: Sequence[int] | None = None,
    target_lengths: Sequence[int] | None = None,
) -> List[Tuple[int, int, int, int, float]]:
    """Recover many-to-many links inside anchor-bounded body corridors.

    Recovery is confined to the *open* intervals between consecutive reliable
    anchors that both lie inside the reviewed body ranges; head and tail
    regions without a bilateral anchor boundary are never recovered, and the
    anchors themselves are never consumed.  Crossed anchors raise ValueError.
    """
    transitions = transitions or EXTENDED_TRANSITIONS
    source_count, target_count = len(vectors), len(other_vectors)
    kept = _validated_anchors(anchors, source_count, target_count, body_ranges)
    sl = list(source_lengths) if source_lengths is not None else [1] * source_count
    tl = list(target_lengths) if target_lengths is not None else [1] * target_count
    links: List[Tuple[int, int, int, int, float]] = []
    for (as_, at), (bs_, bt) in zip(kept, kept[1:]):
        s0, t0 = as_ + 1, at + 1  # open interval: never consume the anchors
        if s0 > bs_ or t0 > bt:
            continue  # zero-width or asymmetric remainder is not a corridor
        if s0 == bs_ and t0 == bt:
            continue
        window = align_pass(
            vectors[s0:bs_], other_vectors[t0:bt], max_align, threshold,
            transitions,
            source_lengths=sl[s0:bs_], target_lengths=tl[t0:bt],
        )
        for ls0, ls1, lt0, lt1, confidence in window:
            links.append((s0 + ls0, s0 + ls1, t0 + lt0, t0 + lt1, confidence))
    return links

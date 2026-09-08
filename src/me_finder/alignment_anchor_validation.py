"""Shared-token false-friend soft-anchor removal (issue #18).

A registry soft anchor (name:/term:/number:) can pair a shared surface token in two
*different* sentences, forging a mislocated landmark that squeezes the corridor and
makes the true target unreachable (e.g. JA「権威ある虚構」paired to a different ZH
「权威」 sentence).  This leaf detects and drops such anchors after the existing
similarity/corridor gates.

Duck-typed and dependency-light: anchors expose ``source_index`` / ``target_index`` /
``key``; the corridor DP is supplied as ``align_partition`` so this module does not
import ``semantic_alignment`` (no cycle).  See
reports/d-anchor-acceptance-2026-09-07.md for the acceptance evidence.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Sequence

import numpy as np


def _leave_one_out_displacement(
    anchors: Sequence, index: int, source_prefix: np.ndarray, target_prefix: np.ndarray,
    source_lengths: Sequence[int], target_lengths: Sequence[int],
    source_groups: Dict[int, np.ndarray], target_groups: Dict[int, np.ndarray],
    low_threshold: float, align_partition: Callable,
) -> int:
    """Segments the anchor's source is re-placed by when the anchor is left out.

    Re-runs the real corridor DP over the span bounded by the anchor's neighbours
    (which pin the coordinates).  A correct anchor re-places at ~0; a mislocated
    false friend jumps tens of segments.
    """
    anchor = anchors[index]
    previous = anchors[index - 1] if index > 0 else None
    following = anchors[index + 1] if index + 1 < len(anchors) else None
    s0 = (previous.source_index + 1) if previous else 0
    t0 = (previous.target_index + 1) if previous else 0
    s1 = following.source_index if following else len(source_lengths)
    t1 = following.target_index if following else len(target_lengths)
    if s1 - s0 < 2 or t1 - t0 < 2 or not s0 <= anchor.source_index < s1:
        return 0
    landed = None
    for link in align_partition(
        source_prefix, target_prefix, source_lengths, target_lengths,
        s0, s1, t0, t1, source_groups, target_groups, low_threshold,
    ):
        if link.source_start <= anchor.source_index < link.source_end and link.target_start < link.target_end:
            landed = (link.target_start + link.target_end - 1) // 2
    return abs(landed - anchor.target_index) if landed is not None else 0


def _source_prefers_other_target(anchor, source_prefix: np.ndarray, target_unit: np.ndarray, margin: float) -> bool:
    """True iff the anchor's SOURCE has a clearly better target than the anchor.

    Source-side better-alternative check (not a bidirectional mutual-nearest test):
    a false friend's source's best target is elsewhere, whereas a correct anchor is
    its own source's best target — this spares correct anchors whose displacement is
    merely inflated by a hard neighbourhood.
    """
    vector = source_prefix[anchor.source_index + 1] - source_prefix[anchor.source_index]
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return False
    vector = vector / norm
    anchor_similarity = float(vector @ target_unit[anchor.target_index])
    best_similarity = float((target_unit @ vector).max())
    return best_similarity - anchor_similarity > margin


def drop_false_friend_anchors(
    anchors: List, source_prefix: np.ndarray, target_prefix: np.ndarray,
    source_lengths: Sequence[int], target_lengths: Sequence[int],
    source_groups: Dict[int, np.ndarray], target_groups: Dict[int, np.ndarray],
    low_threshold: float, *, align_partition: Callable, context_prefixes: tuple,
    displacement_limit: int, better_alt_margin: float,
) -> List:
    """Iteratively drop the worst shared-token false-friend soft anchor.

    A context-gated anchor is dropped when its leave-one-out displacement exceeds
    ``displacement_limit`` and its source prefers another target.  Worst offender
    first, then re-validate, so a correct anchor confounded by a bad neighbour heals
    instead of being dropped.
    """
    if len(anchors) < 2:
        return anchors
    rows = target_prefix[1:] - target_prefix[:-1]
    target_unit = rows / np.clip(np.linalg.norm(rows, axis=1, keepdims=True), 1e-12, None)
    kept = list(anchors)
    while True:
        worst_index, worst_displacement = None, displacement_limit
        for index, anchor in enumerate(kept):
            if not anchor.key.startswith(context_prefixes):
                continue
            displacement = _leave_one_out_displacement(
                kept, index, source_prefix, target_prefix, source_lengths,
                target_lengths, source_groups, target_groups, low_threshold, align_partition,
            )
            if (displacement > worst_displacement
                    and _source_prefers_other_target(anchor, source_prefix, target_unit, better_alt_margin)):
                worst_index, worst_displacement = index, displacement
        if worst_index is None:
            return kept
        del kept[worst_index]

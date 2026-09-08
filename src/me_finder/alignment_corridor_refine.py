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


SEARCH_BAND = 96  # semantic_alignment._SEARCH_BAND


def corridor_ratio(source_lengths: Sequence[int], target_lengths: Sequence[int],
                   s0: int, s1: int, t0: int, t1: int) -> float:
    """Per-corridor local length ratio, exactly as ``_align_partition`` computes it."""
    return sum(target_lengths[t0:t1]) / max(sum(source_lengths[s0:s1]), 1)


def _band_bounds(source_index: int, source_count: int, target_count: int, band: int) -> Tuple[int, int]:
    if not source_count:
        return 0, target_count
    expected = round(source_index * target_count / source_count)
    return max(0, expected - band), min(target_count, expected + band)


def align_corridor(
    source_prefix: np.ndarray, target_prefix: np.ndarray,
    source_lengths: Sequence[int], target_lengths: Sequence[int],
    s0: int, s1: int, t0: int, t1: int, ratio: float,
    *, force_link: Tuple[int, int, int, int] | None = None,
    flagged_targets: frozenset = frozenset(), flagged_sources: frozenset = frozenset(),
    flagged_gap_penalty: float | None = None,
) -> Dict[str, object]:
    """Banded corridor DP reproducing ``_align_partition``'s cost and band.

    The single experimental variable is the gap penalty for *flagged* segments:
    a (0,1) gap over a target in ``flagged_targets`` (or a (1,0) gap over a source
    in ``flagged_sources``) uses ``flagged_gap_penalty`` instead of 2.2 when it is
    given.  Everything else — embeddings, similarity, match cost, band, threshold
    — is identical to production.

    Returns the minimum-cost path (absolute (s0,s1,t0,t1) links), its total cost
    and gap count.  With ``force_link`` (an absolute matched span the path must
    contain as one link) it returns the cheapest path *constrained* to pass
    through that link, on the same corridor, ratio and band — no splitting.
    ``feasible`` is False when the forced link is off-band or not an admissible
    transition; the gold constraint is a diagnostic, never an algorithm input.
    """
    sc, tc = s1 - s0, t1 - t0
    band = max(SEARCH_BAND, math.ceil(tc / max(sc, 1)) + 3)
    inf = math.inf

    def cost(di: int, dj: int, ai: int, aj: int) -> float:
        if flagged_gap_penalty is not None:
            if di == 0 and dj == 1 and (aj - 1) in flagged_targets:
                return flagged_gap_penalty + math.log1p(target_lengths[aj - 1]) / 12.0
            if dj == 0 and di == 1 and (ai - 1) in flagged_sources:
                return flagged_gap_penalty + math.log1p(source_lengths[ai - 1]) / 12.0
        return link_cost(source_prefix, target_prefix, source_lengths, target_lengths,
                         ai - di, ai, aj - dj, aj, ratio)[0]

    # forward[i] maps j -> (best_cost, di, dj) of the transition entering (i, j).
    # fcost[i] is filled *in place* as j increases so intra-row (0,1) gaps — which
    # reference fcost[i][j-1] — resolve, exactly as _align_partition's row array does.
    forward: list[Dict[int, Tuple[float, int, int]]] = [dict() for _ in range(sc + 1)]
    fcost: list[Dict[int, float]] = [dict() for _ in range(sc + 1)]
    for i in range(sc + 1):
        lo, hi = _band_bounds(i, sc, tc, band)
        for j in range(lo, hi + 1):
            if i == 0 and j == 0:
                fcost[i][j] = 0.0
                forward[i][j] = (0.0, -1, -1)
                continue
            best, bdi, bdj = inf, 0, 0
            for di, dj, _pen in TRANSITIONS:
                pi, pj = i - di, j - dj
                if pi < 0 or pj < 0:
                    continue
                pv = fcost[pi].get(pj)  # pi==i is allowed: current row already holds j' < j
                if pv is None or pv == inf:
                    continue
                cand = pv + cost(di, dj, s0 + i, t0 + j)
                if cand < best:
                    best, bdi, bdj = cand, di, dj
            fcost[i][j] = best
            forward[i][j] = (best, bdi, bdj)

    def reconstruct(back: list[Dict[int, Tuple[float, int, int]]], end_i: int, end_j: int):
        links, i, j = [], end_i, end_j
        while i > 0 or j > 0:
            step = back[i].get(j)
            if step is None:
                return None  # unreachable cell
            _c, di, dj = step
            if di < 0 or (di == 0 and dj == 0):
                return None  # start marker or dead cell without a real predecessor
            links.append((s0 + i - di, s0 + i, t0 + j - dj, t0 + j))
            i, j = i - di, j - dj
        links.reverse()
        return links

    total = fcost[sc].get(tc, inf)
    unconstrained = (reconstruct(forward, sc, tc) or []) if total < inf else []

    result: Dict[str, object] = {
        "cost": round(total, 4) if total < inf else None,
        "path": unconstrained,
        "gaps": sum(1 for a, b, c, d in unconstrained if a == b or c == d),
        "band": band,
    }
    if force_link is None:
        return result

    gi0, gi1, gj0, gj1 = force_link[0] - s0, force_link[1] - s0, force_link[2] - t0, force_link[3] - t0
    di, dj = gi1 - gi0, gj1 - gj0
    legal = (di, dj) in _PENALTY and di > 0 and dj > 0
    in_band = False
    if 0 <= gi0 <= sc and 0 <= gi1 <= sc:
        lo0, hi0 = _band_bounds(gi0, sc, tc, band)
        lo1, hi1 = _band_bounds(gi1, sc, tc, band)
        in_band = lo0 <= gj0 <= hi0 and lo1 <= gj1 <= hi1
    if not (legal and in_band):
        result["constrained"] = {"feasible": False, "legal_transition": legal, "in_band": in_band}
        return result

    # Backward pass: bcost[i][j] = min cost from (i, j) to (sc, tc).
    bcost: list[Dict[int, float]] = [dict() for _ in range(sc + 1)]
    bback: list[Dict[int, Tuple[int, int]]] = [dict() for _ in range(sc + 1)]
    bcost[sc][tc] = 0.0
    for i in range(sc, -1, -1):
        lo, hi = _band_bounds(i, sc, tc, band)
        for j in range(hi, lo - 1, -1):
            if i == sc and j == tc:
                continue
            best, bdi, bdj = inf, 0, 0
            for di2, dj2, _pen in TRANSITIONS:
                ni, nj = i + di2, j + dj2
                if ni > sc or nj > tc or ni >= len(bcost):
                    continue
                nv = bcost[ni].get(nj)
                if nv is None or nv == inf:
                    continue
                cand = nv + cost(di2, dj2, s0 + ni, t0 + nj)
                if cand < best:
                    best, bdi, bdj = cand, di2, dj2
            if best < inf:
                bcost[i][j] = best
                bback[i][j] = (bdi, bdj)
    fwd = fcost[gi0].get(gj0, inf)
    bwd = bcost[gi1].get(gj1, inf)
    if fwd == inf or bwd == inf:
        result["constrained"] = {"feasible": False, "legal_transition": True, "in_band": True,
                                 "reason": "gold endpoints unreachable within band"}
        return result
    glink = cost(di, dj, force_link[1], force_link[3])
    ctotal = fwd + glink + bwd
    # reconstruct constrained path: forward to (gi0,gj0), gold link, backward from (gi1,gj1)
    head = reconstruct(forward, gi0, gj0) or []
    tail, i, j = [], gi1, gj1
    while i < sc or j < tc:
        step = bback[i].get(j)
        if step is None:
            break
        di2, dj2 = step
        tail.append((s0 + i, s0 + i + di2, t0 + j, t0 + j + dj2))
        i, j = i + di2, j + dj2
    cpath = head + [tuple(force_link)] + tail
    result["constrained"] = {
        "feasible": ctotal < inf,
        "legal_transition": True, "in_band": True,
        "cost": round(ctotal, 4) if ctotal < inf else None,
        "extra_cost_vs_unconstrained": round(ctotal - total, 4) if (ctotal < inf and total < inf) else None,
        "path": cpath,
        "gaps": sum(1 for a, b, c, d in cpath if a == b or c == d),
    }
    return result


def detect_no_counterpart(
    source_rows: np.ndarray, target_rows: np.ndarray,
    s0: int, s1: int, t0: int, t1: int, *, threshold: float,
) -> Dict[str, list]:
    """Flag corridor segments whose best cross-side cosine is below ``threshold``.

    Explainable, corpus-general signal for an edition-only insertion or an OCR
    noise segment: the model finds no counterpart on the other side of the
    corridor.  Uses each segment's own normalised vector (single-row group), the
    same cosine the DP scores with.  No fixture id, position, or gold answer
    enters this rule; the caller supplies only a similarity threshold.
    """
    src = source_rows[s0:s1]
    tgt = target_rows[t0:t1]
    result: Dict[str, list] = {"target": [], "source": []}
    if len(src) and len(tgt):
        sims = tgt @ src.T  # (target, source) cosine
        tgt_max = sims.max(axis=1)
        src_max = sims.max(axis=0)
        result["target"] = [t0 + int(j) for j in np.flatnonzero(tgt_max < threshold)]
        result["source"] = [s0 + int(i) for i in np.flatnonzero(src_max < threshold)]
    return result


import re  # noqa: E402

# Editorial-apparatus label vocabulary shared across editions of a work — these
# segments are structural markers (a note/addition/remark heading), not running
# text, so they have no counterpart in the other edition's body.  This flags the
# *label* segment only; it never assumes the block after it lacks correspondence.
_APPARATUS_LABEL = re.compile(
    r"^\s*[\"'(\[]*\s*("
    r"addition|zusatz|zusatze|anmerkung|randbemerkung|remark|note|"
    r"translator|editor|herausgeber|ubersetzer|footnote|fussnote"
    r")\b", re.IGNORECASE)


def _alpha_ratio(text: str) -> float:
    stripped = [c for c in text if not c.isspace()]
    if not stripped:
        return 1.0
    alpha = sum(1 for c in stripped if c.isalpha())
    return alpha / len(stripped)


def is_ocr_noise(text: str) -> bool:
    """OCR-garbage segment: mostly punctuation/symbols, or a tiny symbolic scrap."""
    nonspace = [c for c in text if not c.isspace()]
    if not nonspace:
        return True
    if len(nonspace) <= 3 and _alpha_ratio(text) < 1.0:
        return True
    return _alpha_ratio(text) < 0.5


def is_apparatus_label(text: str) -> bool:
    """Short editorial-apparatus label (a heading marker), corpus-general."""
    head = text.strip()
    return bool(_APPARATUS_LABEL.match(head)) and len(head) <= 60


def detect_edition_apparatus(texts: Sequence[str], t0: int, t1: int) -> list[int]:
    """Structural edition-only segments in [t0, t1): apparatus labels + OCR noise.

    Text-structure only — no fixture id, position, embedding, or gold answer.
    Deliberately conservative: flags markers/noise, never elaboration content
    (whose correspondence status cannot be decided structurally).
    """
    return [j for j in range(t0, t1)
            if j < len(texts) and (is_apparatus_label(texts[j]) or is_ocr_noise(texts[j]))]


def arm_a_refine(
    source_prefix: np.ndarray, target_prefix: np.ndarray,
    source_lengths: Sequence[int], target_lengths: Sequence[int],
    path: Sequence[Tuple[int, int, int, int]], ratio: float,
) -> Dict[str, object]:
    """Arm A: relocate one boundary segment between adjacent links while it
    lowers the corridor total; iterate to convergence.

    Operates on the full frozen corridor path (matched *and* gap links) with the
    corridor's own length ratio.  A boundary move perturbs one internal vertex of
    the monotone path by +/-1 on the source or target axis, keeping both incident
    links admissible (spans 1..3, or a 1:0 / 0:1 gap).  Returns the refined path,
    the number of moves applied and the total cost improvement.
    """
    links = [tuple(p) for p in path]
    if not links:
        return {"path": [], "moves": 0, "improvement": 0.0}

    def lc(link) -> float:
        return link_cost(source_prefix, target_prefix, source_lengths, target_lengths, *link, ratio)[0]

    def admissible(link) -> bool:
        di, dj = link[1] - link[0], link[3] - link[2]
        if di < 0 or dj < 0:
            return False
        if di == 0 and dj == 0:
            return False
        if di == 0 or dj == 0:
            return max(di, dj) == 1  # only unit gaps
        return di <= 3 and dj <= 3

    moves = 0
    improvement = 0.0
    changed = True
    while changed:
        changed = False
        for k in range(len(links) - 1):
            a, b = links[k], links[k + 1]
            assert a[1] == b[0] and a[3] == b[2]  # contiguous vertex
            base = lc(a) + lc(b)
            best_delta, best = 0.0, None
            for ds, dt in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                na = (a[0], a[1] + ds, a[2], a[3] + dt)
                nb = (b[0] + ds, b[1], b[2] + dt, b[3])
                if not (admissible(na) and admissible(nb)):
                    continue
                delta = base - (lc(na) + lc(nb))
                if delta > best_delta + 1e-9:
                    best_delta, best = delta, (na, nb)
            if best:
                links[k], links[k + 1] = best
                improvement += best_delta
                moves += 1
                changed = True
    return {"path": links, "moves": moves, "improvement": round(improvement, 4)}


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

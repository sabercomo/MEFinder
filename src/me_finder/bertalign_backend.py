"""Optional semantic-alignment backend built on the original Bertalign.

This adapter is the boundary between MEFinder and the vendored upstream
algorithm (:mod:`me_finder._vendor.bertalign`, pinned at commit df8c63f). It
reuses upstream's LaBSE grouped embedding and two-stage DP unchanged; MEFinder
only *feeds* its already-segmented, already-located segments in and *maps* the
resulting beads back onto segment indices.

What is shared pre-processing vs. what belongs to which backend
---------------------------------------------------------------
* **Shared pre-processing** (same as the default backend): the located segment
  list and the confirmed *body range* (which segments are body vs. front/back
  matter). Out-of-body segments are excluded from the compute on both backends
  and preserved as inspectable one-sided ``rejected`` rows.
* **Default-backend only** (deliberately NOT mixed in here): heading/folio
  structural anchors, numbered-note overrides, false-friend anchor validation,
  and segment-quality demotion. Those are the self-developed DP's own machinery
  and its vector space; forcing them onto Bertalign would corrupt the "original
  algorithm" claim. Bertalign runs its own DP end to end on the body slice.

Honesty of the result
----------------------
Bertalign output is *algorithm-produced*, never human-confirmed:
* matched beads → ``review_status="automatic"``;
* insertions / deletions (one-sided beads) → ``review_status="unmatched"``.
No bead is stamped with a fake ``confidence=1.0`` and no MiniLM/E5 threshold is
applied. ``confidence`` is the plain LaBSE cosine similarity of the bead's
grouped vectors (0 for one-sided beads, which have no cross-language match),
clamped to ``[0, 1]``. It is a similarity, not a calibrated accuracy.

Locating is preserved: one input segment stays one column, beads carry segment
*indices* (never string matches), so page anchors and character offsets remain
whatever MEFinder's locating layer already recorded for those segment ids.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from .alignment_regions import alignment_body_bounds
from .semantic_alignment import SemanticAlignmentError, SemanticLink

# Distinct identifiers so Bertalign runs are never confused with the default
# backend's runs, caches, or vector space. These flow into the run identity and
# the compute protocol.
BERTALIGN_BACKEND = "bertalign"
BERTALIGN_UPSTREAM_COMMIT = "df8c63f51aa203faed9f2fe45ae39e6fca75e667"
# The model id is a *vector-space* identity: LaBSE embeddings must never be
# reused as MiniLM/E5 vectors and vice versa.
BERTALIGN_MODEL_ID = "labse-bertalign"
BERTALIGN_MODEL_HF_NAME = "sentence-transformers/LaBSE"
# Bump on any change that would alter Bertalign's output for identical inputs.
BERTALIGN_ALGORITHM = "bertalign-labse-two-pass"
BERTALIGN_ALGORITHM_VERSION = "1"


@dataclass(frozen=True)
class BertalignParams:
    """Upstream Bertalign defaults, preserved verbatim (no re-tuning)."""

    max_align: int = 5
    top_k: int = 3
    win: int = 5
    skip: float = -0.1
    margin: bool = True
    len_penalty: bool = True


def bertalign_model_dir(cache_dir: Path) -> Path:
    """Local directory holding the LaBSE snapshot for this backend.

    The model is provisioned beforehand through the managed-component mechanism;
    the compute phase only *reads* it and never downloads.
    """

    return Path(cache_dir) / "bertalign" / "labse"


def _clamp_unit(value: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return value


def _bead_confidence(
    src_vecs,
    tgt_vecs,
    src_range: Sequence[int],
    tgt_range: Sequence[int],
) -> float:
    """Plain LaBSE cosine of the bead's grouped vectors (0 if one-sided).

    Uses the same grouped vector the DP scored — ``vecs[overlap-1, last_idx]`` —
    but the raw cosine (no ``margin`` subtraction), so the stored number is an
    honest similarity rather than a DP cost.
    """

    import numpy as np

    if not len(src_range) or not len(tgt_range):
        return 0.0
    src_overlap = len(src_range)
    tgt_overlap = len(tgt_range)
    src_v = src_vecs[src_overlap - 1, src_range[-1], :]
    tgt_v = tgt_vecs[tgt_overlap - 1, tgt_range[-1], :]
    denom = float(np.linalg.norm(src_v)) * float(np.linalg.norm(tgt_v))
    if denom <= 1e-12:
        return 0.0
    return _clamp_unit(float(np.dot(src_v, tgt_v)) / denom)


def _load_encoder(model_dir: Path, device: str = "cpu"):
    """Load the local LaBSE model in this (isolated) process. Never downloads."""

    model_path = Path(model_dir)
    if not model_path.is_dir():
        raise SemanticAlignmentError(
            "Bertalign 语义模型（LaBSE）未安装；请在设置 → 译本对齐中下载 Bertalign 组件后重试。"
        )
    # Belt-and-braces: even if a bare name were ever passed, forbid any network
    # reach-out from the compute phase.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    from ._vendor.bertalign.encoder import Encoder

    return Encoder(str(model_path), device=device)


def beads_to_semantic_links(
    beads: Sequence[Tuple[Sequence[int], Sequence[int]]],
    source_count: int,
    target_count: int,
    *,
    source_start: int,
    source_end: int,
    target_start: int,
    target_end: int,
    confidence,
) -> List[SemanticLink]:
    """Map Bertalign beads (indices into the body slice) to ``SemanticLink``.

    Purely index-based — no text is inspected — so duplicate segment texts and
    intra-segment newlines can never mis-locate a bead. ``confidence`` is called
    with the *body-relative* ``(src_range, tgt_range)`` and must return a matched
    bead's similarity; one-sided beads are forced to 0.

    Beads are body-relative and contiguous; the returned links are absolute
    segment indices. Out-of-body segments are appended as one-sided ``rejected``
    rows so they stay inspectable, mirroring the default backend.
    """

    aligned: List[SemanticLink] = []
    src_cursor = 0
    tgt_cursor = 0
    for src_range, tgt_range in beads:
        # Bead indices come from numba/numpy arrays; coerce to plain ints so the
        # links serialize and persist cleanly (no numpy scalar types leak out).
        src_range = [int(i) for i in src_range]
        tgt_range = [int(j) for j in tgt_range]
        if src_range:
            s0, s1 = src_range[0], src_range[-1] + 1
            if s0 != src_cursor:
                raise SemanticAlignmentError("Bertalign 对齐路径在源侧不连续。")
        else:
            s0 = s1 = src_cursor
        if tgt_range:
            t0, t1 = tgt_range[0], tgt_range[-1] + 1
            if t0 != tgt_cursor:
                raise SemanticAlignmentError("Bertalign 对齐路径在目标侧不连续。")
        else:
            t0 = t1 = tgt_cursor
        matched = bool(src_range) and bool(tgt_range)
        score = _clamp_unit(float(confidence(src_range, tgt_range))) if matched else 0.0
        aligned.append(
            SemanticLink(
                source_start=s0 + source_start,
                source_end=s1 + source_start,
                target_start=t0 + target_start,
                target_end=t1 + target_start,
                cost=1.0 - score,
                confidence=score,
                review_status="automatic" if matched else "unmatched",
            )
        )
        src_cursor = s1
        tgt_cursor = t1

    if src_cursor != source_end - source_start or tgt_cursor != target_end - target_start:
        raise SemanticAlignmentError("Bertalign 对齐路径未覆盖完整正文范围。")

    prefix = [
        SemanticLink(i, i + 1, 0, 0, 0.0, 0.0, "rejected")
        for i in range(source_start)
    ] + [
        SemanticLink(source_start, source_start, j, j + 1, 0.0, 0.0, "rejected")
        for j in range(target_start)
    ]
    suffix = [
        SemanticLink(i, i + 1, target_end, target_end, 0.0, 0.0, "rejected")
        for i in range(source_end, source_count)
    ] + [
        SemanticLink(source_count, source_count, j, j + 1, 0.0, 0.0, "rejected")
        for j in range(target_end, target_count)
    ]
    return prefix + aligned + suffix


def align_segments_bertalign(
    source_texts: Sequence[str],
    target_texts: Sequence[str],
    *,
    model_dir: Path,
    reviewed_body_ranges: Dict[str, List[int]] | None = None,
    source_language: str = "und",
    target_language: str = "und",
    params: BertalignParams | None = None,
    device: str = "cpu",
) -> Tuple[List[SemanticLink], list]:
    """Align two located segment sequences with the original Bertalign.

    Returns ``(links, [])`` — Bertalign produces no structural anchors. Links use
    the same segment-index convention as the default backend, so publication and
    the reader consume them unchanged.
    """

    params = params or BertalignParams()
    source_texts = list(source_texts)
    target_texts = list(target_texts)

    # Shared pre-processing: same body-range determination as the default
    # backend (alignment_kernel). Reviewed ranges win; otherwise detect.
    if reviewed_body_ranges is not None:
        source_start, source_end = reviewed_body_ranges["pivot"]
        target_start, target_end = reviewed_body_ranges["target"]
    else:
        source_start, source_end = alignment_body_bounds(source_texts)
        target_start, target_end = alignment_body_bounds(target_texts)

    body_source = source_texts[source_start:source_end]
    body_target = target_texts[target_start:target_end]
    if not body_source or not body_target:
        raise SemanticAlignmentError("两本文献的正文范围都必须至少包含一个 Segment。")

    encoder = _load_encoder(model_dir, device=device)
    from ._vendor.bertalign.aligner import Bertalign

    aligner = Bertalign(
        encoder,
        body_source,
        body_target,
        src_lang=source_language,
        tgt_lang=target_language,
        max_align=params.max_align,
        top_k=params.top_k,
        win=params.win,
        skip=params.skip,
        margin=params.margin,
        len_penalty=params.len_penalty,
    )
    beads = aligner.align_sents()

    def _confidence(src_range: Sequence[int], tgt_range: Sequence[int]) -> float:
        return _bead_confidence(
            aligner.src_vecs, aligner.tgt_vecs, src_range, tgt_range
        )

    aligned = beads_to_semantic_links(
        beads,
        len(source_texts),
        len(target_texts),
        source_start=source_start,
        source_end=source_end,
        target_start=target_start,
        target_end=target_end,
        confidence=_confidence,
    )
    return aligned, []

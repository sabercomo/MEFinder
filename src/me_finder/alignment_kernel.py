"""The alignment *compute* kernel: texts → chapter-anchored semantic links.

This is the pure compute phase of ``generate_alignment`` — embeddings plus the
monotonic alignment DP — and the only step that needs NumPy / ONNX Runtime /
fastembed (imported lazily inside the function). It touches no database and
re-parses nothing: it turns two segment-text sequences into
``(SemanticLink, HeadingAnchor)`` lists.

It lives in its own module (the boundary the compute seam introduced) so the
main process can drive it either in-process or, via
:mod:`me_finder.alignment_compute`, in a separate process. ``text_alignment``
re-exports ``align_segment_sequences`` for backward compatibility, so existing
importers and test patch targets keep working.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from .alignment_anchors import HeadingAnchor
from .alignment_regions import alignment_body_bounds
from .edition_folio_anchors import (
    FolioBoundaryCandidate,
    verify_folio_boundary_candidates,
)
from .embedding_models import (
    DEFAULT_EMBEDDING_MODEL_ID,
    AlignmentThresholds,
    embedding_model_config,
)
from .semantic_alignment import (
    EmbeddingProvider,
    SemanticLink,
    align_semantic_sequences,
    embed_text_sequences,
    find_heading_anchors,
)


def align_segment_sequences(
    source_texts: Sequence[str],
    target_texts: Sequence[str],
    *,
    cache_dir: Path,
    embedding_provider: EmbeddingProvider | None = None,
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
    thresholds: AlignmentThresholds | None = None,
    reusable_sequences: Sequence[Sequence[str]] = (),
    folio_candidates: Sequence[FolioBoundaryCandidate] = (),
    source_language: str = "und",
    target_language: str = "und",
    reviewed_body_ranges: Dict[str, List[int]] | None = None,
) -> Tuple[List[SemanticLink], list]:
    """Return chapter-anchored semantic links and the anchors used."""
    import numpy as np

    active_thresholds = thresholds or embedding_model_config(
        embedding_model_id
    ).thresholds
    if embedding_provider is None:
        source_vectors, target_vectors = embed_text_sequences(
            [source_texts, target_texts],
            cache_dir,
            reusable_sequences=reusable_sequences,
            model_id=embedding_model_id,
        )
        embeddings = np.vstack([source_vectors, target_vectors])
    else:
        embeddings = embedding_provider([*source_texts, *target_texts], cache_dir)
        source_vectors = embeddings[: len(source_texts)]
        target_vectors = embeddings[len(source_texts) :]
    verified_folios = verify_folio_boundary_candidates(
        folio_candidates, source_vectors, target_vectors
    )
    source_start, source_end = (reviewed_body_ranges["pivot"] if reviewed_body_ranges is not None
                                else alignment_body_bounds(source_texts))
    target_start, target_end = (reviewed_body_ranges["target"] if reviewed_body_ranges is not None
                                else alignment_body_bounds(target_texts))
    structural_anchors = [
        replace(anchor, source_index=anchor.source_index - source_start,
                target_index=anchor.target_index - target_start)
        for anchor in find_heading_anchors(source_texts, target_texts)
        if source_start <= anchor.source_index < source_end
        and target_start <= anchor.target_index < target_end
    ]
    aligned, anchors = align_semantic_sequences(
        source_texts[source_start:source_end],
        target_texts[target_start:target_end],
        np.vstack([
            source_vectors[source_start:source_end],
            target_vectors[target_start:target_end],
        ]),
        [
            HeadingAnchor(
                candidate.pivot_segment_index - source_start,
                candidate.target_segment_index - target_start,
                candidate.key,
            )
            for candidate in verified_folios
            if source_start <= candidate.pivot_segment_index < source_end
            and target_start <= candidate.target_segment_index < target_end
        ],
        source_language=source_language,
        target_language=target_language,
        thresholds=active_thresholds,
        structural_anchors=structural_anchors,
    )
    aligned = [
        replace(link,
                source_start=link.source_start + source_start,
                source_end=link.source_end + source_start,
                target_start=link.target_start + target_start,
                target_end=link.target_end + target_start)
        for link in aligned
    ]
    # Excluded segments remain inspectable as one-sided rejected rows. No
    # cross-book similarity exists for those rows, so confidence is zero.
    prefix = [
        SemanticLink(i, i + 1, 0, 0, 0.0, 0.0, "rejected")
        for i in range(source_start)
    ] + [
        SemanticLink(source_start, source_start, j, j + 1, 0.0, 0.0, "rejected")
        for j in range(target_start)
    ]
    suffix = [
        SemanticLink(i, i + 1, target_end, target_end, 0.0, 0.0, "rejected")
        for i in range(source_end, len(source_texts))
    ] + [
        SemanticLink(len(source_texts), len(source_texts), j, j + 1, 0.0, 0.0, "rejected")
        for j in range(target_end, len(target_texts))
    ]
    return prefix + aligned + suffix, [
        replace(anchor, source_index=anchor.source_index + source_start,
                target_index=anchor.target_index + target_start)
        for anchor in anchors
    ]

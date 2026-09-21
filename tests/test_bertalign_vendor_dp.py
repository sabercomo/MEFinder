"""Runs the *real* vendored Bertalign two-stage DP on CPU (faiss + numba).

Gated on the Bertalign compute stack being importable, so it is skipped in the
default runtime (which ships only fastembed/onnxruntime) and on CI, but runs in
the dedicated Bertalign runtime. It uses a deterministic stub encoder (no model
download) so it proves the vendored corelib/aligner actually execute end-to-end
and produce a full monotonic cover — not that LaBSE is correct (the real-model
proof is the offline DB verification recorded in the report).
"""

from __future__ import annotations

import importlib.util
import unittest

import numpy as np

_HAVE_STACK = all(
    importlib.util.find_spec(name) is not None for name in ("faiss", "numba")
)


class _StubEncoder:
    """Deterministic grouped embeddings: concept i -> basis vector e_i.

    A sentence's vector is the (normalized) sum of its concept basis vectors, so
    parallel sentences that share a concept id embed identically across the two
    "languages" and the DP's optimal path is the obvious 1-1 chain.
    """

    def __init__(self, concept_of, dim):
        self._concept_of = concept_of
        self._dim = dim

    def _vec(self, sent):
        v = np.zeros(self._dim, dtype=np.float32)
        for concept in self._concept_of(sent):
            v[concept] += 1.0
        norm = float(np.linalg.norm(v)) or 1.0
        return v / norm

    def transform(self, sents, num_overlaps):
        base = [self._vec(s) for s in sents]
        n = len(sents)
        sent_vecs = np.zeros((num_overlaps, n, self._dim), dtype=np.float32)
        len_vecs = np.zeros((num_overlaps, n), dtype=np.float32)
        for o in range(num_overlaps):
            for idx in range(n):
                lo = max(0, idx - o)
                group = np.sum(base[lo : idx + 1], axis=0)
                norm = float(np.linalg.norm(group)) or 1.0
                sent_vecs[o, idx] = group / norm
                len_vecs[o, idx] = sum(
                    len(sents[k].encode("utf-8")) for k in range(lo, idx + 1)
                )
        return sent_vecs, len_vecs


@unittest.skipUnless(_HAVE_STACK, "Bertalign compute stack (faiss/numba) not installed")
class VendorDpTests(unittest.TestCase):
    def test_real_two_stage_dp_covers_parallel_input(self) -> None:
        from src.me_finder._vendor.bertalign.aligner import Bertalign
        from src.me_finder.bertalign_backend import beads_to_semantic_links

        # Six parallel "sentences"; source concept i <-> target concept i.
        src = [f"s{i}" for i in range(6)]
        tgt = [f"t{i}" for i in range(6)]

        def concept_of(sent):
            return [int(sent[1:])]

        encoder = _StubEncoder(concept_of, dim=8)
        aligner = Bertalign(encoder, src, tgt, src_lang="de", tgt_lang="zh")
        beads = aligner.align_sents()

        # Beads are body-relative; map them the same way production does.
        links = beads_to_semantic_links(
            beads,
            len(src),
            len(tgt),
            source_start=0,
            source_end=len(src),
            target_start=0,
            target_end=len(tgt),
            confidence=lambda s, t: 1.0,
        )
        # Full monotonic coverage on both sides.
        covered_src = [i for lk in links for i in range(lk.source_start, lk.source_end)]
        covered_tgt = [j for lk in links for j in range(lk.target_start, lk.target_end)]
        self.assertEqual(covered_src, list(range(6)))
        self.assertEqual(covered_tgt, list(range(6)))
        # Parallel input aligns 1-1.
        matched = [lk for lk in links if lk.review_status == "automatic"]
        self.assertEqual(len(matched), 6)
        for lk in matched:
            self.assertEqual(lk.source_end - lk.source_start, 1)
            self.assertEqual(lk.target_end - lk.target_start, 1)
            self.assertEqual(lk.source_start, lk.target_start)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

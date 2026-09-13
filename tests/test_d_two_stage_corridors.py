"""Round-2 corridor ablation driver checks (issue #18 experiment path).

The experiment DP (``scripts.d_two_stage_corridors.corridor_dp``) must be
cost- and path-equivalent to the production ``_align_partition`` when given the
production transition table and the production aggregation similarity —
otherwise arms B/C/D4 would not be comparable to the baseline.  The concat
representation is checked for shape/normalisation invariants and for the
Bertalign-style 1:4 recovery the production table cannot express.
"""
import unittest
from unittest import TestCase

import numpy as np

try:
    from scripts.d_two_stage_corridors import (
        EXTENDED_TRANSITIONS,
        PRODUCTION_TRANSITIONS,
        ConcatEmbedder,
        corridor_dp,
        make_agg_sim,
        make_concat_sim,
    )
    from src.me_finder.semantic_alignment import _align_partition, _group_rows
    _AVAILABLE = True
except ImportError:  # pragma: no cover - numpy/typing differences on CI
    _AVAILABLE = False


def _random_unit_vectors(count: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vectors = rng.normal(size=(count, 16)).astype(np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def _production_partition_links(source_vectors, target_vectors, s0, s1, t0, t1):
    prefix_s = np.vstack([np.zeros((1, source_vectors.shape[1]), dtype=np.float32),
                          np.cumsum(source_vectors, axis=0)])
    prefix_t = np.vstack([np.zeros((1, target_vectors.shape[1]), dtype=np.float32),
                          np.cumsum(target_vectors, axis=0)])
    groups_s = _group_rows(prefix_s)
    groups_t = _group_rows(prefix_t)
    lengths_s = [max(1, 7 + (i * 13) % 40) for i in range(len(source_vectors))]
    lengths_t = [max(1, 9 + (i * 29) % 55) for i in range(len(target_vectors))]
    links = _align_partition(prefix_s, prefix_t, lengths_s, lengths_t,
                             s0, s1, t0, t1, groups_s, groups_t, 0.83)
    return {(l.source_start, l.source_end, l.target_start, l.target_end)
            for l in links}, lengths_s, lengths_t


@unittest.skipUnless(_AVAILABLE, "experiment driver dependencies unavailable")
class CorridorDpEquivalenceTests(TestCase):
    def test_production_settings_reproduce_align_partition(self):
        source = _random_unit_vectors(60, seed=11)
        target = _random_unit_vectors(52, seed=12)
        s0, s1, t0, t1 = 5, 55, 4, 48
        expected, lengths_s, lengths_t = _production_partition_links(
            source, target, s0, s1, t0, t1)
        prefix_s = np.vstack([np.zeros((1, source.shape[1]), dtype=np.float32),
                              np.cumsum(source, axis=0)])
        prefix_t = np.vstack([np.zeros((1, target.shape[1]), dtype=np.float32),
                              np.cumsum(target, axis=0)])
        sim = make_agg_sim(prefix_s, prefix_t, 0, 0)
        rebuilt = corridor_dp(sim, lengths_s, lengths_t, s0, s1, t0, t1,
                              PRODUCTION_TRANSITIONS)
        self.assertEqual({(a, b, c, d) for a, b, c, d, _ in rebuilt["path"]},
                         expected)

    def test_extended_table_is_a_superset_of_production_paths(self):
        production_types = {(di, dj) for di, dj, _ in PRODUCTION_TRANSITIONS}
        extended_types = {(di, dj) for di, dj, _ in EXTENDED_TRANSITIONS}
        self.assertTrue(production_types < extended_types)
        self.assertEqual(extended_types - production_types, {(1, 4), (4, 1)})

    def test_extended_path_can_recover_one_to_four(self):
        # One source segment whose aggregated vector sits between four target
        # segments: only the 1:4 bead can express the true grouping.
        base = _random_unit_vectors(1, seed=21)
        pieces = []
        for index in range(4):
            piece = base + 0.05 * index
            pieces.append(piece / np.linalg.norm(piece))
        target = np.vstack(pieces).astype(np.float32)
        source = (target.sum(axis=0, keepdims=True)).astype(np.float32)
        source = source / np.linalg.norm(source, axis=1, keepdims=True)
        lengths_s = [10]
        lengths_t = [10, 10, 10, 10]
        prefix_s = np.vstack([np.zeros((1, source.shape[1]), dtype=np.float32),
                              np.cumsum(source, axis=0)])
        prefix_t = np.vstack([np.zeros((1, target.shape[1]), dtype=np.float32),
                              np.cumsum(target, axis=0)])
        sim = make_agg_sim(prefix_s, prefix_t, 0, 0)
        extended = corridor_dp(sim, lengths_s, lengths_t, 0, 1, 0, 4,
                               EXTENDED_TRANSITIONS)
        production = corridor_dp(sim, lengths_s, lengths_t, 0, 1, 0, 4,
                                 PRODUCTION_TRANSITIONS)
        self.assertIn((0, 1, 0, 4), [(a, b, c, d) for a, b, c, d, _ in extended["path"]])
        self.assertNotIn((0, 1, 0, 4),
                         [(a, b, c, d) for a, b, c, d, _ in production["path"]])

    def test_concat_similarity_requires_unit_overlap_rows(self):
        embedder_like = [
            np.array([[1.0, 0.0], [0.70710678, 0.70710678]], dtype=np.float32),
            np.array([[0.6, 0.8]], dtype=np.float32),
        ]
        sim = make_concat_sim(embedder_like, embedder_like, 0, 0)
        value = sim(0, 1, 0, 1)
        self.assertAlmostEqual(abs(value), 1.0, places=5)
        with self.assertRaises(ValueError):
            sim(0, 1, 0, 3)  # target span 3 > available overlap rows (2)

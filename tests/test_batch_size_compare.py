"""Pure comparison helpers for the batch64-vs-16 pre-experiment.

The model-dependent driver needs a local model cache and is not exercised here;
these tests pin the vector-diff, full-link-identity and verdict logic that turn
raw batch outputs into an adoption recommendation — the part that must be
correct so a real run cannot be misread (e.g. "只比 967 计数").
"""

from __future__ import annotations

import sqlite3
import unittest

import numpy as np

from scripts.batch_size_compare import (
    link_set_diff,
    read_links,
    vector_diff_stats,
    verdict,
)


class VectorDiffStatsTests(unittest.TestCase):
    def test_identical_matrices_are_bitwise_equal(self) -> None:
        a = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        stats = vector_diff_stats(a, a)
        self.assertEqual(stats["bitwise_equal_fraction"], 1.0)
        self.assertEqual(stats["max_abs"], 0.0)
        self.assertEqual(stats["rows_above_noise"], 0)

    def test_tiny_float_noise_is_within_threshold(self) -> None:
        a = np.array([[1.0, 2.0]], dtype=np.float64)
        b = a + 1e-6
        stats = vector_diff_stats(a, b)
        self.assertGreater(stats["max_abs"], 0.0)
        self.assertEqual(stats["rows_above_noise"], 0)
        self.assertLess(stats["bitwise_equal_fraction"], 1.0)

    def test_large_difference_exceeds_noise(self) -> None:
        a = np.array([[1.0, 0.0]], dtype=np.float64)
        b = np.array([[0.0, 1.0]], dtype=np.float64)
        stats = vector_diff_stats(a, b)
        self.assertEqual(stats["rows_above_noise"], 1)
        self.assertLess(stats["min_cosine"], 0.5)

    def test_shape_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            vector_diff_stats(np.zeros((2, 3)), np.zeros((2, 4)))

    def test_length_buckets_cover_boundary_samples(self) -> None:
        a = np.zeros((3, 2))
        b = np.array([[0.5, 0], [0, 0], [0.01, 0]])  # short big, medium zero, long tiny
        stats = vector_diff_stats(a, b, lengths=[5, 50, 200])
        self.assertEqual(stats["by_length_rows"], {"short(<20)": 1, "medium(20-99)": 1, "long(>=100)": 1})
        self.assertAlmostEqual(stats["by_length_max_abs"]["short(<20)"], 0.5)
        self.assertEqual(stats["by_length_max_abs"]["medium(20-99)"], 0.0)


class LinkSetDiffTests(unittest.TestCase):
    def _link(self, order, pivot, target, status="automatic", conf=0.9, cost=0.1, anchor=""):
        return {"order_index": order, "pivot_segments": pivot, "target_segments": target,
                "review_status": status, "confidence": conf, "cost": cost, "anchor_key": anchor}

    def test_identical_link_sets(self) -> None:
        a = [self._link(0, ["p0"], ["t0"]), self._link(1, ["p1"], ["t1"])]
        b = [self._link(0, ["p0"], ["t0"]), self._link(1, ["p1"], ["t1"])]
        diff = link_set_diff(a, b)
        self.assertTrue(diff["identical_structure"])
        self.assertEqual(diff["flipped_count"], 0)

    def test_same_count_but_flipped_membership_is_detected(self) -> None:
        # Both have 2 links (count identical — the trap the user warned about),
        # but link 1's target membership differs -> a real structural change.
        a = [self._link(0, ["p0"], ["t0"]), self._link(1, ["p1"], ["t1"])]
        b = [self._link(0, ["p0"], ["t0"]), self._link(1, ["p1"], ["t2"])]
        diff = link_set_diff(a, b)
        self.assertEqual(diff["count_a"], diff["count_b"])  # counts equal
        self.assertFalse(diff["identical_structure"])  # but structure differs
        self.assertEqual(diff["flipped_count"], 1)
        self.assertIn(1, diff["flipped_order_indices"])

    def test_score_deltas_reported_for_common_links(self) -> None:
        a = [self._link(0, ["p0"], ["t0"], conf=0.90, cost=0.10)]
        b = [self._link(0, ["p0"], ["t0"], conf=0.88, cost=0.13)]
        diff = link_set_diff(a, b)
        self.assertTrue(diff["identical_structure"])
        self.assertAlmostEqual(diff["max_confidence_delta"], 0.02)
        self.assertAlmostEqual(diff["max_cost_delta"], 0.03)


class VerdictTests(unittest.TestCase):
    def _vec(self, frac, above):
        return {"bitwise_equal_fraction": frac, "rows_above_noise": above}

    def test_bitwise_identical_is_safe(self) -> None:
        v = verdict(self._vec(1.0, 0), {"identical_structure": True, "flipped_count": 0})
        self.assertEqual(v["recommendation"], "safe-bitwise-identical")
        self.assertTrue(v["do_not_change_product_default"])

    def test_within_noise_flags_cache_version(self) -> None:
        v = verdict(self._vec(0.0, 0), {"identical_structure": True, "flipped_count": 0})
        self.assertEqual(v["recommendation"], "safe-within-noise-but-cache-version")

    def test_changed_links_is_not_safe(self) -> None:
        v = verdict(self._vec(1.0, 0), {"identical_structure": False, "flipped_count": 3})
        self.assertEqual(v["recommendation"], "not-safe-links-changed")


class ReadLinksTests(unittest.TestCase):
    def test_read_links_joins_members(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            "CREATE TABLE alignment_links(alignment_link_id TEXT, alignment_run_id TEXT, "
            "order_index INT, cost REAL, review_status TEXT, confidence REAL, anchor_key TEXT);"
            "CREATE TABLE alignment_link_members(alignment_link_id TEXT, side TEXT, "
            "segment_id TEXT, member_order INT);"
        )
        connection.execute("INSERT INTO alignment_links VALUES ('L0','R',0,0.1,'automatic',0.9,'h:1')")
        connection.executemany(
            "INSERT INTO alignment_link_members VALUES (?,?,?,?)",
            [("L0", "pivot", "p0", 0), ("L0", "target", "t0", 0), ("L0", "target", "t1", 1)],
        )
        links = read_links(connection, "R")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["pivot_segments"], ["p0"])
        self.assertEqual(links[0]["target_segments"], ["t0", "t1"])
        self.assertEqual(links[0]["anchor_key"], "h:1")


if __name__ == "__main__":
    unittest.main()

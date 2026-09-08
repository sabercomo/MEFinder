"""Targeted tests for shared-token false-friend soft-anchor removal (issue #18).

Candidate rule: after the existing similarity/corridor gates,
``semantic_alignment._validate_soft_anchors`` calls
``alignment_anchor_validation.drop_false_friend_anchors`` to iteratively drop a
context-gated anchor whose leave-one-out re-placement jumps far AND whose source has
a clearly better target than the anchor.  See reports/d-anchor-acceptance-2026-09-07.md.
"""
import unittest

import numpy as np

from src.me_finder import semantic_alignment as sa
from src.me_finder import alignment_anchor_validation as av
from src.me_finder.alignment_anchors import HeadingAnchor


def _diagonal(n, dim=48, seed=0):
    """n near-orthonormal vectors; source[i] and target[i] are the same vector."""
    rng = np.random.default_rng(seed)
    rows = sa._normalized_rows(rng.standard_normal((n, dim)).astype(np.float32))
    prefix = np.vstack([np.zeros((1, dim), np.float32), np.cumsum(rows, axis=0)])
    return rows, prefix


def _drop(anchors, sp, tp, limit):
    sg, tg = sa._group_rows(sp), sa._group_rows(tp)
    lengths = [1] * (sp.shape[0] - 1)
    return av.drop_false_friend_anchors(
        anchors, sp, tp, lengths, lengths, sg, tg, 0.0,
        align_partition=sa._align_partition, context_prefixes=sa._CONTEXT_GATED_ANCHOR_PREFIXES,
        displacement_limit=limit, better_alt_margin=sa._ANCHOR_BETTER_ALT_MARGIN,
    )


class SourcePrefersOtherTarget(unittest.TestCase):
    def test_true_for_false_friend_false_for_correct(self) -> None:
        rows, sp = _diagonal(20)
        correct = HeadingAnchor(7, 7, "name:x")
        false_friend = HeadingAnchor(7, 15, "name:x")
        self.assertFalse(av._source_prefers_other_target(correct, sp, rows, sa._ANCHOR_BETTER_ALT_MARGIN))
        self.assertTrue(av._source_prefers_other_target(false_friend, sp, rows, sa._ANCHOR_BETTER_ALT_MARGIN))


class DropFalseFriendAnchors(unittest.TestCase):
    def test_drops_mislocated_soft_anchor_keeps_correct(self) -> None:
        _, sp = _diagonal(30)
        anchors = [HeadingAnchor(0, 0, "name:a"),
                   HeadingAnchor(10, 10, "name:good"),   # correct, displacement ~0
                   HeadingAnchor(15, 25, "name:bad"),     # false friend, source 15 -> target 15
                   HeadingAnchor(29, 29, "name:z")]
        keys = {(a.source_index, a.target_index) for a in _drop(anchors, sp, sp, limit=5)}
        self.assertIn((10, 10), keys)
        self.assertNotIn((15, 25), keys)

    def test_keeps_correct_anchor_in_hard_region(self) -> None:
        # Guards the number:1642 false positive: a correct anchor (its source's own
        # best target) is never dropped, even at a very low displacement threshold.
        _, sp = _diagonal(30, seed=3)
        anchors = [HeadingAnchor(0, 0, "name:a"), HeadingAnchor(15, 15, "name:correct"),
                   HeadingAnchor(29, 29, "name:z")]
        keys = {(a.source_index, a.target_index) for a in _drop(anchors, sp, sp, limit=1)}
        self.assertIn((15, 15), keys)


class ValidateSoftAnchorsBackwardCompatible(unittest.TestCase):
    def test_without_lengths_no_drop_pass(self) -> None:
        _, sp = _diagonal(20)
        anchors = [HeadingAnchor(0, 0, "paragraph:1"), HeadingAnchor(10, 10, "name:x")]
        kept = sa._validate_soft_anchors(anchors, sp, sp, 0.0)
        self.assertEqual(len(kept), 2)


if __name__ == "__main__":
    unittest.main()

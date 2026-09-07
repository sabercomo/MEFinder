"""Guard the D-experiment leaf module's cost fidelity (issue #18).

The corridor experiment's conclusion rests on ``link_cost`` reproducing the
production DP's per-transition cost exactly.  If ``semantic_alignment`` changes
its cost model, this test fails and the experiment report must be re-derived.
"""
import unittest

import numpy as np

from src.me_finder import alignment_corridor_refine as refine
from src.me_finder import semantic_alignment as sa


class LinkCostFidelity(unittest.TestCase):
    def test_link_cost_matches_transition_cost(self) -> None:
        rng = np.random.default_rng(20260907)
        source = sa._normalized_rows(rng.standard_normal((12, 8)).astype(np.float32))
        target = sa._normalized_rows(rng.standard_normal((16, 8)).astype(np.float32))
        sp, tp = refine.prefix_sums(source), refine.prefix_sums(target)
        slen = [max(1, int(v)) for v in rng.integers(3, 40, size=12)]
        tlen = [max(1, int(v)) for v in rng.integers(3, 40, size=16)]
        ratio = sum(tlen) / max(sum(slen), 1)
        for di, dj, penalty in sa._TRANSITIONS:
            if di == 0 or dj == 0:
                continue
            s0, t0 = 2, 3
            mine_cost, mine_sim = refine.link_cost(sp, tp, slen, tlen, s0, s0 + di, t0, t0 + dj, ratio)
            prod_cost, prod_sim = sa._transition_cost(sp, tp, slen, tlen, s0, t0, di, dj, ratio, penalty)
            self.assertAlmostEqual(mine_cost, prod_cost, places=5, msg=f"cost {di}:{dj}")
            self.assertAlmostEqual(mine_sim, prod_sim, places=5, msg=f"sim {di}:{dj}")

    def test_gap_to_reach_correct_counts_intervening_segments(self) -> None:
        tlen = [10] * 30
        gap = refine.gap_to_reach_correct([1] * 10, tlen, (5, 8), (10, 11))
        self.assertEqual(gap["gapped_segments"], 2)  # target segments 8, 9
        self.assertGreater(gap["gap_cost"], 2 * 2.2)


if __name__ == "__main__":
    unittest.main()

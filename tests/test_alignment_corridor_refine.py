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

    def test_align_corridor_reproduces_align_partition(self) -> None:
        rng = np.random.default_rng(613)
        src = sa._normalized_rows(rng.standard_normal((18, 8)).astype(np.float32))
        tgt = sa._normalized_rows(rng.standard_normal((26, 8)).astype(np.float32))
        sp, tp = refine.prefix_sums(src), refine.prefix_sums(tgt)
        sg, tg = sa._group_rows(sp), sa._group_rows(tp)
        slen = [max(1, int(v)) for v in rng.integers(3, 40, size=18)]
        tlen = [max(1, int(v)) for v in rng.integers(3, 40, size=26)]
        ratio = refine.corridor_ratio(slen, tlen, 0, 18, 0, 26)
        prod = [(link.source_start, link.source_end, link.target_start, link.target_end)
                for link in sa._align_partition(sp, tp, slen, tlen, 0, 18, 0, 26, sg, tg, 0.83)]
        mine = refine.align_corridor(sp, tp, slen, tlen, 0, 18, 0, 26, ratio)["path"]
        self.assertEqual(prod, mine)

    def test_edition_apparatus_detector_flags_labels_and_ocr_only(self) -> None:
        texts = ["Real running sentence about needs and means.",
                 "Addition (H).",           # apparatus label (short) -> flagged
                 "Another full content sentence with real words here.",
                 "1'1 \n' ;",               # OCR noise -> flagged
                 "Note, in passing, that this is a long ordinary sentence which merely happens to open with the word note."]
        flagged = refine.detect_edition_apparatus(texts, 0, len(texts))
        self.assertEqual(flagged, [1, 3])  # short label + OCR only; running content (even index 4 >60 chars) not flagged

    def test_flagged_gap_penalty_lowers_only_flagged_gap_cost(self) -> None:
        rng = np.random.default_rng(9)
        src = sa._normalized_rows(rng.standard_normal((6, 8)).astype(np.float32))
        tgt = sa._normalized_rows(rng.standard_normal((9, 8)).astype(np.float32))
        sp, tp = refine.prefix_sums(src), refine.prefix_sums(tgt)
        slen, tlen = [7] * 6, [7] * 9
        ratio = refine.corridor_ratio(slen, tlen, 0, 6, 0, 9)
        base = refine.align_corridor(sp, tp, slen, tlen, 0, 6, 0, 9, ratio)
        disc = refine.align_corridor(sp, tp, slen, tlen, 0, 6, 0, 9, ratio,
                                     flagged_targets=frozenset(range(9)), flagged_gap_penalty=0.1)
        self.assertLessEqual(disc["cost"], base["cost"])  # cheaper gaps cannot raise the optimum

    def test_constrained_path_never_cheaper_than_unconstrained(self) -> None:
        rng = np.random.default_rng(71)
        src = sa._normalized_rows(rng.standard_normal((12, 8)).astype(np.float32))
        tgt = sa._normalized_rows(rng.standard_normal((14, 8)).astype(np.float32))
        sp, tp = refine.prefix_sums(src), refine.prefix_sums(tgt)
        slen, tlen = [7] * 12, [7] * 14
        ratio = refine.corridor_ratio(slen, tlen, 0, 12, 0, 14)
        res = refine.align_corridor(sp, tp, slen, tlen, 0, 12, 0, 14, ratio, force_link=(5, 6, 3, 4))
        con = res["constrained"]
        if con.get("feasible"):
            self.assertGreaterEqual(con["extra_cost_vs_unconstrained"], -1e-6)


if __name__ == "__main__":
    unittest.main()

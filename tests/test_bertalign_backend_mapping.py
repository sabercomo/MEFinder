"""Offline tests for the Bertalign bead -> SemanticLink index mapping.

These exercise the locating contract without importing torch / faiss / numba or
loading any model: they feed synthetic beads straight into
``beads_to_semantic_links`` and assert the index math. The point is that mapping
is purely index-based, so duplicate segment texts and intra-segment newlines can
never mis-locate a bead.
"""

from __future__ import annotations

import unittest

from src.me_finder.bertalign_backend import beads_to_semantic_links
from src.me_finder.semantic_alignment import SemanticAlignmentError


def _links(beads, src_n, tgt_n, *, ss=0, se=None, ts=0, te=None, conf=0.9):
    return beads_to_semantic_links(
        beads,
        src_n,
        tgt_n,
        source_start=ss,
        source_end=src_n if se is None else se,
        target_start=ts,
        target_end=tgt_n if te is None else te,
        confidence=lambda s, t: conf,
    )


class BeadMappingTests(unittest.TestCase):
    def test_one_to_one_chain(self) -> None:
        beads = [([0], [0]), ([1], [1]), ([2], [2])]
        links = _links(beads, 3, 3)
        self.assertEqual(len(links), 3)
        for i, link in enumerate(links):
            self.assertEqual(
                (link.source_start, link.source_end, link.target_start, link.target_end),
                (i, i + 1, i, i + 1),
            )
            self.assertEqual(link.review_status, "automatic")
            self.assertAlmostEqual(link.confidence, 0.9)

    def test_many_to_many_bead_spans_all_members(self) -> None:
        # 2 source segments align to 3 target segments as one m-n bead.
        beads = [([0, 1], [0, 1, 2])]
        links = _links(beads, 2, 3)
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertEqual(link.source_start, 0)
        self.assertEqual(link.source_end, 2)
        self.assertEqual(link.target_start, 0)
        self.assertEqual(link.target_end, 3)
        self.assertEqual(link.review_status, "automatic")

    def test_insertion_and_deletion_are_one_sided_unmatched(self) -> None:
        # src0<->tgt0, then a target insertion (0-1), then a source deletion (1-0).
        beads = [([0], [0]), ([], [1]), ([1], [])]
        links = _links(beads, 2, 2)
        self.assertEqual(links[0].review_status, "automatic")
        insertion = links[1]
        self.assertEqual(
            (insertion.source_start, insertion.source_end), (1, 1)
        )  # zero-width on source side
        self.assertEqual((insertion.target_start, insertion.target_end), (1, 2))
        self.assertEqual(insertion.review_status, "unmatched")
        self.assertEqual(insertion.confidence, 0.0)
        deletion = links[2]
        self.assertEqual((deletion.source_start, deletion.source_end), (1, 2))
        self.assertEqual(
            (deletion.target_start, deletion.target_end), (2, 2)
        )  # zero-width on target side
        self.assertEqual(deletion.review_status, "unmatched")

    def test_body_range_offsets_and_excluded_rejected_rows(self) -> None:
        # Body is source[1:3], target[1:2]; segments outside stay rejected.
        beads = [([0], [0]), ([1], [])]
        links = _links(beads, 4, 3, ss=1, se=3, ts=1, te=2)
        rejected = [x for x in links if x.review_status == "rejected"]
        body = [x for x in links if x.review_status != "rejected"]
        # source 0 and 3 excluded; target 0 and 2 excluded -> 4 rejected rows.
        self.assertEqual(len(rejected), 4)
        # first body bead maps to absolute source 1<->target 1.
        self.assertEqual(
            (body[0].source_start, body[0].source_end, body[0].target_start, body[0].target_end),
            (1, 2, 1, 2),
        )
        # rejected rows carry zero confidence (no cross-book similarity).
        self.assertTrue(all(x.confidence == 0.0 for x in rejected))

    def test_duplicate_texts_and_newlines_do_not_affect_index_mapping(self) -> None:
        # Duplicate texts / embedded newlines never enter the mapping (index only).
        beads = [([0], [0]), ([1], [1])]
        links = _links(beads, 2, 2)
        self.assertEqual(links[0].source_start, 0)
        self.assertEqual(links[1].source_start, 1)

    def test_non_contiguous_bead_rejected(self) -> None:
        with self.assertRaises(SemanticAlignmentError):
            _links([([0], [0]), ([2], [1])], 3, 2)  # skips source index 1

    def test_incomplete_coverage_rejected(self) -> None:
        with self.assertRaises(SemanticAlignmentError):
            _links([([0], [0])], 2, 2)  # leaves source[1]/target[1] uncovered


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

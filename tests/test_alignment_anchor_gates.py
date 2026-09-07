"""Regression fixtures for the anchor-admission and segment-quality fixes (A/B/C).

These reproduce the failure modes surfaced by the E5 full rerun review
(reports/e5-full-rerun-2026-09-06.md):

* A class -- same surface term, different meaning: ``term:l-age-d-or`` paired
  a French "golden age of fossil man" paragraph with a Chinese "golden age of
  glass packaging" paragraph (R3, FR->ZH 消费社会, P811->T808).
* B class -- a note number reused across chapters becomes a hard anchor:
  ``number:18`` paired a 6th-chapter side note with a 1st-chapter side note
  (R9, JA->ZH 战斗美少女, P693->T669, P2966->T670), collapsing a 2,273-segment
  source corridor into a single target segment.
* C class -- a lone noise segment carries a body match: an OCR page-layout
  ``|`` was paired with three Chinese body paragraphs at 0.85 (R14 P2512).

All are exercised at the ``align_semantic_sequences`` layer with synthetic
embeddings so the fixtures stay deterministic and model-free.  The frozen DP is
untouched; only anchor admission and post-alignment link quality change.
"""

from __future__ import annotations

import unittest

import numpy as np

from src.me_finder.embedding_models import AlignmentThresholds
from src.me_finder.semantic_alignment import (
    HeadingAnchor,
    align_semantic_sequences,
)


_THRESHOLDS = AlignmentThresholds(low=0.56, note_block=0.80, margin=0.05)


def _stack(source_rows, target_rows):
    return np.asarray([*source_rows, *target_rows], dtype=np.float32)


def _anchor_keys(anchors):
    return [anchor.key for anchor in anchors]


def _has_anchor(anchors, prefix):
    return any(anchor.key.startswith(prefix) for anchor in anchors)


class TermAnchorContextGateTests(unittest.TestCase):
    """A class: a shared Latin term must not anchor unrelated paragraphs."""

    def _run(self, term_source_row, term_target_row):
        source = [
            "开头段落。",
            "关于黄金时代（l'âge d'or）的经济人化石讨论。",
            "结尾段落。",
        ]
        target = [
            "Paragraphe initial.",
            "Discussion sur l'âge d'or, sur l'emballage en verre jetable.",
            "Paragraphe final.",
        ]
        embeddings = _stack(
            [(1.0, 0.0, 0.0, 0.0), term_source_row, (0.0, 0.0, 0.0, 1.0)],
            [(1.0, 0.0, 0.0, 0.0), term_target_row, (0.0, 0.0, 0.0, 1.0)],
        )
        return align_semantic_sequences(
            source,
            target,
            embeddings,
            source_language="zh",
            target_language="fr",
            thresholds=_THRESHOLDS,
        )

    def test_dissimilar_paragraphs_do_not_anchor_on_shared_term(self) -> None:
        # Fossil-man topic vs glass-packaging topic: cosine 0 < low.
        _links, anchors = self._run((0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0))
        self.assertFalse(
            _has_anchor(anchors, "term:"),
            f"unrelated paragraphs were anchored: {_anchor_keys(anchors)}",
        )

    def test_matching_paragraphs_still_anchor_on_shared_term(self) -> None:
        # Same topic on both sides: cosine 1 >= low, the anchor is legitimate.
        _links, anchors = self._run((0.0, 1.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0))
        self.assertTrue(
            _has_anchor(anchors, "term:"),
            f"a genuine term anchor was dropped: {_anchor_keys(anchors)}",
        )


class NumberAnchorNoteNumberTests(unittest.TestCase):
    """B class: small recurring note numbers must not become hard anchors.

    Both sides carry the note number ``18`` twice (as R9 did), so the frequency
    check pairs them into two hard anchors.  The anchor paragraphs are given
    high similarity, so only the note-number exclusion -- not the context gate
    -- can refuse them.
    """

    def test_small_note_number_does_not_anchor(self) -> None:
        source = [
            "序言。",
            "开头正文里出现数字 18 作为计数。",
            "中段正文继续。",
            "很靠后的正文再次出现数字 18 计数。",
            "收尾正文甲。",
        ]
        target = [
            "前言。",
            "开头对应正文出现数字 18 计数。",
            "紧邻的下一段也出现数字 18 计数。",
            "后续对应正文。",
            "收尾正文乙。",
        ]
        embeddings = _stack(
            [
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
                (0.0, 0.0, 1.0),
                (0.0, 1.0, 0.0),
                (1.0, 0.0, 0.0),
            ],
            [
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
                (0.0, 1.0, 0.0),
                (0.0, 0.0, 1.0),
                (1.0, 0.0, 0.0),
            ],
        )
        _links, anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            source_language="ja",
            target_language="zh",
            thresholds=_THRESHOLDS,
        )
        self.assertFalse(
            _has_anchor(anchors, "number:18"),
            f"a recurring note number anchored a corridor: {_anchor_keys(anchors)}",
        )

    def test_rare_year_still_anchors_matching_paragraphs(self) -> None:
        source = [
            "开头。",
            "1848 年的革命浪潮席卷欧洲。",
            "结尾。",
        ]
        target = [
            "Anfang.",
            "Die Revolutionswelle von 1848 erfasste Europa.",
            "Ende.",
        ]
        embeddings = _stack(
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
        )
        _links, anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            source_language="zh",
            target_language="de",
            thresholds=_THRESHOLDS,
        )
        self.assertTrue(
            _has_anchor(anchors, "number:1848"),
            f"a genuine year anchor was dropped: {_anchor_keys(anchors)}",
        )


class AnchorCorridorRatioTests(unittest.TestCase):
    """A soft anchor implying an extreme corridor compression is refused."""

    def test_extreme_compression_ratio_drops_the_later_anchor(self) -> None:
        length = 60
        # Digit-free, distinct filler so no stray number/name anchors compete.
        source = [f"源侧占位内容{chr(0x4e00 + i)}。" for i in range(length)]
        target = [f"标侧占位内容{chr(0x5b00 + i)}。" for i in range(length)]
        # A large shared number that survives the < 100 note-number guard, placed
        # so the second occurrence implies a >50:1 corridor against the first.
        source[1] = "编号五百 500 出现在开头附近。"
        source[55] = "编号七百 700 出现在很靠后的位置。"
        target[1] = "编号五百 500 对应开头。"
        target[2] = "编号七百 700 紧邻开头。"

        rows = []
        for i in range(length):
            vector = [0.0, 0.0, 0.0]
            vector[i % 3] = 1.0
            rows.append(tuple(vector))
        # Force high similarity for the two anchor coordinates so only the
        # corridor-ratio guard (not the context gate) can drop the later anchor.
        source_rows = list(rows)
        target_rows = list(rows)
        source_rows[1] = target_rows[1] = (1.0, 0.0, 0.0)
        source_rows[55] = target_rows[2] = (0.0, 1.0, 0.0)

        embeddings = _stack(source_rows, target_rows)
        _links, anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            source_language="zh",
            target_language="en",
            thresholds=_THRESHOLDS,
        )
        collapsed = [
            anchor
            for anchor in anchors
            if anchor.source_index == 55 and anchor.target_index == 2
        ]
        self.assertEqual(
            collapsed,
            [],
            f"an extreme-compression anchor survived: {_anchor_keys(anchors)}",
        )


class NoiseSegmentQualityGateTests(unittest.TestCase):
    """C class: a lone noise segment must not carry a body match."""

    def _accepted_link_for_source(self, links, source_index):
        return [
            link
            for link in links
            if link.source_start <= source_index < link.source_end
            and link.review_status in ("automatic", "note_automatic")
        ]

    def test_lone_symbol_segment_does_not_carry_a_body_match(self) -> None:
        # Uniform short lengths keep the DP on a clean 1:1 diagonal, so the "|"
        # segment forms its own link (as in R14 P2512) instead of being absorbed
        # into a neighbour group.
        source = ["甲正文。", "|", "乙正文。"]
        target = ["甲对应。", "是的。", "乙对应。"]
        embeddings = _stack(
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
        )
        links, _anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            source_language="zh",
            target_language="zh",
            thresholds=_THRESHOLDS,
        )
        self.assertEqual(
            self._accepted_link_for_source(links, 1),
            [],
            "a lone '|' segment was accepted as a body match",
        )

    def test_short_real_sentence_still_matches(self) -> None:
        # A four-character Chinese clause is real content and must survive.
        source = ["前文正文段。", "他走了。", "后文正文段。"]
        target = ["Body one.", "He left.", "Body three."]
        embeddings = _stack(
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
        )
        links, _anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            source_language="zh",
            target_language="en",
            thresholds=_THRESHOLDS,
        )
        self.assertTrue(
            self._accepted_link_for_source(links, 1),
            "a short but real sentence was wrongly demoted",
        )


class NoteBlockChannelTests(unittest.TestCase):
    """C-class II: a footnote block must not carry a body match.

    Reproduces R2/n33: a run of circled-digit footnote entries (② ③ ④ ...) plus
    marker-less continuation lines was matched to English body paragraphs at
    0.841.  A body paragraph that merely *references* a note with a superscript
    ``$^{②}$`` marker (R3/n7, judged correct) must survive untouched.
    """

    def _accepted_link_for_source(self, links, source_index):
        return [
            link
            for link in links
            if link.source_start <= source_index < link.source_end
            and link.review_status in ("automatic", "note_automatic")
        ]

    def test_circled_footnote_block_does_not_carry_body_matches(self) -> None:
        source = [
            "前文正文段落。",
            "② 参见博尔斯坦《形象》。",
            "③ 立体主义者追求空间的本质。",
            "达达派或杜尚将客体与其功能剥离。",  # marker-less continuation
            "④ 参见《消费之消费》。",
            "后文正文段落。",
        ]
        target = [
            "Body paragraph one.",
            "Body paragraph two.",
            "Body paragraph three.",
            "Body paragraph four.",
            "Body paragraph five.",
            "Body paragraph six.",
        ]
        embeddings = _stack(
            [
                (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                (0.0, 1.0, 0.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
                (0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
            ],
            [
                (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                (0.0, 1.0, 0.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
                (0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
            ],
        )
        links, _anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            source_language="zh",
            target_language="en",
            thresholds=_THRESHOLDS,
        )
        for note_index in (1, 2, 3, 4):
            self.assertEqual(
                self._accepted_link_for_source(links, note_index),
                [],
                f"footnote segment {note_index} carried a body match",
            )
        # Real body around the block is untouched.
        self.assertTrue(self._accepted_link_for_source(links, 0))
        self.assertTrue(self._accepted_link_for_source(links, 5))

    def test_superscript_note_reference_in_body_survives(self) -> None:
        # A body paragraph that references a note ($^{②}$ ...) is not a footnote
        # entry and must keep its body match.
        source = ["前文段。", "$^{②}$ 这一段是正文，只是引用了脚注。", "后文段。"]
        target = ["Body one.", "This is body text that cites a footnote.", "Body three."]
        embeddings = _stack(
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
        )
        links, _anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            source_language="zh",
            target_language="en",
            thresholds=_THRESHOLDS,
        )
        self.assertTrue(
            self._accepted_link_for_source(links, 1),
            "a body paragraph citing a footnote was wrongly demoted",
        )

    def test_lone_circled_marker_is_not_treated_as_a_note_block(self) -> None:
        # A single circled marker (no run of >=3) is not a footnote block.
        source = ["前文段。", "② 这一处只有一个圈号并非注释块。", "后文段。"]
        target = ["Body one.", "A single item, not a note block.", "Body three."]
        embeddings = _stack(
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
        )
        links, _anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            source_language="zh",
            target_language="en",
            thresholds=_THRESHOLDS,
        )
        self.assertTrue(
            self._accepted_link_for_source(links, 1),
            "a lone circled marker was wrongly treated as a note block",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

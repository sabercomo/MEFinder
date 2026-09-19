from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import numpy as np

from src.me_finder.embedding_models import embedding_model_config
from src.me_finder.alignment_regions import alignment_body_bounds
from src.me_finder.text_alignment import align_segment_sequences


class AlignmentRegionTests(TestCase):
    def test_frontmatter_and_spaced_afterword(self):
        texts = ["封面", "导读", "第一章 主体", "正文。", "译 后 记", "译者致谢。"]
        self.assertEqual(alignment_body_bounds(texts), (2, 4))

    def test_author_introduction_before_chapter_one_is_body(self):
        # An author Introduction (导论) that precedes 第一章 is body content, not
        # frontmatter — regression for「所选文字属于副文本区域」on intro passages.
        texts = [
            "封面",
            "导论 社会性别意识形态和对破坏的恐惧",
            "为什么会有人害怕社会性别呢？",
            "第一章 全球局势",
            "正文。",
            "致谢",
            "感谢。",
        ]
        self.assertEqual(alignment_body_bounds(texts), (1, 5))

    def test_editor_reading_guide_stays_frontmatter(self):
        # 导读 (an editor's reading guide) is NOT an author introduction.
        texts = ["封面", "导读", "第一章 主体", "正文。"]
        self.assertEqual(alignment_body_bounds(texts)[0], 2)

    def test_prose_mention_does_not_start_backmatter(self):
        texts = ["Chapter 1 Subjects", "The index of power is discussed here.", "Body."]
        self.assertEqual(alignment_body_bounds(texts), (0, 3))

    def test_unstructured_excerpt_is_not_discarded(self):
        self.assertEqual(alignment_body_bounds(["甲。", "乙。"]), (0, 2))

    def test_collected_notes_end_body(self):
        texts = ["Cover", "Chapter 1 Subjects", "Body.", "Notes\n1. A citation.", "Index"]
        self.assertEqual(alignment_body_bounds(texts), (1, 3))

    def test_chapter_notes_do_not_remove_following_body(self):
        texts = ["第一章 正文", "正文。", "注释", "注一。", "第二章 正文", "后续正文。"]
        self.assertEqual(alignment_body_bounds(texts), (0, 6))

    def test_repeated_notes_with_missing_chapter_headings_do_not_truncate(self):
        texts = ["第一章 正文", "正文。", "注释", "下一章正文。", "注释", "后续正文。"]
        self.assertEqual(alignment_body_bounds(texts), (0, 6))

    def test_cip_is_not_a_body_heading(self):
        texts = ["I. Wood, Allen W. II.", "Preface", "§ 1", "Body.", "§ 2", "Body."]
        self.assertEqual(alignment_body_bounds(texts)[0], 2)

    def test_introduction_references_do_not_end_body(self):
        # Regression (Baudrillard EN, target body collapsed to [42,584)): an
        # editor's Introduction carries its own Notes/References before PART I.
        # Neither may end the body, and a leading introduction with its own
        # reference list is editorial apparatus, so the body opens at chapter 1.
        texts = [
            "Contents\nForeword\nIntroduction\nPart I",
            "Introduction \nGeorge Ritzer \nThis English translation of the book.",
            "Ritzer's argument.",
            "Notes \n1 \nThe other major possibility.",
            "References \nBaudrillard, Jean (1968/1996) The System of Objects.",
            "London: Verso.",
            "PART I",
            "THE FORMAL LITURGY \nOF THE OBJECT \n1 \nProfusion \nThere is all around us.",
            "Body.",
            "PART II",
            "THE THEORY OF CONSUMPTION \n4 \nThe Social Logic of Consumption",
            "Body.",
            "Notes \nChapter 1 \nK. Marx, A Contribution.",
            "Index \naccounting of growth, 41-2",
        ]
        self.assertEqual(alignment_body_bounds(texts), (6, 12))

    def test_author_introduction_with_own_notes_stays_body(self):
        # Chapter-end notes after an author introduction do not make it editorial.
        texts = [
            "封面",
            "导论 社会性别意识形态",
            "导论正文。",
            "注释",
            "注一。",
            "第一章 全球局势",
            "正文。",
            "参考文献",
            "文献一。",
        ]
        self.assertEqual(alignment_body_bounds(texts), (1, 7))

    def test_numbered_prose_is_not_a_body_heading(self):
        texts = ["Body.", "1 der", "Erstauflage des Kapital."]
        self.assertEqual(alignment_body_bounds(texts)[0], 0)

    def test_excluded_high_similarity_is_never_accepted_and_offsets_survive(self):
        source = ["封面", "第一章 精神", "精神。", "译后记", "感谢。"]
        target = ["Cover", "Chapter 1 Spirit", "Spirit.", "Notes", "Citation."]
        def embeddings(texts, _cache):
            return np.ones((len(texts), 4), dtype=np.float32)
        with TemporaryDirectory() as directory:
            links, anchors = align_segment_sequences(
                source, target, cache_dir=Path(directory), embedding_provider=embeddings
            )
        accepted = [l for l in links if l.review_status in ("automatic", "note_automatic")]
        self.assertTrue(accepted)
        for link in accepted:
            self.assertTrue(1 <= link.source_start < link.source_end <= 3)
            self.assertTrue(1 <= link.target_start < link.target_end <= 3)
        self.assertTrue(any(a.source_index == 1 and a.target_index == 1 for a in anchors))
        for index in (0, 3, 4):
            rows = [l for l in links if l.source_start <= index < l.source_end]
            self.assertTrue(rows)
            self.assertTrue(all(l.review_status == "rejected" and l.target_start == l.target_end for l in rows))

    def test_e5_experimental_default(self):
        config = embedding_model_config("multilingual-e5-large")
        self.assertEqual(config.thresholds.low, 0.83)
        self.assertIn("实验档", config.description)

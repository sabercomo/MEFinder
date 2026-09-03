from __future__ import annotations

import unittest

import numpy as np

from src.me_finder.semantic_alignment import (
    DEFAULT_ANCHOR_EXTRACTOR_REGISTRY,
    HeadingAnchor,
    align_semantic_sequences,
    extract_chinese_japanese_name_anchors,
    extract_latin_name_anchors,
    extract_number_anchors,
    extract_parenthetical_term_anchors,
    normalize_numeric_text,
)


class SemanticLexicalAnchorTests(unittest.TestCase):
    def test_language_specific_number_normalizers(self) -> None:
        self.assertEqual(
            normalize_numeric_text("二〇一一年，百分之十二，１２３", "zh-Hans"),
            "2011年,12%,123",
        )
        self.assertEqual(normalize_numeric_text("２０１１", "ja"), "2011")
        self.assertEqual(normalize_numeric_text("3,14", "de"), "3.14")
        self.assertEqual(normalize_numeric_text("1\u202f234\u00a0567", "fr"), "1234567")
        self.assertEqual(normalize_numeric_text("Anno 2011", "la"), "Anno 2011")
        self.assertEqual(normalize_numeric_text("百姓一方面", "zh-Hans"), "百姓一方面")

    def test_number_anchors_cover_year_bce_century_and_percentage(self) -> None:
        source = [
            "事情发生在二〇一一年。",
            "该文献讨论公元前三百年。",
            "这一争论始于二十世纪。",
            "样本占百分之十二。",
        ]
        target = [
            "It happened in 2011.",
            "The text concerns 300 BCE.",
            "The debate began in the 20th century.",
            "The sample accounts for 12 percent.",
        ]

        anchors = extract_number_anchors("zh-Hans", "en", source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [
                (0, 0, "number:2011"),
                (1, 1, "number:bce-300"),
                (2, 2, "number:century-20"),
                (3, 3, "number:percent-12"),
            ],
        )

    def test_number_anchors_exclude_toc_endnotes_pages_and_frequent_tokens(self) -> None:
        source = [
            "目录\n注释\n2011 ........ 12",
            "正文发生于二〇一一年。",
            "42",
            "注释",
            "脚注提到2020年。",
            "88出现一次。",
            "88再次出现。",
            "88第三次出现。",
            "88第四次出现。",
        ]
        target = [
            "Contents\nNotes\n2011 ........ 12",
            "The body concerns 2011.",
            "42",
            "Notes",
            "The note cites 2020.",
            "88 occurs once.",
            "88 occurs twice.",
            "88 occurs three times.",
            "88 occurs four times.",
        ]

        anchors = extract_number_anchors("zh-Hans", "en", source, target)

        self.assertEqual(anchors, [HeadingAnchor(1, 1, "number:2011")])

    def test_parenthetical_original_matches_exactly_after_diacritic_folding(self) -> None:
        source = ["阿涅斯卡·格拉夫（Agnieszka Graff）提出了这一观点。"]
        target = ["Agnieszka Gráff made this argument."]

        anchors = extract_parenthetical_term_anchors(
            "zh-Hans", "en", source, target
        )

        self.assertEqual(
            anchors, [HeadingAnchor(0, 0, "term:agnieszka-graff")]
        )

    def test_registry_rejects_an_ambiguous_parenthetical_occurrence(self) -> None:
        source = ["罗诉韦德案（Roe v. Wade）改变了法律。"]
        target = ["Roe v. Wade was decided.", "Later, Roe v. Wade was reversed."]

        self.assertEqual(
            DEFAULT_ANCHOR_EXTRACTOR_REGISTRY.extract(
                "zh-Hans", "en", (source, target)
            ),
            [],
        )

    def test_structural_bounds_disambiguate_a_quoted_court_name(self) -> None:
        source = [
            "第二章",
            "“罗诉韦德案”（Roe v. Wade）改变了法律。",
            "第三章",
        ]
        target = [
            "Roe v. Wade appears in the preface.",
            "Chapter 2",
            "Roe v. Wade changed the law.",
            "Chapter 3",
        ]

        anchors = DEFAULT_ANCHOR_EXTRACTOR_REGISTRY.extract(
            "zh-Hans",
            "en",
            (source, target),
            fixed_anchors=[
                HeadingAnchor(0, 1, "chapter:2"),
                HeadingAnchor(2, 3, "chapter:3"),
            ],
        )

        self.assertEqual(anchors, [HeadingAnchor(1, 2, "term:roe-v-wade")])

    def test_registry_rejects_terms_that_reveal_a_many_to_one_segment(self) -> None:
        source = [
            "戴尔·奥利里（Dale O’Leary）提出了反对意见。",
            "罗诉韦德案（Roe v. Wade）随后被提及。",
        ]
        target = [
            "Dale O’Leary opposed Roe v. Wade in the same sentence."
        ]

        anchors = DEFAULT_ANCHOR_EXTRACTOR_REGISTRY.extract(
            "zh-Hans", "en", (source, target)
        )

        self.assertEqual(anchors, [])

    def test_registry_prefers_a_coordinate_with_multiple_exact_terms(self) -> None:
        source = [
            "戴尔·奥利里（Dale O’Leary）提出了反对意见。",
            "玛丽·安·格伦顿（Mary Ann Glendon）反对罗诉韦德案（Roe v. Wade）。",
        ]
        target = [
            "Dale O’Leary and Mary Ann Glendon opposed Roe v. Wade."
        ]

        anchors = DEFAULT_ANCHOR_EXTRACTOR_REGISTRY.extract(
            "zh-Hans", "en", (source, target)
        )

        self.assertEqual(len(anchors), 1)
        self.assertEqual((anchors[0].source_index, anchors[0].target_index), (1, 0))

    def test_latin_name_anchors_keep_rare_names_and_gate_german_nouns(self) -> None:
        source = [
            "Judith Butler schrieb darüber.",
            "Obergefell wurde entschieden.",
            "Gender ist hier wichtig.",
            "Gender bleibt wichtig.",
            "Gender wird diskutiert.",
            "Gender erscheint erneut.",
        ]
        target = [
            "Judith Butler wrote about it.",
            "Obergefell was decided.",
            "Gender matters here.",
            "Gender still matters.",
            "Gender is discussed.",
            "Gender appears again.",
        ]

        candidates = extract_latin_name_anchors("de", "en", source, target)
        selected = DEFAULT_ANCHOR_EXTRACTOR_REGISTRY.extract(
            "de", "en", (source, target)
        )

        self.assertFalse(any(anchor.key == "name:gender" for anchor in candidates))
        self.assertEqual(
            selected,
            [
                HeadingAnchor(0, 0, "name:judith-butler"),
                HeadingAnchor(1, 1, "name:obergefell"),
            ],
        )

    def test_chinese_japanese_names_fold_simplified_and_shinjitai_glyphs(self) -> None:
        source = ["东条英机与大塚英志是两个不同的人名。"]
        target = ["東條英機と大塚英志について論じる。"]

        anchors = extract_chinese_japanese_name_anchors(
            "zh-Hans", "ja", source, target
        )

        self.assertEqual(
            anchors,
            [
                HeadingAnchor(0, 0, "name:东条英机"),
                HeadingAnchor(0, 0, "name:大塚英志"),
            ],
        )

    def test_registry_drops_lower_priority_crossing_number_anchor(self) -> None:
        source = [
            "罗诉韦德案（Roe v. Wade）改变了法律。",
            "事情发生在2011年。",
        ]
        target = ["It happened in 2011.", "Roe v. Wade changed the law."]

        anchors = DEFAULT_ANCHOR_EXTRACTOR_REGISTRY.extract(
            "zh-Hans", "en", (source, target)
        )

        self.assertEqual(anchors, [HeadingAnchor(0, 1, "term:roe-v-wade")])

    def test_registry_drops_a_term_that_crosses_a_structural_anchor(self) -> None:
        source = ["罗诉韦德案（Roe v. Wade）。", "第二章"]
        target = ["Before.", "Chapter 2", "Roe v. Wade."]

        anchors = DEFAULT_ANCHOR_EXTRACTOR_REGISTRY.extract(
            "zh-Hans",
            "en",
            (source, target),
            fixed_anchors=[HeadingAnchor(1, 1, "chapter:2")],
        )

        self.assertEqual(anchors, [])

    def test_alignment_uses_registered_anchor_as_a_hard_partition(self) -> None:
        source = ["前文。", "罗诉韦德案（Roe v. Wade）。", "后文。"]
        target = ["Before.", "Roe v. Wade.", "After."]
        embeddings = np.zeros((6, 3), dtype=np.float32)

        links, anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            source_language="zh-Hans",
            target_language="en",
        )

        self.assertIn(HeadingAnchor(1, 1, "term:roe-v-wade"), anchors)
        self.assertTrue(
            any(
                link.source_start == 1
                and link.source_end == 2
                and link.target_start == 1
                and link.target_end == 2
                and link.review_status == "automatic"
                and link.anchor_key == "term:roe-v-wade"
                for link in links
            )
        )


if __name__ == "__main__":
    unittest.main()

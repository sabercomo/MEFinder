from __future__ import annotations

import unittest

import numpy as np

from src.me_finder.semantic_alignment import (
    HeadingAnchor,
    align_semantic_sequences,
    find_heading_anchors,
    mutual_nearest_target_index,
)


class SemanticSectionAnchorTests(unittest.TestCase):
    def test_mutual_nearest_fallback_rejects_a_forward_only_false_friend(self) -> None:
        source = np.asarray(
            [
                [1.0, 0.0],
                [0.8, 0.6],
            ],
            dtype=np.float32,
        )
        target = np.asarray(
            [
                [0.9, 0.4358899],
                [1.0, 0.0],
            ],
            dtype=np.float32,
        )

        target_index = mutual_nearest_target_index(source, target, [1])

        self.assertEqual(target_index, 0)

    def test_folio_anchor_partitions_dp_without_forcing_a_link(self) -> None:
        source = ["Source before.", "Source after."]
        target = ["Target before.", "Target after."]
        embeddings = np.asarray(
            [
                [1.0, 0.0],
                [0.0, 1.0],
                [1.0, 0.0],
                [0.0, 1.0],
            ],
            dtype=np.float32,
        )

        links, anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            boundary_anchors=[HeadingAnchor(1, 1, "folio:60")],
        )

        self.assertEqual([anchor.key for anchor in anchors], ["folio:60"])
        self.assertFalse(any(link.anchor_key == "folio:60" for link in links))
        self.assertTrue(
            all(
                (link.source_end <= 1 and link.target_end <= 1)
                or (link.source_start >= 1 and link.target_start >= 1)
                for link in links
            )
        )

    def test_verified_folio_outranks_a_crossing_chapter_anchor(self) -> None:
        source = ["Before.", "1 First chapter", "Body.", "After."]
        target = ["前文。", "第一章 第一章", "正文。", "后文。"]
        embeddings = np.eye(len(source) + len(target), dtype=np.float32)

        _links, anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            boundary_anchors=[HeadingAnchor(0, 2, "folio:60")],
        )

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [(0, 2, "folio:60")],
        )

    def test_section_sign_anchor_outranks_a_crossing_folio(self) -> None:
        source = ["Before.", "§ 1", "Body."]
        target = ["前文。", "第1节", "正文。"]
        embeddings = np.eye(len(source) + len(target), dtype=np.float32)

        _links, anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            boundary_anchors=[HeadingAnchor(0, 2, "folio:60")],
        )

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [(1, 1, "paragraph:1")],
        )

    def test_split_chinese_section_number_is_a_paragraph_anchor(self) -> None:
        source = ["§ 158", "Section body.", "§ 159"]
        target = ["家庭\n第\n158 节\n正文。", "补充。", "第159 节\n下一节。"]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [(0, 0, "paragraph:158"), (2, 2, "paragraph:159")],
        )

    def test_section_sign_chain_outranks_a_longer_crossing_chapter_chain(self) -> None:
        source = [
            "1 Alpha chapter",
            "2 Beta chapter",
            "3 Gamma chapter",
            "4 Delta chapter",
            "§ 1",
            "§ 2",
            "§ 3",
        ]
        target = [
            "第1节",
            "第2节",
            "第3节",
            "第一章 甲",
            "第二章 乙",
            "第三章 丙",
            "第四章 丁",
        ]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [
                (4, 0, "paragraph:1"),
                (5, 1, "paragraph:2"),
                (6, 2, "paragraph:3"),
            ],
        )

    def test_folio_partition_preserves_one_to_many_dp_transition(self) -> None:
        source = ["One source unit.", "Following source unit."]
        target = ["Target half one.", "Target half two.", "Following target unit."]
        embeddings = np.asarray(
            [
                [1.0, 0.0],
                [0.0, 1.0],
                [1.0, 0.0],
                [1.0, 0.0],
                [0.0, 1.0],
            ],
            dtype=np.float32,
        )

        links, _anchors = align_semantic_sequences(
            source,
            target,
            embeddings,
            boundary_anchors=[HeadingAnchor(1, 2, "folio:60")],
        )

        self.assertTrue(
            any(
                link.source_start == 0
                and link.source_end == 1
                and link.target_start == 0
                and link.target_end == 2
                for link in links
            )
        )

    def test_section_sign_and_chinese_section_numbers_share_paragraph_keys(self) -> None:
        source = [
            "§ 1\nThe abstract concept of right.",
            "§ I I",
            "§ lS Freedom and the will",
            "§ S9\nThe person has a right to property.",
            "§ 2l6 The constitution develops from the concept.",
        ]
        target = [
            "第一节\n法的抽象概念。",
            "第I I节",
            "第lS节 自由与意志",
            "第S9节\n人格拥有财产权。",
            "第2l6节 宪法从概念中发展出来。",
        ]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [
                (0, 0, "paragraph:1"),
                (1, 1, "paragraph:11"),
                (2, 2, "paragraph:15"),
                (3, 3, "paragraph:59"),
                (4, 4, "paragraph:216"),
            ],
        )

    def test_collected_endnote_uses_numbered_inline_note_outside_monotonic_order(self) -> None:
        source = [
            "1 First chapter",
            "Main body.",
            "Notes",
            "1.",
            "First note.",
            "2.",
            "Second note.",
            "3.",
            "Third note.",
            "4.",
            "Fourth note.",
            "5.",
            "Fifth note.",
        ]
        target = [
            "第一章 第一章",
            "[1] 第一条译注。",
            "[2] 第二条译注。",
            "[3] 第三条译注。",
            "[4] 第四条译注。",
            "[5] 第五条译注。",
            "正文继续。",
        ]
        source_vectors = np.zeros((len(source), 8), dtype=np.float32)
        target_vectors = np.zeros((len(target), 8), dtype=np.float32)
        source_vectors[0, 0] = target_vectors[0, 0] = 1.0
        source_vectors[1:3, 1] = target_vectors[6, 1] = 1.0
        for note_number in range(1, 6):
            source_start = 3 + (note_number - 1) * 2
            source_vectors[source_start : source_start + 2, note_number + 1] = 1.0
            target_vectors[note_number, note_number + 1] = 1.0

        links, _anchors = align_semantic_sequences(
            source,
            target,
            np.vstack([source_vectors, target_vectors]),
            boundary_anchors=[HeadingAnchor(5, 3, "folio:60")],
        )

        note_two = [
            link
            for link in links
            if link.review_status == "note_automatic"
            and link.anchor_key == "note:1:2"
        ]
        self.assertEqual(len(note_two), 1)
        self.assertEqual(
            (
                note_two[0].source_start,
                note_two[0].source_end,
                note_two[0].target_start,
                note_two[0].target_end,
            ),
            (5, 7, 2, 3),
        )
        self.assertTrue(
            any(
                link.review_status == "rejected"
                and link.source_start <= 6 < link.source_end
                for link in links
            )
        )

    def test_ambiguous_inline_note_candidates_are_not_overridden(self) -> None:
        source = [
            "1 First chapter",
            "Notes",
            "1.",
            "First note.",
            "2.",
            "Second note.",
            "3.",
            "Third note.",
            "4.",
            "Fourth note.",
            "5.",
            "Fifth note.",
        ]
        target = [
            "第一章 第一章",
            "[1] 第一条译注。",
            "[2] 第二条候选。",
            "[2] 第二条重复候选。",
            "[3] 第三条译注。",
            "[4] 第四条译注。",
            "[5] 第五条译注。",
        ]
        source_vectors = np.zeros((len(source), 8), dtype=np.float32)
        target_vectors = np.zeros((len(target), 8), dtype=np.float32)
        source_vectors[0, 0] = target_vectors[0, 0] = 1.0
        for note_number in range(1, 6):
            source_start = 2 + (note_number - 1) * 2
            source_vectors[source_start : source_start + 2, note_number + 1] = 1.0
        target_vectors[1, 2] = 1.0
        target_vectors[2:4, 3] = 1.0
        target_vectors[4, 4] = 1.0
        target_vectors[5, 5] = 1.0
        target_vectors[6, 6] = 1.0

        links, _anchors = align_semantic_sequences(
            source, target, np.vstack([source_vectors, target_vectors])
        )

        self.assertNotIn(
            "note:1:2",
            {
                link.anchor_key
                for link in links
                if link.review_status == "note_automatic"
            },
        )

    def test_note_numbers_fallback_to_markers_when_citation_editions_differ(self) -> None:
        source = [
            "1 First chapter",
            "Notes",
            "1.",
            "Marx-Engels Werke, foreign edition, p. 10.",
            "2.",
            "Foreign edition, p. 20.",
            "3.",
            "Foreign edition, p. 30.",
            "4.",
            "Foreign edition, p. 40.",
            "5.",
            "Foreign edition, p. 50.",
        ]
        target = [
            "第一章 第一章",
            "[1] 《马克思恩格斯全集》中文第1卷，第100页。",
            "[2] 《马克思恩格斯选集》中文第2卷，第200页。",
            "[3] 中共中央编译局译本，第300页。",
            "[4] 外文版第40页，中译本第400页。",
            "[5] 外文版第50页，中译本第500页。",
        ]
        embeddings = np.eye(len(source) + len(target), dtype=np.float32)

        links, _anchors = align_semantic_sequences(source, target, embeddings)

        self.assertEqual(
            {
                link.anchor_key
                for link in links
                if link.anchor_key.startswith("note-marker:")
            },
            {
                "note-marker:1:1:1",
                "note-marker:1:2:2",
                "note-marker:1:3:3",
                "note-marker:1:4:4",
                "note-marker:1:5:5",
            },
        )

    def test_explicit_translator_note_is_skipped_before_rank_pairing(self) -> None:
        source = [
            "1 First chapter",
            "Notes",
            "1.",
            "First note.",
            "2.",
            "Second note.",
            "3.",
            "Third note.",
            "4.",
            "Fourth note.",
            "5.",
            "Fifth note.",
        ]
        target = [
            "第一章 第一章",
            "[1] 第一条注。",
            "[2] 译者注：本条为中译本所加。",
            "[3] 第二条作者注。",
            "[4] 第三条作者注。",
            "[5] 第四条作者注。",
            "[6] 第五条作者注。",
        ]
        embeddings = np.eye(len(source) + len(target), dtype=np.float32)

        links, _anchors = align_semantic_sequences(source, target, embeddings)

        self.assertEqual(
            {
                link.anchor_key
                for link in links
                if link.anchor_key.startswith("note-marker:")
            },
            {
                "note-marker:1:1:1",
                "note-marker:1:2:3",
                "note-marker:1:3:4",
                "note-marker:1:4:5",
                "note-marker:1:5:6",
            },
        )

    def test_french_parts_anchor_numbered_chinese_chapters(self) -> None:
        source = [
            "PREMIÈRE PARTIE\nLa liturgie formelle de l'objet",
            "Premier corps de texte.",
            "DEUXIÈME PARTIE\nThéorie de la consommation",
        ]
        target = [
            "第一章 物的形式礼拜仪式",
            "第一部分正文。",
            "第二章 消费理论",
        ]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [(0, 0, "chapter:1"), (2, 2, "chapter:2")],
        )
        english = [
            "PART ONE",
            "First body.",
            "PART TWO",
        ]
        english_anchors = find_heading_anchors(source, english)
        self.assertEqual(
            [anchor.key for anchor in english_anchors],
            ["chapter:1", "chapter:2"],
        )

    def test_author_preface_aligns_but_translator_preface_does_not(self) -> None:
        source = ["Preface", "Author text.", "1 First chapter"]
        target = ["代译序", "前言", "作者正文。", "第一章 第一章"]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [(0, 1, "preface:author"), (2, 3, "chapter:1")],
        )

    def test_bibliography_volume_number_is_not_a_chapter_anchor(self) -> None:
        source = ["Vol.", "1: An Introduction.", "PART ONE", "Body."]
        target = ["第一章 正文", "正文。"]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [(2, 0, "chapter:1")],
        )

    def test_chapter_anchors_ignore_toc_leaders_and_preface_prose(self) -> None:
        source = ["1 First chapter", "2 Second chapter", "3 Third chapter"]
        target = [
            "第一章将在译者序中说明本书的主要问题。",
            "第一章 第一章标题 …… (1)",
            "第二章 第二章标题 …… (48)",
            "第三章 第三章标题 …… (106)",
            "第一章 第一章标题",
            "第一章 第一章标题",
            "第二章 第二章标题",
            "第三章 第三章标题",
        ]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [
                (0, 4, "chapter:1"),
                (1, 6, "chapter:2"),
                (2, 7, "chapter:3"),
            ],
        )

    def test_inline_references_are_not_paragraph_anchors(self) -> None:
        source = [
            "See § 1 for the distinction.",
            "§ 38 und 39 gesagt, dass dies nur ein Verweis ist.",
            "§ 34–104",
            "§ 2\nThe actual heading.",
        ]
        target = ["参见第一节的区分。", "第38节，但这只是引用。", "第34—104节", "第二节\n真正的标题。"]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [(3, 3, "paragraph:2")],
        )

    def test_longest_body_sequence_wins_over_an_earlier_toc_copy(self) -> None:
        source = [
            "Contents",
            "§ 1 Introduction",
            "§ 2 The Will",
            "§ 3 Abstract Right",
            "§ 1\nThe body begins with a substantially longer explanation of right.",
            "§ 2\nThe body continues with a substantially longer account of the will.",
            "§ 3\nThe body develops a substantially longer account of abstract right.",
        ]
        target = [
            "目录",
            "第一节 导论",
            "第二节 意志",
            "第三节 抽象法",
            "第一节\n正文从一段较长的法概念说明开始。",
            "第二节\n正文接着对意志作出一段较长的说明。",
            "第三节\n正文继续展开一段较长的抽象法说明。",
        ]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [
                (4, 4, "paragraph:1"),
                (5, 5, "paragraph:2"),
                (6, 6, "paragraph:3"),
            ],
        )

    def test_endnotes_do_not_extend_the_body_number_sequence(self) -> None:
        source = [
            "§ 1\nBody one.",
            "§ 2\nBody two.",
            "§ 3\nBody three.",
            "Endnotes",
            "§ 4 Citation to another edition.",
        ]
        target = [
            "第一节\n正文一。",
            "第二节\n正文二。",
            "第三节\n正文三。",
            "尾注",
            "第四节 另一版本的引文。",
        ]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [anchor.key for anchor in anchors],
            ["paragraph:1", "paragraph:2", "paragraph:3"],
        )

    def test_paragraph_and_chapter_anchors_form_one_monotonic_chain(self) -> None:
        source = [
            "1 First chapter",
            "§ 1\nFirst body paragraph.",
            "§ 2\nSecond body paragraph.",
        ]
        target = [
            "第一章 第一章",
            "第一节\n第一段正文。",
            "第二节\n第二段正文。",
        ]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [
                (0, 0, "chapter:1"),
                (1, 1, "paragraph:1"),
                (2, 2, "paragraph:2"),
            ],
        )

    def test_crossing_toc_chapter_does_not_displace_body_paragraph_chain(self) -> None:
        source = [
            "1 First chapter",
            "Preface",
            "Introduction",
            "§ 1\nFirst body paragraph.",
            "§ 2\nSecond body paragraph.",
        ]
        target = [
            "第一节\n第一段正文。",
            "第二节\n第二段正文。",
            "前言",
            "第一章 第一章",
        ]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [(3, 0, "paragraph:1"), (4, 1, "paragraph:2")],
        )

    def test_repeated_section_ordinals_infer_chapters_when_body_titles_are_missing(self) -> None:
        source = [
            "Contents\n1 First\n i Women\n2 Second\n i Exchange\n3 Third\n i Bodies",
            'i. "Women" as the Subject of Feminism\nBody one.',
            "ii. The Compulsory Order\nBody two.",
            "i. Structuralism's Critical Exchange\nBody three.",
            "ii. Lacan and Masquerade\nBody four.",
            "i. The Body Politics of Julia Kristeva\nBody five.",
        ]
        target = [
            "序\n第二章将在一段很长的序言中介绍这本书的各章论点，但这不是正文标题，也不应当作结构锚点。",
            "第一节 妇女作为女性主义的主体\n正文一。",
            "第二节 强制性秩序\n正文二。",
            "第一节 结构主义的关键交换\n正文三。",
            "第二节 拉康与伪装\n正文四。",
            "第一节 克里斯特娃的身体政治\n正文五。",
        ]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [
                (1, 1, "chapter:1:section:1"),
                (2, 2, "chapter:1:section:2"),
                (3, 3, "chapter:2:section:1"),
                (4, 4, "chapter:2:section:2"),
                (5, 5, "chapter:3:section:1"),
            ],
        )

    def test_latin_contents_recovers_body_headings_whose_ordinals_were_dropped(self) -> None:
        source = [
            "Contents\n1 First Chapter\n i First Topic\n ii Second Topic\n2 Next Chapter\n i Third Topic",
            "First Topic\nBody one.",
            "Second Topic\nBody two.",
            "Next Chapter\nThird Topic\nBody three.",
        ]
        target = [
            "第一节 第一个主题\n正文一。",
            "第二节 第二个主题\n正文二。",
            "第二章 下一章\n第一节 第三个主题\n正文三。",
        ]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [
                (1, 0, "chapter:1:section:1"),
                (2, 1, "chapter:1:section:2"),
                (3, 2, "chapter:2"),
            ],
        )

    def test_chinese_slash_toc_maps_part_titles_without_using_printed_pages(self) -> None:
        source = [
            "目录\n1 / 第一章 物的形式礼拜仪式\n1 / 丰盛\n28 / 第二章 消费理论\n28 / 消费的社会逻辑",
            "61 / 一种结构分析",
            "丰盛\n第一部分正文。",
            "章内正文。",
            "消费的社会逻辑\n第二部分正文。",
        ]
        target = [
            "Contents\nPart I The Formal Liturgy of the Object\n1\nProfusion\nPart II The Theory of Consumption\n4 The Social Logic of Consumption",
            "PART I",
            "First part body.",
            "PART II",
            "Second part body.",
        ]

        anchors = find_heading_anchors(source, target)

        self.assertEqual(
            [(anchor.source_index, anchor.target_index, anchor.key) for anchor in anchors],
            [(2, 1, "chapter:1"), (4, 3, "chapter:2")],
        )


if __name__ == "__main__":
    unittest.main()

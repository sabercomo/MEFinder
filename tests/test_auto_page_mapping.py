from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.auto_page_mapping import (
    PageNumberCandidate,
    apply_auto_mapping_to_pages,
    detect_pdf_page_layout,
    infer_auto_page_mapping,
    extract_pdf_numeric_bookmark_candidates,
    extract_native_pdf_edge_candidates,
    normalize_page_candidate,
    normalize_numeric_bookmark_title,
)
from src.me_finder.page_mapping_service import PageMappingService
from src.me_finder.pdf_page_mapping import int_to_roman


def cand(page_idx: int, number: int, style: str = "arabic", x: float = 0.5, y: float = 0.94) -> PageNumberCandidate:
    normalized = str(number) if style == "arabic" else int_to_roman(number, upper=style == "roman_upper")
    return PageNumberCandidate(
        page_idx=page_idx,
        raw_candidate=normalized,
        normalized_candidate=normalized,
        candidate_type="page_number",
        number_style=style,
        number=number,
        bbox=[x - 0.02, y - 0.01, x + 0.02, y + 0.01],
        source="test",
        confidence=0.9,
        score=1.0,
    )


def infer(candidates, page_count=600, page_texts=None, layout_detection=None):
    return infer_auto_page_mapping(
        candidates,
        page_count,
        page_texts=page_texts or {},
        layout_detection=layout_detection,
    )


def layout_pages(start: int, count: int, *, landscape: bool = True, split: bool = True):
    width, height = (1200, 800) if landscape else (600, 800)
    pages = []
    for page_idx in range(start, start + count):
        blocks = [
            {
                "text": "左侧正文内容" * 30,
                "bbox_normalized": [0.06, 0.10, 0.44 if split else 0.88, 0.88],
            }
        ]
        if split:
            blocks.append(
                {
                    "text": "右侧正文内容" * 30,
                    "bbox_normalized": [0.56, 0.10, 0.94, 0.88],
                }
            )
        pages.append(
            {
                "pdf_page_index": page_idx,
                "page_width": width,
                "page_height": height,
                "blocks": blocks,
                "text_raw": "页面正文",
            }
        )
    return pages


class AutoPageMappingTests(unittest.TestCase):
    def test_detects_ltr_spread_and_fits_two_logical_pages_per_pdf_page(self) -> None:
        pages = layout_pages(4, 8)
        candidates = []
        for offset, page_idx in enumerate(range(4, 12)):
            lower = 10 + offset * 2
            candidates.extend(
                [cand(page_idx, lower, x=0.08), cand(page_idx, lower + 1, x=0.92)]
            )

        layout = detect_pdf_page_layout(pages, candidates)
        self.assertEqual(layout["layout_mode"], "spread")
        self.assertEqual(layout["confidence_level"], "high")
        self.assertEqual(layout["reading_direction"], "ltr")
        self.assertEqual(layout["evidence"]["paired_page_numbers"], 8)

        result = infer(candidates, page_count=20, layout_detection=layout)
        segment = result["applied_segments"][0]
        self.assertEqual(segment["pdf_page_start"], 4)
        self.assertEqual(segment["pdf_page_end"], 11)
        self.assertEqual(segment["citation_page_start"], "10")
        self.assertEqual(segment["citation_page_end"], "25")
        self.assertEqual(segment["layout_mode"], "spread")
        self.assertEqual(segment["reading_direction"], "ltr")

        mapped_pages = [{"pdf_page_index": page_idx} for page_idx in range(4, 12)]
        apply_auto_mapping_to_pages(mapped_pages, result)
        self.assertEqual(mapped_pages[0]["citation_page_start"], "10")
        self.assertEqual(mapped_pages[0]["citation_page_end"], "11")
        self.assertEqual(mapped_pages[1]["citation_page_start"], "12")
        self.assertEqual(mapped_pages[1]["citation_page_end"], "13")

    def test_detects_rtl_spread_from_paired_outer_page_numbers(self) -> None:
        pages = layout_pages(20, 8)
        candidates = []
        for offset, page_idx in enumerate(range(20, 28)):
            lower = 40 + offset * 2
            candidates.extend(
                [cand(page_idx, lower + 1, x=0.08), cand(page_idx, lower, x=0.92)]
            )

        layout = detect_pdf_page_layout(pages, candidates)
        self.assertEqual(layout["layout_mode"], "spread")
        self.assertEqual(layout["reading_direction"], "rtl")
        result = infer(candidates, page_count=40, layout_detection=layout)
        segment = result["applied_segments"][0]
        self.assertEqual(segment["citation_page_start"], "40")
        self.assertEqual(segment["reading_direction"], "rtl")

    def test_landscape_two_column_single_pages_are_not_mistaken_for_spreads(self) -> None:
        pages = layout_pages(10, 10)
        candidates = [cand(page_idx, page_idx - 9, x=0.92) for page_idx in range(10, 20)]

        layout = detect_pdf_page_layout(pages, candidates)

        self.assertEqual(layout["layout_mode"], "single")
        self.assertEqual(layout["evidence"]["paired_page_numbers"], 0)

    def test_spread_without_direction_pairs_requires_review(self) -> None:
        pages = layout_pages(40, 8)
        candidates = [
            cand(page_idx, 80 + offset * 2, x=0.08)
            for offset, page_idx in enumerate(range(40, 48))
        ]

        layout = detect_pdf_page_layout(pages, candidates)
        result = infer(candidates, page_count=60, layout_detection=layout)

        self.assertEqual(layout["layout_mode"], "spread")
        self.assertEqual(layout["confidence_level"], "medium")
        self.assertEqual(result["applied_segments"], [])
        self.assertEqual(result["selected_segments"][0]["confidence_level"], "medium")

    def test_portrait_pages_keep_the_existing_single_page_mapping_path(self) -> None:
        pages = layout_pages(30, 8, landscape=False, split=False)
        candidates = [cand(page_idx, page_idx - 29, x=0.5) for page_idx in range(30, 38)]

        layout = detect_pdf_page_layout(pages, candidates)
        result = infer(candidates, page_count=50, layout_detection=layout)

        self.assertEqual(layout["layout_mode"], "single")
        self.assertEqual(result["applied_segments"][0].get("layout_mode"), None)
        mapped_pages = [{"pdf_page_index": page_idx} for page_idx in range(30, 38)]
        apply_auto_mapping_to_pages(mapped_pages, result)
        self.assertTrue(all(page["layout_mode"] == "single" for page in mapped_pages))

    def test_normalizes_common_page_number_forms(self) -> None:
        self.assertEqual(normalize_page_candidate("— 12 —"), ("arabic", 12, "12"))
        self.assertEqual(normalize_page_candidate("第１２页"), ("arabic", 12, "12"))
        self.assertEqual(normalize_page_candidate("[xii]"), ("roman_lower", 12, "xii"))
        self.assertEqual(normalize_page_candidate("O8"), ("arabic", 8, "8"))

    def test_normalizes_vertical_chinese_page_number_forms(self) -> None:
        self.assertEqual(normalize_page_candidate("七四"), ("cjk_decimal", 74, "74"))
        self.assertEqual(normalize_page_candidate("二三四"), ("cjk_decimal", 234, "234"))
        self.assertEqual(
            normalize_page_candidate("二百三十四"),
            ("cjk_multiplicative", 234, "234"),
        )
        self.assertEqual(normalize_page_candidate("貳參肆"), ("cjk_decimal", 234, "234"))

    def test_normalizes_numeric_bookmark_forms_without_chapter_false_positives(self) -> None:
        for raw in ("1", "003", "第12页", "P.12", "P 12", "Page 12", "页12"):
            self.assertEqual(normalize_numeric_bookmark_title(raw)[1], int(''.join(ch for ch in raw if ch.isdigit())))
        for raw in ("第一章", "第2章", "第三编", "附录一"):
            self.assertIsNone(normalize_numeric_bookmark_title(raw))

    def test_numeric_bookmark_sequence_uses_zero_based_internal_index(self) -> None:
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        document = MagicMock()
        document.__len__.return_value = 100
        document.get_toc.return_value = [[1, "1", 27], [1, "2", 28], [1, "10", 36], [1, "第一章", 40]]
        with patch("fitz.open", return_value=document):
            candidates = extract_pdf_numeric_bookmark_candidates(Path("book.pdf"))
        self.assertEqual(candidates[0].page_idx, 26)
        self.assertEqual(candidates[0].target_pdf_page_1based, 27)
        self.assertEqual(len(candidates), 3)
        result = infer(candidates, page_count=100)
        segment = result["selected_segments"][0]
        self.assertEqual(segment["pdf_page_start"], 26)
        self.assertEqual(segment["citation_page_start"], "1")
        self.assertEqual(segment["method"], "numeric_bookmark_sequence")

    def test_sparse_numeric_bookmarks_fit_one_offset(self) -> None:
        bookmarks = [cand(26, 1), cand(35, 10), cand(45, 20), cand(75, 50)]
        bookmarks = [
            PageNumberCandidate(**{**item.__dict__, "candidate_type": "numeric_bookmark", "source": "pdf_outline"})
            for item in bookmarks
        ]
        result = infer(bookmarks, page_count=100)
        segment = result["applied_segments"][0]
        self.assertEqual(segment["pdf_page_start"], 26)
        self.assertEqual(segment["citation_page_start"], "1")
        self.assertEqual(segment["method"], "numeric_bookmark_sequence")

    def test_native_pdf_without_labels_uses_edge_number_sequence(self) -> None:
        pages = []
        for page_idx, number in ((35, 1), (36, 2), (38, 4), (40, 6)):
            pages.append(
                {
                    "pdf_page_index": page_idx,
                    "text_raw": f"正文第 {number} 个测试页",
                    "page_width": 600,
                    "page_height": 800,
                    "blocks": [
                        {
                            "text": str(number),
                            "bbox": [285, 760, 315, 785],
                        }
                    ],
                }
            )
        candidates = extract_native_pdf_edge_candidates(pages)
        self.assertEqual([(item.page_idx, item.number) for item in candidates], [(35, 1), (36, 2), (38, 4), (40, 6)])
        with mock.patch("src.me_finder.page_mapping_service.extract_pdf_page_label_candidates", return_value=[]), mock.patch(
            "src.me_finder.page_mapping_service.extract_pdf_numeric_bookmark_candidates", return_value=[]
        ):
            result = PageMappingService().infer(Path("missing.pdf"), pages, page_count=50, dry_run=True)
        segment = result["applied_segments"][0]
        self.assertEqual(segment["pdf_page_start"], 35)
        self.assertEqual(segment["citation_page_start"], "1")
        self.assertEqual(segment["method"], "native_pdf_edge_sequence")
        self.assertEqual(result["mapping_status"], "auto_mapped_high")
        self.assertIn("no_page_labels", result["failure_reasons"])

    def test_native_edge_provider_filters_years_and_body_numbers(self) -> None:
        pages = [
            {
                "pdf_page_index": 0,
                "page_width": 600,
                "page_height": 800,
                "blocks": [
                    {"text": "2024", "bbox": [280, 770, 320, 790]},
                    {"text": "12", "bbox": [280, 380, 320, 400]},
                    {"text": "— 3 —", "bbox": [280, 770, 320, 790]},
                ],
            }
        ]
        candidates = extract_native_pdf_edge_candidates(pages)
        self.assertEqual([item.number for item in candidates], [3])

    def test_mineru_canvas_finds_alternating_vertical_chinese_folios(self) -> None:
        digits = "一二三四五六七八九"
        pages = []
        for offset, page_idx in enumerate(range(27, 36)):
            right_page = offset % 2 == 0
            x0, x1 = (910, 940) if right_page else (65, 95)
            pages.append(
                {
                    "pdf_page_index": page_idx,
                    # Real MinerU imports retain physical dimensions that do
                    # not match the structured 1000 x 1000 bbox canvas.
                    "page_width": 407.52 if offset < 5 else 1634,
                    "page_height": 589.68 if offset < 5 else 2400,
                    "blocks": [
                        {
                            "text": digits[offset],
                            "bbox": [x0, 760, x1, 815],
                            "mineru_item_index": 2,
                        }
                    ],
                }
            )

        candidates = extract_native_pdf_edge_candidates(pages)

        self.assertEqual([item.number for item in candidates], list(range(1, 10)))
        self.assertTrue(all(item.number_style == "cjk_decimal" for item in candidates))
        result = infer(candidates, page_count=40)
        segment = result["applied_segments"][0]
        self.assertEqual(segment["pdf_page_start"], 27)
        self.assertEqual(segment["citation_page_start"], "1")
        self.assertEqual(segment["citation_page_end"], "9")
        self.assertEqual(segment["confidence_level"], "high")

    def test_split_vertical_digit_blocks_are_joined_before_sequence_fitting(self) -> None:
        pages = [
            {
                "pdf_page_index": 260,
                "page_width": 407.52,
                "page_height": 589.68,
                "blocks": [
                    {"text": "二", "bbox": [910, 748, 940, 766], "mineru_item_index": 1},
                    {"text": "三", "bbox": [910, 768, 940, 786], "mineru_item_index": 2},
                    {"text": "四", "bbox": [910, 788, 940, 806], "mineru_item_index": 3},
                ],
            }
        ]

        candidates = extract_native_pdf_edge_candidates(pages)
        best = max(candidates, key=lambda item: item.score)

        self.assertEqual(best.raw_candidate, "二三四")
        self.assertEqual(best.number, 234)
        self.assertEqual(best.number_style, "cjk_decimal")
        self.assertEqual(best.score, 0.96)

    def test_pdf_without_any_evidence_reports_failure_reasons(self) -> None:
        with mock.patch("src.me_finder.page_mapping_service.extract_pdf_page_label_candidates", return_value=[]), mock.patch(
            "src.me_finder.page_mapping_service.extract_pdf_numeric_bookmark_candidates", return_value=[]
        ):
            result = PageMappingService().infer(Path("missing.pdf"), [], page_count=20, dry_run=True)
        self.assertEqual(result["mapping_status"], "auto_mapping_failed")
        self.assertIn("no_edge_candidates", result["failure_reasons"])
        self.assertIn("sequence_not_found", result["failure_reasons"])

    def test_manual_mapping_is_only_previewed_in_dry_run(self) -> None:
        candidates = [cand(10 + index, 1 + index) for index in range(6)]
        with mock.patch("src.me_finder.page_mapping_service.extract_pdf_page_label_candidates", return_value=[]), mock.patch(
            "src.me_finder.page_mapping_service.extract_pdf_numeric_bookmark_candidates", return_value=candidates
        ):
            preview = PageMappingService().infer(
                Path("missing.pdf"), [], page_count=30, dry_run=True, manual_mapping_present=True
            )
            import_result = PageMappingService().infer(
                Path("missing.pdf"), [], page_count=30, dry_run=False, manual_mapping_present=True
            )
        self.assertEqual(preview["mapping_status"], "auto_mapped_high")
        self.assertTrue(preview["manual_mapping_present"])
        self.assertEqual(import_result["mapping_status"], "manual_mapped")

    def test_body_starts_from_one(self) -> None:
        result = infer([cand(10 + i, 1 + i) for i in range(10)])
        segment = result["applied_segments"][0]
        self.assertEqual(segment["pdf_page_start"], 10)
        self.assertEqual(segment["citation_page_start"], "1")
        self.assertEqual(segment["confidence_level"], "high")

    def test_late_offset_cluster_does_not_backfill_over_earlier_sequence(self) -> None:
        candidates = [cand(20 + i, 1 + i) for i in range(20)]
        # One missing scan changes the local PDF-to-print offset.  The second
        # cluster starts at printed page 21 and must not be extrapolated back
        # over the already-established first cluster.
        candidates += [cand(40 + i, 22 + i) for i in range(20)]

        result = infer(candidates, page_count=80)
        segments = result["applied_segments"]

        self.assertEqual(
            [
                (
                    segment["pdf_page_start"],
                    segment["pdf_page_end"],
                    segment["citation_page_start"],
                    segment["citation_page_end"],
                )
                for segment in segments
            ],
            [(20, 39, "1", "20"), (40, 59, "22", "41")],
        )

    def test_ocr_sequence_does_not_backfill_across_nearby_toc(self) -> None:
        candidates = [cand(22 + i, 3 + i) for i in range(12)]
        page_texts = {
            18: "\n".join(["目次條目"] * 10 + ["莊子集釋", "目", "錄"]),
            22: "莊子集釋卷一上",
        }

        result = infer(candidates, page_count=80, page_texts=page_texts)
        segment = result["applied_segments"][0]

        self.assertEqual(segment["pdf_page_start"], 22)
        self.assertEqual(segment["citation_page_start"], "3")
        self.assertTrue(segment["mapping_evidence"]["backfill_suppressed_near_toc"])

    def test_dominant_recurring_offset_ignores_small_conflicting_cluster(self) -> None:
        candidates = [cand(20 + i, 1 + i) for i in range(20)]
        candidates += [cand(45 + i, 10 + i) for i in range(4)]
        candidates += [cand(60 + i, 41 + i) for i in range(30)]

        result = infer(candidates, page_count=100)
        segment = result["applied_segments"][0]

        self.assertEqual(segment["pdf_page_start"], 20)
        self.assertEqual(segment["pdf_page_end"], 89)
        self.assertEqual(segment["citation_page_start"], "1")
        self.assertEqual(segment["citation_page_end"], "70")

    def test_preface_and_body_can_reset_to_one(self) -> None:
        candidates = [cand(15 + i, 1 + i) for i in range(8)]
        candidates += [cand(35 + i, 1 + i) for i in range(12)]
        result = infer(candidates)
        starts = [seg["pdf_page_start"] for seg in result["applied_segments"]]
        self.assertIn(15, starts)
        self.assertIn(35, starts)

    def test_roman_front_matter_and_arabic_body(self) -> None:
        candidates = [cand(1 + i, 1 + i, "roman_lower") for i in range(6)]
        candidates += [cand(12 + i, 1 + i) for i in range(8)]
        result = infer(candidates)
        styles = {seg["number_style"] for seg in result["applied_segments"]}
        self.assertIn("roman_lower", styles)
        self.assertIn("arabic", styles)

    def test_body_discussing_disorder_is_not_classified_as_preface(self) -> None:
        # Regression for 追寻美德/法哲学原理: body text about 无序/秩序 plus an
        # arabic-numeral chapter heading must classify as body, not preface.
        page_texts = {
            9: "目录\n序……(1)\n第 1 章 一个令人忧虑的联想 …… (1)",
            11: "第1章 一个令人忧虑的联想\n想像一下自然科学遭受一场浩劫后的可怕情形。",
            12: "处于一种严重的无序状态。我们会注意到有序与无序的标准。",
            13: "道德语言的秩序问题贯穿全书。",
        }
        candidates = [cand(11 + i, 1 + i) for i in range(30)]
        result = infer(candidates, page_count=400, page_texts=page_texts)
        self.assertEqual(result["applied_segments"][0]["page_scope"], "body")

    def test_true_preface_heading_line_still_classifies_as_preface(self) -> None:
        page_texts = {
            5: "译 序\n本书作者阿拉斯代尔·麦金太尔是当代德性伦理学的代表人物。",
            6: "译序继续讨论翻译体例与术语。",
        }
        candidates = [cand(5 + i, 1 + i) for i in range(6)]
        result = infer(candidates, page_count=400, page_texts=page_texts)
        self.assertEqual(result["applied_segments"][0]["page_scope"], "preface")

    def test_classic_title_ending_in_preface_marker_is_preface(self) -> None:
        page_texts = {
            11: "淮南鴻烈集解序\n整理國故，約有三途。",
            12: "序文繼續討論校勘方法。",
        }
        candidates = [cand(11 + i, 1 + i) for i in range(6)]
        result = infer(candidates, page_count=100, page_texts=page_texts)
        self.assertEqual(result["applied_segments"][0]["page_scope"], "preface")

    def test_vertical_toc_heading_is_front_matter(self) -> None:
        page_texts = {20: "目\n錄\n校點前言……一\n尚書正義序……一"}
        candidates = [cand(20 + i, 1 + i) for i in range(7)]
        result = infer(candidates, page_count=100, page_texts=page_texts)
        self.assertEqual(result["applied_segments"][0]["page_scope"], "front_matter")

    def test_long_segment_is_never_classified_as_preface(self) -> None:
        page_texts = {20: "序言\n这个分段有一个像序言的开头，但长达六十页。"}
        candidates = [cand(20 + i, 1 + i) for i in range(60)]
        result = infer(candidates, page_count=400, page_texts=page_texts)
        self.assertEqual(result["applied_segments"][0]["page_scope"], "body")

    def test_pages_before_segment_do_not_taint_scope(self) -> None:
        page_texts = {
            9: "序言\n真正的序言在分段开始之前。",
            10: "出版说明",
            11: "平实的正文段落，没有任何标题。",
        }
        candidates = [cand(11 + i, 1 + i) for i in range(10)]
        result = infer(candidates, page_count=400, page_texts=page_texts)
        self.assertEqual(result["applied_segments"][0]["page_scope"], "body")

    def test_preface_part_title_beats_noisy_body_heading_later_in_window(self) -> None:
        # 法哲学原理式场景：序言分段首页是整行"序"扉页标题，窗口内某页的
        # OCR 噪声混有"第一节"字样，不应把序言误判为正文。
        page_texts = {
            54: "序\n我的职务是担任讲授法哲学，需要发给听众讲授提纲。",
            60: "序\n士\n同\n……第一节……",
        }
        candidates = [cand(54 + i, 1 + i) for i in range(18)]
        result = infer(candidates, page_count=500, page_texts=page_texts)
        self.assertEqual(result["applied_segments"][0]["page_scope"], "preface")

    def test_missing_chapter_first_page_is_backfilled(self) -> None:
        result = infer([cand(51 + i, 2 + i) for i in range(4)])
        segment = result["applied_segments"][0]
        self.assertEqual(segment["pdf_page_start"], 50)
        pages = [{"pdf_page_index": i} for i in range(50, 55)]
        apply_auto_mapping_to_pages(pages, result)
        self.assertEqual(pages[0]["citation_page"], "1")
        self.assertEqual(pages[1]["citation_page"], "2")

    def test_missing_half_of_page_numbers_still_fits_offset(self) -> None:
        candidates = [cand(50, 21), cand(51, 22), cand(53, 24), cand(55, 26)]
        result = infer(candidates)
        pages = [{"pdf_page_index": i} for i in range(30, 56)]
        apply_auto_mapping_to_pages(pages, result)
        by_page = {page["pdf_page_index"]: page for page in pages}
        self.assertEqual(by_page[52]["citation_page"], "23")
        self.assertEqual(by_page[52]["citation_page_start"], "23")
        self.assertEqual(by_page[52]["citation_page_end"], "23")
        self.assertEqual(by_page[52]["layout_mode"], "single")
        self.assertEqual(by_page[54]["citation_page"], "25")

    def test_single_wrong_ocr_number_is_ignored(self) -> None:
        candidates = [cand(50 + i, 21 + i) for i in range(8)]
        candidates.append(cand(54, 99))
        result = infer(candidates)
        self.assertEqual(result["applied_segments"][0]["citation_page_start"], "1")

    def test_alternating_outer_corner_positions_are_allowed(self) -> None:
        candidates = [cand(80 + i, 1 + i, x=0.12 if i % 2 == 0 else 0.88) for i in range(10)]
        result = infer(candidates)
        self.assertEqual(result["applied_segments"][0]["confidence_level"], "high")

    def test_bottom_centered_page_numbers_are_allowed(self) -> None:
        candidates = [cand(100 + i, 1 + i, x=0.5, y=0.95) for i in range(8)]
        result = infer(candidates)
        self.assertEqual(result["applied_segments"][0]["pdf_page_start"], 100)

    def test_missing_pdf_page_keeps_stable_offset(self) -> None:
        candidates = [cand(10, 1), cand(11, 2), cand(13, 4), cand(14, 5), cand(15, 6)]
        result = infer(candidates)
        self.assertEqual(result["applied_segments"][0]["pdf_page_start"], 10)

    def test_inserted_unnumbered_image_page_splits_sequence(self) -> None:
        candidates = [cand(20 + i, 1 + i) for i in range(5)]
        candidates += [cand(26 + i, 6 + i) for i in range(5)]
        result = infer(candidates)
        self.assertGreaterEqual(len(result["segments"]), 2)

    def test_toc_is_only_auxiliary_and_body_can_start_later(self) -> None:
        page_texts = {30: "目录\n第一章  1\n第二章  19", 38: "第一章 承认的形式"}
        candidates = [cand(38 + i, 1 + i) for i in range(7)]
        result = infer(candidates, page_texts=page_texts)
        segment = result["applied_segments"][0]
        self.assertEqual(segment["pdf_page_start"], 38)
        self.assertIn(segment["method"], {"ocr_sequence", "ocr_sequence_with_structure"})

    def test_too_few_observed_numbers_are_not_auto_applied(self) -> None:
        result = infer([cand(70, 1), cand(71, 2)])
        self.assertEqual(result["applied_segments"], [])
        self.assertEqual(result["selected_segments"][0]["confidence_level"], "low")


if __name__ == "__main__":
    unittest.main()

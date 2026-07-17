from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.auto_page_mapping import (
    PageNumberCandidate,
    apply_auto_mapping_to_pages,
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


def infer(candidates, page_count=600, page_texts=None):
    return infer_auto_page_mapping(candidates, page_count, page_texts=page_texts or {})


class AutoPageMappingTests(unittest.TestCase):
    def test_normalizes_common_page_number_forms(self) -> None:
        self.assertEqual(normalize_page_candidate("— 12 —"), ("arabic", 12, "12"))
        self.assertEqual(normalize_page_candidate("第１２页"), ("arabic", 12, "12"))
        self.assertEqual(normalize_page_candidate("[xii]"), ("roman_lower", 12, "xii"))
        self.assertEqual(normalize_page_candidate("O8"), ("arabic", 8, "8"))

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

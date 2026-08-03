from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from src.me_finder.database import DEFAULT_DATABASE_PATH
from src.me_finder.indexer import DEFAULT_INDEX_PATH, build_index
from src.me_finder.pdf_extractors import (
    _attach_bibliographic_metadata,
    attach_page_block_offsets,
    detect_pdf_type,
    make_pdf_paragraphs,
)
from src.me_finder.pdf_page_mapping import (
    PageMapper,
    normalize_manual_mapping_segments,
)
from src.me_finder.search import SearchEngine


PDF_CORPUS = Path("corpus/raw_pdf")
SEARCHABLE_PDF = PDF_CORPUS / "Critique of Forms of Life (Jaeggi, RahelCronin, Ciaran(Translation)) (Z-Library).pdf"
SCANNED_PDF = PDF_CORPUS / "伦理学简史 (阿拉斯代尔·麦金太尔（Alasdair Macintyre）) (z-library.sk, 1lib.sk, z-lib.sk).pdf"
COMPLEX_PDF = PDF_CORPUS / "Axel Honneth Reconceiving Social Philosophy (Dagmar Wilhelm) (z-library.sk, 1lib.sk, z-lib.sk).pdf"


def ensure_pdf_index() -> None:
    if not DEFAULT_INDEX_PATH.exists():
        build_index(include_pdf=True, pdf_limit=1, backup_existing=False)
        return
    connection = sqlite3.connect(str(DEFAULT_DATABASE_PATH))
    try:
        has_pdf = connection.execute(
            "SELECT 1 FROM paragraphs WHERE source_type = 'pdf' AND eligible_for_search = 1 LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if not has_pdf:
        build_index(include_pdf=True, pdf_limit=1, backup_existing=False)


class PDFPageMappingTests(unittest.TestCase):
    def test_layout_blocks_receive_exact_page_text_offsets(self) -> None:
        text = "左页正文\n右页正文\n"
        blocks = [
            {"text": "左页正文\n", "bbox_normalized": [0.1, 0.1, 0.45, 0.9]},
            {"text": "右页正文\n", "bbox_normalized": [0.55, 0.1, 0.9, 0.9]},
        ]

        attach_page_block_offsets(text, blocks)

        self.assertEqual((blocks[0]["page_char_start"], blocks[0]["page_char_end"]), (0, 5))
        self.assertEqual((blocks[1]["page_char_start"], blocks[1]["page_char_end"]), (5, 10))
        self.assertEqual(blocks[0]["offset_unit"], "unicode_codepoint")

    def test_manual_segment_mapping(self) -> None:
        mapper = PageMapper.from_config(
            {
                "page_mapping": {
                    "segments": [
                        {"pdf_page_start": 0, "pdf_page_end": 20, "citation": None},
                        {
                            "pdf_page_start": 21,
                            "pdf_page_end": 405,
                            "citation_page_start": "1",
                            "number_style": "arabic",
                            "method": "manual_segment",
                            "confidence": 0.95,
                        },
                    ]
                }
            }
        )
        self.assertIsNone(mapper.map_page(20, "xx").citation_page)
        mapped = mapper.map_page(21, "1")
        self.assertEqual(mapped.citation_page, "1")
        self.assertEqual(mapped.method, "manual_segment")
        self.assertEqual(mapper.map_page(22, "2").citation_page, "2")

    def test_spread_segment_maps_each_pdf_page_to_two_citation_pages(self) -> None:
        mapper = PageMapper.from_config(
            {
                "page_mapping": {
                    "segments": [
                        {
                            "pdf_page_start": 4,
                            "pdf_page_end": 45,
                            "citation_page_start": "10",
                            "layout_mode": "spread",
                            "reading_direction": "ltr",
                            "gutter_x": 0.505,
                        }
                    ]
                }
            }
        )

        page_14 = mapper.map_page(13)
        self.assertEqual(page_14.citation_page_start, "28")
        self.assertEqual(page_14.citation_page_end, "29")
        self.assertEqual(page_14.layout_mode, "spread")
        self.assertEqual(page_14.reading_direction, "ltr")
        self.assertEqual(page_14.gutter_x, 0.505)
        page_46 = mapper.map_page(45)
        self.assertEqual(page_46.citation_page_start, "92")
        self.assertEqual(page_46.citation_page_end, "93")

    def test_legacy_segment_without_layout_remains_single_page(self) -> None:
        mapper = PageMapper.from_config(
            {
                "page_mapping": {
                    "segments": [
                        {
                            "pdf_page_start": 4,
                            "pdf_page_end": 45,
                            "citation_page_start": "10",
                        }
                    ]
                }
            }
        )

        page_14 = mapper.map_page(13)
        self.assertEqual(page_14.citation_page_start, "19")
        self.assertEqual(page_14.citation_page_end, "19")
        self.assertEqual(page_14.layout_mode, "single")

    def test_manual_spread_segment_is_normalized_before_persistence(self) -> None:
        segments = normalize_manual_mapping_segments(
            [
                {
                    "pdf_page_start": "4",
                    "pdf_page_end": "45",
                    "citation_page_start": 10,
                    "layout_mode": "spread",
                    "reading_direction": "rtl",
                    "gutter_x": 0.52,
                }
            ]
        )

        self.assertEqual(
            segments[0],
            {
                "pdf_page_start": 4,
                "pdf_page_end": 45,
                "citation_page_start": "10",
                "number_style": "arabic",
                "method": "manual_segment",
                "confidence": 0.9,
                "layout_mode": "spread",
                "reading_direction": "rtl",
                "gutter_x": 0.52,
            },
        )

    def test_spread_page_paragraph_uses_the_full_logical_page_range(self) -> None:
        page = {
            "pdf_page_id": "pdf-spread-PAGE-000013",
            "source_file_id": "pdf-spread",
            "pdf_page_index": 13,
            "text_raw": (
                "左页与右页组成一张双开扫描页，当前阶段按可靠范围生成引用页码。"
            ),
            "citation_page": "28",
            "citation_page_start": "28",
            "citation_page_end": "29",
            "printed_page_start": "28",
            "printed_page_end": "29",
            "page_mapping_method": "manual_segment",
            "page_mapping_confidence": 0.9,
            "segment_id": "MAPSEG-000004-000045",
            "layout_mode": "spread",
            "reading_direction": "ltr",
            "gutter_x": 0.5,
        }

        paragraphs = make_pdf_paragraphs(
            "pdf-spread",
            "PDF_SPREAD",
            "双开扫描测试",
            "测试作者",
            "spread.pdf",
            [page],
            "WORK-SPREAD",
        )

        self.assertEqual(len(paragraphs), 1)
        paragraph = paragraphs[0]
        self.assertEqual(paragraph["citation_page_start"], "28")
        self.assertEqual(paragraph["citation_page_end"], "29")
        self.assertEqual(paragraph["page_display"], "引用页码：28-29")
        self.assertEqual(paragraph["layout_mode"], "spread")

    def test_critical_theory_duplicate_scan_pages_use_segmented_mapping(self) -> None:
        mapper = PageMapper.from_config(
            {
                "page_mapping": {
                    "segments": [
                        {
                            "pdf_page_start": 47,
                            "pdf_page_end": 140,
                            "citation_page_start": "1",
                            "number_style": "arabic",
                        },
                        {
                            "pdf_page_start": 141,
                            "pdf_page_end": 213,
                            "citation_page_start": "94",
                            "number_style": "arabic",
                        },
                        {
                            "pdf_page_start": 214,
                            "pdf_page_end": 324,
                            "citation_page_start": "166",
                            "number_style": "arabic",
                        }
                    ]
                }
            }
        )
        self.assertIsNone(mapper.map_page(46).citation_page)
        self.assertEqual(mapper.map_page(47).citation_page, "1")
        self.assertEqual(mapper.map_page(48).citation_page, "2")
        self.assertEqual(mapper.map_page(140).citation_page, "94")
        self.assertEqual(mapper.map_page(141).citation_page, "94")
        self.assertEqual(mapper.map_page(194).citation_page, "147")
        self.assertEqual(mapper.map_page(213).citation_page, "166")
        self.assertEqual(mapper.map_page(214).citation_page, "166")
        self.assertEqual(mapper.map_page(324).citation_page, "276")


class PDFBibliographicProjectionTests(unittest.TestCase):
    def test_source_projection_keeps_country_and_journal_fields(self) -> None:
        source = {"source_file_id": "pdf-test"}
        _attach_bibliographic_metadata(
            source,
            {
                "author": "马克斯·霍克海默",
                "country": "德",
                "journal_name": "哲学研究",
                "issue": "2",
                "metadata_status": "complete",
            },
        )

        self.assertEqual(source["country"], "德")
        self.assertEqual(source["bibliographic_metadata"]["country"], "德")
        self.assertEqual(source["bibliographic_metadata"]["journal_name"], "哲学研究")
        self.assertEqual(source["bibliographic_metadata"]["issue"], "2")


class PDFDetectionTests(unittest.TestCase):
    def test_native_text_pdf_detection(self) -> None:
        profile = detect_pdf_type(SEARCHABLE_PDF)
        self.assertEqual(profile["detected_pdf_type"], "native_text")
        self.assertGreater(profile["avg_text_chars_per_page"], 100)

    def test_scanned_or_broken_pdf_detection(self) -> None:
        profile = detect_pdf_type(SCANNED_PDF)
        self.assertIn(profile["detected_pdf_type"], {"scanned", "broken_text"})

    def test_object_stream_pdf_detection(self) -> None:
        profile = detect_pdf_type(COMPLEX_PDF)
        self.assertIn(profile["detected_pdf_type"], {"native_text", "complex_layout"})


class PDFSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_pdf_index()
        cls.engine = SearchEngine()
        cls.cases = json.loads(Path("tests/known_pdf_quotes.json").read_text(encoding="utf-8"))

    def test_known_pdf_quotes(self) -> None:
        for case in self.cases:
            with self.subTest(case=case["id"]):
                result = self.engine.search(
                    case["query"],
                    case.get("mode", "auto"),
                    case.get("limit", 10),
                    case.get("source_type", "pdf"),
                )
                self.assertGreater(result["total"], 0)
                matches = result["results"]
                self.assertTrue(any(item["source_type"] == case["expected_source_type"] for item in matches))
                self.assertTrue(
                    any(item["document_title"] == case["expected_document_title"] for item in matches),
                    "Expected PDF document title in results",
                )
                if "expected_contains" in case:
                    joined = "\n".join(item["paragraph_text"] for item in matches)
                    self.assertIn(case["expected_contains"], joined)
                if "expected_citation_page_start" in case:
                    self.assertTrue(
                        any(item.get("citation_page_start") == case["expected_citation_page_start"] for item in matches),
                        f"Expected citation page {case['expected_citation_page_start']}",
                    )
                if "expected_citation_page_end" in case:
                    self.assertTrue(
                        any(item.get("citation_page_end") == case["expected_citation_page_end"] for item in matches),
                        f"Expected citation end page {case['expected_citation_page_end']}",
                    )
                if case.get("expected_uncalibrated"):
                    self.assertTrue(any("引用页码尚未校准" in str(item.get("page")) for item in matches))
                if case.get("expected_mapping_method"):
                    self.assertTrue(
                        any(item.get("page_mapping_method") == case["expected_mapping_method"] for item in matches),
                        f"Expected mapping method {case['expected_mapping_method']}",
                    )
                if case.get("expected_cross_page"):
                    self.assertTrue(any(item.get("is_cross_page") for item in matches))

    def test_source_type_filter_excludes_pdf_from_word_only_search(self) -> None:
        result = self.engine.search("We make and cannot escape making value judgments", source_type="word")
        self.assertEqual(result["total"], 0)

    def test_open_source_url_is_present_for_pdf_results(self) -> None:
        result = self.engine.search("We make and cannot escape making value judgments", source_type="pdf", limit=1)
        self.assertTrue(result["results"][0]["open_source_url"].startswith("/source/pdf-critique-forms-life"))

    def test_single_page_hit_is_not_duplicated_by_cross_page_window(self) -> None:
        result = self.engine.search("马克思主义需要有新的大发展", source_type="pdf", limit=10)
        matches = [item for item in result["results"] if item.get("document_title") == "批判理论"]
        self.assertEqual(len(matches), 1)
        self.assertFalse(matches[0].get("is_cross_page"))


if __name__ == "__main__":
    unittest.main()

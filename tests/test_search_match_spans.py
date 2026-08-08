from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List

from src.me_finder.database import build_database
from src.me_finder.normalization import (
    compact_text,
    normalize_pdf_text,
    normalize_text,
    punctuationless_text,
)
from src.me_finder.search import SearchEngine


class SearchMatchSpanTests(unittest.TestCase):
    @contextmanager
    def _engine(
        self,
        paragraphs: List[Dict[str, object]],
        pdf_pages: List[Dict[str, object]] | None = None,
    ) -> Iterator[SearchEngine]:
        index = {
            "metadata": {"anchor_spec_version": 1},
            "source_files": [],
            "volumes": [],
            "works": [],
            "paragraphs": paragraphs,
            "pdf_pages": pdf_pages or [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "index.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
            engine = SearchEngine(index_path)
            try:
                yield engine
            finally:
                engine.close()

    @contextmanager
    def _sqlite_engine(
        self,
        paragraphs: List[Dict[str, object]],
        pdf_pages: List[Dict[str, object]] | None = None,
    ) -> Iterator[SearchEngine]:
        index = {
            "metadata": {"anchor_spec_version": 1},
            "source_files": [],
            "volumes": [],
            "works": [],
            "paragraphs": paragraphs,
            "pdf_pages": pdf_pages or [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "index.sqlite3"
            build_database(index, database_path)
            engine = SearchEngine(database_path)
            try:
                yield engine
            finally:
                engine.close()

    @staticmethod
    def _paragraph(
        paragraph_id: str,
        text: str,
        spans: object = None,
        *,
        source_type: str = "pdf",
        is_cross_page: bool = False,
        paragraph_index: int = 0,
        pdf_page_start_index: int = 0,
        pdf_page_end_index: int | None = None,
    ) -> Dict[str, object]:
        if pdf_page_end_index is None:
            pdf_page_end_index = (
                pdf_page_start_index + 1 if is_cross_page else pdf_page_start_index
            )
        paragraph: Dict[str, object] = {
            "paragraph_id": paragraph_id,
            "volume_id": "TEST-VOLUME",
            "volume_number": None,
            "work_id": "TEST-WORK",
            "source_file_id": "pdf-test",
            "source_type": source_type,
            "paragraph_index": paragraph_index,
            "eligible_for_search": True,
            "text_raw": text,
            "normalized_text": normalize_text(text),
            "compact_text": compact_text(text),
            "plain_text": punctuationless_text(text),
            "document_title": "测试文献",
            "work_title": "测试文献",
            "volume_display": "测试文献",
            "page_display": "引用页码尚未校准",
            "page_source_type": "uncalibrated" if source_type == "pdf" else "unknown",
            "pdf_page_start_index": (
                pdf_page_start_index if source_type == "pdf" else None
            ),
            "pdf_page_end_index": (
                pdf_page_end_index if source_type == "pdf" else None
            ),
            "is_cross_page": is_cross_page,
            "original_file_name": "test.pdf" if source_type == "pdf" else "test.docx",
        }
        if spans is not None:
            paragraph["text_source_spans"] = spans
        return paragraph

    def test_regular_pdf_page_maps_half_open_codepoint_offsets(self) -> None:
        text = "前言目标句后记"
        paragraph = self._paragraph(
            "PDF-PAGE",
            text,
            [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(text),
                    "pdf_page_id": "pdf-test-PAGE-000000",
                    "page_char_start": 40,
                    "page_char_end": 40 + len(text),
                    "page_text_hash": "hash-page-zero",
                }
            ],
        )
        with self._engine([paragraph]) as engine:
            item = engine.search("目标句", mode="exact", source_type="pdf")["results"][0]

        self.assertEqual((item["match_start"], item["match_end"]), (2, 5))
        self.assertEqual(item["match_offset_unit"], "unicode_codepoint")
        self.assertEqual(
            item["page_match_spans"],
            [
                {
                    "pdf_page_id": "pdf-test-PAGE-000000",
                    "page_char_start": 42,
                    "page_char_end": 45,
                    "match_quote": "目标句",
                    "page_text_hash": "hash-page-zero",
                }
            ],
        )
        self.assertEqual(item["match_quote"], "目标句")
        self.assertTrue(item["precise_highlight_available"])

    def test_pdf_dehyphenation_maps_back_to_the_complete_raw_range(self) -> None:
        text = "before inter-\nnational after"
        paragraph = self._paragraph(
            "PDF-DEHYPHENATED",
            text,
            [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(text),
                    "pdf_page_id": "pdf-test-PAGE-000000",
                    "page_char_start": 20,
                    "page_char_end": 20 + len(text),
                }
            ],
        )
        paragraph["normalized_text"] = normalize_pdf_text(text)
        expected_start = text.index("inter-")
        expected_end = expected_start + len("inter-\nnational")

        for engine_factory in (self._engine, self._sqlite_engine):
            with self.subTest(backend=engine_factory.__name__):
                with engine_factory([paragraph]) as engine:
                    item = engine.search(
                        "international", mode="exact", source_type="pdf"
                    )["results"][0]

                self.assertEqual(
                    (item["match_start"], item["match_end"]),
                    (expected_start, expected_end),
                )
                self.assertEqual(item["matched_text"], "inter-\nnational")
                self.assertEqual(
                    item["page_match_spans"],
                    [
                        {
                            "pdf_page_id": "pdf-test-PAGE-000000",
                            "page_char_start": 20 + expected_start,
                            "page_char_end": 20 + expected_end,
                            "match_quote": "inter-\nnational",
                        }
                    ],
                )

    def test_nfkc_composition_maps_to_all_consumed_source_codepoints(self) -> None:
        text = "prefix e\u0301 suffix"
        paragraph = self._paragraph(
            "WORD-COMBINING",
            text,
            source_type="word",
        )

        with self._engine([paragraph]) as engine:
            item = engine.search("é", mode="exact", source_type="word")["results"][0]

        start = text.index("e\u0301")
        self.assertEqual((item["match_start"], item["match_end"]), (start, start + 2))
        self.assertEqual(item["matched_text"], "e\u0301")

    def test_cross_hits_map_to_left_right_and_both_pages(self) -> None:
        left = "左页开头与左页尾"
        right = "右页首句和右页末尾"
        text = f"{left}\n{right}"
        spans = [
            {
                "paragraph_char_start": 0,
                "paragraph_char_end": len(left),
                "pdf_page_id": "pdf-test-PAGE-000010",
                "page_char_start": 100,
                "page_char_end": 100 + len(left),
            },
            {
                "paragraph_char_start": len(left) + 1,
                "paragraph_char_end": len(text),
                "pdf_page_id": "pdf-test-PAGE-000011",
                "page_char_start": 20,
                "page_char_end": 20 + len(right),
            },
        ]
        paragraph = self._paragraph("PDF-CROSS", text, spans, is_cross_page=True)
        with self._engine([paragraph]) as engine:
            left_item = engine.search("左页尾", mode="exact", source_type="pdf")["results"][0]
            right_item = engine.search("右页首句", mode="exact", source_type="pdf")["results"][0]
            both_item = engine.search("尾\n右", mode="exact", source_type="pdf")["results"][0]

        left_start = left.index("左页尾")
        self.assertEqual(
            left_item["page_match_spans"],
            [
                {
                    "pdf_page_id": "pdf-test-PAGE-000010",
                    "page_char_start": 100 + left_start,
                    "page_char_end": 100 + left_start + len("左页尾"),
                    "match_quote": "左页尾",
                }
            ],
        )
        self.assertEqual(
            right_item["page_match_spans"],
            [
                {
                    "pdf_page_id": "pdf-test-PAGE-000011",
                    "page_char_start": 20,
                    "page_char_end": 20 + len("右页首句"),
                    "match_quote": "右页首句",
                }
            ],
        )
        self.assertEqual(
            both_item["page_match_spans"],
            [
                {
                    "pdf_page_id": "pdf-test-PAGE-000010",
                    "page_char_start": 100 + len(left) - 1,
                    "page_char_end": 100 + len(left),
                    "match_quote": "尾",
                },
                {
                    "pdf_page_id": "pdf-test-PAGE-000011",
                    "page_char_start": 20,
                    "page_char_end": 21,
                    "match_quote": "右",
                },
            ],
        )
        self.assertEqual(
            [span["match_quote"] for span in both_item["page_match_spans"]],
            ["尾", "右"],
        )
        self.assertTrue(both_item["precise_highlight_available"])

    def test_true_cross_page_hit_survives_same_text_on_real_page(self) -> None:
        query = "跨页目标"
        left_fragment = "跨页目"
        right_fragment = "标随后继续"
        left_page_text = f"{query}在左页另有一处；{left_fragment}"
        right_page_text = right_fragment
        cross_text = f"{left_fragment}\n{right_fragment}"
        left_fragment_start = left_page_text.rindex(left_fragment)
        paragraphs = [
            self._paragraph(
                "PDF-PAGE-TRUE-CROSS-0",
                left_page_text,
                [
                    {
                        "paragraph_char_start": 0,
                        "paragraph_char_end": len(left_page_text),
                        "pdf_page_id": "pdf-test-PAGE-000000",
                        "pdf_page_index": 0,
                        "page_char_start": 0,
                        "page_char_end": len(left_page_text),
                    }
                ],
                paragraph_index=0,
                pdf_page_start_index=0,
            ),
            self._paragraph(
                "PDF-CROSS-TRUE-0-1",
                cross_text,
                [
                    {
                        "paragraph_char_start": 0,
                        "paragraph_char_end": len(left_fragment),
                        "pdf_page_id": "pdf-test-PAGE-000000",
                        "pdf_page_index": 0,
                        "page_char_start": left_fragment_start,
                        "page_char_end": left_fragment_start + len(left_fragment),
                    },
                    {
                        "paragraph_char_start": len(left_fragment) + 1,
                        "paragraph_char_end": len(cross_text),
                        "pdf_page_id": "pdf-test-PAGE-000001",
                        "pdf_page_index": 1,
                        "page_char_start": 0,
                        "page_char_end": len(right_fragment),
                    },
                ],
                is_cross_page=True,
                paragraph_index=1,
                pdf_page_start_index=0,
                pdf_page_end_index=1,
            ),
            self._paragraph(
                "PDF-PAGE-TRUE-CROSS-1",
                right_page_text,
                [
                    {
                        "paragraph_char_start": 0,
                        "paragraph_char_end": len(right_page_text),
                        "pdf_page_id": "pdf-test-PAGE-000001",
                        "pdf_page_index": 1,
                        "page_char_start": 0,
                        "page_char_end": len(right_page_text),
                    }
                ],
                paragraph_index=2,
                pdf_page_start_index=1,
            ),
        ]

        for factory in (self._engine, self._sqlite_engine):
            with self.subTest(backend=factory.__name__), factory(paragraphs) as engine:
                results = engine.search(
                    query, mode="compact", limit="all", source_type="pdf"
                )["results"]

            by_id = {item["paragraph_id"]: item for item in results}
            self.assertEqual(
                set(by_id),
                {"PDF-PAGE-TRUE-CROSS-0", "PDF-CROSS-TRUE-0-1"},
            )
            self.assertEqual(
                {span["pdf_page_id"] for span in by_id["PDF-CROSS-TRUE-0-1"]["page_match_spans"]},
                {"pdf-test-PAGE-000000", "pdf-test-PAGE-000001"},
            )

    def test_single_page_cross_helper_exact_duplicate_is_removed(self) -> None:
        query = "页内唯一目标"
        left_page_text = f"{query}留在左页"
        right_page_text = "右页无关内容"
        cross_text = f"{left_page_text}\n{right_page_text}"
        paragraphs = [
            self._paragraph(
                "PDF-PAGE-SINGLE-0",
                left_page_text,
                [
                    {
                        "paragraph_char_start": 0,
                        "paragraph_char_end": len(left_page_text),
                        "pdf_page_id": "pdf-test-PAGE-000000",
                        "pdf_page_index": 0,
                        "page_char_start": 0,
                        "page_char_end": len(left_page_text),
                    }
                ],
                paragraph_index=0,
                pdf_page_start_index=0,
            ),
            self._paragraph(
                "PDF-CROSS-SINGLE-0-1",
                cross_text,
                [
                    {
                        "paragraph_char_start": 0,
                        "paragraph_char_end": len(left_page_text),
                        "pdf_page_id": "pdf-test-PAGE-000000",
                        "pdf_page_index": 0,
                        "page_char_start": 0,
                        "page_char_end": len(left_page_text),
                    },
                    {
                        "paragraph_char_start": len(left_page_text) + 1,
                        "paragraph_char_end": len(cross_text),
                        "pdf_page_id": "pdf-test-PAGE-000001",
                        "pdf_page_index": 1,
                        "page_char_start": 0,
                        "page_char_end": len(right_page_text),
                    },
                ],
                is_cross_page=True,
                paragraph_index=1,
                pdf_page_start_index=0,
                pdf_page_end_index=1,
            ),
            self._paragraph(
                "PDF-PAGE-SINGLE-1",
                right_page_text,
                [
                    {
                        "paragraph_char_start": 0,
                        "paragraph_char_end": len(right_page_text),
                        "pdf_page_id": "pdf-test-PAGE-000001",
                        "pdf_page_index": 1,
                        "page_char_start": 0,
                        "page_char_end": len(right_page_text),
                    }
                ],
                paragraph_index=2,
                pdf_page_start_index=1,
            ),
        ]

        for factory in (self._engine, self._sqlite_engine):
            with self.subTest(backend=factory.__name__), factory(paragraphs) as engine:
                results = engine.search(
                    query, mode="exact", limit="all", source_type="pdf"
                )["results"]

            self.assertEqual(
                [item["paragraph_id"] for item in results],
                ["PDF-PAGE-SINGLE-0"],
            )

    def test_cross_joiner_only_has_no_page_mapping(self) -> None:
        left = "左页"
        right = "右页"
        text = f"{left}\n{right}"
        paragraph = self._paragraph(
            "PDF-CROSS-JOINER",
            text,
            [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(left),
                    "pdf_page_id": "pdf-test-PAGE-000000",
                    "page_char_start": 0,
                    "page_char_end": len(left),
                },
                {
                    "paragraph_char_start": len(left) + 1,
                    "paragraph_char_end": len(text),
                    "pdf_page_id": "pdf-test-PAGE-000001",
                    "page_char_start": 0,
                    "page_char_end": len(right),
                },
            ],
            is_cross_page=True,
        )
        with self._engine([paragraph]) as engine:
            item = engine._format_result(
                engine.by_id["PDF-CROSS-JOINER"],
                "exact",
                1.0,
                len(left),
                len(left) + 1,
            )

        self.assertEqual(item["matched_text"], "\n")
        self.assertEqual(item["match_quote"], "\n")
        self.assertEqual(item["page_match_spans"], [])
        self.assertFalse(item["precise_highlight_available"])
        with self._engine([paragraph]) as engine:
            empty_item = engine._format_result(
                engine.by_id["PDF-CROSS-JOINER"],
                "exact",
                1.0,
                len(left),
                len(left),
            )
        self.assertEqual(empty_item["match_quote"], "")

    def test_repeated_text_maps_the_occurrence_selected_by_search(self) -> None:
        text = "重复原句／间隔／重复原句"
        paragraph = self._paragraph(
            "PDF-REPEATED",
            text,
            [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(text),
                    "pdf_page_id": "pdf-test-PAGE-000003",
                    "page_char_start": 70,
                    "page_char_end": 70 + len(text),
                }
            ],
        )
        with self._engine([paragraph]) as engine:
            item = engine.search("重复原句", mode="exact", source_type="pdf")["results"][0]

        self.assertEqual(item["match_start"], 0)
        self.assertEqual(item["match_end"], len("重复原句"))
        self.assertEqual(item["page_match_spans"][0]["page_char_start"], 70)

    def test_emoji_offsets_are_unicode_codepoints_not_utf16_code_units(self) -> None:
        text = "甲😀乙结尾"
        paragraph = self._paragraph(
            "PDF-EMOJI",
            text,
            [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(text),
                    "pdf_page_id": "pdf-test-PAGE-000004",
                    "page_char_start": 10,
                    "page_char_end": 10 + len(text),
                }
            ],
        )
        with self._engine([paragraph]) as engine:
            item = engine.search("😀乙", mode="exact", source_type="pdf")["results"][0]

        self.assertEqual((item["match_start"], item["match_end"]), (1, 3))
        self.assertEqual(item["matched_text"], "😀乙")
        self.assertEqual(item["match_offset_unit"], "unicode_codepoint")
        self.assertEqual(
            item["page_match_spans"][0],
            {
                "pdf_page_id": "pdf-test-PAGE-000004",
                "page_char_start": 11,
                "page_char_end": 13,
                "match_quote": "😀乙",
            },
        )

    def test_match_quote_is_capped_at_fifty_unicode_codepoints(self) -> None:
        query = "😀" * 55
        text = f"前{query}后"
        paragraph = self._paragraph(
            "PDF-LONG-QUOTE",
            text,
            [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(text),
                    "pdf_page_id": "pdf-test-PAGE-000005",
                    "page_char_start": 0,
                    "page_char_end": len(text),
                }
            ],
        )
        with self._engine([paragraph]) as engine:
            item = engine.search(query, mode="exact", source_type="pdf")["results"][0]

        self.assertEqual(item["match_quote"], "😀" * 50)
        self.assertEqual(len(item["match_quote"]), 50)

    def test_legacy_pdf_without_spans_degrades_per_paragraph(self) -> None:
        paragraph = self._paragraph("PDF-LEGACY", "旧索引仍然可以检索")
        with self._engine([paragraph]) as engine:
            item = engine.search("仍然可以", mode="exact", source_type="pdf")["results"][0]

        self.assertEqual((item["match_start"], item["match_end"]), (3, 7))
        self.assertEqual(item["page_match_spans"], [])
        self.assertFalse(item["precise_highlight_available"])

    def test_spread_hits_resolve_left_right_and_cross_gutter_pages(self) -> None:
        text = "左页包含定位目标\n右页同样包含定位目标"
        right_start = text.index("右页")
        page = {
            "pdf_page_id": "pdf-test-PAGE-000013",
            "source_file_id": "pdf-test",
            "pdf_page_index": 13,
            "text_raw": text,
            "citation_page": "28",
            "citation_page_start": "28",
            "citation_page_end": "29",
            "layout_mode": "spread",
            "reading_direction": "ltr",
            "gutter_x": 0.5,
            "page_width": 1000,
            "page_height": 800,
            "blocks": [
                {
                    "text": text[:right_start].rstrip("\n"),
                    "page_char_start": 0,
                    "page_char_end": right_start - 1,
                    "bbox_normalized": [0.08, 0.08, 0.46, 0.88],
                },
                {
                    "text": text[right_start:],
                    "page_char_start": right_start,
                    "page_char_end": len(text),
                    "bbox_normalized": [0.54, 0.08, 0.94, 0.88],
                },
            ],
        }
        paragraph = self._paragraph(
            "PDF-SPREAD",
            text,
            [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(text),
                    "pdf_page_id": page["pdf_page_id"],
                    "pdf_page_index": 13,
                    "page_char_start": 0,
                    "page_char_end": len(text),
                }
            ],
            pdf_page_start_index=13,
        )
        paragraph.update(
            {
                "page_display": "引用页码：28-29",
                "page_source_type": "manual_segment",
                "page_mapping_method": "manual_segment",
                "citation_page_start": "28",
                "citation_page_end": "29",
                "printed_page_start": "28",
                "printed_page_end": "29",
                "layout_mode": "spread",
                "reading_direction": "ltr",
                "gutter_x": 0.5,
            }
        )

        for factory in (self._engine, self._sqlite_engine):
            with self.subTest(backend=factory.__name__), factory([paragraph], [page]) as engine:
                left = engine.search("左页包含", mode="exact", source_type="pdf")["results"][0]
                right = engine.search("右页同样", mode="exact", source_type="pdf")["results"][0]
                both = engine.search("目标\n右页", mode="exact", source_type="pdf")["results"][0]

            self.assertEqual(left["page"], "引用页码：28")
            self.assertEqual(left["citation_page_start"], "28")
            self.assertEqual(left["citation_page_end"], "28")
            self.assertEqual(left["logical_page_side"], "left")
            self.assertEqual(left["spread_hit_precision"], "exact_region")
            self.assertEqual(left["page_match_spans"][0]["logical_page_side"], "left")
            self.assertEqual(right["page"], "引用页码：29")
            self.assertEqual(right["logical_page_side"], "right")
            self.assertEqual(both["page"], "引用页码：28–29")
            self.assertEqual(both["logical_page_side"], "both")
            self.assertIn("第28—29页", both["citation_formats"]["chinese"])

        rtl_page = dict(page)
        rtl_page["reading_direction"] = "rtl"
        rtl_paragraph = dict(paragraph)
        rtl_paragraph["reading_direction"] = "rtl"
        with self._engine([rtl_paragraph], [rtl_page]) as engine:
            left = engine.search("左页包含", mode="exact", source_type="pdf")["results"][0]
            right = engine.search("右页同样", mode="exact", source_type="pdf")["results"][0]
        self.assertEqual(left["citation_page_start"], "29")
        self.assertEqual(right["citation_page_start"], "28")

    def test_spread_hit_without_aligned_blocks_falls_back_to_full_range(self) -> None:
        text = "双开页坐标缺失时保留可靠范围"
        page = {
            "pdf_page_id": "pdf-test-PAGE-000020",
            "source_file_id": "pdf-test",
            "pdf_page_index": 20,
            "text_raw": text,
            "citation_page_start": "42",
            "citation_page_end": "43",
            "layout_mode": "spread",
            "blocks": [{"text": text, "bbox_normalized": [0.08, 0.1, 0.46, 0.9]}],
        }
        paragraph = self._paragraph(
            "PDF-SPREAD-FALLBACK",
            text,
            [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(text),
                    "pdf_page_id": page["pdf_page_id"],
                    "pdf_page_index": 20,
                    "page_char_start": 0,
                    "page_char_end": len(text),
                }
            ],
            pdf_page_start_index=20,
        )
        paragraph.update(
            {
                "page_source_type": "manual_segment",
                "citation_page_start": "42",
                "citation_page_end": "43",
                "layout_mode": "spread",
            }
        )

        with self._engine([paragraph], [page]) as engine:
            item = engine.search("坐标缺失", mode="exact", source_type="pdf")["results"][0]

        self.assertEqual(item["page"], "引用页码：42–43")
        self.assertIsNone(item["logical_page_side"])
        self.assertEqual(item["spread_hit_precision"], "range_fallback")

    def test_unknown_span_offset_unit_degrades_safely(self) -> None:
        text = "偏移单位不兼容"
        paragraph = self._paragraph(
            "PDF-FUTURE-OFFSET",
            text,
            [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(text),
                    "offset_unit": "utf16_code_unit",
                    "pdf_page_id": "pdf-test-PAGE-000006",
                    "page_char_start": 0,
                    "page_char_end": len(text),
                }
            ],
        )
        with self._engine([paragraph]) as engine:
            item = engine.search(
                "单位",
                mode="exact",
                source_type="pdf",
            )["results"][0]

        self.assertEqual(item["page_match_spans"], [])
        self.assertFalse(item["precise_highlight_available"])

    def test_word_paragraph_never_claims_precise_pdf_highlighting(self) -> None:
        text = "Word 普通段落"
        paragraph = self._paragraph(
            "WORD-PARAGRAPH",
            text,
            [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(text),
                    "pdf_page_id": "should-not-be-used",
                    "page_char_start": 0,
                    "page_char_end": len(text),
                }
            ],
            source_type="word",
        )
        with self._engine([paragraph]) as engine:
            item = engine.search("普通段落", mode="exact", source_type="word")["results"][0]

        self.assertEqual(item["page_match_spans"], [])
        self.assertFalse(item["precise_highlight_available"])

    def test_json_and_sqlite_backends_return_the_same_anchor_payload(self) -> None:
        text = "前文😀数据库一致性后文"
        paragraph = self._paragraph(
            "PDF-BACKEND-PARITY",
            text,
            [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(text),
                    "pdf_page_id": "pdf-test-PAGE-000008",
                    "page_char_start": 25,
                    "page_char_end": 25 + len(text),
                    "page_text_hash": "same-page-hash",
                }
            ],
        )
        with self._engine([paragraph]) as json_engine:
            json_item = json_engine.search(
                "😀数据库",
                mode="exact",
                source_type="pdf",
            )["results"][0]
        with self._sqlite_engine([paragraph]) as sqlite_engine:
            sqlite_item = sqlite_engine.search(
                "😀数据库",
                mode="exact",
                source_type="pdf",
            )["results"][0]

        for key in (
            "match_start",
            "match_end",
            "match_offset_unit",
            "match_quote",
            "page_match_spans",
            "precise_highlight_available",
        ):
            self.assertEqual(sqlite_item[key], json_item[key], key)

    def test_context_uses_one_real_neighbor_per_side_on_both_backends(self) -> None:
        word_paragraphs = [
            self._paragraph(
                "WORD-0",
                "完整的第一段上文",
                source_type="word",
                paragraph_index=0,
            ),
            self._paragraph(
                "WORD-1",
                "第二段包含唯一检索目标",
                source_type="word",
                paragraph_index=1,
            ),
            self._paragraph(
                "WORD-2",
                "完整的第三段下文",
                source_type="word",
                paragraph_index=2,
            ),
            self._paragraph(
                "WORD-3",
                "不应返回的第四段",
                source_type="word",
                paragraph_index=3,
            ),
        ]
        # Context is document adjacency, not search eligibility.  Headings and
        # other non-searchable paragraphs must behave the same in both stores.
        word_paragraphs[0]["eligible_for_search"] = False

        for factory in (self._engine, self._sqlite_engine):
            with self.subTest(backend=factory.__name__), factory(word_paragraphs) as engine:
                item = engine.search(
                    "唯一检索目标", mode="exact", source_type="word"
                )["results"][0]

            self.assertEqual(
                item["context_before"],
                [{"paragraph_id": "WORD-0", "text": "完整的第一段上文"}],
            )
            self.assertEqual(
                item["context_after"],
                [{"paragraph_id": "WORD-2", "text": "完整的第三段下文"}],
            )

    def test_pdf_context_skips_cross_windows_and_selected_page_range(self) -> None:
        previous_text = "前一真实页完整文本" + "甲" * 80
        following_text = "后一真实页完整文本" + "乙" * 80
        paragraphs = [
            self._paragraph(
                "PDF-PAGE-0",
                previous_text,
                paragraph_index=0,
                pdf_page_start_index=0,
            ),
            # A legacy helper can be identifiable only by its CROSS id.  It
            # must not outrank the real page even if its stored range is bad.
            self._paragraph(
                "PDF-CROSS-LEGACY-0",
                "重复的旧跨页窗口",
                paragraph_index=1,
                pdf_page_start_index=0,
            ),
            self._paragraph(
                "PDF-PAGE-1",
                "命中范围左页",
                paragraph_index=2,
                pdf_page_start_index=1,
            ),
            self._paragraph(
                "PDF-CROSS-1-2",
                "跨页唯一检索目标",
                is_cross_page=True,
                paragraph_index=3,
                pdf_page_start_index=1,
                pdf_page_end_index=2,
            ),
            self._paragraph(
                "PDF-PAGE-2",
                "命中范围右页",
                paragraph_index=4,
                pdf_page_start_index=2,
            ),
            self._paragraph(
                "PDF-CROSS-2-3",
                "重复的后方跨页窗口",
                is_cross_page=True,
                paragraph_index=5,
                pdf_page_start_index=2,
                pdf_page_end_index=3,
            ),
            self._paragraph(
                "PDF-PAGE-3",
                following_text,
                paragraph_index=6,
                pdf_page_start_index=3,
            ),
        ]
        paragraphs[0]["eligible_for_search"] = False
        paragraphs[-1]["eligible_for_search"] = False

        for factory in (self._engine, self._sqlite_engine):
            with self.subTest(backend=factory.__name__), factory(paragraphs) as engine:
                cross_item = engine.search(
                    "跨页唯一检索目标", mode="exact", source_type="pdf"
                )["results"][0]
                page_item = engine.search(
                    "命中范围左页", mode="exact", source_type="pdf"
                )["results"][0]

            self.assertTrue(cross_item["is_cross_page"])
            self.assertEqual(
                cross_item["context_before"],
                [{"paragraph_id": "PDF-PAGE-0", "text": previous_text}],
            )
            self.assertEqual(
                cross_item["context_after"],
                [{"paragraph_id": "PDF-PAGE-3", "text": following_text}],
            )
            self.assertEqual(
                page_item["context_before"],
                [{"paragraph_id": "PDF-PAGE-0", "text": previous_text}],
            )
            self.assertEqual(
                page_item["context_after"],
                [{"paragraph_id": "PDF-PAGE-2", "text": "命中范围右页"}],
            )

    def test_pdf_context_is_empty_outside_first_and_last_real_pages(self) -> None:
        paragraphs = [
            self._paragraph(
                "PDF-FIRST",
                "第一页首尾边界检索词",
                paragraph_index=0,
                pdf_page_start_index=0,
            ),
            self._paragraph(
                "PDF-CROSS-0-1",
                "首尾之间的跨页辅助窗口",
                is_cross_page=True,
                paragraph_index=1,
                pdf_page_start_index=0,
                pdf_page_end_index=1,
            ),
            self._paragraph(
                "PDF-LAST",
                "最后一页首尾边界检索词",
                paragraph_index=2,
                pdf_page_start_index=1,
            ),
        ]

        for factory in (self._engine, self._sqlite_engine):
            with self.subTest(backend=factory.__name__), factory(paragraphs) as engine:
                results = engine.search(
                    "首尾边界检索词", mode="exact", source_type="pdf"
                )["results"]

            by_id = {item["paragraph_id"]: item for item in results}
            self.assertEqual(by_id["PDF-FIRST"]["context_before"], [])
            self.assertEqual(by_id["PDF-FIRST"]["context_after"], [
                {"paragraph_id": "PDF-LAST", "text": "最后一页首尾边界检索词"}
            ])
            self.assertEqual(by_id["PDF-LAST"]["context_before"], [
                {"paragraph_id": "PDF-FIRST", "text": "第一页首尾边界检索词"}
            ])
            self.assertEqual(by_id["PDF-LAST"]["context_after"], [])

    def test_pdf_context_skips_empty_real_page_records_on_both_backends(self) -> None:
        paragraphs = [
            self._paragraph(
                "PDF-NONEMPTY-BEFORE",
                "空页之前的真实全文",
                paragraph_index=0,
                pdf_page_start_index=0,
            ),
            self._paragraph(
                "PDF-EMPTY-BEFORE",
                "",
                paragraph_index=1,
                pdf_page_start_index=1,
            ),
            self._paragraph(
                "PDF-EMPTY-CONTEXT-TARGET",
                "空页上下文唯一检索目标",
                paragraph_index=2,
                pdf_page_start_index=2,
            ),
            self._paragraph(
                "PDF-WHITESPACE-AFTER",
                " \n\t ",
                paragraph_index=3,
                pdf_page_start_index=3,
            ),
            self._paragraph(
                "PDF-NONEMPTY-AFTER",
                "空页之后的真实全文",
                paragraph_index=4,
                pdf_page_start_index=4,
            ),
        ]

        for factory in (self._engine, self._sqlite_engine):
            with self.subTest(backend=factory.__name__), factory(paragraphs) as engine:
                item = engine.search(
                    "空页上下文唯一检索目标", mode="exact", source_type="pdf"
                )["results"][0]

            self.assertEqual(item["context_before"], [
                {"paragraph_id": "PDF-NONEMPTY-BEFORE", "text": "空页之前的真实全文"}
            ])
            self.assertEqual(item["context_after"], [
                {"paragraph_id": "PDF-NONEMPTY-AFTER", "text": "空页之后的真实全文"}
            ])

    def test_sqlite_pdf_context_query_uses_position_index_without_temp_sort(self) -> None:
        paragraphs = [
            self._paragraph(
                "PDF-PLAN-0",
                "查询计划上文",
                paragraph_index=0,
                pdf_page_start_index=0,
            ),
            self._paragraph(
                "PDF-PLAN-CROSS-0-1",
                "查询计划跨页辅助记录",
                is_cross_page=True,
                paragraph_index=1,
                pdf_page_start_index=0,
                pdf_page_end_index=1,
            ),
            self._paragraph(
                "PDF-PLAN-1",
                "查询计划唯一检索目标",
                paragraph_index=2,
                pdf_page_start_index=1,
            ),
            self._paragraph(
                "PDF-PLAN-CROSS-1-2",
                "查询计划后方跨页辅助记录",
                is_cross_page=True,
                paragraph_index=3,
                pdf_page_start_index=1,
                pdf_page_end_index=2,
            ),
            self._paragraph(
                "PDF-PLAN-2",
                "查询计划下文",
                paragraph_index=4,
                pdf_page_start_index=2,
            ),
        ]

        with self._sqlite_engine(paragraphs) as engine:
            statements: List[str] = []
            self.assertIsNotNone(engine.db)
            engine.db.set_trace_callback(statements.append)
            try:
                engine.search(
                    "查询计划唯一检索目标", mode="exact", source_type="pdf"
                )
            finally:
                engine.db.set_trace_callback(None)

            context_queries = [
                statement for statement in statements if "/* pdf_context */" in statement
            ]
            self.assertEqual(len(context_queries), 2)
            for query in context_queries:
                self.assertNotIn("ORDER BY p.pdf_page_", query)
                plan = engine.db.execute("EXPLAIN QUERY PLAN " + query).fetchall()
                detail = " | ".join(str(row["detail"]) for row in plan)
                self.assertIn("idx_paragraphs_source_position", detail)
                self.assertNotIn("USE TEMP B-TREE", detail.upper())

    def test_search_result_uses_source_driven_page_display(self) -> None:
        pdf = self._paragraph("PDF-PAGE-DISPLAY", "PDF 页码展示")
        word = self._paragraph(
            "WORD-PAGE-DISPLAY",
            "旧 DOC 页码展示",
            source_type="word",
        )
        word.update(
            {
                "page_source_type": "toc_range_bound",
                "page_display": "38-45",
                "original_page_start": "38",
                "original_page_end": "45",
            }
        )

        with self._engine([pdf, word]) as engine:
            pdf_item = engine.search(
                "PDF 页码",
                mode="exact",
                source_type="pdf",
            )["results"][0]
            word_item = engine.search(
                "旧 DOC",
                mode="exact",
                source_type="word",
            )["results"][0]

        self.assertEqual(
            pdf_item["page"],
            "PDF 第 1 页，引用页码尚未校准",
        )
        self.assertEqual(
            word_item["page"],
            "目录范围 38–45（非段落精确页码）",
        )
        self.assertEqual(
            word_item["page_note"],
            "目录页码范围，非段落级精确页码",
        )


if __name__ == "__main__":
    unittest.main()

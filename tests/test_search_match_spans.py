from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List

from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.search import SearchEngine


class SearchMatchSpanTests(unittest.TestCase):
    @contextmanager
    def _engine(self, paragraphs: List[Dict[str, object]]) -> Iterator[SearchEngine]:
        index = {
            "metadata": {"anchor_spec_version": 1},
            "source_files": [],
            "volumes": [],
            "works": [],
            "paragraphs": paragraphs,
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
    def _sqlite_engine(self, paragraphs: List[Dict[str, object]]) -> Iterator[SearchEngine]:
        index = {
            "metadata": {"anchor_spec_version": 1},
            "source_files": [],
            "volumes": [],
            "works": [],
            "paragraphs": paragraphs,
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
    ) -> Dict[str, object]:
        paragraph: Dict[str, object] = {
            "paragraph_id": paragraph_id,
            "volume_id": "TEST-VOLUME",
            "volume_number": None,
            "work_id": "TEST-WORK",
            "source_file_id": "pdf-test",
            "source_type": source_type,
            "paragraph_index": 0,
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
            "pdf_page_start_index": 0 if source_type == "pdf" else None,
            "pdf_page_end_index": 1 if is_cross_page else (0 if source_type == "pdf" else None),
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
                    "page_text_hash": "hash-page-zero",
                }
            ],
        )
        self.assertEqual(item["match_quote"], "目标句")
        self.assertTrue(item["precise_highlight_available"])

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
                },
                {
                    "pdf_page_id": "pdf-test-PAGE-000011",
                    "page_char_start": 20,
                    "page_char_end": 21,
                },
            ],
        )
        self.assertTrue(both_item["precise_highlight_available"])

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

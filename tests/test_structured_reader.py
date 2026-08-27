from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import build_database
from src.me_finder.pdf_extractors import pdf_page_text_hash
from src.me_finder.pdf_page_mapping import PageMapper
from src.me_finder.search import SearchEngine
from src.me_finder.structured_reader import (
    CitationPositionNotFound,
    InvalidCitationRange,
    InvalidPagination,
    InvalidSourceId,
    SourceNotFound,
    UnsupportedSourceType,
    get_document_citation,
    get_document_window,
)


def _paragraph(
    source_id: str,
    index: int,
    text: str,
    *,
    page_source_type: str,
    page_display: str | None = None,
    original_page_start: str | None = None,
    work_id: str | None = None,
) -> dict[str, object]:
    return {
        "paragraph_id": f"{source_id}-P{index:06d}",
        "source_file_id": source_id,
        "source_type": "word",
        "paragraph_index": index,
        "eligible_for_search": bool(text),
        "text_raw": text,
        "page_source_type": page_source_type,
        "page_display": page_display,
        "original_page_start": original_page_start,
        "work_id": work_id,
    }


class StructuredReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "index.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _build(self, index: dict[str, object]) -> None:
        build_database(index, self.database_path)

    def test_same_file_parsing_records_keep_their_own_text_and_parser_label(self) -> None:
        sources = [
            {
                "source_file_id": "pdf-native",
                "source_type": "pdf",
                "file_name": "same-book.pdf",
                "sha256": "a" * 64,
                "pdf_profile": {"detected_pdf_type": "native_text", "parser": "pymupdf"},
            },
            {
                "source_file_id": "pdf-mineru",
                "source_type": "pdf",
                "file_name": "same-book.pdf",
                "sha256": "a" * 64,
                "pdf_profile": {"detected_pdf_type": "mineru_structured", "parser_label": "MinerU"},
            },
        ]
        self._build({
            "metadata": {}, "source_files": sources, "volumes": [],
            "works": [], "paragraphs": [],
            "pdf_pages": [
                {"source_file_id": source["source_file_id"], "pdf_page_index": 0,
                 "text_raw": text}
                for source, text in zip(sources, ("Native text.", "MinerU text with different offsets."))
            ],
        })
        for source_id, label, text in (
            ("pdf-native", "原生文本", "Native text."),
            ("pdf-mineru", "MinerU", "MinerU text with different offsets."),
        ):
            with self.subTest(source_id=source_id):
                result = get_document_window(self.database_path, source_id, start=0, count=1)
                self.assertEqual(result["source"]["source_file_id"], source_id)
                self.assertEqual(result["source"]["parser_label"], label)
                self.assertEqual(result["items"][0]["text_raw"], text)

    def test_pdf_window_has_stable_anchors_page_states_and_empty_page(self) -> None:
        source_id = "pdf-reader"
        pages = [
            {
                "pdf_page_id": f"{source_id}-PAGE-000000",
                "source_file_id": source_id,
                "pdf_page_index": 0,
                "pdf_page_number_1based": 1,
                "text_raw": "已校准正文",
                "page_text_hash": pdf_page_text_hash("已校准正文"),
                "citation_page": "38",
                "page_mapping_method": "manual_segment",
                "blocks": [
                    {
                        "text": "不得整包返回",
                        "result_dir": "/Users/example/private/checkpoint",
                    }
                ],
            },
            {
                "pdf_page_id": f"{source_id}-PAGE-000001",
                "source_file_id": source_id,
                "pdf_page_index": 1,
                "pdf_page_number_1based": 2,
                "pdf_page_label": "vii",
                "text_raw": "标签页正文",
                "page_mapping_method": "uncalibrated",
            },
            {
                "pdf_page_id": f"{source_id}-PAGE-000002",
                "source_file_id": source_id,
                "pdf_page_index": 2,
                "pdf_page_number_1based": 3,
                "text_raw": " \n",
                "page_mapping_method": "uncalibrated",
            },
        ]
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "pdf",
                        "file_name": "reader.pdf",
                        "display_title": "阅读器样例",
                        "file_format": "pdf",
                        "bibliographic_metadata": {
                            "document_type": "book",
                            "title": "阅读器样例",
                            "author": "测试作者",
                            "publish_place": "北京",
                            "publisher": "测试出版社",
                            "publish_year": "2026",
                        },
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": [],
                "pdf_pages": pages,
            }
        )

        result = get_document_window(
            self.database_path, source_id, start="0", count="3"
        )

        self.assertEqual(result["source"]["display_title"], "阅读器样例")
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["last_position"], 2)
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["previous_start"])
        self.assertEqual(result["next_start"], 3)
        calibrated, labelled, empty = result["items"]
        self.assertEqual(
            calibrated["anchor_id"], f"{source_id}-PAGE-000000"
        )
        self.assertEqual(calibrated["page_display"], "引用页码：38")
        self.assertTrue(calibrated["page_verified"])
        self.assertIn("第38页", calibrated["citation_formats"]["chinese"])
        self.assertIn("38", calibrated["citation_formats"]["gb"])
        self.assertTrue(calibrated["citation_formats"]["page_verified"])
        self.assertTrue(calibrated["citation_formats"]["can_copy"])
        self.assertNotIn("blocks", calibrated)
        self.assertNotIn("result_dir", calibrated)
        self.assertEqual(
            labelled["page_display"],
            "PDF 标签页：vii，引用页码尚未校准",
        )
        self.assertFalse(labelled["page_verified"])
        self.assertEqual(
            labelled["citation_formats"]["chinese"],
            "该文献页码尚未校准，不能生成可靠脚注。",
        )
        self.assertEqual(
            labelled["citation_formats"]["gb"],
            "该文献页码尚未校准，不能生成 GB/T 引文。",
        )
        self.assertFalse(labelled["citation_formats"]["page_verified"])
        self.assertFalse(labelled["citation_formats"]["can_copy"])
        self.assertEqual(
            labelled["page_text_hash"],
            pdf_page_text_hash("标签页正文"),
        )
        self.assertTrue(empty["is_empty"])
        self.assertEqual(empty["pdf_page_index"], 2)
        self.assertEqual(
            empty["page_display"], "PDF 第 3 页，引用页码尚未校准"
        )

    def test_pdf_pagination_boundaries_and_unknown_page_source_are_safe(self) -> None:
        source_id = "pdf-boundary"
        page_indexes = (0, 2, 5, 9)
        pages = [
            {
                "pdf_page_id": f"{source_id}-PAGE-{index:06d}",
                "source_file_id": source_id,
                "pdf_page_index": index,
                "text_raw": f"page {index}",
                "page_mapping_method": "future_mapping"
                if index == 5
                else "uncalibrated",
            }
            for index in page_indexes
        ]
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "pdf",
                        "file_name": "boundary.pdf",
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": [],
                "pdf_pages": pages,
            }
        )

        middle = get_document_window(
            self.database_path, source_id, start=3, count=1
        )
        self.assertEqual(middle["start"], 3)
        self.assertEqual(middle["count"], 1)
        self.assertEqual(middle["total"], 4)
        self.assertEqual(middle["last_position"], 9)
        self.assertTrue(middle["has_more"])
        self.assertEqual(middle["previous_start"], 2)
        self.assertEqual(middle["next_start"], 6)
        self.assertEqual(middle["items"][0]["pdf_page_index"], 5)
        self.assertEqual(middle["items"][0]["page_source_type"], "future_mapping")
        self.assertFalse(middle["items"][0]["page_verified"])
        self.assertIn("尚未验证", middle["items"][0]["page_note"])

        beyond = get_document_window(
            self.database_path, source_id, start=99, count=10
        )
        self.assertEqual(beyond["items"], [])
        self.assertEqual(beyond["last_position"], 9)
        self.assertFalse(beyond["has_more"])
        self.assertEqual(beyond["previous_start"], 0)
        self.assertIsNone(beyond["next_start"])

    def test_docx_anchors_only_the_first_paragraph_of_each_inferred_page(self) -> None:
        source_id = "source-01"
        paragraphs = [
            _paragraph(
                source_id,
                0,
                "第一页首段",
                page_source_type="section_break_inferred",
                page_display="38",
                original_page_start="38",
            ),
            _paragraph(
                source_id,
                1,
                "第一页次段",
                page_source_type="section_break_inferred",
                page_display="38",
                original_page_start="38",
            ),
            _paragraph(
                source_id,
                2,
                "第二页首段",
                page_source_type="section_break_inferred",
                page_display="39",
                original_page_start="39",
            ),
        ]
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "word",
                        "file_name": "第1卷.docx",
                        "file_format": "docx",
                    }
                ],
                "volumes": [
                    {
                        "volume_id": "MEWJ-01",
                        "source_file_id": source_id,
                        "source_type": "word",
                        "display_title": "《马克思恩格斯文集》第1卷",
                    }
                ],
                "works": [],
                "paragraphs": paragraphs,
            }
        )

        first = get_document_window(
            self.database_path, source_id, start=0, count=2
        )
        self.assertEqual(
            first["items"][0]["anchor_id"], f"{source_id}-P000000"
        )
        self.assertIsNone(first["previous_start"])
        self.assertIsNone(first["items"][1]["anchor_id"])
        self.assertFalse(first["items"][0]["page_verified"])
        self.assertIn(
            "不能生成可靠脚注",
            first["items"][0]["citation_formats"]["chinese"],
        )
        self.assertEqual(
            first["items"][0]["page_display"],
            "第 38 页（分节推断，未验证）",
        )
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_start"], 2)

        second = get_document_window(
            self.database_path, source_id, start=1, count=2
        )
        self.assertIsNone(second["items"][0]["anchor_id"])
        self.assertEqual(
            second["items"][1]["anchor_id"], f"{source_id}-P000002"
        )
        self.assertEqual(second["previous_start"], 0)

    def test_sparse_word_cursor_supports_previous_window_and_past_end(self) -> None:
        source_id = "docx-sparse"
        paragraphs = [
            _paragraph(
                source_id,
                index,
                f"段落 {index}",
                page_source_type="section_break_inferred",
                page_display=str(index + 1),
                original_page_start=str(index + 1),
            )
            for index in (0, 4, 10, 20)
        ]
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "word",
                        "file_name": "sparse.docx",
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": paragraphs,
            }
        )

        middle = get_document_window(
            self.database_path, source_id, start=5, count=1
        )
        self.assertEqual(middle["items"][0]["paragraph_index"], 10)
        self.assertEqual(middle["last_position"], 20)
        self.assertEqual(middle["previous_start"], 4)
        self.assertEqual(middle["next_start"], 11)
        self.assertTrue(middle["has_more"])

        beyond = get_document_window(
            self.database_path, source_id, start=999, count=2
        )
        self.assertEqual(beyond["items"], [])
        self.assertEqual(beyond["last_position"], 20)
        self.assertEqual(beyond["previous_start"], 10)
        self.assertIsNone(beyond["next_start"])
        self.assertFalse(beyond["has_more"])

    def test_legacy_doc_ranges_and_unknown_pages_never_create_anchors(self) -> None:
        source_id = "source-02"
        paragraphs = [
            _paragraph(
                source_id,
                0,
                "目录约束正文",
                page_source_type="toc_range_bound",
                page_display="38-45",
            ),
            _paragraph(
                source_id,
                1,
                "无页码正文",
                page_source_type="unknown",
                page_display="999",
            ),
        ]
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "word",
                        "file_name": "第2卷.doc",
                        "file_format": "doc",
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": paragraphs,
            }
        )

        result = get_document_window(
            self.database_path, source_id, start=0, count=10
        )
        ranged, unknown = result["items"]
        self.assertIsNone(ranged["anchor_id"])
        self.assertEqual(
            ranged["document_page_range"],
            "目录范围 38–45（非段落精确页码）",
        )
        self.assertFalse(ranged["page_verified"])
        self.assertIn(
            "不能生成可靠脚注",
            ranged["citation_formats"]["chinese"],
        )
        self.assertIsNone(unknown["anchor_id"])
        self.assertIsNone(unknown["document_page_range"])
        self.assertEqual(unknown["page_display"], "页码尚未解析")
        self.assertFalse(unknown["page_verified"])
        self.assertIn(
            "不能生成 GB/T 引文",
            unknown["citation_formats"]["gb"],
        )

    def test_verified_word_page_can_build_a_citation_but_three_unverified_states_cannot(
        self,
    ) -> None:
        source_id = "word-citation-states"
        paragraphs = [
            _paragraph(
                source_id,
                0,
                "已验证正文",
                page_source_type="section_break_verified",
                page_display="38",
                original_page_start="38",
            ),
            _paragraph(
                source_id,
                1,
                "分节推断正文",
                page_source_type="section_break_inferred",
                page_display="39",
                original_page_start="39",
            ),
            _paragraph(
                source_id,
                2,
                "目录范围正文",
                page_source_type="toc_range_bound",
                page_display="40-45",
            ),
            _paragraph(
                source_id,
                3,
                "未知正文",
                page_source_type="unknown",
            ),
        ]
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "word",
                        "file_name": "states.docx",
                        "bibliographic_metadata": {
                            "document_type": "book",
                            "title": "Word 引文状态",
                            "author": "测试作者",
                            "publish_place": "北京",
                            "publisher": "测试出版社",
                            "publish_year": "2026",
                        },
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": paragraphs,
            }
        )

        result = get_document_window(
            self.database_path, source_id, start=0, count=10
        )
        verified, inferred, toc_range, unknown = result["items"]
        self.assertTrue(verified["page_verified"])
        self.assertIn("第38页", verified["citation_formats"]["chinese"])
        for item in result["items"]:
            with self.subTest(hash_paragraph=item["paragraph_id"]):
                self.assertEqual(
                    item["page_text_hash"],
                    pdf_page_text_hash(item["text_raw"]),
                )
        for item in (inferred, toc_range, unknown):
            with self.subTest(page_source_type=item["page_source_type"]):
                self.assertFalse(item["page_verified"])
                self.assertIn(
                    "不能生成可靠脚注",
                    item["citation_formats"]["chinese"],
                )
                self.assertIn(
                    "不能生成 GB/T 引文",
                    item["citation_formats"]["gb"],
                )

    def test_search_and_reader_share_the_same_word_page_verification_rule(
        self,
    ) -> None:
        for page_source_type in (
            "section_break_inferred",
            "toc_range_bound",
            "unknown",
        ):
            with self.subTest(page_source_type=page_source_type):
                hit_page = SearchEngine._hit_page(
                    {
                        "source_type": "word",
                        "page_source_type": page_source_type,
                        "original_page_start": "38",
                        "page_display": "38",
                    },
                    "word",
                    "页码未验证",
                )
                self.assertTrue(hit_page["uncalibrated"])
                self.assertNotIn("start", hit_page)

    def test_pdf_cross_page_citation_uses_verified_original_page_range(self) -> None:
        source_id = "pdf-citation-range"
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "pdf",
                        "file_name": "citation.pdf",
                        "bibliographic_metadata": {
                            "document_type": "book",
                            "title": "跨页引文",
                            "author": "测试作者",
                            "publish_place": "北京",
                            "publisher": "测试出版社",
                            "publish_year": "2026",
                        },
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": [],
                "pdf_pages": [
                    {
                        "pdf_page_id": f"{source_id}-PAGE-{index:06d}",
                        "source_file_id": source_id,
                        "pdf_page_index": index,
                        "text_raw": f"第 {index} 页",
                        "citation_page": str(38 + index),
                        "page_mapping_method": "manual_segment",
                        "segment_id": "MAPSEG-000000-000002",
                    }
                    for index in range(3)
                ],
            }
        )

        citation = get_document_citation(
            self.database_path,
            source_id,
            start_anchor_id=f"{source_id}-PAGE-000000",
            end_anchor_id=f"{source_id}-PAGE-000002",
        )
        self.assertTrue(citation["page_range"]["verified"])
        self.assertEqual(citation["page_range"]["citation_page_start"], "38")
        self.assertEqual(citation["page_range"]["citation_page_end"], "40")
        self.assertIn("第38—40页", citation["citation_formats"]["chinese"])
        self.assertIn("38-40", citation["citation_formats"]["gb"])
        self.assertTrue(citation["citation_formats"]["page_verified"])
        self.assertTrue(citation["citation_formats"]["can_copy"])
        self.assertNotIn("text_raw", citation)
        self.assertNotIn("relative_path", citation["source"])

    def test_verified_pdf_cross_page_citation_requires_one_persisted_segment(
        self,
    ) -> None:
        source_id = "pdf-segment-boundary"
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "pdf",
                        "file_name": "segments.pdf",
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": [],
                "pdf_pages": [
                    {
                        "pdf_page_id": f"{source_id}-PAGE-{index:06d}",
                        "source_file_id": source_id,
                        "pdf_page_index": index,
                        "text_raw": f"第 {index} 页",
                        "citation_page": str(10 + index),
                        "page_mapping_method": "manual_segment",
                    }
                    for index in range(2)
                ],
            }
        )

        with self.assertRaisesRegex(InvalidCitationRange, "重新导入"):
            get_document_citation(
                self.database_path,
                source_id,
                start_anchor_id=f"{source_id}-PAGE-000000",
                end_anchor_id=f"{source_id}-PAGE-000001",
            )

        connection = sqlite3.connect(str(self.database_path))
        try:
            rows = connection.execute(
                """
                SELECT row_id, payload_json
                FROM pdf_pages
                ORDER BY pdf_page_index
                """
            ).fetchall()
            for offset, (row_id, payload_json) in enumerate(rows):
                payload = json.loads(payload_json)
                payload["segment_id"] = f"segment-{offset}"
                connection.execute(
                    "UPDATE pdf_pages SET payload_json = ? WHERE row_id = ?",
                    (json.dumps(payload, ensure_ascii=False), row_id),
                )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(InvalidCitationRange, "跨越不同页码映射分段"):
            get_document_citation(
                self.database_path,
                source_id,
                start_anchor_id=f"{source_id}-PAGE-000000",
                end_anchor_id=f"{source_id}-PAGE-000001",
            )

    def test_page_mapper_generates_stable_distinct_segment_ids(self) -> None:
        mapper = PageMapper.from_config(
            {
                "page_mapping": {
                    "segments": [
                        {
                            "pdf_page_start": 0,
                            "pdf_page_end": 9,
                            "citation_page_start": "1",
                        },
                        {
                            "pdf_page_start": 10,
                            "pdf_page_end": 19,
                            "citation_page_start": "1",
                        },
                    ]
                }
            }
        )

        first = mapper.map_page(3)
        first_again = mapper.map_page(4)
        second = mapper.map_page(10)
        self.assertEqual(first.segment_id, "MAPSEG-000000-000009")
        self.assertEqual(first_again.segment_id, first.segment_id)
        self.assertEqual(second.segment_id, "MAPSEG-000010-000019")
        self.assertNotEqual(first.segment_id, second.segment_id)

    def test_cross_page_citation_rejects_any_unverified_page_without_using_physical_indexes(
        self,
    ) -> None:
        source_id = "pdf-citation-unverified"
        pages = [
            {
                "pdf_page_id": f"{source_id}-PAGE-{index:06d}",
                "source_file_id": source_id,
                "pdf_page_index": index,
                "text_raw": f"第 {index} 页",
                "citation_page": None,
                "page_mapping_method": "uncalibrated",
            }
            for index in range(3)
        ]
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "pdf",
                        "file_name": "unverified.pdf",
                        "bibliographic_metadata": {
                            "document_type": "book",
                            "title": "未校准测试",
                            "author": "测试作者",
                            "publish_place": "北京",
                            "publisher": "测试出版社",
                            "publish_year": "2026",
                        },
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": [],
                "pdf_pages": pages,
            }
        )

        citation = get_document_citation(
            self.database_path,
            source_id,
            start_anchor_id=f"{source_id}-PAGE-000000",
            end_anchor_id=f"{source_id}-PAGE-000002",
        )
        self.assertFalse(citation["page_range"]["verified"])
        self.assertIsNone(citation["page_range"]["citation_page_start"])
        self.assertIsNone(citation["page_range"]["citation_page_end"])
        self.assertEqual(
            citation["citation_formats"]["chinese"],
            "该文献页码尚未校准，不能生成可靠脚注。",
        )
        self.assertEqual(
            citation["citation_formats"]["gb"],
            "该文献页码尚未校准，不能生成 GB/T 引文。",
        )
        self.assertFalse(citation["citation_formats"]["page_verified"])
        self.assertFalse(citation["citation_formats"]["can_copy"])
        self.assertNotIn("PDF 第", citation["citation_formats"]["chinese"])

        reversed_citation = get_document_citation(
            self.database_path,
            source_id,
            start_anchor_id=f"{source_id}-PAGE-000002",
            end_anchor_id=f"{source_id}-PAGE-000000",
        )
        self.assertTrue(reversed_citation["selection_reversed"])
        self.assertEqual(reversed_citation["start_index"], 0)
        self.assertEqual(reversed_citation["end_index"], 2)
        with self.assertRaises(CitationPositionNotFound):
            get_document_citation(
                self.database_path,
                source_id,
                start_anchor_id=f"{source_id}-PAGE-000000",
                end_anchor_id=f"{source_id}-PAGE-000009",
            )

    def test_sparse_pdf_anchor_range_is_bounded_before_continuity_expansion(
        self,
    ) -> None:
        source_id = "pdf-sparse-range"
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "pdf",
                        "file_name": "sparse.pdf",
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": [],
                "pdf_pages": [
                    {
                        "pdf_page_id": f"{source_id}-PAGE-{index:06d}",
                        "source_file_id": source_id,
                        "pdf_page_index": index,
                        "text_raw": "稀疏页",
                        "citation_page": str(index + 1),
                        "page_mapping_method": "manual_segment",
                    }
                    for index in (0, 500_000)
                ],
            }
        )

        with self.assertRaises(InvalidCitationRange):
            get_document_citation(
                self.database_path,
                source_id,
                start_anchor_id=f"{source_id}-PAGE-000000",
                end_anchor_id=f"{source_id}-PAGE-500000",
            )

    def test_word_citation_range_cannot_cross_work_boundaries(self) -> None:
        source_id = "word-cross-work"
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "word",
                        "file_name": "cross-work.docx",
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": [
                    _paragraph(
                        source_id,
                        0,
                        "篇目一",
                        page_source_type="section_break_verified",
                        page_display="38",
                        original_page_start="38",
                        work_id="work-one",
                    ),
                    _paragraph(
                        source_id,
                        1,
                        "篇目二",
                        page_source_type="section_break_verified",
                        page_display="39",
                        original_page_start="39",
                        work_id="work-two",
                    ),
                ],
            }
        )

        with self.assertRaises(InvalidCitationRange):
            get_document_citation(
                self.database_path,
                source_id,
                start_anchor_id=f"{source_id}-P000000",
                end_anchor_id=f"{source_id}-P000001",
            )

    def test_word_citation_cannot_cross_known_and_unknown_work_boundaries(
        self,
    ) -> None:
        source_id = "word-missing-work"
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "word",
                        "file_name": "missing-work.docx",
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": [
                    _paragraph(
                        source_id,
                        0,
                        "未归属篇目",
                        page_source_type="section_break_verified",
                        page_display="38",
                        original_page_start="38",
                    ),
                    _paragraph(
                        source_id,
                        1,
                        "已归属篇目",
                        page_source_type="section_break_verified",
                        page_display="39",
                        original_page_start="39",
                        work_id="work-known",
                    ),
                ],
            }
        )

        with self.assertRaises(InvalidCitationRange):
            get_document_citation(
                self.database_path,
                source_id,
                start_anchor_id=f"{source_id}-P000000",
                end_anchor_id=f"{source_id}-P000001",
            )

    def test_word_citation_uses_authoritative_work_columns_not_stale_payloads(
        self,
    ) -> None:
        source_id = "word-authoritative-work"
        paragraphs = [
            _paragraph(
                source_id,
                index,
                f"篇目 {index}",
                page_source_type="section_break_verified",
                page_display=str(38 + index),
                original_page_start=str(38 + index),
                work_id=f"work-{index}",
            )
            for index in range(2)
        ]
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "word",
                        "file_name": "authoritative.docx",
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": paragraphs,
            }
        )
        connection = sqlite3.connect(str(self.database_path))
        try:
            for paragraph_id, payload_json in connection.execute(
                "SELECT paragraph_id, payload_json FROM paragraphs"
            ).fetchall():
                payload = json.loads(payload_json)
                payload["work_id"] = "stale-shared-work"
                connection.execute(
                    "UPDATE paragraphs SET payload_json = ? WHERE paragraph_id = ?",
                    (json.dumps(payload, ensure_ascii=False), paragraph_id),
                )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(InvalidCitationRange, "不同文献条目"):
            get_document_citation(
                self.database_path,
                source_id,
                start_anchor_id=f"{source_id}-P000000",
                end_anchor_id=f"{source_id}-P000001",
            )

    def test_multi_paragraph_word_citation_requires_known_work_membership(
        self,
    ) -> None:
        source_id = "word-all-unknown-work"
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "word",
                        "file_name": "unknown-work.docx",
                    }
                ],
                "volumes": [],
                "works": [],
                "paragraphs": [
                    _paragraph(
                        source_id,
                        index,
                        f"未归属段落 {index}",
                        page_source_type="section_break_verified",
                        page_display=str(38 + index),
                        original_page_start=str(38 + index),
                    )
                    for index in range(2)
                ],
            }
        )

        with self.assertRaisesRegex(InvalidCitationRange, "缺少可靠的文献条目归属"):
            get_document_citation(
                self.database_path,
                source_id,
                start_anchor_id=f"{source_id}-P000000",
                end_anchor_id=f"{source_id}-P000001",
            )

    def test_inputs_are_strict_and_window_size_is_bounded(self) -> None:
        self._build({"metadata": {}})
        invalid_source_ids = (
            "",
            " leading-space",
            "slash/id",
            "../escape",
            "中文-id",
            "a" * 129,
        )
        for source_id in invalid_source_ids:
            with self.subTest(source_id=source_id):
                with self.assertRaises(InvalidSourceId):
                    get_document_window(self.database_path, source_id)

        for start, count in (
            (-1, 1),
            (0, 0),
            (0, 101),
            (" 1", 1),
            ("9" * 5000, 1),
            (True, 1),
        ):
            with self.subTest(start=start, count=count):
                with self.assertRaises(InvalidPagination):
                    get_document_window(
                        self.database_path,
                        "source-01",
                        start=start,
                        count=count,
                    )

    def test_missing_and_unsupported_sources_have_distinct_errors(self) -> None:
        self._build(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": "html-reader",
                        "source_type": "html",
                        "file_name": "reader.html",
                    }
                ],
            }
        )
        with self.assertRaises(SourceNotFound):
            get_document_window(self.database_path, "missing-source")
        with self.assertRaises(UnsupportedSourceType):
            get_document_window(self.database_path, "html-reader")


if __name__ == "__main__":
    unittest.main()

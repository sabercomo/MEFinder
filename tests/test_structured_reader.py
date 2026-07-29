from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import build_database
from src.me_finder.pdf_extractors import pdf_page_text_hash
from src.me_finder.structured_reader import (
    InvalidPagination,
    InvalidSourceId,
    SourceNotFound,
    UnsupportedSourceType,
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
    }


class StructuredReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "index.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _build(self, index: dict[str, object]) -> None:
        build_database(index, self.database_path)

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
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["previous_start"])
        self.assertEqual(result["next_start"], 3)
        calibrated, labelled, empty = result["items"]
        self.assertEqual(
            calibrated["anchor_id"], f"{source_id}-PAGE-000000"
        )
        self.assertEqual(calibrated["page_display"], "引用页码：38")
        self.assertTrue(calibrated["page_verified"])
        self.assertNotIn("blocks", calibrated)
        self.assertNotIn("result_dir", calibrated)
        self.assertEqual(
            labelled["page_display"],
            "PDF 标签页：vii，引用页码尚未校准",
        )
        self.assertFalse(labelled["page_verified"])
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
        self.assertEqual(middle["previous_start"], 4)
        self.assertEqual(middle["next_start"], 11)
        self.assertTrue(middle["has_more"])

        beyond = get_document_window(
            self.database_path, source_id, start=999, count=2
        )
        self.assertEqual(beyond["items"], [])
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
        self.assertIsNone(unknown["anchor_id"])
        self.assertIsNone(unknown["document_page_range"])
        self.assertEqual(unknown["page_display"], "页码尚未解析")
        self.assertFalse(unknown["page_verified"])

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

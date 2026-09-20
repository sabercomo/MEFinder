"""正文范围检查与修正的后端读模型与提交链路。

钉住产品决定：两本书各自独立的连续范围、界面「含最后一段」与库内半开区间的换算、
优先显示适用的已保存人工范围、页码只来自既有锚点（无可信页码时不虚构）、
一次提交同时带两个范围且只作用于当前版本对。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from src.me_finder.alignment_body_range import (
    MAX_SEGMENT_WINDOW,
    read_body_range_segments,
    read_pair_body_ranges,
)
from src.me_finder.persistence.index_schema import SCHEMA
from src.me_finder.text_alignment import (
    InvalidAlignmentRequest,
    generate_alignment,
)


PDF_PAGES = (
    "目录\n第一章 商品\n第二章 货币\n",
    "序言\n本书由译者整理出版。",
    "第一章 商品\n商品首先是一个外界的对象。它的有用性使它成为使用价值。",
    "商品的价值量由劳动时间决定。这是政治经济学的起点。",
    "第二章 货币\n货币是商品交换发展的产物。它承担一般等价物的职能。",
    "参考文献\n马克思：资本论，人民出版社。",
)

EPUB_PARAGRAPHS = (
    "Contents",
    "Translator's Preface",
    "Chapter 1 The Commodity",
    "The commodity is an external object. Its usefulness makes it a use value.",
    "Chapter 2 Money",
    "Money is the product of exchange. It serves as universal equivalent.",
    "Bibliography",
)


def _fake_embedding_sequences(sequences, _cache_dir, **_kwargs):
    return [
        np.asarray([(float(len(text)), 1.0) for text in texts], dtype=np.float32)
        for texts in sequences
    ]


class BodyRangeReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "index.sqlite3"
        connection = sqlite3.connect(str(self.db))
        connection.executescript(SCHEMA)
        sources = (
            {
                "source_file_id": "pdf-zh",
                "source_type": "pdf",
                "file_name": "ziben.pdf",
                "title": "资本论",
                "language_code": "zh-Hans",
            },
            {
                "source_file_id": "epub-en",
                "source_type": "word",
                "file_name": "capital.epub",
                "title": "Capital",
                "language_code": "en",
                "file_format": "epub",
            },
        )
        connection.executemany(
            "INSERT INTO source_files(source_file_id, source_type, file_name, "
            "relative_path, volume_number, payload_json) VALUES (?, ?, ?, NULL, NULL, ?)",
            [
                (
                    source["source_file_id"],
                    source["source_type"],
                    source["file_name"],
                    json.dumps(source, ensure_ascii=False),
                )
                for source in sources
            ],
        )
        connection.execute(
            "INSERT INTO document_groups(document_group_id, title, "
            "base_source_file_id, created_at, updated_at) "
            "VALUES ('work', '资本论', 'pdf-zh', 't', 't')"
        )
        connection.executemany(
            "INSERT INTO document_group_members(document_group_id, source_file_id, "
            "version_label, member_order, added_at) VALUES ('work', ?, ?, ?, 't')",
            (("pdf-zh", "中文", 0), ("epub-en", "English", 1)),
        )
        connection.executemany(
            "INSERT INTO pdf_pages(source_file_id, pdf_page_index, payload_json) "
            "VALUES ('pdf-zh', ?, ?)",
            [
                (
                    index,
                    json.dumps(
                        {
                            "source_file_id": "pdf-zh",
                            "source_type": "pdf",
                            "pdf_page_id": f"pdf-zh-PAGE-{index:06d}",
                            "pdf_page_index": index,
                            "pdf_page_number_1based": index + 1,
                            "text_raw": text,
                            "blocks": [],
                        },
                        ensure_ascii=False,
                    ),
                )
                for index, text in enumerate(PDF_PAGES)
            ],
        )
        connection.executemany(
            "INSERT INTO paragraphs(paragraph_id, volume_id, work_id, source_file_id, "
            "source_type, paragraph_index, eligible_for_search, text_raw, "
            "normalized_text, compact_text, plain_text, payload_json) "
            "VALUES (?, NULL, NULL, 'epub-en', 'word', ?, 1, ?, ?, ?, ?, ?)",
            [
                (
                    f"epub-en-p{index}",
                    index,
                    text,
                    text.casefold(),
                    text.replace(" ", "").casefold(),
                    text,
                    json.dumps(
                        {
                            "paragraph_id": f"epub-en-p{index}",
                            "paragraph_index": index,
                            "text_raw": text,
                            "source_format": "epub",
                        }
                    ),
                )
                for index, text in enumerate(EPUB_PARAGRAPHS)
            ],
        )
        connection.commit()
        connection.close()

    def _overview(self):
        return read_pair_body_ranges(self.db, "work", "pdf-zh", "epub-en")

    @staticmethod
    def _side(overview, side):
        return next(item for item in overview["sides"] if item["side"] == side)

    def test_detected_range_is_reported_per_book_with_its_own_anchors(self) -> None:
        overview = self._overview()
        self.assertEqual(overview["range_source"], "detected")
        pivot = self._side(overview, "pivot")
        target = self._side(overview, "target")

        # 每本书一个连续范围，两边互不牵连；结尾是「最后一段」本身。
        self.assertEqual(
            (pivot["body_start_index"], pivot["body_end_index"]), (4, 11)
        )
        self.assertEqual(
            (target["body_start_index"], target["body_end_index"]), (2, 7)
        )
        self.assertLess(pivot["body_end_index"], pivot["segment_count"])
        self.assertEqual(pivot["start_segment"]["text"], "第一章 商品")
        self.assertEqual(target["start_segment"]["text"], "Chapter 1 The Commodity")

        # 页码只来自既有锚点：PDF 有物理页，EPUB 无出版方页码时如实说未解析。
        self.assertEqual(pivot["start_segment"]["physical_page_1based"], 3)
        self.assertEqual(pivot["locator_kind"], "pdf_page")
        self.assertIsNone(target["start_segment"]["physical_page_1based"])
        self.assertEqual(target["start_segment"]["page_display"], "页码尚未解析")
        self.assertEqual(target["locator_kind"], "segment")

    def test_outline_titles_come_from_the_indexed_text(self) -> None:
        overview = self._overview()
        self.assertEqual(
            [entry["title"] for entry in self._side(overview, "pivot")["outline"]],
            ["第一章 商品", "第二章 货币"],
        )
        # 目录页里的同名行不是章节位置，不得混进定位入口。
        self.assertEqual(
            [entry["segment_index"] for entry in self._side(overview, "pivot")["outline"]],
            [4, 9],
        )
        self.assertEqual(
            [entry["title"] for entry in self._side(overview, "target")["outline"]],
            ["Chapter 1 The Commodity", "Chapter 2 Money"],
        )

    def test_segment_windows_page_through_the_real_text(self) -> None:
        pivot = self._side(self._overview(), "pivot")
        window = read_body_range_segments(
            self.db, "pdf-zh", pivot["segment_set_id"], start=4, count=3
        )
        self.assertEqual(window["segment_count"], pivot["segment_count"])
        self.assertEqual(
            [item["segment_index"] for item in window["segments"]], [4, 5, 6]
        )
        self.assertEqual(window["segments"][1]["text"], "商品首先是一个外界的对象。")

        # 越界的起点收敛到最后一段，不返回空窗口。
        tail = read_body_range_segments(
            self.db, "pdf-zh", pivot["segment_set_id"], start=999, count=3
        )
        self.assertEqual(tail["start"], pivot["segment_count"] - 1)
        self.assertEqual(len(tail["segments"]), 1)

    def test_page_jump_uses_existing_page_anchors_only(self) -> None:
        overview = self._overview()
        pivot = self._side(overview, "pivot")
        window = read_body_range_segments(
            self.db, "pdf-zh", pivot["segment_set_id"], count=2, pdf_page=5
        )
        self.assertEqual(window["start"], 9)
        self.assertEqual(window["segments"][0]["text"], "第二章 货币")

        with self.assertRaises(InvalidAlignmentRequest):
            read_body_range_segments(
                self.db, "pdf-zh", pivot["segment_set_id"], pdf_page=99
            )
        # 没有可信页码的格式不提供按页跳转，而不是编一个页码。
        target = self._side(overview, "target")
        with self.assertRaises(InvalidAlignmentRequest):
            read_body_range_segments(
                self.db, "epub-en", target["segment_set_id"], pdf_page=1
            )

    def test_segment_window_rejects_mismatched_or_oversized_requests(self) -> None:
        pivot = self._side(self._overview(), "pivot")
        for call in (
            lambda: read_body_range_segments(
                self.db, "epub-en", pivot["segment_set_id"]
            ),
            lambda: read_body_range_segments(self.db, "pdf-zh", ""),
            lambda: read_body_range_segments(
                self.db, "pdf-zh", pivot["segment_set_id"], count=0
            ),
            lambda: read_body_range_segments(
                self.db,
                "pdf-zh",
                pivot["segment_set_id"],
                count=MAX_SEGMENT_WINDOW + 1,
            ),
            lambda: read_body_range_segments(
                self.db, "pdf-zh", pivot["segment_set_id"], start=-1
            ),
        ):
            with self.assertRaises(InvalidAlignmentRequest):
                call()

    def test_saved_review_is_shown_instead_of_detection_and_scoped_to_this_pair(
        self,
    ) -> None:
        with mock.patch(
            "src.me_finder.alignment_kernel.embed_text_sequences",
            side_effect=_fake_embedding_sequences,
        ):
            result = generate_alignment(
                self.db,
                "work",
                "pdf-zh",
                "epub-en",
                force=True,
                # 界面「含最后一段」在这里已换算成半开区间。
                reviewed_body_ranges={"pivot": [3, 12], "target": [1, 8]},
            )
        self.assertEqual(result["status"], "completed")

        overview = self._overview()
        self.assertEqual(overview["range_source"], "reviewed")
        self.assertEqual(
            (
                self._side(overview, "pivot")["body_start_index"],
                self._side(overview, "pivot")["body_end_index"],
            ),
            (3, 11),
        )
        self.assertEqual(
            (
                self._side(overview, "target")["body_start_index"],
                self._side(overview, "target")["body_end_index"],
            ),
            (1, 7),
        )
        self.assertEqual(
            self._side(overview, "pivot")["start_segment"]["text"],
            "序言\n本书由译者整理出版。",
        )

        connection = sqlite3.connect(str(self.db))
        try:
            parameters = json.loads(
                connection.execute(
                    "SELECT parameters_json FROM alignment_runs WHERE status='completed'"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        self.assertEqual(parameters["body_range_source"], "reviewed")
        self.assertEqual(parameters["body_ranges"]["pivot"], [3, 12])

    def test_invalid_pair_and_interval_are_refused(self) -> None:
        with self.assertRaises(InvalidAlignmentRequest):
            read_pair_body_ranges(self.db, "", "pdf-zh", "epub-en")
        with self.assertRaises(InvalidAlignmentRequest):
            read_pair_body_ranges(self.db, "missing-work", "pdf-zh", "epub-en")
        with self.assertRaises(InvalidAlignmentRequest):
            read_pair_body_ranges(self.db, "work", "pdf-zh", "pdf-zh")
        # 结尾早于开头、越出分段数都必须在写入前被拒绝。
        for ranges in (
            {"pivot": [11, 4], "target": [2, 8]},
            {"pivot": [4, 12], "target": [2, 99]},
        ):
            with self.assertRaises(InvalidAlignmentRequest):
                generate_alignment(
                    self.db,
                    "work",
                    "pdf-zh",
                    "epub-en",
                    force=True,
                    reviewed_body_ranges=ranges,
                )


if __name__ == "__main__":
    unittest.main()

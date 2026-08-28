from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import DEFAULT_DATABASE_PATH, build_database, replace_source_in_database
from src.me_finder.indexer import build_index
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.search import SearchEngine

from tests.corpus_fixtures import CORPUS_REASON, has_corpus


@unittest.skipUnless(has_corpus() or DEFAULT_DATABASE_PATH.exists(), CORPUS_REASON)
class SQLiteSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not DEFAULT_DATABASE_PATH.exists():
            build_index(include_pdf=True, backup_existing=False)
        cls.engine = SearchEngine()

    def test_default_search_uses_sqlite(self) -> None:
        self.assertEqual(self.engine.backend, "sqlite")
        self.assertEqual(self.engine.index_path, DEFAULT_DATABASE_PATH)

    def test_word_and_pdf_search_share_sqlite_backend(self) -> None:
        word = self.engine.search("宗教是人民的鸦片。", source_type="word", limit=1)
        self.assertGreater(word["total"], 0)
        self.assertEqual(word["results"][0]["source_type"], "word")

        pdf = self.engine.search("We make and cannot escape making value judgments", source_type="pdf", limit=1)
        self.assertGreater(pdf["total"], 0)
        self.assertEqual(pdf["results"][0]["source_type"], "pdf")

    def test_typeset_soft_hyphens_do_not_block_a_cleanly_typed_quote(self) -> None:
        # 排版 PDF 会在断词处留下软连字符（U+00AD），导出还常夹带零宽空格。
        # 这些字符的 Unicode 类别是 Cf，既非标点也非空白，曾经原样留在索引里，
        # 使读者手打的干净引文永远匹配不上原句。
        typeset = "­These ­things, as they say, are every­one's own busi­ness"
        typed = "These things as they say are everyone's own business"
        self.assertEqual(punctuationless_text(typed), punctuationless_text(typeset))
        self.assertNotIn("­", normalize_text(typeset))
        for invisible in ("​", "⁠", "﻿"):
            self.assertEqual(
                punctuationless_text(f"原{invisible}句"),
                punctuationless_text("原句"),
            )

    def test_database_has_searchable_paragraphs_and_catalog(self) -> None:
        connection = sqlite3.connect(str(DEFAULT_DATABASE_PATH))
        try:
            paragraph_count = connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0]
            source_count = connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0]
        finally:
            connection.close()
        self.assertGreater(paragraph_count, 0)
        self.assertGreater(source_count, 0)

    def test_targeted_source_replacement_preserves_other_sources(self) -> None:
        source_id = "pdf-target"
        other_id = "pdf-other"
        initial = {
            "metadata": {},
            "source_files": [
                {"source_file_id": source_id, "source_type": "pdf", "file_name": "target.pdf"},
                {"source_file_id": other_id, "source_type": "pdf", "file_name": "other.pdf"},
            ],
            "volumes": [
                {"volume_id": "TARGET", "source_file_id": source_id, "source_type": "pdf", "display_title": "旧标题"},
                {"volume_id": "OTHER", "source_file_id": other_id, "source_type": "pdf", "display_title": "保留文献"},
            ],
            "works": [
                {"work_id": "TARGET-W1", "volume_id": "TARGET", "source_type": "pdf", "title": "旧标题"},
                {"work_id": "OTHER-W1", "volume_id": "OTHER", "source_type": "pdf", "title": "保留文献"},
            ],
        }
        raw = "消费控制当代人的全部生活。"
        paragraph = {
            "paragraph_id": "TARGET-P1",
            "volume_id": "TARGET",
            "volume_number": None,
            "work_id": "TARGET-W1",
            "source_file_id": source_id,
            "source_type": "pdf",
            "paragraph_index": 1,
            "eligible_for_search": True,
            "text_raw": raw,
            "normalized_text": normalize_text(raw),
            "compact_text": compact_text(raw),
            "plain_text": punctuationless_text(raw),
            "document_title": "消费社会",
            "work_title": "消费社会",
            "volume_display": "消费社会",
            "page_display": "引用页码：序言第4页",
            "page_source_type": "ocr_sequence_with_structure",
            "citation_page_start": "序言第4页",
            "citation_page_end": "序言第4页",
            "pdf_page_start_index": 9,
            "pdf_page_end_index": 9,
            "original_file_name": "target.pdf",
            "text_source": "mineru",
        }
        replacement = {
            "source_files": [
                {
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "file_name": "target.pdf",
                    "title": "消费社会",
                    "pdf_profile": {"detected_pdf_type": "mineru_structured"},
                }
            ],
            "volumes": [
                {"volume_id": "TARGET", "source_file_id": source_id, "source_type": "pdf", "display_title": "消费社会"}
            ],
            "works": [
                {"work_id": "TARGET-W1", "volume_id": "TARGET", "source_type": "pdf", "title": "消费社会"}
            ],
            "paragraphs": [paragraph],
            "pdf_pages": [{"source_file_id": source_id, "pdf_page_index": 9, "text_raw": raw}],
            "pdf_page_mappings": [{"source_file_id": source_id, "pdf_page_index": None, "method": "ocr_sequence"}],
            "pdf_import_runs": [{"source_file_id": source_id, "status": "success", "parser": "mineru"}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "index.sqlite3"
            build_database(initial, database_path)
            summary = replace_source_in_database(replacement, database_path, backup_existing=False)
            engine = SearchEngine(database_path)
            try:
                result = engine.search("消费控制当代人的全部生活", source_type="pdf")
            finally:
                engine.close()
            connection = sqlite3.connect(str(database_path))
            try:
                source_ids = {row[0] for row in connection.execute("SELECT source_file_id FROM source_files")}
            finally:
                connection.close()
        self.assertEqual(summary["source_count"], 2)
        self.assertEqual(source_ids, {source_id, other_id})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["source_file_id"], source_id)
        self.assertEqual(result["results"][0]["page"], "引用页码：序言第4页")

    def test_search_can_scope_to_one_document_and_caps_results_at_200(self) -> None:
        sources = [
            {"source_file_id": "pdf-a", "source_type": "pdf", "file_name": "a.pdf"},
            {"source_file_id": "pdf-b", "source_type": "pdf", "file_name": "b.pdf"},
        ]
        volumes = [
            {"volume_id": "VOL-A", "source_file_id": "pdf-a", "source_type": "pdf", "display_title": "甲书"},
            {"volume_id": "VOL-B", "source_file_id": "pdf-b", "source_type": "pdf", "display_title": "乙书"},
        ]
        works = [
            {"work_id": "WORK-A", "volume_id": "VOL-A", "source_type": "pdf", "title": "甲书"},
            {"work_id": "WORK-B", "volume_id": "VOL-B", "source_type": "pdf", "title": "乙书"},
        ]
        paragraphs = []
        for index in range(205):
            source_id = "pdf-a" if index < 204 else "pdf-b"
            volume_id = "VOL-A" if source_id == "pdf-a" else "VOL-B"
            work_id = "WORK-A" if source_id == "pdf-a" else "WORK-B"
            title = "甲书" if source_id == "pdf-a" else "乙书"
            raw = f"共同检索词，第 {index + 1} 条独立文本。"
            paragraphs.append(
                {
                    "paragraph_id": f"P-{index}",
                    "volume_id": volume_id,
                    "volume_number": None,
                    "work_id": work_id,
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "paragraph_index": index,
                    "eligible_for_search": True,
                    "text_raw": raw,
                    "normalized_text": normalize_text(raw),
                    "compact_text": compact_text(raw),
                    "plain_text": punctuationless_text(raw),
                    "document_title": title,
                    "work_title": title,
                    "volume_display": title,
                    "page_display": "引用页码尚未校准",
                    "page_source_type": "uncalibrated",
                    "pdf_page_start_index": index,
                    "pdf_page_end_index": index,
                    "original_file_name": f"{source_id}.pdf",
                }
            )
        index = {"metadata": {}, "source_files": sources, "volumes": volumes, "works": works, "paragraphs": paragraphs}
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "index.sqlite3"
            build_database(index, database_path)
            engine = SearchEngine(database_path)
            try:
                all_result = engine.search("共同检索词", source_type="pdf", limit=999)
                unlimited_result = engine.search("共同检索词", source_type="pdf", limit="all")
                scoped_result = engine.search("共同检索词", source_type="pdf", limit=999, source_file_id="pdf-b")
            finally:
                engine.close()
        self.assertEqual(all_result["total"], 205)
        self.assertEqual(len(all_result["results"]), 200)
        self.assertEqual(unlimited_result["total"], 205)
        self.assertEqual(len(unlimited_result["results"]), 205)
        self.assertTrue(unlimited_result["return_all"])
        self.assertEqual(scoped_result["total"], 1)
        self.assertEqual(scoped_result["source_file_id"], "pdf-b")
        self.assertEqual(scoped_result["results"][0]["source_file_id"], "pdf-b")


if __name__ == "__main__":
    unittest.main()

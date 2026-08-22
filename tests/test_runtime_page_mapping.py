from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import SCHEMA
from src.me_finder.bibliographic_metadata import manual_metadata, update_metadata_in_database
from src.me_finder.runtime_page_mapping import apply_mapping_to_database, normalize_auto_segments
from src.me_finder.search import SearchEngine
from src.me_finder.web import HTML


class RuntimePageMappingTests(unittest.TestCase):
    def test_live_apply_updates_pages_paragraphs_source_and_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            connection = sqlite3.connect(str(database))
            connection.executescript(SCHEMA)
            source = {"source_file_id": "pdf-test", "source_type": "pdf", "pdf_profile": {"mapping_status": "unmapped"}}
            connection.execute(
                "INSERT INTO source_files(source_file_id,source_type,payload_json) VALUES(?,?,?)",
                ("pdf-test", "pdf", json.dumps(source)),
            )
            for page_idx in (10, 11):
                page = {
                    "source_file_id": "pdf-test",
                    "pdf_page_index": page_idx,
                    "pdf_page_label": None,
                    "text_raw": f"page {page_idx}",
                }
                connection.execute(
                    "INSERT INTO pdf_pages(source_file_id,pdf_page_index,payload_json) VALUES(?,?,?)",
                    ("pdf-test", page_idx, json.dumps(page)),
                )
            paragraph = {
                "paragraph_id": "p1",
                "source_file_id": "pdf-test",
                "source_type": "pdf",
                "volume_id": "pdf-test",
                "volume_number": None,
                "work_id": "pdf-test-W0001",
                "document_title": "测试文献",
                "volume_display": "测试文献",
                "paragraph_index": 1,
                "eligible_for_search": True,
                "text_raw": "测试段落",
                "normalized_text": "测试段落",
                "compact_text": "测试段落",
                "plain_text": "测试段落",
                "pdf_page_start_index": 10,
                "pdf_page_end_index": 11,
            }
            connection.execute(
                """
                INSERT INTO paragraphs(
                    paragraph_id,source_file_id,source_type,paragraph_index,eligible_for_search,
                    text_raw,normalized_text,compact_text,plain_text,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                ("p1", "pdf-test", "pdf", 1, 1, "测试段落", "测试段落", "测试段落", "测试段落", json.dumps(paragraph)),
            )
            connection.commit()
            connection.close()
            segment = {
                "pdf_page_start": 10,
                "pdf_page_end": 20,
                "citation_page_start": "1",
                "number_style": "arabic",
                "method": "native_pdf_edge_sequence",
                "mapping_confidence": 0.96,
                "confidence_level": "high",
                "mapping_evidence": {"inferred_offset": -9},
            }
            updated = apply_mapping_to_database(
                database,
                "pdf-test",
                [segment],
                auto_mapping={"selected_segments": [segment]},
                mapping_status="auto_mapped_high",
            )
            self.assertEqual(updated, {"pages": 2, "paragraphs": 1, "segments": 1})
            connection = sqlite3.connect(str(database))
            pages = [json.loads(row[0]) for row in connection.execute("SELECT payload_json FROM pdf_pages ORDER BY pdf_page_index")]
            stored_paragraph = json.loads(connection.execute("SELECT payload_json FROM paragraphs").fetchone()[0])
            stored_source = json.loads(connection.execute("SELECT payload_json FROM source_files").fetchone()[0])
            connection.close()
            self.assertEqual([page["citation_page"] for page in pages], ["1", "2"])
            self.assertEqual(
                {page["segment_id"] for page in pages},
                {"MAPSEG-000010-000020"},
            )
            self.assertEqual(
                stored_paragraph["segment_id"],
                "MAPSEG-000010-000020",
            )
            self.assertEqual(stored_paragraph["citation_page_start"], "1")
            self.assertEqual(stored_paragraph["citation_page_end"], "2")
            self.assertEqual(stored_paragraph["page_mapping_method"], "native_pdf_edge_sequence")
            self.assertEqual(stored_source["pdf_profile"]["mapping_status"], "auto_mapped_high")
            metadata = manual_metadata(
                {
                    "author": "南希·弗雷泽",
                    "title": "食人资本主义",
                    "translator": "蓝江",
                    "publish_place": "上海",
                    "publisher": "上海人民出版社",
                    "publish_year": "2023",
                    "isbn": "",
                }
            )
            update_metadata_in_database(database, "pdf-test", metadata)
            engine = SearchEngine(database)
            try:
                search_result = engine.search("测试段落", mode="exact", source_type="pdf")
            finally:
                engine.close()
            self.assertEqual(search_result["results"][0]["citation_page_start"], "1")
            self.assertEqual(
                search_result["results"][0]["citation_formats"]["gb"],
                "南希·弗雷泽. 食人资本主义[M]. 蓝江, 译. 上海: 上海人民出版社, 2023: 1-2.",
            )

    def test_auto_segments_keep_detected_method(self) -> None:
        cleaned = normalize_auto_segments(
            [
                {
                    "pdf_page_start": 35,
                    "pdf_page_end": 40,
                    "citation_page_start": "1",
                    "method": "native_pdf_edge_sequence",
                    "mapping_confidence": 0.91,
                }
            ]
        )
        self.assertEqual(cleaned[0]["method"], "native_pdf_edge_sequence")
        self.assertEqual(cleaned[0]["segment_id"], "MAPSEG-000035-000040")

    def test_calibration_ui_has_dry_run_and_explicit_apply_controls(self) -> None:
        self.assertIn("自动检测页码", HTML)
        self.assertIn("/api/auto-page-mapping/detect", HTML)
        self.assertIn("/api/auto-page-mapping/apply", HTML)
        self.assertIn("用自动结果替换人工映射", HTML)
        self.assertIn("自动识别", HTML)
        self.assertIn("/api/bibliographic-metadata/detect", HTML)
        self.assertIn("/api/bibliographic-metadata/save", HTML)
        self.assertIn("粘贴引用", HTML)
        self.assertIn("从引用文字补全", HTML)
        self.assertIn("/api/bibliographic-metadata/parse-cnki-citation", HTML)
        self.assertIn("/api/bibliographic-metadata/lookup-cnki", HTML)
        self.assertIn("/api/bibliographic-metadata/cnki-candidate", HTML)
        self.assertIn("查询知网", HTML)
        self.assertIn("获取完整题录", HTML)
        self.assertIn("打开知网检索", HTML)
        self.assertIn("data.query_notice", HTML)
        self.assertIn("field('doi','doi','DOI'", HTML)
        self.assertIn("field('issn','issn','ISSN'", HTML)
        self.assertIn("if (!existing)", HTML)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder.indexer import IncompleteIndexBuildError, build_index
from src.me_finder.database import build_database


class IndexPublicationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.word_corpus = self.root / "corpus" / "raw_docx"
        self.pdf_corpus = self.root / "corpus" / "raw_pdf"
        self.pdf_config = self.root / "config" / "pdf_imports.json"
        self.database_path = self.root / "data" / "index.sqlite3"
        self.index_path = self.root / "data" / "index.json"
        self.word_corpus.mkdir(parents=True)
        self.pdf_corpus.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_pdf_config(self, documents: list[dict[str, object]]) -> None:
        self.pdf_config.parent.mkdir(parents=True, exist_ok=True)
        self.pdf_config.write_text(
            json.dumps({"documents": documents}, ensure_ascii=False),
            encoding="utf-8",
        )

    def seed_existing_database(self) -> bytes:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        build_database({"metadata": {}}, self.database_path)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES ('old-index')")
            connection.commit()
        finally:
            connection.close()
        return self.database_path.read_bytes()

    def seed_existing_pdf_database(
        self,
        *source_ids: str,
        searchable_paragraphs: bool = False,
        pdf_pages: bool = False,
    ) -> bytes:
        build_database(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "pdf",
                        "file_name": f"{source_id}.pdf",
                    }
                    for source_id in source_ids
                ],
                "paragraphs": [
                    {
                        "paragraph_id": f"{source_id}-P000000",
                        "source_file_id": source_id,
                        "source_type": "pdf",
                        "paragraph_index": 0,
                        "eligible_for_search": True,
                        "text_raw": "活动索引中原本可搜索的 PDF 正文。",
                    }
                    for source_id in source_ids
                    if searchable_paragraphs
                ],
                "pdf_pages": [
                    {
                        "pdf_page_id": f"{source_id}-PAGE-000000",
                        "source_file_id": source_id,
                        "pdf_page_index": 0,
                        "text_raw": "活动索引中原本保留的 PDF 页。",
                    }
                    for source_id in source_ids
                    if pdf_pages
                ],
            },
            self.database_path,
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES ('old-index')")
            connection.commit()
        finally:
            connection.close()
        return self.database_path.read_bytes()

    def seed_existing_word_database(self, *source_ids: str) -> bytes:
        build_database(
            {
                "metadata": {},
                "source_files": [
                    {
                        "source_file_id": source_id,
                        "source_type": "word",
                        "file_name": f"{source_id}.docx",
                    }
                    for source_id in source_ids
                ],
            },
            self.database_path,
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES ('old-index')")
            connection.commit()
        finally:
            connection.close()
        return self.database_path.read_bytes()

    @staticmethod
    def empty_pdf_extraction(source_id: str) -> dict[str, list[dict[str, object]]]:
        return {
            "source_files": [
                {
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "file_name": f"{source_id}.pdf",
                }
            ],
            "volumes": [],
            "works": [],
            "paragraphs": [],
            "pdf_pages": [],
            "pdf_page_mappings": [],
            "pdf_import_runs": [],
            "audit_issues": [],
        }

    def build(self, *, export_json: bool = False) -> dict[str, object]:
        return build_index(
            corpus_dir=self.word_corpus,
            index_path=self.index_path,
            include_pdf=True,
            pdf_corpus_dir=self.pdf_corpus,
            pdf_config_path=self.pdf_config,
            parsed_pdf_dir=self.root / "corpus" / "parsed" / "pdf",
            database_path=self.database_path,
            backup_existing=True,
            export_json=export_json,
            root=self.root,
        )

    def assert_old_database_is_untouched(self, original_bytes: bytes) -> None:
        self.assertEqual(self.database_path.read_bytes(), original_bytes)
        connection = sqlite3.connect(self.database_path)
        try:
            value = connection.execute("SELECT value FROM sentinel").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(value, "old-index")

    def test_missing_enabled_pdf_does_not_replace_existing_database(self) -> None:
        self.write_pdf_config(
            [
                {
                    "source_file_id": "pdf-missing",
                    "file_name": "missing.pdf",
                    "enabled": True,
                }
            ]
        )
        original_bytes = self.seed_existing_database()

        with self.assertRaises(IncompleteIndexBuildError) as raised:
            self.build()

        self.assertIn("pdf_missing[pdf-missing]", str(raised.exception))
        self.assertEqual(
            raised.exception.audit_issues[0]["issue_type"], "pdf_missing"
        )
        self.assert_old_database_is_untouched(original_bytes)

    def test_missing_enabled_pdf_does_not_publish_a_first_database_or_json(self) -> None:
        self.write_pdf_config(
            [
                {
                    "source_file_id": "pdf-first-build-missing",
                    "file_name": "missing.pdf",
                    "enabled": True,
                }
            ]
        )

        with self.assertRaisesRegex(
            IncompleteIndexBuildError, "数据库未更新或创建"
        ):
            self.build(export_json=True)

        self.assertFalse(self.database_path.exists())
        self.assertFalse(self.index_path.exists())

    def test_pdf_parser_failure_does_not_replace_existing_database(self) -> None:
        source = self.pdf_corpus / "broken.pdf"
        source.write_bytes(b"%PDF-1.4\n")
        self.write_pdf_config(
            [
                {
                    "source_file_id": "pdf-parser-failure",
                    "file_name": source.name,
                    "enabled": True,
                }
            ]
        )
        original_bytes = self.seed_existing_database()

        with patch(
            "src.me_finder.pdf_extractors.extract_pdf_source",
            side_effect=RuntimeError("parser exploded"),
        ):
            with self.assertRaises(IncompleteIndexBuildError) as raised:
                self.build()

        self.assertIn("pdf_import_failed[pdf-parser-failure]", str(raised.exception))
        self.assertIn("parser exploded", str(raised.exception))
        self.assert_old_database_is_untouched(original_bytes)

    def test_missing_disabled_pdf_does_not_block_first_build(self) -> None:
        self.write_pdf_config(
            [
                {
                    "source_file_id": "pdf-disabled",
                    "file_name": "missing.pdf",
                    "enabled": False,
                }
            ]
        )

        index = self.build()

        self.assertTrue(self.database_path.is_file())
        self.assertEqual(index["metadata"]["source_count"], 0)
        self.assertEqual(index["audit_issues"], [])

    def test_missing_config_does_not_erase_existing_pdf_catalog(self) -> None:
        original_bytes = self.seed_existing_pdf_database("pdf-existing")

        with self.assertRaisesRegex(
            IncompleteIndexBuildError,
            "pdf_config_missing",
        ):
            self.build()

        self.assert_old_database_is_untouched(original_bytes)

    def test_empty_config_does_not_erase_existing_pdf_catalog(self) -> None:
        self.write_pdf_config([])
        original_bytes = self.seed_existing_pdf_database("pdf-existing")

        with self.assertRaisesRegex(
            IncompleteIndexBuildError,
            "pdf_config_empty",
        ):
            self.build()

        self.assert_old_database_is_untouched(original_bytes)

    def test_word_only_rebuild_cannot_silently_drop_active_pdf_sources(self) -> None:
        original_bytes = self.seed_existing_pdf_database("pdf-existing")

        with self.assertRaisesRegex(
            IncompleteIndexBuildError,
            "pdf_sources_excluded",
        ):
            build_index(
                corpus_dir=self.word_corpus,
                index_path=self.index_path,
                include_pdf=False,
                database_path=self.database_path,
                root=self.root,
            )

        self.assert_old_database_is_untouched(original_bytes)

    def test_existing_word_source_missing_from_corpus_fails_before_pdf_work(
        self,
    ) -> None:
        self.write_pdf_config([])
        original_bytes = self.seed_existing_word_database("word-missing")

        with patch("src.me_finder.indexer.extract_configured_pdfs") as extractor:
            with self.assertRaises(IncompleteIndexBuildError) as raised:
                self.build()

        extractor.assert_not_called()
        self.assertEqual(
            raised.exception.audit_issues,
            [
                {
                    "severity": "error",
                    "issue_type": "word_source_set_incomplete",
                    "message": (
                        "Word 提取结果缺少活动索引中的文献："
                        "word-missing"
                    ),
                }
            ],
        )
        self.assertIn("word_source_set_incomplete", str(raised.exception))
        self.assert_old_database_is_untouched(original_bytes)

    def test_unreadable_active_database_fails_closed_before_publication(self) -> None:
        self.write_pdf_config([])
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        original_bytes = b"not-a-sqlite-database"
        self.database_path.write_bytes(original_bytes)

        with patch("src.me_finder.indexer.extract_configured_pdfs") as extractor:
            with self.assertRaisesRegex(
                IncompleteIndexBuildError,
                "active_index_unreadable",
            ):
                self.build()

        extractor.assert_not_called()
        self.assertEqual(self.database_path.read_bytes(), original_bytes)

    def test_pdf_limit_cannot_publish_over_an_active_catalog(self) -> None:
        self.write_pdf_config(
            [
                {"source_file_id": "pdf-one", "file_name": "one.pdf"},
                {"source_file_id": "pdf-two", "file_name": "two.pdf"},
            ]
        )
        original_bytes = self.seed_existing_pdf_database("pdf-one", "pdf-two")

        with self.assertRaisesRegex(IncompleteIndexBuildError, "pdf_partial_build"):
            build_index(
                corpus_dir=self.word_corpus,
                include_pdf=True,
                pdf_corpus_dir=self.pdf_corpus,
                pdf_config_path=self.pdf_config,
                parsed_pdf_dir=self.root / "corpus" / "parsed" / "pdf",
                database_path=self.database_path,
                pdf_limit=1,
                root=self.root,
            )

        self.assert_old_database_is_untouched(original_bytes)

    def test_silent_extractor_omission_is_detected_by_source_identity(self) -> None:
        source = self.pdf_corpus / "configured.pdf"
        source.write_bytes(b"%PDF-test")
        self.write_pdf_config(
            [{"source_file_id": "pdf-configured", "file_name": source.name}]
        )
        empty_extraction = {
            "source_files": [],
            "volumes": [],
            "works": [],
            "paragraphs": [],
            "pdf_pages": [],
            "pdf_page_mappings": [],
            "pdf_import_runs": [],
            "audit_issues": [],
        }

        with patch(
            "src.me_finder.indexer.extract_configured_pdfs",
            return_value=empty_extraction,
        ):
            with self.assertRaisesRegex(
                IncompleteIndexBuildError,
                "pdf_source_set_incomplete",
            ):
                self.build()

        self.assertFalse(self.database_path.exists())

    def test_existing_searchable_pdf_cannot_be_replaced_by_empty_source_row(
        self,
    ) -> None:
        source_id = "pdf-searchable"
        self.write_pdf_config(
            [{"source_file_id": source_id, "file_name": f"{source_id}.pdf"}]
        )
        original_bytes = self.seed_existing_pdf_database(
            source_id,
            searchable_paragraphs=True,
        )
        extracted = self.empty_pdf_extraction(source_id)
        extracted["audit_issues"] = [
            {
                "severity": "warning",
                "issue_type": "pdf_needs_mineru",
                "source_file_id": source_id,
                "message": "本轮没有可搜索正文。",
            }
        ]

        with patch(
            "src.me_finder.indexer.extract_configured_pdfs",
            return_value=extracted,
        ):
            with self.assertRaises(IncompleteIndexBuildError) as raised:
                self.build()

        content_issue = next(
            issue
            for issue in raised.exception.audit_issues
            if issue["issue_type"] == "pdf_source_content_incomplete"
        )
        self.assertEqual(content_issue["source_file_id"], source_id)
        self.assertEqual(content_issue["previous_searchable_paragraph_count"], 1)
        self.assertEqual(content_issue["extracted_searchable_paragraph_count"], 0)
        self.assertIn("可搜索段落", content_issue["message"])
        self.assert_old_database_is_untouched(original_bytes)

    def test_existing_pdf_pages_cannot_be_replaced_by_empty_source_row(self) -> None:
        source_id = "pdf-with-pages"
        self.write_pdf_config(
            [{"source_file_id": source_id, "file_name": f"{source_id}.pdf"}]
        )
        original_bytes = self.seed_existing_pdf_database(
            source_id,
            pdf_pages=True,
        )

        with patch(
            "src.me_finder.indexer.extract_configured_pdfs",
            return_value=self.empty_pdf_extraction(source_id),
        ):
            with self.assertRaises(IncompleteIndexBuildError) as raised:
                self.build()

        content_issue = next(
            issue
            for issue in raised.exception.audit_issues
            if issue["issue_type"] == "pdf_source_content_incomplete"
        )
        self.assertEqual(content_issue["source_file_id"], source_id)
        self.assertEqual(content_issue["previous_pdf_page_count"], 1)
        self.assertEqual(content_issue["extracted_pdf_page_count"], 0)
        self.assertIn("PDF 页", content_issue["message"])
        self.assert_old_database_is_untouched(original_bytes)

    def test_new_pdf_may_remain_empty_while_waiting_for_parser(self) -> None:
        source_id = "pdf-new-needs-parser"
        self.write_pdf_config(
            [{"source_file_id": source_id, "file_name": f"{source_id}.pdf"}]
        )
        extracted = self.empty_pdf_extraction(source_id)
        extracted["audit_issues"] = [
            {
                "severity": "warning",
                "issue_type": "pdf_needs_mineru",
                "source_file_id": source_id,
                "message": "新文献等待解析。",
            }
        ]

        with patch(
            "src.me_finder.indexer.extract_configured_pdfs",
            return_value=extracted,
        ):
            index = self.build()

        self.assertEqual(index["metadata"]["source_count"], 1)
        self.assertEqual(index["metadata"]["paragraph_count"], 0)
        self.assertEqual(index["audit_issues"], extracted["audit_issues"])
        self.assertTrue(self.database_path.is_file())

    def test_active_catalog_rejects_config_without_stable_source_identity(self) -> None:
        self.write_pdf_config([{"file_name": "replacement.pdf"}])
        original_bytes = self.seed_existing_pdf_database("pdf-existing")
        extracted = {
            "source_files": [
                {
                    "source_file_id": "pdf-generated",
                    "source_type": "pdf",
                    "file_name": "replacement.pdf",
                }
            ],
            "volumes": [],
            "works": [],
            "paragraphs": [],
            "pdf_pages": [],
            "pdf_page_mappings": [],
            "pdf_import_runs": [],
            "audit_issues": [],
        }

        with patch(
            "src.me_finder.indexer.extract_configured_pdfs",
            return_value=extracted,
        ):
            with self.assertRaisesRegex(
                IncompleteIndexBuildError,
                "pdf_config_identity_invalid",
            ):
                self.build()

        self.assert_old_database_is_untouched(original_bytes)


if __name__ == "__main__":
    unittest.main()

"""Refreshing an indexed MinerU book moves page text only, nothing else."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.me_finder.database import build_database, replace_source_in_database
from src.me_finder.pdf_extractors import extract_pdf_source
from src.me_finder.pdf_page_refresh import refresh_page_text
from src.me_finder.persistence.translation_work_store import (
    save_reading_position_row,
    workspace_write_transaction,
)
from tests.test_export_page_reconstruction import reconstruction_fixture
from tests.test_pdf_page_split import PREFIX, TAIL, content_list

UNCALIBRATED = {"method": "uncalibrated", "selected_segments": [],
                "applied_segments": [], "failure_reasons": []}


class PageTextRefreshTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.result_dir = self.root / "corpus/processed/mineru/results/book"
        self.result_dir.mkdir(parents=True)
        (self.result_dir / "content_list.json").write_text(
            json.dumps(content_list(), ensure_ascii=False), encoding="utf-8"
        )
        (self.root / "corpus/raw_pdf").mkdir(parents=True)
        (self.root / "corpus/raw_pdf/book.pdf").write_bytes(b"%PDF-test")
        self.config = {
            "source_file_id": "book", "document_id": "book", "title": "书", "file_name": "book.pdf",
            "mineru": {"segments": [{"page_ranges": "1-2", "page_index_offset": 0, "parser": "mineru",
                                     # Recorded by another machine; the cache lives under root.
                                     "result_dir": "D:\\ME_Finder\\runtime\\corpus\\processed\\mineru"
                                                   "\\results\\book"}]},
        }
        (self.root / "config").mkdir()
        (self.root / "config/pdf_imports.json").write_text(
            json.dumps({"documents": [self.config]}, ensure_ascii=False), encoding="utf-8"
        )
        self.index = self.root / "data" / "index.sqlite3"
        build_database({"metadata": {}}, self.index)
        with self.patched():
            # An index written before the split: MinerU's join stays on page 0.
            replace_source_in_database(
                extract_pdf_source(self.root / "corpus/raw_pdf/book.pdf", self.root, self.config),
                self.index, backup_existing=False,
            )
        with closing(sqlite3.connect(self.index)) as connection, connection:
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM source_files WHERE source_file_id='book'").fetchone()[0])
            payload["title"] = "用户改过的书名"
            connection.execute("UPDATE source_files SET payload_json=? WHERE source_file_id='book'",
                               (json.dumps(payload, ensure_ascii=False),))
        (self.result_dir / "layout.json").write_text(
            json.dumps(reconstruction_fixture()[1], ensure_ascii=False), encoding="utf-8"
        )

    def patched(self):
        stack = patch("src.me_finder.pdf_extractors.load_pymupdf", return_value=None)
        mapping = patch("src.me_finder.pdf_extractors.PageMappingService.infer", return_value=UNCALIBRATED)

        class Both:
            def __enter__(self_inner):
                stack.__enter__()
                mapping.__enter__()

            def __exit__(self_inner, *exc):
                mapping.__exit__(*exc)
                stack.__exit__(*exc)
        return Both()

    def refresh(self, **options):
        with self.patched():
            return refresh_page_text(self.root, self.index, **options)

    def page_texts(self):
        with closing(sqlite3.connect(self.index)) as connection:
            return [json.loads(row[0])["text_raw"] for row in connection.execute(
                "SELECT payload_json FROM pdf_pages WHERE source_file_id='book' ORDER BY pdf_page_index")]

    def payloads(self, table):
        return [json.loads(row[0]) for row in self.stored(f"SELECT payload_json FROM {table} ORDER BY rowid")]

    def stored(self, sql):
        with closing(sqlite3.connect(self.index)) as connection:
            return connection.execute(sql).fetchall()

    def test_plan_reports_changed_pages_without_writing(self):
        before = self.page_texts()
        result = self.refresh()

        [book] = result["books"]
        self.assertEqual((book["status"], book["changed_pages"]), ("refresh", 2))
        self.assertIsNone(result["backup_path"])
        self.assertEqual(self.page_texts(), before)
        self.assertEqual(list((self.root / "data").glob("backups/*")), [])

    def test_apply_moves_text_keeps_book_records_and_backs_up_once(self):
        source_before = self.payloads("source_files")
        runs_before = self.payloads("pdf_import_runs")
        result = self.refresh(apply=True)

        self.assertEqual(result["books"][0]["status"], "refreshed")
        self.assertEqual(self.page_texts(), ["第一章 方法\n" + PREFIX, TAIL + "\n下一段。"])
        self.assertEqual(self.payloads("source_files"), source_before)
        self.assertEqual(source_before[0]["title"], "用户改过的书名")
        self.assertEqual(self.payloads("pdf_import_runs"), runs_before)
        backup = Path(result["backup_path"])
        self.assertTrue(backup.name.startswith("page-refresh-") and backup.is_file())
        hit = self.stored("SELECT pdf_page_start_index FROM paragraphs "
                          f"WHERE source_file_id='book' AND text_raw LIKE '%{TAIL}%' AND paragraph_id NOT LIKE '%CROSS%'")
        self.assertEqual(hit, [(1,)])
        self.assertEqual(self.refresh(apply=True)["books"][0]["status"], "unchanged")

    def test_reader_position_survives_the_source_replacement(self):
        with workspace_write_transaction(self.index) as connection:
            connection.execute(
                "INSERT INTO document_groups(document_group_id, title, base_source_file_id, "
                "created_at, updated_at) VALUES ('g', '作品', NULL, 't', 't')")
            save_reading_position_row(connection, "g", "book", None, 1, 3, "2026-09-19T12:00:00+00:00")
        position = self.stored("SELECT * FROM document_group_reading_positions")

        self.assertEqual(self.refresh(apply=True)["books"][0]["status"], "refreshed")

        self.assertEqual(self.stored("SELECT * FROM document_group_reading_positions"), position)

    def test_book_whose_page_numbers_would_change_is_skipped(self):
        with closing(sqlite3.connect(self.index)) as connection, connection:
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM pdf_pages WHERE pdf_page_index=0").fetchone()[0])
            payload["citation_page_start"] = payload["citation_page_end"] = "1"
            connection.execute("UPDATE pdf_pages SET payload_json=? WHERE pdf_page_index=0",
                               (json.dumps(payload, ensure_ascii=False),))
        before = self.page_texts()

        [book] = self.refresh(apply=True)["books"]

        self.assertEqual((book["status"], book["reason"]), ("skipped", "citation_pages_changed"))
        self.assertEqual(self.page_texts(), before)

    def test_requested_skip_and_only_filters(self):
        self.assertEqual(self.refresh(skip={"book"})["books"][0]["reason"], "requested")
        self.assertEqual(self.refresh(only={"other"})["books"], [])


if __name__ == "__main__":
    unittest.main()

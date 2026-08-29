from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.application import LiteratureVerificationService
from src.me_finder.structured_reader import SourceNotFound
from tests.mcp_v1_fixture import (
    CALIBRATED_QUOTE,
    MISSING_QUOTE,
    PDF_SOURCE_ID,
    UNCALIBRATED_QUOTE,
    WORD_QUOTE,
    WORD_SOURCE_ID,
    build_mcp_v1_fixture,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "docs" / "contracts" / "v0.4.4-mcp-v1-tools.json"


class LiteratureVerificationServiceTests(unittest.TestCase):
    def test_index_path_is_resolved_again_for_each_use_case(self) -> None:
        roots = iter((Path("/runtime/one"), Path("/runtime/two")))
        service = LiteratureVerificationService(lambda: next(roots))

        self.assertEqual(
            service.index_path,
            Path("/runtime/one/data/index.sqlite3"),
        )
        self.assertEqual(
            service.index_path,
            Path("/runtime/two/data/index.sqlite3"),
        )

    def test_construction_does_not_resolve_a_path_or_open_the_index(self) -> None:
        calls = []
        service = LiteratureVerificationService(
            lambda: calls.append("resolved") or Path("/runtime")
        )

        self.assertEqual(calls, [])
        self.assertEqual(
            set(service.__dict__),
            {"_runtime_root_provider"},
        )

    def test_import_does_not_load_desktop_http_or_sqlite_modules(self) -> None:
        code = """
import sys
from src.me_finder.application.literature_verification_service import LiteratureVerificationService
assert 'desktop' not in sys.modules
assert 'src.me_finder.web' not in sys.modules
assert 'webview' not in sys.modules
assert 'sqlite3' not in sys.modules
assert LiteratureVerificationService
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class LiteratureVerificationServiceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        cls.tools = {tool["name"]: tool for tool in cls.contract["tools"]}

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / "runtime"
        database_path = self.runtime_root / "data" / "index.sqlite3"
        build_mcp_v1_fixture(database_path)
        self.service = LiteratureVerificationService(lambda: self.runtime_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def assert_required_keys(self, value: dict[str, object], schema: dict) -> None:
        self.assertEqual(set(value), set(schema["required"]))

    def test_list_documents_filters_catalog_and_matches_contract(self) -> None:
        result = self.service.list_documents()
        self.assert_required_keys(
            result,
            self.tools["list_documents"]["outputSchema"],
        )
        document_schema = self.contract["$defs"]["document"]
        self.assertEqual(result["schema_version"], "1")
        self.assertEqual(result["total"], 2)
        self.assertFalse(result["has_more"])
        self.assertEqual(
            [item["source_file_id"] for item in result["documents"]],
            [PDF_SOURCE_ID, WORD_SOURCE_ID],
        )
        for document in result["documents"]:
            self.assert_required_keys(document, document_schema)

        author_match = self.service.list_documents(query="测试作者乙")
        self.assertEqual(author_match["total"], 1)
        self.assertEqual(
            author_match["documents"][0],
            {
                "source_file_id": WORD_SOURCE_ID,
                "source_type": "word",
                "title": "MCP 合成 Word 样例",
                "author": "测试作者乙",
                "original_file_name": "mcp-fixture.docx",
            },
        )
        pdf_only = self.service.list_documents(source_type="pdf", limit=1)
        self.assertEqual(pdf_only["total"], 1)
        self.assertFalse(pdf_only["has_more"])
        limited = self.service.list_documents(limit=1)
        self.assertEqual(limited["total"], 2)
        self.assertTrue(limited["has_more"])
        self.assertEqual(len(limited["documents"]), 1)

    def test_locate_quote_returns_calibrated_pdf_evidence(self) -> None:
        result = self.service.locate_quote(
            CALIBRATED_QUOTE,
            mode="exact",
            source_file_id=PDF_SOURCE_ID,
            source_type="pdf",
        )
        self.assert_required_keys(
            result,
            self.tools["locate_quote"]["outputSchema"],
        )
        self.assertEqual(result["total"], 1)
        match = result["matches"][0]
        self.assert_required_keys(match, self.contract["$defs"]["match"])
        self.assertEqual(match["matched_text"], CALIBRATED_QUOTE)
        self.assertEqual(
            match["physical_page"],
            {
                "start_index": 0,
                "end_index": 0,
                "start_label": "1",
                "end_label": "1",
            },
        )
        self.assertEqual(
            match["citation_page"],
            {"start": "38", "end": "38", "status": "calibrated"},
        )
        self.assertEqual(match["page_mapping"]["method"], "manual_segment")
        self.assertEqual(match["page_mapping"]["confidence"], 1.0)
        self.assertEqual(match["reader"], {"unit": "pdf_page", "start": 0})
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("highlighted_html", serialized)
        self.assertNotIn("copy_text", serialized)
        self.assertNotIn(str(self.runtime_root), serialized)

    def test_locate_quote_distinguishes_uncalibrated_pdf_and_verified_word(self) -> None:
        pdf_result = self.service.locate_quote(
            UNCALIBRATED_QUOTE,
            mode="exact",
            source_file_id=PDF_SOURCE_ID,
            source_type="pdf",
        )
        pdf_match = pdf_result["matches"][0]
        self.assertEqual(
            pdf_match["citation_page"],
            {"start": None, "end": None, "status": "uncalibrated"},
        )
        self.assertEqual(pdf_match["physical_page"]["start_index"], 1)

        word_result = self.service.locate_quote(
            WORD_QUOTE,
            mode="exact",
            source_file_id=WORD_SOURCE_ID,
            source_type="word",
        )
        word_match = word_result["matches"][0]
        self.assertEqual(
            word_match["physical_page"],
            {
                "start_index": None,
                "end_index": None,
                "start_label": None,
                "end_label": None,
            },
        )
        self.assertEqual(
            word_match["citation_page"],
            {"start": "7", "end": "7", "status": "verified"},
        )
        self.assertEqual(
            word_match["reader"],
            {"unit": "word_paragraph", "start": 0},
        )

    def test_locate_quote_keeps_no_result_separate_from_errors(self) -> None:
        result = self.service.locate_quote(MISSING_QUOTE, mode="exact")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["matches"], [])
        self.assertFalse(result["has_more"])
        with self.assertRaises(SourceNotFound):
            self.service.locate_quote(
                CALIBRATED_QUOTE,
                source_file_id="missing-source",
            )

    def test_locate_quote_preserves_candidates_and_is_deterministic(self) -> None:
        first = self.service.locate_quote("必须", mode="exact", limit=20)
        second = self.service.locate_quote("必须", mode="exact", limit=20)

        self.assertEqual(first, second)
        self.assertEqual(first["total"], 2)
        self.assertEqual(len(first["matches"]), 2)
        self.assertEqual(
            {item["source_file_id"] for item in first["matches"]},
            {PDF_SOURCE_ID, WORD_SOURCE_ID},
        )

    def test_read_document_window_returns_trimmed_pdf_and_word_items(self) -> None:
        output_schema = self.tools["read_document_window"]["outputSchema"]
        source_schema = self.contract["$defs"]["reader_source"]
        item_schema = self.contract["$defs"]["reader_item"]

        pdf_result = self.service.read_document_window(
            PDF_SOURCE_ID,
            count=2,
        )
        self.assert_required_keys(pdf_result, output_schema)
        self.assert_required_keys(pdf_result["source"], source_schema)
        self.assertEqual(
            [item["citation_page"] for item in pdf_result["items"]],
            [
                {"start": "38", "end": "38", "status": "calibrated"},
                {"start": None, "end": None, "status": "uncalibrated"},
            ],
        )
        self.assertEqual(
            [item["physical_page"]["start_index"] for item in pdf_result["items"]],
            [0, 1],
        )
        for item in pdf_result["items"]:
            self.assert_required_keys(item, item_schema)
            self.assert_required_keys(
                item["citation_formats"],
                self.contract["$defs"]["citation_formats"],
            )

        word_result = self.service.read_document_window(
            WORD_SOURCE_ID,
            count=2,
        )
        self.assertEqual(
            [item["citation_page"]["start"] for item in word_result["items"]],
            ["7", "8"],
        )
        self.assertTrue(all(item["page_verified"] for item in word_result["items"]))
        serialized = json.dumps([pdf_result, word_result], ensure_ascii=False)
        self.assertNotIn("page_text_hash", serialized)
        self.assertNotIn("result_dir", serialized)
        self.assertNotIn(str(self.runtime_root), serialized)

    def test_verify_quotes_batches_status_and_matches_contract(self) -> None:
        output_schema = self.tools["verify_quotes"]["outputSchema"]
        misquote = CALIBRATED_QUOTE.replace("必须", "必需")
        result = self.service.verify_quotes(
            [CALIBRATED_QUOTE, misquote, MISSING_QUOTE],
            mode="auto",
        )
        self.assert_required_keys(result, output_schema)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["verified_count"], 1)
        self.assertEqual(result["approximate_count"], 1)
        self.assertEqual(result["not_found_count"], 1)
        self.assertEqual(
            [item["status"] for item in result["results"]],
            ["verified", "approximate", "not_found"],
        )
        verify_result_schema = self.contract["$defs"]["verify_result"]
        for index, item in enumerate(result["results"]):
            self.assert_required_keys(item, verify_result_schema)
            self.assertEqual(item["index"], index)
        # not_found carries no candidates; verified keeps the exact original.
        self.assertEqual(result["results"][2]["matches"], [])
        self.assertEqual(result["results"][2]["total"], 0)
        verified_match = result["results"][0]["matches"][0]
        self.assert_required_keys(verified_match, self.contract["$defs"]["match"])
        self.assertEqual(verified_match["matched_text"], CALIBRATED_QUOTE)

    def test_verify_quotes_scopes_to_a_single_source(self) -> None:
        scoped = self.service.verify_quotes(
            [CALIBRATED_QUOTE],
            source_file_id=PDF_SOURCE_ID,
            source_type="pdf",
        )
        self.assertEqual(
            scoped["results"][0]["matches"][0]["source_file_id"],
            PDF_SOURCE_ID,
        )
        with self.assertRaises(SourceNotFound):
            self.service.verify_quotes(
                [CALIBRATED_QUOTE],
                source_file_id="unknown-source",
            )

    def test_diff_quote_reports_identical_difference_and_absence(self) -> None:
        output_schema = self.tools["diff_quote"]["outputSchema"]

        identical = self.service.diff_quote(
            CALIBRATED_QUOTE,
            source_file_id=PDF_SOURCE_ID,
            mode="exact",
        )
        self.assert_required_keys(identical, output_schema)
        self.assertEqual(identical["status"], "identical")
        self.assertEqual(identical["similarity"], 1.0)
        self.assertTrue(all(segment["op"] == "equal" for segment in identical["diff"]))
        self.assert_required_keys(
            identical["stats"], self.contract["$defs"]["diff_stats"]
        )
        self.assertEqual(identical["stats"]["added"], 0)
        self.assertEqual(identical["stats"]["missing"], 0)
        self.assertEqual(identical["stats"]["changed_quote"], 0)

        changed = self.service.diff_quote(
            CALIBRATED_QUOTE.replace("必须", "必需"), mode="fuzzy"
        )
        self.assertEqual(changed["status"], "different")
        self.assertIsNotNone(changed["match"])
        change_segment = next(
            segment for segment in changed["diff"] if segment["op"] == "changed"
        )
        self.assertEqual(change_segment["quote"], "需")
        self.assertEqual(change_segment["source"], "须")
        self.assertEqual(changed["stats"]["changed_quote"], 1)
        self.assertEqual(changed["stats"]["changed_source"], 1)

        added = self.service.diff_quote(CALIBRATED_QUOTE + "误", mode="fuzzy")
        self.assertEqual(added["status"], "different")
        self.assertTrue(any(segment["op"] == "added" for segment in added["diff"]))
        self.assertEqual(added["stats"]["added"], 1)

        missing = self.service.diff_quote(MISSING_QUOTE, mode="exact")
        self.assert_required_keys(missing, output_schema)
        self.assertEqual(missing["status"], "not_found")
        self.assertIsNone(missing["match"])
        self.assertIsNone(missing["similarity"])
        self.assertIsNone(missing["stats"])
        self.assertEqual(missing["diff"], [])

    def test_service_validates_mcp_boundaries(self) -> None:
        with self.assertRaisesRegex(ValueError, "quotes"):
            self.service.verify_quotes([])
        with self.assertRaisesRegex(ValueError, "quotes"):
            self.service.verify_quotes(["占位句"] * 51)
        with self.assertRaisesRegex(ValueError, "quotes"):
            self.service.verify_quotes("不是数组")
        with self.assertRaisesRegex(ValueError, "非空"):
            self.service.verify_quotes([CALIBRATED_QUOTE, ""])
        with self.assertRaisesRegex(ValueError, "matches_per_quote"):
            self.service.verify_quotes([CALIBRATED_QUOTE], matches_per_quote=6)
        with self.assertRaisesRegex(ValueError, "非空"):
            self.service.diff_quote("  ")
        with self.assertRaisesRegex(ValueError, "mode"):
            self.service.diff_quote(CALIBRATED_QUOTE, mode="unknown")
        with self.assertRaisesRegex(ValueError, "limit"):
            self.service.list_documents(limit=0)
        with self.assertRaisesRegex(ValueError, "非空"):
            self.service.locate_quote("  ")
        with self.assertRaisesRegex(ValueError, "mode"):
            self.service.locate_quote(CALIBRATED_QUOTE, mode="unknown")
        with self.assertRaisesRegex(ValueError, "mode"):
            self.service.locate_quote(CALIBRATED_QUOTE, mode=[])
        with self.assertRaisesRegex(ValueError, "source_type"):
            self.service.list_documents(source_type=[])
        with self.assertRaisesRegex(ValueError, "source_file_id"):
            self.service.locate_quote(
                CALIBRATED_QUOTE,
                source_file_id="invalid source",
            )
        with self.assertRaisesRegex(ValueError, "count"):
            self.service.read_document_window(PDF_SOURCE_ID, count=51)

    def test_search_engine_is_closed_when_search_fails(self) -> None:
        class FailingEngine:
            sources_by_id = {}

            def __init__(self) -> None:
                self.closed = False

            def search(self, *arguments) -> dict[str, object]:
                raise RuntimeError("search failed")

            def close(self) -> None:
                self.closed = True

        engine = FailingEngine()
        with mock.patch("src.me_finder.search.SearchEngine", return_value=engine):
            with self.assertRaisesRegex(RuntimeError, "search failed"):
                self.service.locate_quote(CALIBRATED_QUOTE)
        self.assertTrue(engine.closed)


if __name__ == "__main__":
    unittest.main()

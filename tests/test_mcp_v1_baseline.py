from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from src.me_finder import __version__
from src.me_finder.search import SearchEngine
from src.me_finder.structured_reader import get_document_window
from tests.mcp_v1_fixture import build_mcp_v1_fixture


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "docs" / "contracts" / "v0.4.4-mcp-v1-tools.json"
BASELINE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "mcp_v1_baseline.json"


class McpV1BaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "index.sqlite3"
        build_mcp_v1_fixture(self.database_path)
        self.baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_contract_freezes_three_read_only_tools(self) -> None:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["contract"], "mefinder.mcp.v1")
        self.assertEqual(contract["schema_version"], "1")
        self.assertEqual(contract["release"], "0.4.4")
        self.assertEqual(__version__, "0.4.4")

        tools = {tool["name"]: tool for tool in contract["tools"]}
        self.assertEqual(
            list(tools),
            ["list_documents", "locate_quote", "read_document_window"],
        )
        expected_annotations = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        }
        for tool in tools.values():
            self.assertEqual(tool["annotations"], expected_annotations)
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertFalse(tool["outputSchema"]["additionalProperties"])

        self.assertEqual(
            set(tools["locate_quote"]["inputSchema"]["properties"]),
            {"quote", "mode", "source_file_id", "source_type", "limit"},
        )
        self.assertEqual(
            set(tools["read_document_window"]["inputSchema"]["properties"]),
            {"source_file_id", "start", "count"},
        )
        serialized_contract = json.dumps(contract, ensure_ascii=False)
        self.assertNotIn("highlighted_html", serialized_contract)
        self.assertNotIn("absolute_path", serialized_contract)
        referenced_defs = set(
            re.findall(r'#/\$defs/([A-Za-z0-9_-]+)', serialized_contract)
        )
        self.assertLessEqual(referenced_defs, set(contract["$defs"]))
        self.assertEqual(
            contract["$defs"]["match"]["properties"]["match_type"]["enum"],
            [
                "exact",
                "normalized_exact",
                "space_insensitive",
                "punctuation_insensitive",
                "fuzzy",
            ],
        )

    def test_contract_freezes_error_codes_and_retryability(self) -> None:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        errors = {item["code"]: item["retryable"] for item in contract["errors"]}
        self.assertEqual(
            errors,
            {
                "invalid_input": False,
                "index_not_found": False,
                "index_unavailable": True,
                "source_not_found": False,
                "unsupported_source_type": False,
                "internal_error": False,
            },
        )

    def test_existing_search_engine_matches_recorded_mcp_baseline(self) -> None:
        engine = SearchEngine(self.database_path)
        try:
            for case in self.baseline["search_cases"]:
                with self.subTest(case=case["name"]):
                    result = engine.search(**case["request"])
                    expected = case["expected"]
                    self.assertEqual(result["total"], expected["total"])
                    if expected["total"] == 0:
                        self.assertEqual(result["results"], expected["results"])
                        continue
                    match = result["results"][0]
                    for field, value in expected.items():
                        if field == "total":
                            continue
                        self.assertEqual(match[field], value, field)
        finally:
            engine.close()

    def test_existing_reader_matches_recorded_mcp_baseline(self) -> None:
        for case in self.baseline["reader_cases"]:
            with self.subTest(case=case["name"]):
                result = get_document_window(self.database_path, **case["request"])
                expected = case["expected"]
                self.assertEqual(result["source"]["source_type"], expected["source_type"])
                self.assertEqual(result["total"], expected["total"])
                position_field = (
                    "pdf_page_index"
                    if expected["source_type"] == "pdf"
                    else "paragraph_index"
                )
                self.assertEqual(
                    [item[position_field] for item in result["items"]],
                    expected["positions"],
                )
                self.assertEqual(
                    [item["page_verified"] for item in result["items"]],
                    expected["page_verified"],
                )
                self.assertEqual(
                    [item["page_display"] for item in result["items"]],
                    expected["page_display"],
                )


if __name__ == "__main__":
    unittest.main()

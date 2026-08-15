from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from src.me_finder.application import LiteratureVerificationService
from src.me_finder.mcp_server import CONTRACT, TOOLS, _call_tool
from tests.mcp_v1_fixture import (
    CALIBRATED_QUOTE,
    CROSS_PAGE_QUERY,
    DUPLICATE_QUOTE,
    FUZZY_QUERY,
    MISSING_QUOTE,
    NFKC_QUERY,
    PDF_SOURCE_ID,
    SPREAD_BOTH_QUERY,
    SPREAD_LEFT_QUERY,
    SPREAD_RIGHT_QUERY,
    UNCALIBRATED_QUOTE,
    WORD_QUOTE,
    WORD_SOURCE_ID,
    build_mcp_v1_fixture,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUALITY_BASELINE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "mcp_v1_quality_baseline.json"


class MCPQualityMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / "runtime"
        build_mcp_v1_fixture(
            self.runtime_root / "data" / "index.sqlite3",
            include_quality_cases=True,
        )
        self.service = LiteratureVerificationService(lambda: self.runtime_root)
        self.tool_schemas = {tool.name: tool.output_schema for tool in TOOLS}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def call(self, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        result = _call_tool(self.service, tool_name, arguments)
        self.assertFalse(result.is_error, result.structured_content)
        Draft202012Validator(self.tool_schemas[tool_name]).validate(
            result.structured_content
        )
        return result.structured_content

    def test_exact_normalized_and_fuzzy_modes_use_public_match_types(self) -> None:
        cases = [
            (CALIBRATED_QUOTE, "exact", "exact"),
            ("技术 判断 必须 由 可复核 证据 支撑", "compact", "space_insensitive"),
            ("技术判断，必须由可复核证据支撑", "punctuation", "punctuation_insensitive"),
            (NFKC_QUERY, "exact", "normalized_exact"),
            (FUZZY_QUERY, "fuzzy", "fuzzy"),
        ]
        for quote, mode, expected_type in cases:
            with self.subTest(mode=mode, expected_type=expected_type):
                result = self.call(
                    "locate_quote",
                    {"quote": quote, "mode": mode, "limit": 20},
                )
                self.assertEqual(result["total"], 1)
                match = result["matches"][0]
                self.assertEqual(match["match_type"], expected_type)
                if expected_type == "fuzzy":
                    self.assertGreaterEqual(match["match_score"], 0.58)
                    self.assertLess(match["match_score"], 1.0)

    def test_duplicate_candidates_source_scope_and_no_result_stay_explicit(self) -> None:
        ambiguous = self.call(
            "locate_quote",
            {"quote": DUPLICATE_QUOTE, "mode": "exact", "limit": 20},
        )
        self.assertEqual(ambiguous["total"], 2)
        self.assertEqual(
            {item["source_file_id"] for item in ambiguous["matches"]},
            {PDF_SOURCE_ID, WORD_SOURCE_ID},
        )
        self.assertEqual(
            len({item["paragraph_id"] for item in ambiguous["matches"]}),
            2,
        )

        scoped = self.call(
            "locate_quote",
            {
                "quote": DUPLICATE_QUOTE,
                "mode": "exact",
                "source_file_id": PDF_SOURCE_ID,
                "limit": 20,
            },
        )
        self.assertEqual(scoped["total"], 1)
        self.assertEqual(scoped["matches"][0]["source_file_id"], PDF_SOURCE_ID)

        missing = self.call(
            "locate_quote",
            {"quote": MISSING_QUOTE, "mode": "exact"},
        )
        self.assertEqual(missing["total"], 0)
        self.assertEqual(missing["matches"], [])

    def test_cross_page_and_spread_hits_keep_physical_and_citation_evidence(self) -> None:
        cross = self.call(
            "locate_quote",
            {"quote": CROSS_PAGE_QUERY, "mode": "exact"},
        )["matches"][0]
        self.assertEqual(
            cross["physical_page"],
            {
                "start_index": 2,
                "end_index": 3,
                "start_label": "3",
                "end_label": "4",
            },
        )
        self.assertEqual(
            cross["citation_page"],
            {"start": "39", "end": "40", "status": "calibrated"},
        )

        for quote, expected_start, expected_end in (
            (SPREAD_LEFT_QUERY, "41", "41"),
            (SPREAD_RIGHT_QUERY, "42", "42"),
            (SPREAD_BOTH_QUERY, "41", "42"),
        ):
            with self.subTest(quote=quote):
                spread = self.call(
                    "locate_quote",
                    {"quote": quote, "mode": "exact"},
                )["matches"][0]
                self.assertEqual(spread["physical_page"]["start_index"], 4)
                self.assertEqual(
                    spread["citation_page"],
                    {
                        "start": expected_start,
                        "end": expected_end,
                        "status": "calibrated",
                    },
                )

    def test_page_statuses_distinguish_calibrated_physical_only_and_word(self) -> None:
        cases = [
            (
                CALIBRATED_QUOTE,
                PDF_SOURCE_ID,
                {"start": "38", "end": "38", "status": "calibrated"},
                0,
            ),
            (
                UNCALIBRATED_QUOTE,
                PDF_SOURCE_ID,
                {"start": None, "end": None, "status": "uncalibrated"},
                1,
            ),
            (
                WORD_QUOTE,
                WORD_SOURCE_ID,
                {"start": "7", "end": "7", "status": "verified"},
                None,
            ),
        ]
        for quote, source_id, citation_page, physical_index in cases:
            with self.subTest(quote=quote):
                match = self.call(
                    "locate_quote",
                    {
                        "quote": quote,
                        "mode": "exact",
                        "source_file_id": source_id,
                    },
                )["matches"][0]
                self.assertEqual(match["citation_page"], citation_page)
                self.assertEqual(
                    match["physical_page"]["start_index"],
                    physical_index,
                )

    def test_search_evidence_is_traceable_through_reader_cursors(self) -> None:
        for quote, count in (
            (CROSS_PAGE_QUERY, 2),
            (SPREAD_RIGHT_QUERY, 1),
            (DUPLICATE_QUOTE, 1),
        ):
            with self.subTest(quote=quote):
                match = self.call(
                    "locate_quote",
                    {
                        "quote": quote,
                        "mode": "exact",
                        "source_file_id": PDF_SOURCE_ID,
                    },
                )["matches"][0]
                window = self.call(
                    "read_document_window",
                    {
                        "source_file_id": match["source_file_id"],
                        "start": match["reader"]["start"],
                        "count": count,
                    },
                )
                self.assertEqual(
                    window["source"]["source_file_id"],
                    match["source_file_id"],
                )
                self.assertEqual(window["items"][0]["position"], match["reader"]["start"])
                reader_text = "\n".join(item["text"] for item in window["items"])
                self.assertIn(match["matched_text"], reader_text)

    def test_server_instructions_state_page_ambiguity_and_evidence_limits(self) -> None:
        instructions = CONTRACT["server"]["instructions"]
        self.assertLessEqual(len(instructions), 512)
        self.assertIn("不得把 PDF 物理页称为正式引用页", instructions)
        self.assertIn("不得隐藏多候选歧义", instructions)
        self.assertIn("搜索命中只证明文本在本地索引中出现", instructions)

        descriptions = "\n".join(tool["description"] for tool in CONTRACT["tools"])
        self.assertIn("稳定 source_file_id", descriptions)
        self.assertIn("物理页和引用页状态", descriptions)
        self.assertIn("不替调用方作最终语义裁决", descriptions)

    def test_missing_and_temporarily_unavailable_indexes_are_distinct(self) -> None:
        missing_service = LiteratureVerificationService(
            lambda: Path(self.temp_dir.name) / "missing-runtime"
        )
        missing = _call_tool(missing_service, "list_documents", {})
        self.assertEqual(
            missing.structured_content["error"]["code"],
            "index_not_found",
        )

        class UnavailableService:
            def list_documents(self, **_arguments: object) -> dict[str, object]:
                raise sqlite3.OperationalError("locked")

        unavailable = _call_tool(UnavailableService(), "list_documents", {})
        self.assertEqual(
            unavailable.structured_content["error"]["code"],
            "index_unavailable",
        )
        self.assertTrue(unavailable.structured_content["error"]["retryable"])

    def test_tool_content_is_concise_and_does_not_duplicate_evidence(self) -> None:
        result = _call_tool(
            self.service,
            "locate_quote",
            {"quote": CALIBRATED_QUOTE, "mode": "exact"},
        )
        self.assertEqual(len(result.content), 1)
        self.assertNotIn(CALIBRATED_QUOTE, result.content[0].text)
        self.assertLess(
            len(result.content[0].text.encode("utf-8")),
            len(json.dumps(result.structured_content, ensure_ascii=False).encode("utf-8")),
        )

    def test_recorded_model_context_result_sizes_and_workflow_calls(self) -> None:
        baseline = json.loads(QUALITY_BASELINE_PATH.read_text(encoding="utf-8"))
        compact_json = lambda value: json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        instructions = CONTRACT["server"]["instructions"]
        advertised_context = {
            "instructions": instructions,
            "tools": [
                tool.model_dump(by_alias=True, exclude_none=True) for tool in TOOLS
            ],
        }
        self.assertEqual(
            len(instructions),
            baseline["advertised_context"]["instructions_characters"],
        )
        self.assertEqual(
            len(instructions.encode("utf-8")),
            baseline["advertised_context"]["instructions_utf8_bytes"],
        )
        self.assertEqual(
            len(compact_json(advertised_context)),
            baseline["advertised_context"]["utf8_bytes"],
        )

        measured = {}
        for name, expected in baseline["responses"].items():
            result = _call_tool(
                self.service,
                expected["tool"],
                expected["arguments"],
            )
            self.assertFalse(result.is_error, name)
            measured[name] = {
                "structured_content_utf8_bytes": len(
                    compact_json(result.structured_content)
                ),
                "content_utf8_bytes": sum(
                    len(item.text.encode("utf-8")) for item in result.content
                ),
            }
            self.assertEqual(
                measured[name],
                {
                    "structured_content_utf8_bytes": expected[
                        "structured_content_utf8_bytes"
                    ],
                    "content_utf8_bytes": expected["content_utf8_bytes"],
                },
                name,
            )

        for name, workflow in baseline["workflows"].items():
            self.assertEqual(
                len(workflow["responses"]),
                workflow["call_count"],
                name,
            )
            result_bytes = sum(
                measured[response]["structured_content_utf8_bytes"]
                + measured[response]["content_utf8_bytes"]
                for response in workflow["responses"]
            )
            self.assertEqual(result_bytes, workflow["result_utf8_bytes"], name)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.metadata
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import anyio
from jsonschema import Draft202012Validator
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from src.me_finder.mcp_server import (
    CONTRACT,
    _call_tool,
)
from src.me_finder.structured_reader import UnsupportedSourceType
from tests.mcp_v1_fixture import (
    CALIBRATED_QUOTE,
    PDF_SOURCE_ID,
    build_mcp_v1_fixture,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MCPServerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / "runtime"
        build_mcp_v1_fixture(self.runtime_root / "data" / "index.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def server_parameters(
        self,
        *,
        runtime_root: Path | None = None,
        code: str | None = None,
    ) -> StdioServerParameters:
        if code is None:
            args = [
                "-m",
                "src.me_finder.mcp_server",
                "--runtime-root",
                str(runtime_root or self.runtime_root),
            ]
        else:
            args = ["-c", code]
        return StdioServerParameters(
            command=sys.executable,
            args=args,
            cwd=PROJECT_ROOT,
            env={"PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        )

    def test_initialize_tools_list_and_all_tool_calls(self) -> None:
        async def scenario() -> None:
            error_path = Path(self.temp_dir.name) / "protocol-stderr.log"
            with error_path.open("w+", encoding="utf-8") as errlog:
                started = time.monotonic()
                async with stdio_client(
                    self.server_parameters(),
                    errlog=errlog,
                ) as streams:
                    async with ClientSession(*streams) as session:
                        initialized = await session.initialize()
                        self.assertLess(time.monotonic() - started, 10)
                        self.assertEqual(initialized.server_info.name, "mefinder")
                        self.assertEqual(initialized.server_info.version, "0.5.0")
                        self.assertEqual(
                            initialized.instructions,
                            CONTRACT["server"]["instructions"],
                        )

                        listed = await session.list_tools()
                        contract_tools = {
                            item["name"]: item for item in CONTRACT["tools"]
                        }
                        expected_defs = {
                            "list_documents": {"document", "nullable_string"},
                            "locate_quote": {
                                "citation_page",
                                "context_item",
                                "match",
                                "nullable_nonnegative_integer",
                                "nullable_string",
                                "page_mapping",
                                "physical_page",
                                "reader_cursor",
                            },
                            "read_document_window": {
                                "citation_formats",
                                "citation_page",
                                "nullable_nonnegative_integer",
                                "nullable_string",
                                "page_mapping",
                                "physical_page",
                                "reader_item",
                                "reader_source",
                            },
                            "verify_quotes": {
                                "citation_page",
                                "context_item",
                                "match",
                                "nullable_nonnegative_integer",
                                "nullable_string",
                                "page_mapping",
                                "physical_page",
                                "reader_cursor",
                                "verify_result",
                            },
                            "diff_quote": {
                                "citation_page",
                                "context_item",
                                "diff_segment",
                                "diff_stats",
                                "match",
                                "nullable_nonnegative_integer",
                                "nullable_string",
                                "page_mapping",
                                "physical_page",
                                "reader_cursor",
                            },
                            "read_bibliographic_pages": {
                                "bibliographic_page",
                                "citation_page",
                                "nullable_nonnegative_integer",
                                "nullable_string",
                                "physical_page",
                                "reader_source",
                            },
                        }
                        self.assertEqual(
                            [item.name for item in listed.tools],
                            list(contract_tools),
                        )
                        for tool in listed.tools:
                            expected = contract_tools[tool.name]
                            self.assertEqual(tool.title, expected["title"])
                            self.assertEqual(tool.description, expected["description"])
                            self.assertEqual(
                                tool.input_schema,
                                expected["inputSchema"],
                            )
                            self.assertEqual(
                                tool.annotations.model_dump(
                                    by_alias=True,
                                    exclude_none=True,
                                ),
                                expected["annotations"],
                            )
                            self.assertEqual(
                                set(tool.output_schema["$defs"]),
                                expected_defs[tool.name],
                            )

                        documents = await session.call_tool("list_documents", {})
                        located = await session.call_tool(
                            "locate_quote",
                            {
                                "quote": CALIBRATED_QUOTE,
                                "mode": "exact",
                                "source_file_id": PDF_SOURCE_ID,
                            },
                        )
                        window = await session.call_tool(
                            "read_document_window",
                            {
                                "source_file_id": PDF_SOURCE_ID,
                                "count": 2,
                            },
                        )
                        results = {
                            "list_documents": documents,
                            "locate_quote": located,
                            "read_document_window": window,
                        }
                        for name, result in results.items():
                            self.assertFalse(result.is_error)
                            self.assertEqual(len(result.content), 1)
                            tool = next(
                                item for item in listed.tools if item.name == name
                            )
                            Draft202012Validator(tool.output_schema).validate(
                                result.structured_content
                            )
                        self.assertEqual(documents.structured_content["total"], 2)
                        self.assertEqual(located.structured_content["total"], 1)
                        self.assertEqual(len(window.structured_content["items"]), 2)
                        self.assertNotIn(CALIBRATED_QUOTE, located.content[0].text)
                errlog.seek(0)
                self.assertEqual(errlog.read(), "")

        anyio.run(scenario)

    def test_missing_index_initializes_and_returns_known_error(self) -> None:
        async def scenario() -> None:
            missing_root = Path(self.temp_dir.name) / "missing-runtime"
            error_path = Path(self.temp_dir.name) / "missing-stderr.log"
            with error_path.open("w+", encoding="utf-8") as errlog:
                async with stdio_client(
                    self.server_parameters(runtime_root=missing_root),
                    errlog=errlog,
                ) as streams:
                    async with ClientSession(*streams) as session:
                        initialized = await session.initialize()
                        self.assertEqual(initialized.server_info.name, "mefinder")
                        result = await session.call_tool("list_documents", {})
                        self.assertTrue(result.is_error)
                        Draft202012Validator(CONTRACT["errorSchema"]).validate(
                            result.structured_content
                        )
                        self.assertEqual(
                            result.structured_content["error"]["code"],
                            "index_not_found",
                        )

        anyio.run(scenario)

    def test_invalid_input_and_missing_source_are_distinct_errors(self) -> None:
        async def scenario() -> None:
            error_path = Path(self.temp_dir.name) / "errors-stderr.log"
            with error_path.open("w+", encoding="utf-8") as errlog:
                async with stdio_client(
                    self.server_parameters(),
                    errlog=errlog,
                ) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        invalid = await session.call_tool("locate_quote", {})
                        missing = await session.call_tool(
                            "locate_quote",
                            {
                                "quote": CALIBRATED_QUOTE,
                                "source_file_id": "missing-source",
                            },
                        )
                        self.assertEqual(
                            invalid.structured_content["error"]["code"],
                            "invalid_input",
                        )
                        self.assertEqual(
                            missing.structured_content["error"]["code"],
                            "source_not_found",
                        )

        anyio.run(scenario)

    def test_stdio_keeps_handler_prints_out_of_protocol_frames(self) -> None:
        code = """
from src.me_finder.mcp_server import create_server, run_stdio_server

class NoisyService:
    def list_documents(self, **arguments):
        print("handler-noise")
        return {
            "schema_version": "1",
            "total": 0,
            "has_more": False,
            "documents": [],
        }

run_stdio_server(create_server(NoisyService()))
"""

        async def scenario() -> None:
            error_path = Path(self.temp_dir.name) / "noise-stderr.log"
            with error_path.open("w+", encoding="utf-8") as errlog:
                async with stdio_client(
                    self.server_parameters(code=code),
                    errlog=errlog,
                ) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        result = await session.call_tool("list_documents", {})
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content["total"], 0)
                errlog.seek(0)
                self.assertIn("handler-noise", errlog.read())

        anyio.run(scenario)


class MCPServerErrorMappingTests(unittest.TestCase):
    class FailingService:
        def __init__(self, error: Exception) -> None:
            self.error = error

        def list_documents(self, **arguments) -> dict[str, object]:
            raise self.error

    def error_code(self, error: Exception) -> str:
        result = _call_tool(self.FailingService(error), "list_documents", {})
        self.assertTrue(result.is_error)
        Draft202012Validator(CONTRACT["errorSchema"]).validate(
            result.structured_content
        )
        return result.structured_content["error"]["code"]

    def test_known_runtime_errors_are_mapped(self) -> None:
        self.assertEqual(
            self.error_code(UnsupportedSourceType("unsupported")),
            "unsupported_source_type",
        )
        self.assertEqual(
            self.error_code(sqlite3.OperationalError("locked")),
            "index_unavailable",
        )

    def test_unknown_error_is_logged_and_sanitized(self) -> None:
        with mock.patch(
            "src.me_finder.mcp_server.LOGGER.exception"
        ) as logged:
            result = _call_tool(
                self.FailingService(RuntimeError("private failure detail")),
                "list_documents",
                {},
            )
        self.assertEqual(
            result.structured_content["error"]["code"],
            "internal_error",
        )
        self.assertNotIn("private failure detail", result.content[0].text)
        logged.assert_called_once()

    def test_sdk_dependency_is_pinned_for_both_release_targets(self) -> None:
        self.assertEqual(importlib.metadata.version("mcp"), "2.0.0")
        for name in ("requirements-macos.txt", "requirements-windows.txt"):
            requirements = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            self.assertIn("mcp==2.0.0", requirements.splitlines())


if __name__ == "__main__":
    unittest.main()

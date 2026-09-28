"""v0.5.8 read-only MCP structure queries: page mapping, sections, groups, context."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from src.me_finder import page_mapping_overview
from src.me_finder.application import LiteratureVerificationService
from src.me_finder.application.document_sections import build_sections
from src.me_finder.mcp_server import TOOLS, _call_tool
from tests.mcp_v1_fixture import (
    DUPLICATE_QUOTE,
    NFKC_TEXT,
    PARALLEL_SOURCE_ID,
    PDF_SOURCE_ID,
    WORD_QUOTE,
    WORD_SOURCE_ID,
    add_mcp_parallel_fixture,
    build_mcp_v1_fixture,
)


def _output_validator(name: str) -> Draft202012Validator:
    tool = next(item for item in TOOLS if item.name == name)
    return Draft202012Validator(tool.output_schema)


class _FixtureCase(unittest.TestCase):
    include_quality_cases = False

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name)
        self.index_path = self.runtime_root / "data" / "index.sqlite3"
        build_mcp_v1_fixture(
            self.index_path, include_quality_cases=self.include_quality_cases
        )
        self.service = LiteratureVerificationService(lambda: self.runtime_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        result = _call_tool(self.service, name, arguments)
        self.assertFalse(result.is_error, result.structured_content)
        _output_validator(name).validate(result.structured_content)
        return result.structured_content


class DescribePageMappingTests(_FixtureCase):
    def test_pdf_runs_separate_calibrated_and_uncalibrated_pages(self) -> None:
        result = self.call("describe_page_mapping", {"source_file_id": PDF_SOURCE_ID})

        self.assertEqual(result["unit"], "pdf_page")
        self.assertEqual(result["source_format"], "pdf")
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual((result["calibrated_units"], result["total_units"]), (1, 2))
        calibrated, uncalibrated = result["segments"]
        self.assertEqual(
            (calibrated["start"], calibrated["end"], calibrated["status"]),
            (0, 0, "calibrated"),
        )
        # Printed page 38 sits on PDF page 1 (1-based): printed = pdf + 37.
        self.assertEqual(calibrated["offset"], 37)
        self.assertEqual(calibrated["method"], "manual_segment")
        self.assertEqual(uncalibrated["status"], "uncalibrated")
        self.assertIsNone(uncalibrated["citation_page_start"])
        self.assertIsNone(uncalibrated["offset"])

    def test_word_pages_are_verified_without_pdf_offsets(self) -> None:
        result = self.call("describe_page_mapping", {"source_file_id": WORD_SOURCE_ID})

        self.assertEqual(result["unit"], "word_paragraph")
        self.assertEqual(result["source_format"], "docx")
        self.assertEqual(result["coverage"], "full")
        self.assertIsNone(result["mapping_record"])
        [segment] = result["segments"]
        self.assertEqual(segment["status"], "verified")
        self.assertEqual(
            (segment["citation_page_start"], segment["citation_page_end"]), ("7", "8")
        )
        self.assertIsNone(segment["offset"])

    def test_missing_source_is_a_known_error(self) -> None:
        result = _call_tool(
            self.service, "describe_page_mapping", {"source_file_id": "no-such-doc"}
        )
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "source_not_found")


class PageMappingRunTests(unittest.TestCase):
    """Run splitting over synthetic evidence, without building an index."""

    def describe(self, units: list[dict[str, object]]) -> dict[str, object]:
        evidence = {
            "source": {"source_file_id": "doc", "source_type": "pdf",
                       "file_name": "doc.pdf", "payload": {}},
            "units": units,
            "mapping_record": None,
        }
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle, mock.patch.object(
            page_mapping_overview, "read_page_mapping_evidence", return_value=evidence
        ):
            return page_mapping_overview.describe_page_mapping(Path(handle.name), "doc")

    @staticmethod
    def page(position: int, start: str, end: str | None = None) -> dict[str, object]:
        return {
            "position": position,
            "citation_page_start": start,
            "citation_page_end": end or start,
            "page_mapping_method": "manual_segment",
        }

    def test_offset_change_mid_book_opens_a_new_run(self) -> None:
        result = self.describe(
            [self.page(0, "i"), self.page(1, "ii"), self.page(2, "1"),
             self.page(3, "2"), self.page(4, "10"), self.page(5, "11")]
        )
        self.assertEqual(
            [(item["start"], item["end"], item["number_style"], item["offset"])
             for item in result["segments"]],
            [(0, 1, "roman", 0), (2, 3, "arabic", -2), (4, 5, "arabic", 5)],
        )
        self.assertEqual(result["coverage"], "full")

    def test_spread_pages_keep_one_run_without_a_fake_offset(self) -> None:
        result = self.describe(
            [self.page(0, "2", "3"), self.page(1, "4", "5"), self.page(2, "6", "7")]
        )
        [segment] = result["segments"]
        self.assertEqual((segment["start"], segment["end"]), (0, 2))
        self.assertEqual(
            (segment["citation_page_start"], segment["citation_page_end"]), ("2", "7")
        )
        self.assertIsNone(segment["offset"])

    def test_unmapped_pages_never_receive_citation_pages(self) -> None:
        result = self.describe(
            [{"position": 0, "page_mapping_method": "uncalibrated"},
             {"position": 1, "citation_page": "5", "page_mapping_method": "uncalibrated"}]
        )
        [segment] = result["segments"]
        self.assertEqual(segment["status"], "uncalibrated")
        self.assertIsNone(segment["citation_page_start"])
        self.assertEqual(result["coverage"], "none")


class _HeadedWordCase(_FixtureCase):
    """Word fixture with a chapter at paragraph 1 and a subsection at 3."""

    include_quality_cases = True

    def setUp(self) -> None:
        super().setUp()
        connection = sqlite3.connect(self.index_path)
        try:
            for index, style in ((1, "Heading 1"), (3, "Heading 2")):
                connection.execute(
                    "UPDATE paragraphs SET payload_json = "
                    "json_set(payload_json, '$.style_name', ?) "
                    "WHERE paragraph_id = ?",
                    (style, f"{WORD_SOURCE_ID}-P{index:06d}"),
                )
            connection.commit()
        finally:
            connection.close()

    def error_code(self, name: str, arguments: dict[str, object]) -> str:
        result = _call_tool(self.service, name, arguments)
        self.assertTrue(result.is_error)
        return str(result.structured_content["error"]["code"])


class SectionReadingTests(_HeadedWordCase):
    def test_sections_carry_nested_ranges(self) -> None:
        result = self.call("list_sections", {"source_file_id": WORD_SOURCE_ID})
        last = result["total"] - 1
        self.assertEqual(
            [(item["section_index"], item["level"], item["start"], item["end"])
             for item in result["sections"]],
            [(0, 1, 1, last), (1, 2, 3, last)],
        )
        self.assertFalse(result["sections_truncated"])

    def test_document_without_headings_lists_no_sections(self) -> None:
        result = _call_tool(
            self.service, "list_sections", {"source_file_id": PDF_SOURCE_ID}
        )
        self.assertEqual(result.structured_content["sections"], [])
        self.assertIn("read_document_window", result.content[0].text)

    def test_section_window_stays_inside_the_section_and_pages_on(self) -> None:
        first = self.call(
            "read_document_window",
            {"source_file_id": WORD_SOURCE_ID, "section_index": 1, "count": 1},
        )
        self.assertEqual([item["position"] for item in first["items"]], [3])
        section = first["section"]
        self.assertEqual(section["next_start"], 4 if section["end"] > 3 else None)

        whole = self.call(
            "read_document_window",
            {"source_file_id": WORD_SOURCE_ID, "section_index": 0, "count": 50},
        )
        positions = [item["position"] for item in whole["items"]]
        self.assertEqual(positions[0], 1)
        self.assertLessEqual(positions[-1], whole["section"]["end"])
        self.assertIsNone(whole["section"]["next_start"])

    def test_plain_window_is_unchanged_without_section(self) -> None:
        result = self.call(
            "read_document_window", {"source_file_id": WORD_SOURCE_ID, "count": 2}
        )
        self.assertNotIn("section", result)
        self.assertEqual([item["position"] for item in result["items"]], [0, 1])

    def test_out_of_section_start_and_unknown_section_are_invalid(self) -> None:
        self.assertEqual(
            self.error_code(
                "read_document_window",
                {"source_file_id": WORD_SOURCE_ID, "section_index": 1, "start": 0},
            ),
            "invalid_input",
        )
        self.assertEqual(
            self.error_code(
                "read_document_window",
                {"source_file_id": WORD_SOURCE_ID, "section_index": 9},
            ),
            "invalid_input",
        )


class SectionBoundaryTests(unittest.TestCase):
    def test_pdf_section_ends_on_the_page_holding_the_next_heading(self) -> None:
        entries = [
            {"level": 1, "title": "第一章", "item_index": 3, "char_start": 5},
            {"level": 2, "title": "第一节", "item_index": 4, "char_start": 0},
            {"level": 2, "title": "第二节", "item_index": 8, "char_start": 40},
            {"level": 1, "title": "第二章", "item_index": 12, "char_start": 2},
        ]
        sections = build_sections(entries, total_units=20, is_pdf=True)
        self.assertEqual(
            [(item["start"], item["end"]) for item in sections],
            [(3, 12), (4, 8), (8, 12), (12, 19)],
        )

    def test_misdetected_long_heading_title_is_bounded(self) -> None:
        [section] = build_sections(
            [{"level": 1, "title": "长" * 500, "item_index": 0}],
            total_units=1,
            is_pdf=False,
        )
        self.assertEqual(len(section["title"]), 200)
        self.assertTrue(section["title"].endswith("…"))


class QuoteSectionTests(_HeadedWordCase):
    def test_verified_matches_report_their_heading_path(self) -> None:
        result = self.call(
            "verify_quotes",
            {
                "quotes": [WORD_QUOTE, NFKC_TEXT, DUPLICATE_QUOTE],
                "mode": "exact",
                "source_file_id": WORD_SOURCE_ID,
            },
        )
        sections = [item["matches"][0]["section"] for item in result["results"]]
        before_first_heading, chapter, subsection = sections
        self.assertIsNone(before_first_heading)
        self.assertEqual(chapter["section_index"], 0)
        self.assertEqual(len(chapter["path"]), 1)
        self.assertEqual(subsection["section_index"], 1)
        self.assertEqual(len(subsection["path"]), 2)

    def test_locate_quote_output_is_unchanged(self) -> None:
        result = self.call(
            "locate_quote",
            {"quote": WORD_QUOTE, "mode": "exact", "source_file_id": WORD_SOURCE_ID},
        )
        self.assertNotIn("section", result["matches"][0])


class DocumentWorkTests(_FixtureCase):
    def setUp(self) -> None:
        super().setUp()
        add_mcp_parallel_fixture(self.index_path)

    def test_grouped_documents_list_their_aligned_versions(self) -> None:
        result = self.call("list_documents", {})
        works = {item["source_file_id"]: item["work"] for item in result["documents"]}

        self.assertIsNone(works[WORD_SOURCE_ID])
        base = works[PDF_SOURCE_ID]
        self.assertTrue(base["is_base"])
        [translation] = base["other_versions"]
        self.assertEqual(translation["source_file_id"], PARALLEL_SOURCE_ID)
        self.assertTrue(translation["aligned"])
        self.assertFalse(translation["is_base"])
        self.assertFalse(works[PARALLEL_SOURCE_ID]["is_base"])
        self.assertEqual(
            works[PARALLEL_SOURCE_ID]["document_group_id"], base["document_group_id"]
        )

    def test_listing_documents_never_writes_the_index(self) -> None:
        before = self.index_path.stat().st_mtime_ns
        self.call("list_documents", {})
        self.assertEqual(self.index_path.stat().st_mtime_ns, before)


class LegacyIndexWorkTests(_FixtureCase):
    def test_index_without_group_tables_is_not_migrated_by_reads(self) -> None:
        connection = sqlite3.connect(self.index_path)
        try:
            connection.execute("DROP TABLE document_group_members")
            connection.execute("DROP TABLE document_groups")
            connection.commit()
        finally:
            connection.close()

        result = self.call("list_documents", {})

        self.assertTrue(all(item["work"] is None for item in result["documents"]))
        connection = sqlite3.connect(self.index_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'document_group%'"
                )
            }
        finally:
            connection.close()
        self.assertEqual(tables, set())


if __name__ == "__main__":
    unittest.main()

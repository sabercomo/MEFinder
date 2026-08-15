from __future__ import annotations

import errno
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from src.me_finder import data_location, database
from src.me_finder.application import LiteratureVerificationService
from src.me_finder.application.search_service import SearchService
from src.me_finder.data_location import migrate_data_root, read_data_root
from src.me_finder.normalization import (
    compact_text,
    normalize_text,
    punctuationless_text,
)
from tests.mcp_v1_fixture import CALIBRATED_QUOTE, build_mcp_v1_fixture


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPLACEMENT_QUOTE = "索引替换完成后，新调用必须读取新快照"


def _build_replacement_index(path: Path) -> None:
    paragraph = {
        "paragraph_id": "replacement-P000000",
        "source_file_id": "replacement-source",
        "source_type": "word",
        "volume_id": "replacement-volume",
        "volume_number": None,
        "volume_display": "并发替换快照",
        "work_id": "replacement-work",
        "work_title": "并发替换快照",
        "document_title": "并发替换快照",
        "author_label": "测试作者",
        "paragraph_index": 0,
        "eligible_for_search": True,
        "text_raw": REPLACEMENT_QUOTE,
        "normalized_text": normalize_text(REPLACEMENT_QUOTE),
        "compact_text": compact_text(REPLACEMENT_QUOTE),
        "plain_text": punctuationless_text(REPLACEMENT_QUOTE),
        "page_display": "第 1 页",
        "page_source_type": "section_break_verified",
    }
    database.build_database(
        {
            "metadata": {},
            "source_files": [
                {
                    "source_file_id": "replacement-source",
                    "source_type": "word",
                    "file_name": "replacement.docx",
                    "document_title": "并发替换快照",
                }
            ],
            "volumes": [
                {
                    "volume_id": "replacement-volume",
                    "source_file_id": "replacement-source",
                    "source_type": "word",
                    "display_title": "并发替换快照",
                }
            ],
            "works": [
                {
                    "work_id": "replacement-work",
                    "volume_id": "replacement-volume",
                    "source_file_id": "replacement-source",
                    "source_type": "word",
                    "title": "并发替换快照",
                }
            ],
            "paragraphs": [paragraph],
        },
        path,
    )


class MCPConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.runtime = self.base / "current" / "runtime"
        self.index_path = self.runtime / "data" / "index.sqlite3"
        build_mcp_v1_fixture(self.index_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_running_stdio_server_releases_database_between_calls(self) -> None:
        replacement = self.base / "replacement.sqlite3"
        _build_replacement_index(replacement)

        async def scenario() -> None:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "src.me_finder.mcp_server",
                    "--runtime-root",
                    str(self.runtime),
                ],
                cwd=PROJECT_ROOT,
                env={"PYTHONPATH": os.environ.get("PYTHONPATH", "")},
            )
            async with stdio_client(parameters) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    old_result = await session.call_tool(
                        "locate_quote",
                        {"quote": CALIBRATED_QUOTE, "mode": "exact"},
                    )
                    self.assertEqual(old_result.structured_content["total"], 1)

                    replacement.replace(self.index_path)

                    old_after_replace = await session.call_tool(
                        "locate_quote",
                        {"quote": CALIBRATED_QUOTE, "mode": "exact"},
                    )
                    new_after_replace = await session.call_tool(
                        "locate_quote",
                        {"quote": REPLACEMENT_QUOTE, "mode": "exact"},
                    )
                    self.assertEqual(old_after_replace.structured_content["total"], 0)
                    self.assertEqual(new_after_replace.structured_content["total"], 1)

        anyio.run(scenario)

    def test_short_lived_search_allows_retrying_windows_style_replace(self) -> None:
        replacement = self.base / "replacement.sqlite3"
        _build_replacement_index(replacement)
        service = LiteratureVerificationService(lambda: self.runtime)
        search_started = threading.Event()
        release_search = threading.Event()
        replace_attempted = threading.Event()
        search_finished = threading.Event()
        search_results: list[dict[str, object]] = []
        errors: list[BaseException] = []
        original_execute = SearchService.execute
        original_replace = Path.replace

        def held_execute(engine, request):
            search_started.set()
            if not release_search.wait(timeout=5):
                raise TimeoutError("search release timed out")
            return original_execute(engine, request)

        def windows_guarded_replace(path: Path, target: Path):
            if path == replacement and Path(target) == self.index_path:
                replace_attempted.set()
                if not search_finished.is_set():
                    raise PermissionError(errno.EACCES, "database is in use")
            return original_replace(path, target)

        def run_search() -> None:
            try:
                search_results.append(
                    service.locate_quote(CALIBRATED_QUOTE, mode="exact")
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                search_finished.set()

        def run_replace() -> None:
            try:
                database._replace_database_file(
                    replacement,
                    self.index_path,
                    attempts=20,
                )
            except BaseException as exc:
                errors.append(exc)

        search_thread = threading.Thread(target=run_search)
        replace_thread = threading.Thread(target=run_replace)
        with mock.patch.object(SearchService, "execute", side_effect=held_execute), mock.patch.object(
            Path,
            "replace",
            new=windows_guarded_replace,
        ):
            search_thread.start()
            self.assertTrue(search_started.wait(timeout=2))
            replace_thread.start()
            self.assertTrue(replace_attempted.wait(timeout=2))
            release_search.set()
            search_thread.join(timeout=5)
            replace_thread.join(timeout=5)

        self.assertFalse(search_thread.is_alive())
        self.assertFalse(replace_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(search_results[0]["total"], 1)
        self.assertEqual(
            service.locate_quote(REPLACEMENT_QUOTE, mode="exact")["total"],
            1,
        )

    def test_calls_use_old_root_until_migration_atomically_switches_marker(self) -> None:
        current = self.base / "current"
        default = current
        target = self.base / "target" / "MEFinder"
        service = LiteratureVerificationService(
            lambda: read_data_root(default, fallback_root=current) / "runtime"
        )
        marker_ready = threading.Event()
        release_marker = threading.Event()
        migration_errors: list[BaseException] = []
        original_write_marker = data_location._write_root_marker

        def blocked_marker(marker: Path, new_root: Path) -> None:
            marker_ready.set()
            if not release_marker.wait(timeout=5):
                raise TimeoutError("marker release timed out")
            original_write_marker(marker, new_root)

        def run_migration() -> None:
            try:
                migrate_data_root(current, target, default)
            except BaseException as exc:
                migration_errors.append(exc)

        with mock.patch(
            "src.me_finder.data_location._write_root_marker",
            side_effect=blocked_marker,
        ):
            migration_thread = threading.Thread(target=run_migration)
            migration_thread.start()
            self.assertTrue(marker_ready.wait(timeout=5))
            during = service.locate_quote(CALIBRATED_QUOTE, mode="exact")
            release_marker.set()
            migration_thread.join(timeout=5)

        self.assertFalse(migration_thread.is_alive())
        self.assertEqual(migration_errors, [])
        after = service.locate_quote(CALIBRATED_QUOTE, mode="exact")
        self.assertEqual(during["total"], 1)
        self.assertEqual(after["total"], 1)
        self.assertEqual(read_data_root(default), target.resolve())


if __name__ == "__main__":
    unittest.main()

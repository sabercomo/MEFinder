from __future__ import annotations

import sqlite3
import unittest

from src.me_finder.application.document_query_service import (
    DocumentQueryUnavailable,
)
from src.me_finder.library_query_controller import LibraryQueryController


class FakeIndexRuntime:
    def metadata(self):
        return {"version": "0.4.3"}

    def catalog(self):
        return {
            "source_files": [{"source_file_id": "source-1"}],
            "volumes": [],
            "works": [],
        }


class FakeDocumentQueries:
    def __init__(self) -> None:
        self.calls = []
        self.detail = {"item": {"source_file_id": "source-1"}}
        self.failure = None

    def _result(self, name, active):
        self.calls.append((name, set(active)))
        if self.failure is not None:
            raise self.failure
        return {"view": name}

    def library_data(self, *, additional_active_source_ids=()):
        return self._result("full", additional_active_source_ids)

    def library_summary(self, *, additional_active_source_ids=()):
        return self._result("summary", additional_active_source_ids)

    def calibration_library_data(
        self, *, additional_active_source_ids=()
    ):
        return self._result("calibration", additional_active_source_ids)

    def library_detail(
        self,
        source_file_id,
        *,
        additional_active_source_ids=(),
    ):
        self.calls.append(
            (f"detail:{source_file_id}", set(additional_active_source_ids))
        )
        if self.failure is not None:
            raise self.failure
        return self.detail


class LibraryQueryControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queries = FakeDocumentQueries()
        self.controller = LibraryQueryController(
            self.queries,
            FakeIndexRuntime(),
            additional_active_source_ids=lambda: {"calibrating"},
        )

    def test_index_metadata_and_sources_are_runtime_snapshots(self) -> None:
        self.assertEqual(
            self.controller.index_metadata(),
            (200, {"version": "0.4.3"}),
        )
        self.assertEqual(
            self.controller.sources()[1]["source_files"][0][
                "source_file_id"
            ],
            "source-1",
        )

    def test_library_selects_full_or_summary_projection(self) -> None:
        self.assertEqual(self.controller.library(), (200, {"view": "full"}))
        self.assertEqual(
            self.controller.library("summary"),
            (200, {"view": "summary"}),
        )
        self.assertEqual(
            self.queries.calls,
            [
                ("full", {"calibrating"}),
                ("summary", {"calibrating"}),
            ],
        )

    def test_document_returns_detail_or_existing_not_found_contract(self) -> None:
        status, payload = self.controller.document("source-1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["item"]["source_file_id"], "source-1")

        self.queries.detail = None
        self.assertEqual(
            self.controller.document("missing"),
            (404, {"error": "文献不存在或已被移除。"}),
        )

    def test_calibration_library_uses_active_mapping_snapshot(self) -> None:
        self.assertEqual(
            self.controller.calibration_library(),
            (200, {"view": "calibration"}),
        )
        self.assertEqual(
            self.queries.calls[-1],
            ("calibration", {"calibrating"}),
        )

    def test_library_and_detail_keep_existing_storage_error_contracts(self) -> None:
        self.queries.failure = sqlite3.OperationalError("locked")
        self.assertEqual(
            self.controller.library(),
            (500, {"error": "文献库加载失败：locked"}),
        )
        self.assertEqual(
            self.controller.document("source-1"),
            (500, {"error": "文献详情加载失败：locked"}),
        )
        self.assertEqual(
            self.controller.calibration_library(),
            (500, {"error": "页码校准文献加载失败：locked"}),
        )

    def test_rebuilding_index_is_explicitly_unavailable(self) -> None:
        self.queries.failure = DocumentQueryUnavailable("索引正在重建。")

        self.assertEqual(
            self.controller.library(),
            (503, {"error": "索引正在重建。"}),
        )
        self.assertEqual(
            self.controller.document("source-1"),
            (503, {"error": "索引正在重建。"}),
        )
        self.assertEqual(
            self.controller.calibration_library(),
            (503, {"error": "索引正在重建。"}),
        )


if __name__ == "__main__":
    unittest.main()

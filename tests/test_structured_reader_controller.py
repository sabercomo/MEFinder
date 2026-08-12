from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from src.me_finder.structured_reader import (
    CitationPositionNotFound,
    InvalidCitationRange,
    InvalidPagination,
    InvalidSourceId,
    SourceNotFound,
    StructuredReaderError,
    UnsupportedSourceType,
)
from src.me_finder.structured_reader_controller import (
    StructuredReaderController,
)


class StructuredReaderControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database_path = Path("/runtime/data/index.sqlite3")
        self.ready = True
        self.calls: list[tuple[object, ...]] = []
        self.window_result: dict[str, object] = {"items": ["page"]}
        self.citation_result: dict[str, object] = {"citation": "result"}
        self.window_error: Exception | None = None
        self.citation_error: Exception | None = None
        self.logged: list[str] = []
        self.controller = StructuredReaderController(
            self._run_when_ready,
            get_window=self._get_window,
            get_citation=self._get_citation,
            log_exception=self.logged.append,
        )

    def _run_when_ready(self, operation):
        self.calls.append(("ready",))
        if not self.ready:
            return None
        return operation(self.database_path)

    def _get_window(
        self,
        database_path: Path,
        source_id: object,
        *,
        start: object,
        count: object,
    ) -> dict[str, object]:
        self.calls.append(
            ("window", database_path, source_id, start, count)
        )
        if self.window_error is not None:
            raise self.window_error
        return self.window_result

    def _get_citation(
        self,
        database_path: Path,
        source_id: object,
        *,
        start_anchor_id: object,
        end_anchor_id: object,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "citation",
                database_path,
                source_id,
                start_anchor_id,
                end_anchor_id,
            )
        )
        if self.citation_error is not None:
            raise self.citation_error
        return self.citation_result

    @staticmethod
    def _citation_payload() -> dict[str, object]:
        return {
            "source_id": "pdf-one",
            "start_anchor_id": "pdf-one-PAGE-000001",
            "end_anchor_id": "pdf-one-PAGE-000003",
        }

    def test_pages_preserves_defaults_and_passes_query_values_once(self) -> None:
        self.assertEqual(
            self.controller.pages({"source_id": ["pdf-one"]}),
            (200, self.window_result),
        )
        self.assertEqual(
            self.calls,
            [
                ("ready",),
                (
                    "window",
                    self.database_path,
                    "pdf-one",
                    "0",
                    "20",
                ),
            ],
        )

        self.calls.clear()
        self.assertEqual(
            self.controller.pages(
                {
                    "source_id": ["pdf-two"],
                    "start": ["12"],
                    "count": ["8"],
                }
            ),
            (200, self.window_result),
        )
        self.assertEqual(
            self.calls[-1],
            ("window", self.database_path, "pdf-two", "12", "8"),
        )

    def test_pages_rejects_missing_or_repeated_query_values(self) -> None:
        cases = [
            {},
            {"source_id": ["one", "two"]},
            {"source_id": ["one"], "start": ["0", "1"]},
            {"source_id": ["one"], "count": ["10", "20"]},
        ]
        for params in cases:
            with self.subTest(params=params):
                self.assertEqual(
                    self.controller.pages(params),
                    (
                        400,
                        {
                            "error": (
                                "source_id 必须提供一次，start 和 "
                                "count 最多各提供一次。"
                            )
                        },
                    ),
                )
        self.assertEqual(self.calls, [])

    def test_pages_keeps_validation_and_not_found_statuses(self) -> None:
        cases = [
            (InvalidPagination("count 无效"), 400),
            (InvalidSourceId("source_id 无效"), 400),
            (UnsupportedSourceType("类型不支持"), 400),
            (SourceNotFound("未找到文献"), 404),
        ]
        for error, expected_status in cases:
            with self.subTest(error=error):
                self.window_error = error
                self.assertEqual(
                    self.controller.pages({"source_id": ["pdf-one"]}),
                    (expected_status, {"error": str(error)}),
                )

    def test_pages_keeps_ready_and_storage_failure_contracts(self) -> None:
        self.ready = False
        self.assertEqual(
            self.controller.pages({"source_id": ["pdf-one"]}),
            (
                503,
                {
                    "error": (
                        "索引正在重建，请稍候再打开结构化阅读。"
                    )
                },
            ),
        )
        self.assertNotIn("window", [call[0] for call in self.calls])

        self.ready = True
        for error in (
            OSError("disk failed"),
            sqlite3.DatabaseError("locked"),
            StructuredReaderError("bad index"),
        ):
            with self.subTest(error=error):
                self.window_error = error
                self.logged.clear()
                self.assertEqual(
                    self.controller.pages({"source_id": ["pdf-one"]}),
                    (
                        500,
                        {
                            "error": (
                                "结构化阅读数据读取失败，请稍后重试。"
                            )
                        },
                    ),
                )
                self.assertEqual(
                    self.logged,
                    ["structured reader data request failed"],
                )

    def test_citation_rejects_non_object_missing_and_extra_fields(self) -> None:
        self.assertEqual(
            self.controller.citation([]),
            (400, {"error": "引文请求必须是 JSON 对象。"}),
        )
        self.assertEqual(
            self.controller.citation({"source_id": "pdf-one"}),
            (
                400,
                {
                    "error": (
                        "source_id、start_anchor_id 和 end_anchor_id "
                        "必须各提供一次。"
                    )
                },
            ),
        )
        payload = self._citation_payload()
        payload["relative_path"] = "/private/book.pdf"
        self.assertEqual(
            self.controller.citation(payload),
            (400, {"error": "引文请求包含不支持的字段。"}),
        )
        self.assertEqual(self.calls, [])

    def test_citation_passes_the_exact_selection_to_the_reader(self) -> None:
        payload = self._citation_payload()
        self.assertEqual(
            self.controller.citation(payload),
            (200, self.citation_result),
        )
        self.assertEqual(
            self.calls,
            [
                ("ready",),
                (
                    "citation",
                    self.database_path,
                    "pdf-one",
                    "pdf-one-PAGE-000001",
                    "pdf-one-PAGE-000003",
                ),
            ],
        )

    def test_citation_keeps_validation_and_not_found_statuses(self) -> None:
        cases = [
            (InvalidCitationRange("范围无效"), 400),
            (InvalidPagination("位置无效"), 400),
            (InvalidSourceId("source_id 无效"), 400),
            (UnsupportedSourceType("类型不支持"), 400),
            (CitationPositionNotFound("锚点不存在"), 404),
            (SourceNotFound("未找到文献"), 404),
        ]
        for error, expected_status in cases:
            with self.subTest(error=error):
                self.citation_error = error
                self.assertEqual(
                    self.controller.citation(self._citation_payload()),
                    (expected_status, {"error": str(error)}),
                )

    def test_citation_keeps_ready_and_storage_failure_contracts(self) -> None:
        self.ready = False
        self.assertEqual(
            self.controller.citation(self._citation_payload()),
            (
                503,
                {
                    "error": (
                        "索引正在重建，请稍候再生成引文。"
                    )
                },
            ),
        )
        self.assertNotIn("citation", [call[0] for call in self.calls])

        self.ready = True
        for error in (
            OSError("disk failed"),
            sqlite3.DatabaseError("locked"),
            StructuredReaderError("bad index"),
        ):
            with self.subTest(error=error):
                self.citation_error = error
                self.logged.clear()
                self.assertEqual(
                    self.controller.citation(self._citation_payload()),
                    (
                        500,
                        {
                            "error": (
                                "结构化阅读引文生成失败，"
                                "请稍后重试。"
                            )
                        },
                    ),
                )
                self.assertEqual(
                    self.logged,
                    ["structured reader citation request failed"],
                )

    def test_operation_dependencies_can_be_late_bound(self) -> None:
        current_window = {"operation": self._get_window}
        controller = StructuredReaderController(
            self._run_when_ready,
            get_window=lambda *args, **kwargs: current_window[
                "operation"
            ](*args, **kwargs),
            get_citation=self._get_citation,
            log_exception=self.logged.append,
        )

        replacement_result = {"items": ["replacement"]}

        def replacement(*_args, **_kwargs):
            return replacement_result

        current_window["operation"] = replacement
        self.assertEqual(
            controller.pages({"source_id": ["pdf-one"]}),
            (200, replacement_result),
        )


if __name__ == "__main__":
    unittest.main()

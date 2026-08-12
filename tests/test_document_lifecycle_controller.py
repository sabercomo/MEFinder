from __future__ import annotations

import unittest

from src.me_finder.application.document_deletion_coordinator import (
    BatchDeletionConflict,
    DocumentDeletionFailed,
    DocumentDeletionRejected,
)
from src.me_finder.document_lifecycle_controller import (
    DocumentLifecycleController,
)
from src.me_finder.mineru_api import MinerUError


class FakeDeletionCoordinator:
    def __init__(self) -> None:
        self.remove_calls: list[tuple[str, dict[str, object]]] = []
        self.batch_calls: list[tuple[list[object], dict[str, object]]] = []
        self.remove_error: Exception | None = None
        self.batch_error: Exception | None = None

    def remove(self, source_file_id: str, **options):
        self.remove_calls.append((source_file_id, dict(options)))
        if self.remove_error is not None:
            raise self.remove_error
        return {"source_file_id": source_file_id}

    def remove_many(self, source_file_ids, **options):
        self.batch_calls.append((list(source_file_ids), dict(options)))
        if self.batch_error is not None:
            raise self.batch_error
        return {"removed_source_ids": list(source_file_ids)}


class DocumentLifecycleControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.deletion = FakeDeletionCoordinator()
        self.controller = DocumentLifecycleController(self.deletion)

    def test_single_remove_preserves_options_and_change_event(self) -> None:
        self.assertEqual(
            self.controller.remove(
                {
                    "source_id": "pdf-one",
                    "delete_generated_artifacts": 0,
                    "delete_internal_copy": 1,
                }
            ),
            (
                200,
                {
                    "ok": True,
                    "result": {"source_file_id": "pdf-one"},
                    "event": "library_changed",
                },
            ),
        )
        self.assertEqual(
            self.deletion.remove_calls,
            [
                (
                    "pdf-one",
                    {
                        "delete_generated_artifacts": False,
                        "delete_internal_copy": True,
                    },
                )
            ],
        )

    def test_batch_remove_preserves_options_and_change_event(self) -> None:
        self.assertEqual(
            self.controller.remove_batch(
                {
                    "source_ids": ["pdf-one", "pdf-two"],
                    "delete_generated_artifacts": False,
                    "internal_copy_source_ids": ["pdf-two"],
                }
            ),
            (
                200,
                {
                    "ok": True,
                    "result": {
                        "removed_source_ids": ["pdf-one", "pdf-two"]
                    },
                    "event": "library_changed",
                },
            ),
        )
        self.assertEqual(
            self.deletion.batch_calls,
            [
                (
                    ["pdf-one", "pdf-two"],
                    {
                        "delete_generated_artifacts": False,
                        "internal_copy_source_ids": ["pdf-two"],
                    },
                )
            ],
        )

    def test_invalid_payloads_fail_before_deletion(self) -> None:
        for operation, payload in (
            (self.controller.remove, []),
            (self.controller.remove, {}),
            (self.controller.remove_batch, None),
            (self.controller.remove_batch, {"source_ids": []}),
            (self.controller.remove_batch, {"source_ids": "pdf-one"}),
        ):
            with self.subTest(operation=operation.__name__, payload=payload):
                self.assertEqual(
                    operation(payload),
                    (400, {"error": "invalid request"}),
                )
        self.assertEqual(self.deletion.remove_calls, [])
        self.assertEqual(self.deletion.batch_calls, [])

    def test_single_remove_maps_conflict_rejection_and_failure(self) -> None:
        cases = [
            (MinerUError("文献正在导入"), 409, "文献正在导入"),
            (DocumentDeletionRejected(""), 400, "删除文献失败。"),
            (DocumentDeletionFailed("索引重载失败"), 500, "索引重载失败"),
        ]
        for error, expected_status, expected_message in cases:
            with self.subTest(error=type(error).__name__):
                self.deletion.remove_error = error
                self.assertEqual(
                    self.controller.remove({"source_id": "pdf-one"}),
                    (expected_status, {"error": expected_message}),
                )

    def test_batch_remove_exposes_failures_for_every_error_class(self) -> None:
        failures = [{"source_id": "busy", "error": "处理中"}]
        cases = [
            (
                BatchDeletionConflict("处理中", failures),
                409,
                "处理中",
            ),
            (
                DocumentDeletionRejected("", failures),
                400,
                "删除文献失败。",
            ),
            (
                DocumentDeletionFailed("回滚失败", failures),
                500,
                "回滚失败",
            ),
        ]
        for error, expected_status, expected_message in cases:
            with self.subTest(error=type(error).__name__):
                self.deletion.batch_error = error
                self.assertEqual(
                    self.controller.remove_batch({"source_ids": ["busy"]}),
                    (
                        expected_status,
                        {
                            "error": expected_message,
                            "failures": failures,
                        },
                    ),
                )


if __name__ == "__main__":
    unittest.main()

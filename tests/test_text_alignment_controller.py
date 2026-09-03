from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from src.me_finder.application.text_alignment_coordinator import (
    TextAlignmentFailed,
    TextAlignmentRejected,
)
from src.me_finder.text_alignment import AlignmentNotFound
from src.me_finder.text_alignment_controller import TextAlignmentController


class _Coordinator:
    error = None

    def generate(self, *values, **options):
        if self.error:
            raise self.error
        return {"values": values, "options": options, "status": "completed"}


class TextAlignmentControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path("/runtime/data/index.sqlite3")
        self.ready = True
        self.coordinator = _Coordinator()
        self.logged = []
        self.controller = TextAlignmentController(
            self.coordinator,
            self._run_when_ready,
            list_targets=lambda path, source_id: {
                "path": path,
                "source_file_id": source_id,
                "targets": [],
            },
            locate=self._locate,
            log_exception=self.logged.append,
        )

    def _run_when_ready(self, operation):
        return operation(self.path) if self.ready else None

    @staticmethod
    def _locate(path, source_id, target_id, **selection):
        return {
            "path": path,
            "source_file_id": source_id,
            "target_source_file_id": target_id,
            "selection": selection,
        }

    @staticmethod
    def _generate_payload():
        return {
            "document_group_id": "group-one",
            "pivot_source_file_id": "pdf-de",
            "target_source_file_id": "pdf-zh",
        }

    @staticmethod
    def _locate_payload():
        return {
            "source_file_id": "pdf-de",
            "target_source_file_id": "pdf-zh",
            "start_page_index": 1,
            "end_page_index": 1,
            "start_offset": 2,
            "end_offset": 8,
        }

    def test_generate_and_read_routes_keep_exact_contracts(self) -> None:
        status, body = self.controller.generate(self._generate_payload())
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["status"], "completed")
        self.assertFalse(body["result"]["options"]["force"])

        forced_payload = self._generate_payload() | {"force": True}
        status, body = self.controller.generate(forced_payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["options"]["force"])

        status, body = self.controller.targets({"source_id": ["pdf-de"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["source_file_id"], "pdf-de")

        status, body = self.controller.locate(self._locate_payload())
        self.assertEqual(status, 200)
        self.assertEqual(body["selection"]["start_offset"], 2)

    def test_invalid_shapes_are_rejected_before_dependencies(self) -> None:
        self.assertEqual(
            self.controller.generate({"document_group_id": "group-one"})[0],
            400,
        )
        self.assertEqual(
            self.controller.generate(self._generate_payload() | {"force": 1})[0],
            400,
        )
        self.assertEqual(self.controller.targets({})[0], 400)
        self.assertEqual(
            self.controller.locate({"source_file_id": "pdf-de"})[0],
            400,
        )

    def test_expected_failures_keep_400_404_500_and_503(self) -> None:
        self.coordinator.error = TextAlignmentRejected("bad pair")
        self.assertEqual(self.controller.generate(self._generate_payload())[0], 400)
        self.coordinator.error = TextAlignmentFailed("disk")
        self.assertEqual(self.controller.generate(self._generate_payload())[0], 500)
        self.assertEqual(self.logged[-1], "automatic text alignment failed")

        self.controller._locate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AlignmentNotFound("missing")
        )
        self.assertEqual(self.controller.locate(self._locate_payload())[0], 404)
        self.controller._locate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.DatabaseError("locked")
        )
        self.assertEqual(self.controller.locate(self._locate_payload())[0], 500)

        self.ready = False
        self.assertEqual(self.controller.targets({"source_id": ["pdf-de"]})[0], 503)
        self.assertEqual(self.controller.locate(self._locate_payload())[0], 503)


if __name__ == "__main__":
    unittest.main()

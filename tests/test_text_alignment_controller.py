from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.me_finder.application.text_alignment_coordinator import (
    TextAlignmentCancelled,
    TextAlignmentComponentUnavailable,
    TextAlignmentCoordinator,
    TextAlignmentFailed,
    TextAlignmentRejected,
)
from src.me_finder import embedding_runtime
from src.me_finder.text_alignment import AlignmentNotFound
from src.me_finder.text_alignment_controller import TextAlignmentController


class _Coordinator:
    error = None

    def generate(self, *values, **options):
        if self.error:
            raise self.error
        return {"values": values, "options": options, "status": "completed"}


class TextAlignmentControllerTests(unittest.TestCase):
    def test_background_generation_returns_before_computation_and_deduplicates(self):
        entered, release = threading.Event(), threading.Event()
        original = self.coordinator.generate

        def slow_generate(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        self.coordinator.generate = slow_generate
        try:
            status, job = self.controller.start(self._generate_payload())
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.controller.start(self._generate_payload()), (202, job))
            self.assertEqual(self.controller.start(self._generate_payload() | {"force": True})[0], 409)
            self.assertEqual(self.controller.status({"job_id": [job["job_id"]]})[0], 202)
        finally:
            release.set()
            if hasattr(self.controller, "_job_thread"):
                self.controller._job_thread.join(5)
        status, result = self.controller.status({"job_id": [job["job_id"]]})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["status"], "completed")

    def test_background_terminal_errors_and_cancellation_are_returned_to_polling(self):
        for error, expected in ((TextAlignmentRejected("bad pair"), 400),
                                (TextAlignmentFailed("disk"), 500),
                                (TextAlignmentCancelled("stop"), 200),
                                (ValueError("unexpected worker error"), 500)):
            with self.subTest(error=error):
                self.coordinator.error = error
                status, job = self.controller.start(self._generate_payload())
                self.assertEqual(status, 202)
                self.controller._job_thread.join(5)
                status, result = self.controller.status({"job_id": [job["job_id"]]})
                self.assertEqual(status, expected)
                self.assertTrue(result.get("cancelled") or result.get("error"))
        self.assertEqual(self.controller.status({})[0], 400)
        self.assertEqual(self.controller.status({"job_id": ["missing"]})[0], 404)
        self.assertEqual(self.controller.start({})[0], 400)

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
            read_body_ranges=self._read_body_ranges,
            read_body_range_segments=self._read_body_range_segments,
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
    def _read_body_ranges(path, group_id, pivot_id, target_id):
        return {
            "path": path,
            "document_group_id": group_id,
            "range_source": "detected",
            "sides": [
                {"side": "pivot", "source_file_id": pivot_id},
                {"side": "target", "source_file_id": target_id},
            ],
        }

    @staticmethod
    def _read_body_range_segments(path, source_id, segment_set_id, **window):
        return {
            "path": path,
            "source_file_id": source_id,
            "segment_set_id": segment_set_id,
            "window": window,
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

    def test_body_range_review_reads_both_sides_and_submits_two_ranges(self) -> None:
        status, body = self.controller.body_ranges(self._generate_payload())
        self.assertEqual(status, 200)
        self.assertEqual(
            [side["source_file_id"] for side in body["sides"]], ["pdf-de", "pdf-zh"]
        )
        self.assertEqual(body["range_source"], "detected")

        status, body = self.controller.body_range_segments(
            {
                "source_id": ["pdf-de"],
                "segment_set_id": ["segment-set-1"],
                "start": ["12"],
                "count": ["9"],
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["window"], {"start": "12", "count": "9", "pdf_page": None})

        # One submission carries both books' ranges; the half-open interval is
        # what reaches the coordinator.
        payload = self._generate_payload() | {
            "force": True,
            "reviewed_body_ranges": {"pivot": [4, 900], "target": [7, 1200]},
        }
        status, body = self.controller.generate(payload)
        self.assertEqual(status, 200)
        self.assertEqual(
            body["result"]["options"]["reviewed_body_ranges"],
            {"pivot": [4, 900], "target": [7, 1200]},
        )

    def test_malformed_body_ranges_never_reach_the_coordinator(self) -> None:
        for ranges in (
            {"pivot": [4, 900]},
            {"pivot": [4, 900], "target": [7, 7]},
            {"pivot": [4, 900], "target": [-1, 7]},
            {"pivot": [4, 900], "target": [7, "9"]},
            {"pivot": [4, 900], "target": [900, 7]},
            {"pivot": [4, 900], "target": [7, 1200], "extra": [0, 1]},
        ):
            with self.subTest(ranges=ranges):
                payload = self._generate_payload() | {"reviewed_body_ranges": ranges}
                self.assertEqual(self.controller.generate(payload)[0], 400)
                self.assertEqual(self.controller.start(payload)[0], 400)
        self.assertEqual(
            self.controller.body_ranges({"document_group_id": "group-one"})[0], 400
        )
        self.assertEqual(self.controller.body_range_segments({})[0], 400)
        self.assertEqual(
            self.controller.body_range_segments(
                {"source_id": ["pdf-de"], "segment_set_id": ["a", "b"]}
            )[0],
            400,
        )
        self.ready = False
        self.assertEqual(self.controller.body_ranges(self._generate_payload())[0], 503)

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

    def test_component_unavailable_surfaces_specific_reason_not_parse_message(self) -> None:
        # A missing / incompatible / unstartable compute runtime must reach the
        # user with its own actionable reason and a distinct status — not the
        # misleading "请检查两本文献的解析文本" 500.
        self.coordinator.error = TextAlignmentComponentUnavailable(
            "对齐计算运行时未安装：请安装包含对齐组件的版本后再生成。"
        )
        status, body = self.controller.generate(self._generate_payload())
        self.assertEqual(status, 503)
        self.assertTrue(body.get("component_unavailable"))
        self.assertIn("对齐计算运行时未安装", body["error"])
        self.assertNotIn("解析文本", body["error"])

    def test_uninstalled_model_surfaces_install_hint_from_real_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.controller._coordinator = TextAlignmentCoordinator(
                SimpleNamespace(runtime_root=root, index_path=root / "index.sqlite3"),
                None, None,
            )
            status, body = self.controller.generate(self._generate_payload())
        self.assertEqual(status, 503)
        self.assertTrue(body.get("component_unavailable"))
        self.assertIn("下载模型", body["error"])
        self.assertNotIn("解析文本", body["error"])

    def test_cancelled_alignment_is_reported_not_failed(self) -> None:
        self.coordinator.error = TextAlignmentCancelled("stopped")
        status, body = self.controller.generate(self._generate_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["cancelled"])
        self.assertFalse(body["ok"])
        # A cancellation is a user action, not an error to log.
        self.assertEqual(self.logged, [])

    def test_cancel_endpoint_signals_the_embedding_run(self) -> None:
        embedding_runtime.begin_embedding_run()
        self.assertFalse(embedding_runtime.embedding_cancel_requested())
        try:
            status, body = self.controller.cancel(None)
            self.assertEqual(status, 200)
            self.assertTrue(body["cancelled"])
            self.assertTrue(embedding_runtime.embedding_cancel_requested())
        finally:
            embedding_runtime.begin_embedding_run()


if __name__ == "__main__":
    unittest.main()

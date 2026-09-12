from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.me_finder.application.text_alignment_coordinator import (
    TextAlignmentCancelled,
    TextAlignmentCoordinator,
)
from src.me_finder.lifecycle import DurableOperationClosedError


class _IndexRuntime:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.suspended = False

    @contextmanager
    def mutation(self):
        self.events.append("mutation-enter")
        try:
            yield
        finally:
            self.events.append("mutation-exit")

    def suspend(self) -> None:
        self.suspended = True
        self.events.append("suspend")

    def reopen(self, *, attempts: int = 1) -> bool:
        self.suspended = False
        self.events.append(f"reopen-{attempts}")
        return True


class _DurableOperations:
    @contextmanager
    def operation(self):
        yield


class TextAlignmentCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        # These tests cover write windows and shutdown semantics; the managed
        # model component is assumed installed.
        self.component_patch = mock.patch(
            "src.me_finder.application.text_alignment_coordinator."
            "model_component_installed",
            return_value=True,
        )
        runtime_patch = mock.patch(
            "src.me_finder.application.text_alignment_coordinator.find_spec", return_value=object()
        )
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)
        self.component_patch.start()
        self.addCleanup(self.component_patch.stop)

    def test_generation_keeps_the_runtime_available_across_write_windows(
        self,
    ) -> None:
        # The alignment 503 fix: write windows must not suspend/reopen the
        # shared engine (that mapped to HTTP 503 for every overlapping
        # search). Availability behavior is proven end-to-end in
        # tests.test_alignment_write_window_availability; here the contract
        # is that the coordinator never touches the runtime's live engine.
        index_runtime = _IndexRuntime()
        paths = SimpleNamespace(
            index_path=Path("D:/runtime/data/index.sqlite3"),
            runtime_root=Path("D:/runtime"),
        )
        coordinator = TextAlignmentCoordinator(
            paths, index_runtime, _DurableOperations()
        )
        expected = {"status": "completed"}

        def fake_generate(*_args, write_window, **_kwargs):
            with write_window():
                index_runtime.events.append("prepare-write")
            self.assertFalse(index_runtime.suspended)
            index_runtime.events.append("embedding")
            with write_window():
                index_runtime.events.append("publish-write")
            self.assertFalse(index_runtime.suspended)
            return expected

        with mock.patch(
            "src.me_finder.application.text_alignment_coordinator.generate_alignment",
            side_effect=fake_generate,
        ) as generate:
            result = coordinator.generate("group", "pdf-de", "epub-en")

        self.assertEqual(result, expected)
        self.assertEqual(
            index_runtime.events,
            [
                "mutation-enter",
                "prepare-write",
                "embedding",
                "publish-write",
                "mutation-exit",
            ],
        )
        self.assertFalse(index_runtime.suspended)
        self.assertEqual(
            generate.call_args.kwargs["model_cache_dir"],
            Path("D:/runtime/components/text-alignment/models"),
        )
        self.assertEqual(
            generate.call_args.kwargs["embedding_model_id"], "minilm-l12-v2"
        )
        self.assertEqual(
            generate.call_args.kwargs["alignment_thresholds"].low, 0.56
        )
        self.assertFalse(generate.call_args.kwargs["force"])

    def test_force_recomputation_is_forwarded(self) -> None:
        index_runtime = _IndexRuntime()
        paths = SimpleNamespace(
            index_path=Path("D:/runtime/data/index.sqlite3"),
            runtime_root=Path("D:/runtime"),
        )
        coordinator = TextAlignmentCoordinator(
            paths, index_runtime, _DurableOperations()
        )
        with mock.patch(
            "src.me_finder.application.text_alignment_coordinator.generate_alignment",
            return_value={"status": "completed"},
        ) as generate:
            coordinator.generate("group", "pdf-de", "epub-en", force=True)

        self.assertTrue(generate.call_args.kwargs["force"])

    def test_shutdown_closed_durable_gate_reports_cancellation_not_failure(self) -> None:
        # A queued alignment that never starts because the app is closing must
        # surface as a cancellation, not the misleading "请检查解析文本" failure.
        class _ClosedDurableOperations:
            @contextmanager
            def operation(self):
                raise DurableOperationClosedError("应用正在关闭，未开始的持久化操作已取消。")
                yield  # pragma: no cover - unreachable, keeps this a generator

        index_runtime = _IndexRuntime()
        paths = SimpleNamespace(
            index_path=Path("D:/runtime/data/index.sqlite3"),
            runtime_root=Path("D:/runtime"),
        )
        coordinator = TextAlignmentCoordinator(
            paths, index_runtime, _ClosedDurableOperations()
        )
        with mock.patch(
            "src.me_finder.application.text_alignment_coordinator.generate_alignment",
        ) as generate:
            with self.assertRaises(TextAlignmentCancelled):
                coordinator.generate("group", "pdf-de", "epub-en")
        generate.assert_not_called()
        # The mutation window must still close cleanly on the cancel path.
        self.assertEqual(index_runtime.events[-1], "mutation-exit")


if __name__ == "__main__":
    unittest.main()

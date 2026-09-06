from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.me_finder.application.text_alignment_coordinator import (
    TextAlignmentCoordinator,
)


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
    def test_generation_suspends_only_the_two_short_write_windows(self) -> None:
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
                "suspend",
                "prepare-write",
                "reopen-5",
                "embedding",
                "suspend",
                "publish-write",
                "reopen-5",
                "mutation-exit",
            ],
        )
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


if __name__ == "__main__":
    unittest.main()

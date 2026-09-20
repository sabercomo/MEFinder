from __future__ import annotations

import threading
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.me_finder import embedding_runtime
from src.me_finder.alignment_compute import (
    AlignmentComputeError,
    CANCELLED,
    COMPONENT_MISSING,
    WORKER_CRASHED,
)
from src.me_finder.application.text_alignment_coordinator import (
    TextAlignmentCancelled,
    TextAlignmentComponentUnavailable,
    TextAlignmentCoordinator,
    TextAlignmentFailed,
)
from src.me_finder.lifecycle import DurableOperationClosedError, DurableOperationGate


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


class _StubComputeRunner:
    """A compute runner whose external probe succeeds; compute is never called
    here because these tests mock ``generate_alignment``."""

    def __init__(self, **_kwargs) -> None:
        self.probed = False

    def probe(self):
        self.probed = True
        return {"numpy": True, "fastembed": True, "onnxruntime": True}


class TextAlignmentCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # These tests cover write windows and shutdown semantics; the managed
        # model component is assumed installed and the external compute runtime
        # is assumed capable (its probe is stubbed — the compute itself is
        # covered by tests.test_alignment_compute_*).
        self.component_patch = mock.patch(
            "src.me_finder.application.text_alignment_coordinator."
            "model_component_installed",
            return_value=True,
        )
        self.component_patch.start()
        self.addCleanup(self.component_patch.stop)
        self.compute_runner_factory = lambda **kwargs: _StubComputeRunner(**kwargs)

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
            index_path=self.root / "data/index.sqlite3",
            runtime_root=self.root,
        )
        coordinator = TextAlignmentCoordinator(
            paths, index_runtime, _DurableOperations(),
            compute_runner_factory=self.compute_runner_factory,
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
            self.root / "components/text-alignment/models",
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
            index_path=self.root / "data/index.sqlite3",
            runtime_root=self.root,
        )
        coordinator = TextAlignmentCoordinator(
            paths, index_runtime, _DurableOperations(),
            compute_runner_factory=self.compute_runner_factory,
        )
        with mock.patch(
            "src.me_finder.application.text_alignment_coordinator.generate_alignment",
            return_value={"status": "completed"},
        ) as generate:
            coordinator.generate("group", "pdf-de", "epub-en", force=True,
                                 reviewed_body_ranges={"pivot": [1, 4], "target": [2, 5]},
                                 expected_segment_set_ids={"pivot": "set-de", "target": "set-en"})

        self.assertTrue(generate.call_args.kwargs["force"])
        self.assertEqual(generate.call_args.kwargs["reviewed_body_ranges"],
                         {"pivot": [1, 4], "target": [2, 5]})
        self.assertEqual(generate.call_args.kwargs["expected_segment_set_ids"],
                         {"pivot": "set-de", "target": "set-en"})

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
            index_path=self.root / "data/index.sqlite3",
            runtime_root=self.root,
        )
        coordinator = TextAlignmentCoordinator(
            paths, index_runtime, _ClosedDurableOperations(),
            compute_runner_factory=self.compute_runner_factory,
        )
        with mock.patch(
            "src.me_finder.application.text_alignment_coordinator.generate_alignment",
        ) as generate:
            with self.assertRaises(TextAlignmentCancelled):
                coordinator.generate("group", "pdf-de", "epub-en")
        generate.assert_not_called()
        # The mutation window must still close cleanly on the cancel path.
        self.assertEqual(index_runtime.events[-1], "mutation-exit")


class _RaisingProbeRunner:
    def __init__(self, exc: AlignmentComputeError, **_kwargs) -> None:
        self._exc = exc

    def probe(self):
        raise self._exc


class _SlowProbeRunner:
    """Probe blocks until the cancel signal fires, then reports cancellation —
    mimicking a real probe that honours cancellation while a process is live."""

    def __init__(self, started: threading.Event, **kwargs) -> None:
        self._started = started
        self._cancel = kwargs.get("cancel_check")

    def probe(self):
        self._started.set()
        while not (self._cancel and self._cancel()):
            time.sleep(0.01)
        raise AlignmentComputeError(CANCELLED, "cancelled during probe")


class TextAlignmentCoordinatorProbeTests(unittest.TestCase):
    """The probe spawns a process, so it must be inside the lifecycle (drained
    on close, cancellable) and its failures mapped like compute failures."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch = mock.patch(
            "src.me_finder.application.text_alignment_coordinator."
            "model_component_installed",
            return_value=True,
        )
        patch.start()
        self.addCleanup(patch.stop)
        # Leave the module-global cancel flag clean for other tests.
        self.addCleanup(embedding_runtime.begin_embedding_run)
        self.paths = SimpleNamespace(
            index_path=self.root / "data/index.sqlite3",
            runtime_root=self.root,
        )

    def test_probe_component_missing_maps_to_component_unavailable(self) -> None:
        coordinator = TextAlignmentCoordinator(
            self.paths, _IndexRuntime(), _DurableOperations(),
            compute_runner_factory=lambda **kw: _RaisingProbeRunner(
                AlignmentComputeError(COMPONENT_MISSING, "missing")
            ),
        )
        with self.assertRaises(TextAlignmentComponentUnavailable):
            coordinator.generate("group", "a", "b")

    def test_queued_request_does_not_clear_active_shutdown_cancel(self) -> None:
        lock = threading.RLock()
        queued = threading.Event()

        @contextmanager
        def mutation():
            queued.set()
            with lock:
                yield

        gate = DurableOperationGate()
        factory = mock.Mock(side_effect=_StubComputeRunner)
        coordinator = TextAlignmentCoordinator(
            self.paths, SimpleNamespace(mutation=mutation), gate,
            compute_runner_factory=factory,
        )
        outcome = []

        def run():
            try:
                coordinator.generate("group", "a", "b")
            except Exception as exc:
                outcome.append(exc)

        worker = threading.Thread(target=run)
        # An active alignment owns this same mutation lock while shutdown asks
        # it to cancel. A late synchronous request must not erase that signal.
        with gate.operation(), lock:
            embedding_runtime.request_embedding_cancel()
            gate.begin_shutdown()
            worker.start()
            reached_lock = queued.wait(5)
            cancel_preserved = embedding_runtime.embedding_cancel_requested()
        worker.join(5)
        self.assertTrue(reached_lock)
        self.assertFalse(worker.is_alive())
        self.assertTrue(cancel_preserved, "queued request cleared active cancellation")
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], TextAlignmentCancelled)
        factory.assert_not_called()

    def test_probe_crash_maps_to_plain_failure(self) -> None:
        coordinator = TextAlignmentCoordinator(
            self.paths, _IndexRuntime(), _DurableOperations(),
            compute_runner_factory=lambda **kw: _RaisingProbeRunner(
                AlignmentComputeError(WORKER_CRASHED, "boom")
            ),
        )
        with self.assertRaises(TextAlignmentFailed) as ctx:
            coordinator.generate("group", "a", "b")
        self.assertNotIsInstance(ctx.exception, TextAlignmentComponentUnavailable)

    def test_probe_is_inside_durable_operation_and_cancellable(self) -> None:
        # A real gate: while the probe runs it must count as an active durable
        # operation (so a close waits for it), and firing the cancel signal must
        # let that operation drain — proving a close during probe reclaims it
        # instead of leaving an orphan.
        gate = DurableOperationGate()
        started = threading.Event()
        embedding_runtime.begin_embedding_run()  # clear cancel flag
        coordinator = TextAlignmentCoordinator(
            self.paths, _IndexRuntime(), gate,
            compute_runner_factory=lambda **kw: _SlowProbeRunner(started, **kw),
        )
        outcome: dict = {}

        def run() -> None:
            try:
                coordinator.generate("group", "a", "b")
            except BaseException as exc:  # noqa: BLE001
                outcome["exc"] = exc

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(started.wait(5), "probe did not start")
            self.assertEqual(gate.active, 1, "probe must run inside the durable operation")
            self.assertFalse(gate.wait(timeout=0.2), "gate should still be active during probe")
            embedding_runtime.request_embedding_cancel()  # simulate shutdown/cancel
            self.assertTrue(gate.wait(timeout=5), "operation must drain after cancel")
        finally:
            worker.join(5)
        self.assertEqual(gate.active, 0)
        self.assertIsInstance(outcome.get("exc"), TextAlignmentCancelled)


if __name__ == "__main__":
    unittest.main()

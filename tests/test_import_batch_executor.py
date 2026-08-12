from __future__ import annotations

import threading
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from src.me_finder.application.import_batch_executor import ImportBatchExecutor
from src.me_finder.application.import_job_lifecycle import ImportJobCleanupFailed
from src.me_finder.application.import_job_store import ImportJobCancelled
from src.me_finder.import_queue import ImportQueueFullError
from src.me_finder.mineru_api import MinerUError


class _RecordingQueue:
    def __init__(self, *, fail_at: Sequence[int] = ()) -> None:
        self.tasks: List[Tuple[Callable[..., None], Tuple[object, ...]]] = []
        self._fail_at = set(fail_at)
        self._submissions = 0

    def submit(self, task: Callable[..., None], *args: object) -> None:
        self._submissions += 1
        if self._submissions in self._fail_at:
            raise ImportQueueFullError("full")
        self.tasks.append((task, args))

    def run_all(self) -> None:
        for task, args in self.tasks:
            task(*args)


class _ThreadQueue:
    def __init__(self) -> None:
        self.threads: List[threading.Thread] = []

    def submit(self, task: Callable[..., None], *args: object) -> None:
        thread = threading.Thread(target=task, args=args)
        self.threads.append(thread)
        thread.start()

    def join(self) -> None:
        for thread in self.threads:
            thread.join(timeout=1)


class _Jobs:
    def __init__(self) -> None:
        self.updates: List[Tuple[str, Dict[str, object]]] = []
        self.prepare_calls: List[Tuple[object, ...]] = []
        self.prepare_results: Dict[str, bool] = {}
        self.prepare_barrier: Optional[threading.Barrier] = None
        self.index_calls: List[Tuple[str, str, bool]] = []
        self.index_failures: Dict[str, Exception] = {}
        self.rebuild_calls: List[Tuple[str, List[str]]] = []
        self.rebuild_result: set[str] = set()
        self.index_failures_recorded: List[Tuple[str, Exception, bool]] = []
        self.queue_failures: List[str] = []
        self.finalized: List[Tuple[str, str, bool]] = []
        self.cancelled: set[str] = set()
        self.finished_cancelled: List[str] = []
        self.cancel_cleanup_failures: set[str] = set()
        self.cancel_during_index: set[str] = set()
        self.cancel_during_finalize: set[str] = set()
        self.cancel_during_rebuild: set[str] = set()
        self.cancel_during_update: set[str] = set()
        self.cancel_during_failure: set[str] = set()
        self.first_index_entered = threading.Event()
        self.index_overlap = threading.Event()
        self.release_first_index = threading.Event()
        self.track_index_concurrency = False
        self._active_index_calls = 0
        self._index_state_lock = threading.Lock()

    def update_import_job(self, job_id: str, **updates: object) -> None:
        self.updates.append((job_id, updates))
        if job_id in self.cancel_during_update:
            self.cancelled.add(job_id)

    def prepare_import_job(
        self,
        job_id: str,
        target: Path,
        source_file_id: str,
        profile: Dict[str, object],
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
    ) -> bool:
        self.prepare_calls.append(
            (
                job_id,
                target,
                source_file_id,
                profile,
                is_pdf,
                force_mineru,
                vision_provider_id,
            )
        )
        if self.prepare_barrier is not None:
            self.prepare_barrier.wait(timeout=1)
        return self.prepare_results.get(job_id, True)

    def index_registered_pdf(
        self,
        job_id: str,
        source_file_id: str,
        *,
        backup_existing: bool = False,
    ) -> None:
        self.index_calls.append((job_id, source_file_id, backup_existing))
        if job_id in self.cancel_during_index:
            self.cancelled.add(job_id)
            raise RuntimeError("index stopped")
        if self.track_index_concurrency:
            with self._index_state_lock:
                self._active_index_calls += 1
                if self._active_index_calls > 1:
                    self.index_overlap.set()
                first = len(self.index_calls) == 1
            if first:
                self.first_index_entered.set()
                self.release_first_index.wait(timeout=1)
            with self._index_state_lock:
                self._active_index_calls -= 1
        failure = self.index_failures.get(job_id)
        if failure is not None:
            raise failure

    def rebuild_runtime_index(
        self,
        job_id: str,
        expected_source_ids: Optional[List[str]] = None,
    ) -> set[str]:
        self.rebuild_calls.append((job_id, list(expected_source_ids or [])))
        if job_id in self.cancel_during_rebuild:
            self.cancel_during_rebuild.remove(job_id)
            self.cancelled.add(job_id)
            raise ImportJobCancelled("cancelled during rebuild")
        return set(self.rebuild_result)

    def fail_import_at_index(
        self,
        job_id: str,
        exc: Exception,
        *,
        parsed: bool = False,
    ) -> None:
        if job_id in self.cancel_during_failure:
            self.cancelled.add(job_id)
            raise ImportJobCancelled("cancelled before failure transition")
        self.index_failures_recorded.append((job_id, exc, parsed))

    def fail_import_at_queue(self, job_id: str) -> None:
        self.queue_failures.append(job_id)

    def finalize_import_job(
        self,
        job_id: str,
        source_file_id: str,
        is_pdf: bool,
    ) -> None:
        self.finalized.append((job_id, source_file_id, is_pdf))
        if job_id in self.cancel_during_finalize:
            self.cancelled.add(job_id)

    def ensure_import_not_cancelled(self, job_id: str) -> None:
        if job_id in self.cancelled:
            raise ImportJobCancelled("cancelled")

    def finish_cancelled_import_job(self, job_id: str) -> None:
        if job_id in self.cancel_cleanup_failures:
            raise ImportJobCleanupFailed(f"cleanup failed for {job_id}")
        self.cancelled.discard(job_id)
        self.finished_cancelled.append(job_id)


def _item(
    job_id: str,
    source_file_id: str,
    *,
    is_pdf: bool,
) -> Dict[str, object]:
    suffix = ".pdf" if is_pdf else ".docx"
    return {
        "job_id": job_id,
        "target": Path(f"/tmp/{source_file_id}{suffix}"),
        "profile": {"detected_pdf_type": "native_text"},
        "source_file_id": source_file_id,
        "is_pdf": is_pdf,
        "force_mineru": False,
        "vision_provider_id": None,
        "display_file_name": f"{source_file_id}{suffix}",
    }


class ImportBatchExecutorTests(unittest.TestCase):
    def test_native_initial_update_followed_by_cancellation_is_finished(self) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        jobs.cancel_during_update.add("job-first")
        executor = ImportBatchExecutor(queue)

        executor.submit_native(
            [_item("job-first", "word-first", is_pdf=False)],
            jobs=jobs,
        )
        queue.run_all()

        self.assertEqual(jobs.finished_cancelled, ["job-first"])
        self.assertEqual(jobs.rebuild_calls, [])

    def test_failure_transition_race_finishes_cancelled_job(self) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        jobs.index_failures["job-first"] = RuntimeError("broken PDF")
        jobs.cancel_during_failure.add("job-first")
        executor = ImportBatchExecutor(queue)

        executor.submit_native(
            [_item("job-first", "pdf-first", is_pdf=True)],
            jobs=jobs,
        )
        queue.run_all()

        self.assertEqual(jobs.finished_cancelled, ["job-first"])
        self.assertEqual(jobs.index_failures_recorded, [])

    def test_native_pdf_batch_finishes_cancelled_first_job(self) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        jobs.cancelled.add("job-first")
        executor = ImportBatchExecutor(queue)

        executor.submit_native(
            [
                _item("job-first", "pdf-first", is_pdf=True),
                _item("job-second", "pdf-second", is_pdf=True),
            ],
            jobs=jobs,
        )
        queue.run_all()

        self.assertEqual(jobs.finished_cancelled, ["job-first"])
        self.assertEqual(jobs.finalized, [("job-second", "pdf-second", True)])

    def test_native_word_batch_finishes_cancelled_non_first_job(self) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        jobs.cancelled.add("job-second")
        executor = ImportBatchExecutor(queue)

        executor.submit_native(
            [
                _item("job-first", "word-first", is_pdf=False),
                _item("job-second", "word-second", is_pdf=False),
            ],
            jobs=jobs,
        )
        queue.run_all()

        self.assertEqual(jobs.finished_cancelled, ["job-second"])
        self.assertEqual(jobs.rebuild_calls, [("job-first", [])])
        self.assertEqual(jobs.finalized, [("job-first", "word-first", False)])

    def test_native_cancel_cleanup_failure_does_not_block_siblings(self) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        jobs.cancelled.add("job-first")
        jobs.cancel_cleanup_failures.add("job-first")
        executor = ImportBatchExecutor(queue)

        executor.submit_native(
            [
                _item("job-first", "word-first", is_pdf=False),
                _item("job-second", "word-second", is_pdf=False),
            ],
            jobs=jobs,
        )
        with self.assertLogs(level="ERROR") as logs:
            queue.run_all()

        self.assertIn("job-first", "\n".join(logs.output))
        self.assertEqual(jobs.rebuild_calls, [("job-second", [])])
        self.assertEqual(jobs.finalized, [("job-second", "word-second", False)])

    def test_native_word_batch_retries_with_next_anchor_when_first_is_cancelled(
        self,
    ) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        jobs.cancel_during_rebuild.add("job-first")
        executor = ImportBatchExecutor(queue)

        executor.submit_native(
            [
                _item("job-first", "word-first", is_pdf=False),
                _item("job-second", "word-second", is_pdf=False),
            ],
            jobs=jobs,
        )
        queue.run_all()

        self.assertEqual(jobs.finished_cancelled, ["job-first"])
        self.assertEqual(
            jobs.rebuild_calls,
            [("job-first", []), ("job-second", [])],
        )
        self.assertEqual(jobs.finalized, [("job-second", "word-second", False)])

    def test_remote_batch_finishes_cancelled_pdf_and_word_jobs(self) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        jobs.cancelled.update({"job-first", "job-third"})
        executor = ImportBatchExecutor(queue)

        executor.submit_remote(
            [
                _item("job-first", "pdf-first", is_pdf=True),
                _item("job-second", "pdf-second", is_pdf=True),
                _item("job-third", "word-third", is_pdf=False),
            ],
            jobs=jobs,
        )
        queue.run_all()

        self.assertEqual(jobs.finished_cancelled, ["job-first", "job-third"])
        self.assertEqual(jobs.finalized, [("job-second", "pdf-second", True)])

    def test_remote_index_failure_cannot_overwrite_cancellation(self) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        jobs.cancel_during_index.add("job-first")
        executor = ImportBatchExecutor(queue)

        executor.submit_remote(
            [_item("job-first", "pdf-first", is_pdf=True)],
            jobs=jobs,
        )
        queue.run_all()

        self.assertEqual(jobs.finished_cancelled, ["job-first"])
        self.assertEqual(jobs.index_failures_recorded, [])

    def test_native_completed_transition_cannot_overwrite_cancellation(self) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        jobs.cancel_during_finalize.add("job-first")
        executor = ImportBatchExecutor(queue)

        executor.submit_native(
            [_item("job-first", "word-first", is_pdf=False)],
            jobs=jobs,
        )
        queue.run_all()

        self.assertEqual(jobs.finalized, [("job-first", "word-first", False)])
        self.assertEqual(jobs.finished_cancelled, ["job-first"])

    def test_native_mixed_batch_rebuilds_once_and_reports_missing_pdf(self) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        jobs.rebuild_result = {"pdf-missing"}
        executor = ImportBatchExecutor(queue)

        executor.submit_native(
            [
                _item("job-word", "word-one", is_pdf=False),
                _item("job-pdf", "pdf-missing", is_pdf=True),
            ],
            jobs=jobs,
        )
        queue.run_all()

        self.assertEqual(jobs.rebuild_calls, [("job-word", ["pdf-missing"])])
        self.assertEqual(jobs.finalized, [("job-word", "word-one", False)])
        self.assertEqual(jobs.index_failures_recorded[0][0], "job-pdf")
        self.assertIsInstance(jobs.index_failures_recorded[0][1], MinerUError)
        self.assertFalse(jobs.index_failures_recorded[0][2])

    def test_native_pdf_failure_does_not_stop_the_next_pdf(self) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        jobs.index_failures["job-first"] = RuntimeError("broken PDF")
        executor = ImportBatchExecutor(queue)

        executor.submit_native(
            [
                _item("job-first", "pdf-first", is_pdf=True),
                _item("job-second", "pdf-second", is_pdf=True),
            ],
            jobs=jobs,
        )
        queue.run_all()

        self.assertEqual(
            [call[0] for call in jobs.index_calls],
            ["job-first", "job-second"],
        )
        self.assertEqual(jobs.index_failures_recorded[0][0], "job-first")
        self.assertEqual(jobs.finalized, [("job-second", "pdf-second", True)])
        self.assertEqual(jobs.rebuild_calls, [])

    def test_native_queue_failure_marks_every_created_job(self) -> None:
        queue = _RecordingQueue(fail_at=(1,))
        jobs = _Jobs()
        executor = ImportBatchExecutor(queue)

        executor.submit_native(
            [
                _item("job-first", "word-first", is_pdf=False),
                _item("job-second", "word-second", is_pdf=False),
            ],
            jobs=jobs,
        )

        self.assertEqual(jobs.queue_failures, ["job-first", "job-second"])
        self.assertEqual(queue.tasks, [])

    def test_remote_queue_failure_is_isolated_to_that_item(self) -> None:
        queue = _RecordingQueue(fail_at=(2,))
        jobs = _Jobs()
        executor = ImportBatchExecutor(queue)
        items = [
            _item("job-first", "pdf-first", is_pdf=True),
            _item("job-second", "pdf-second", is_pdf=True),
            _item("job-third", "pdf-third", is_pdf=True),
        ]

        executor.submit_remote(items, jobs=jobs)
        queue.run_all()

        self.assertEqual(jobs.queue_failures, ["job-second"])
        self.assertEqual(
            jobs.finalized,
            [
                ("job-first", "pdf-first", True),
                ("job-third", "pdf-third", True),
            ],
        )

    def test_remote_index_commits_share_one_serializing_lock(self) -> None:
        queue = _ThreadQueue()
        jobs = _Jobs()
        jobs.prepare_barrier = threading.Barrier(2)
        jobs.track_index_concurrency = True
        executor = ImportBatchExecutor(queue)

        executor.submit_remote(
            [
                _item("job-first", "pdf-first", is_pdf=True),
                _item("job-second", "pdf-second", is_pdf=True),
            ],
            jobs=jobs,
        )

        self.assertTrue(jobs.first_index_entered.wait(timeout=1))
        self.assertFalse(jobs.index_overlap.wait(timeout=0.1))
        jobs.release_first_index.set()
        queue.join()

        self.assertFalse(jobs.index_overlap.is_set())
        self.assertEqual(len(jobs.finalized), 2)
        self.assertTrue(all(not thread.is_alive() for thread in queue.threads))

    def test_job_callbacks_are_resolved_when_the_queued_task_runs(self) -> None:
        queue = _RecordingQueue()
        jobs = _Jobs()
        executor = ImportBatchExecutor(queue)
        replacement_calls: List[Tuple[str, List[str]]] = []

        executor.submit_native(
            [_item("job-word", "word-one", is_pdf=False)],
            jobs=jobs,
        )

        def replacement(job_id: str, expected: Optional[List[str]] = None) -> set[str]:
            replacement_calls.append((job_id, list(expected or [])))
            return set()

        jobs.rebuild_runtime_index = replacement  # type: ignore[method-assign]
        queue.run_all()

        self.assertEqual(replacement_calls, [("job-word", [])])
        self.assertEqual(jobs.rebuild_calls, [])


if __name__ == "__main__":
    unittest.main()

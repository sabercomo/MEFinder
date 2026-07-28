from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from src.me_finder.import_queue import ImportBatchCompletion, ImportTaskQueue


class ImportTaskQueueTests(unittest.TestCase):
    def test_rejects_invalid_worker_count(self) -> None:
        with self.assertRaises(ValueError):
            ImportTaskQueue(worker_count=0)

    def test_limits_concurrency_and_continues_after_task_failure(self) -> None:
        task_queue = ImportTaskQueue(worker_count=2)
        state_lock = threading.Lock()
        release = threading.Event()
        finished = threading.Event()
        active = 0
        maximum_active = 0
        completed: list[int] = []

        def task(index: int) -> None:
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            if index < 2:
                release.wait(timeout=2)
            with state_lock:
                active -= 1
                completed.append(index)
                if len(completed) == 4:
                    finished.set()

        task_queue.submit(task, 0)
        task_queue.submit(task, 1)
        task_queue.submit(task, 2)
        task_queue.submit(task, 3)
        time.sleep(0.05)
        with state_lock:
            self.assertEqual(maximum_active, 2)
        release.set()
        self.assertTrue(finished.wait(timeout=2))
        self.assertEqual(sorted(completed), [0, 1, 2, 3])

        failure_seen = threading.Event()
        recovery_seen = threading.Event()

        def failing_task() -> None:
            failure_seen.set()
            raise RuntimeError("expected test failure")

        with patch("src.me_finder.import_queue.logging.exception") as log_exception:
            task_queue.submit(failing_task)
            task_queue.submit(recovery_seen.set)
            self.assertTrue(failure_seen.wait(timeout=2))
            self.assertTrue(recovery_seen.wait(timeout=2))
            log_exception.assert_called_once_with("background import task failed")

    def test_batch_completion_calls_back_once_with_only_successes(self) -> None:
        callback_seen = threading.Event()
        callback_items: list[object] = []

        def on_complete(items: list[object]) -> None:
            callback_items.extend(items)
            callback_seen.set()

        group = ImportBatchCompletion(3, on_complete)
        threads = [
            threading.Thread(target=group.finish, args=("one", True)),
            threading.Thread(target=group.finish, args=("two", False)),
            threading.Thread(target=group.finish, args=("three", True)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(callback_seen.is_set())
        self.assertCountEqual(callback_items, ["one", "three"])
        with self.assertRaises(RuntimeError):
            group.finish("duplicate", True)


if __name__ == "__main__":
    unittest.main()

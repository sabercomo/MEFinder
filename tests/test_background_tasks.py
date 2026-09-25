"""Runtime-owned background work stops before its resources are closed."""

from __future__ import annotations

import threading
import unittest

from src.me_finder.tasks.background_tasks import BackgroundTasks


class BackgroundTasksTests(unittest.TestCase):
    def test_cancel_and_join_named_task(self) -> None:
        tasks = BackgroundTasks()
        started = threading.Event()
        exited = threading.Event()

        def work(cancel: threading.Event) -> None:
            started.set()
            cancel.wait()
            exited.set()

        thread = tasks.start("warm-up", work)
        self.assertTrue(started.wait(1))
        self.assertTrue(tasks.close(timeout=1))
        self.assertTrue(exited.is_set())
        self.assertFalse(thread.is_alive())
        with self.assertRaises(RuntimeError):
            tasks.start("later", work)

    def test_timeout_can_be_retried_after_worker_exits(self) -> None:
        tasks = BackgroundTasks()
        started = threading.Event()
        release = threading.Event()

        def work(_cancel: threading.Event) -> None:
            started.set()
            release.wait()

        thread = tasks.start("slow", work)
        self.assertTrue(started.wait(1))
        self.assertFalse(tasks.close(timeout=0))
        release.set()
        self.assertTrue(tasks.close(timeout=1))
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()

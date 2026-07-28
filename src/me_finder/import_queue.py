"""Small daemon worker queue for background document imports."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, List


class ImportTaskQueue:
    """Run import tasks with bounded concurrency and without blocking app exit."""

    def __init__(self, worker_count: int = 2) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        self.worker_count = worker_count
        self._tasks: queue.Queue[tuple[Callable[..., None], tuple[object, ...]]] = (
            queue.Queue()
        )
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"mefinder-import-{index + 1}",
                daemon=True,
            )
            for index in range(worker_count)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, task: Callable[..., None], *args: object) -> None:
        self._tasks.put((task, args))

    def _worker(self) -> None:
        while True:
            task, args = self._tasks.get()
            try:
                task(*args)
            except Exception:
                logging.exception("background import task failed")
            finally:
                self._tasks.task_done()


class ImportBatchCompletion:
    """Invoke one callback after every member of a concurrent batch finishes."""

    def __init__(
        self,
        total: int,
        on_complete: Callable[[List[object]], None],
    ) -> None:
        if total < 1:
            raise ValueError("total must be at least 1")
        self._remaining = total
        self._successful: List[object] = []
        self._on_complete = on_complete
        self._lock = threading.Lock()

    def finish(self, item: object, succeeded: bool) -> None:
        completed: List[object] | None = None
        with self._lock:
            if self._remaining < 1:
                raise RuntimeError("batch member reported completion more than once")
            if succeeded:
                self._successful.append(item)
            self._remaining -= 1
            if self._remaining == 0:
                completed = list(self._successful)
        if completed is not None:
            self._on_complete(completed)

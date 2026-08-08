"""Small daemon worker queue for background document imports."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, List


class ImportQueueFullError(RuntimeError):
    """Raised when the durable job cannot enter the in-process worker queue."""


class ImportQueueClosedError(RuntimeError):
    """Raised after application shutdown has stopped accepting new work."""


class ImportTaskQueue:
    """Run import tasks with bounded concurrency and without blocking app exit."""

    def __init__(self, worker_count: int = 2, max_pending_tasks: int = 32) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        if max_pending_tasks < 1:
            raise ValueError("max_pending_tasks must be at least 1")
        self.worker_count = worker_count
        self.max_pending_tasks = max_pending_tasks
        self._tasks: queue.Queue[tuple[Callable[..., None], tuple[object, ...]]] = (
            queue.Queue(maxsize=max_pending_tasks)
        )
        self._state_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._accepting = True
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
        with self._state_lock:
            if not self._accepting:
                raise ImportQueueClosedError("导入队列已停止接收新任务。")
            try:
                self._tasks.put_nowait((task, args))
            except queue.Full as exc:
                raise ImportQueueFullError(
                    "导入队列已满，请等待当前任务完成后重试。"
                ) from exc

    @property
    def accepting(self) -> bool:
        with self._state_lock:
            return self._accepting

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> bool:
        """Stop accepting work and let already accepted tasks finish.

        Workers poll the shutdown event instead of relying on sentinels, so a
        full bounded queue can always be shut down without blocking the caller.
        The return value reports whether every worker exited before ``timeout``.
        """

        with self._state_lock:
            self._accepting = False
            self._shutdown.set()
        if not wait:
            return all(not thread.is_alive() for thread in self._threads)
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        for thread in self._threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        return all(not thread.is_alive() for thread in self._threads)

    def _worker(self) -> None:
        while True:
            try:
                task, args = self._tasks.get(timeout=0.1)
            except queue.Empty:
                # submit() and shutdown() serialize through _state_lock.  Once
                # shutdown is visible no later submit can succeed, but a
                # submit that won the lock immediately beforehand may already
                # have filled the queue after get() timed out.
                if self._shutdown.is_set() and self._tasks.empty():
                    return
                continue
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

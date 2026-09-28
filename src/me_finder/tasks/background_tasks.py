"""Own small runtime-scoped threads until shutdown finishes."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class BackgroundTasks:
    """Start named daemon tasks, request cancellation, and join on close."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closing = False
        self._tasks: dict[str, tuple[threading.Thread, threading.Event]] = {}

    def start(self, name: str, work: Callable[[threading.Event], None]) -> threading.Thread:
        """Start one task; its cancellation event is set at shutdown."""

        with self._lock:
            if self._closing:
                raise RuntimeError("background tasks are closing")
            self._tasks = {
                task_name: task
                for task_name, task in self._tasks.items()
                if task[0].is_alive()
            }
            previous = self._tasks.get(name)
            if previous is not None and previous[0].is_alive():
                raise RuntimeError(f"background task already running: {name}")
            cancel = threading.Event()
            thread = threading.Thread(target=work, args=(cancel,), name=name, daemon=True)
            self._tasks[name] = (thread, cancel)
            thread.start()
            return thread

    def begin_shutdown(self) -> None:
        """Prevent new work and signal every active task to finish."""

        with self._lock:
            self._closing = True
            for _thread, cancel in self._tasks.values():
                cancel.set()

    def close(self, timeout: float | None = None) -> bool:
        """Return whether all registered tasks exited before the deadline."""

        self.begin_shutdown()
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._lock:
            threads = [thread for thread, _cancel in self._tasks.values()]
        for thread in threads:
            thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in threads)

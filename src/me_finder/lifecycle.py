"""Small lifecycle primitives shared by local transport adapters."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator


class DurableOperationClosedError(RuntimeError):
    """Raised when shutdown rejects a not-yet-started durable mutation."""


class DurableOperationGate:
    """Drain consistency-critical writes without waiting on every request.

    A desktop app must not exit between writing import configuration and
    committing the corresponding SQLite update.  At the same time, waiting
    forever for an arbitrary half-open HTTP body would make a WebView process
    impossible to close.  This gate distinguishes the short durable region:
    shutdown rejects operations that have not entered it and can explicitly
    wait for operations that already have.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = 0
        self._closing = False

    @contextmanager
    def operation(self) -> Iterator[None]:
        with self._condition:
            if self._closing:
                raise DurableOperationClosedError(
                    "应用正在关闭，未开始的持久化操作已取消。"
                )
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def begin_shutdown(self) -> None:
        with self._condition:
            self._closing = True
            self._condition.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._active:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    @property
    def active(self) -> int:
        with self._condition:
            return self._active

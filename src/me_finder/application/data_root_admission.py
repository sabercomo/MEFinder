"""Mutual exclusion between current-root mutations and data migration."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from ..mineru_api import MinerUError


class DataRootAdmissionError(MinerUError):
    """A current-root operation conflicts with data-location migration."""


class DataRootAdmissionGate:
    """Drain admitted root operations, then seal the old root after migration."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_operations = 0
        self._state = "open"

    @contextmanager
    def operation(self) -> Iterator[None]:
        """Admit one short operation that may mutate or register root work."""

        with self._condition:
            if self._state == "migrating":
                raise DataRootAdmissionError(
                    "数据位置正在迁移，暂时不能修改文献数据。"
                )
            if self._state == "migrated":
                raise DataRootAdmissionError(
                    "数据位置已迁移，请重启应用后再修改文献数据。"
                )
            self._active_operations += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_operations -= 1
                self._condition.notify_all()

    @contextmanager
    def migration(self) -> Iterator[None]:
        """Close admission, drain entered operations, and seal on success."""

        with self._condition:
            if self._state == "migrating":
                raise DataRootAdmissionError("数据位置正在迁移。")
            if self._state == "migrated":
                raise DataRootAdmissionError(
                    "数据位置已迁移，请重启应用。"
                )
            self._state = "migrating"
            while self._active_operations:
                self._condition.wait()

        completed = False
        try:
            yield
            completed = True
        finally:
            with self._condition:
                self._state = "migrated" if completed else "open"
                self._condition.notify_all()

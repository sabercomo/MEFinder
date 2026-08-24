"""Publish automatic alignment writes through the shared index runtime."""

from __future__ import annotations

import sqlite3

from ..text_alignment import InvalidAlignmentRequest, generate_alignment


class TextAlignmentRejected(ValueError):
    """The requested pair cannot be aligned."""


class TextAlignmentFailed(RuntimeError):
    """Alignment computation or index publication failed."""


class TextAlignmentCoordinator:
    def __init__(self, paths, index_runtime, durable_operations) -> None:
        self._paths = paths
        self._index_runtime = index_runtime
        self._durable_operations = durable_operations

    def generate(
        self,
        document_group_id: object,
        pivot_source_file_id: object,
        target_source_file_id: object,
    ):
        with self._index_runtime.mutation():
            self._index_runtime.suspend()
            try:
                with self._durable_operations.operation():
                    result = generate_alignment(
                        self._paths.index_path,
                        document_group_id,
                        pivot_source_file_id,
                        target_source_file_id,
                    )
            except InvalidAlignmentRequest as exc:
                self._index_runtime.reopen(attempts=5)
                raise TextAlignmentRejected(str(exc)) from exc
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                self._index_runtime.reopen(attempts=5)
                raise TextAlignmentFailed(str(exc)) from exc
            try:
                self._index_runtime.reopen(attempts=5)
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise TextAlignmentFailed(
                    "自动对齐已写入，但索引未能重新加载。"
                ) from exc
        return result

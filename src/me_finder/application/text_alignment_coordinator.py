"""Publish automatic alignment writes through the shared index runtime."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from ..embedding_models import resolve_alignment_thresholds
from ..preferences import read_preferences, resolve_preferences_path
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
        *,
        force: bool = False,
    ):
        preferences = read_preferences(
            resolve_preferences_path(self._paths.runtime_root)
        )
        model_id = str(preferences["alignment_embedding_model_id"])
        thresholds = resolve_alignment_thresholds(
            model_id, preferences["alignment_thresholds"]
        )
        with self._index_runtime.mutation():
            try:
                with self._durable_operations.operation():
                    result = generate_alignment(
                        self._paths.index_path,
                        document_group_id,
                        pivot_source_file_id,
                        target_source_file_id,
                        force=force,
                        model_cache_dir=(
                            self._paths.runtime_root
                            / "components"
                            / "text-alignment"
                            / "models"
                        ),
                        embedding_model_id=model_id,
                        alignment_thresholds=thresholds,
                        write_window=self._write_window,
                    )
            except InvalidAlignmentRequest as exc:
                raise TextAlignmentRejected(str(exc)) from exc
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                raise TextAlignmentFailed(str(exc)) from exc
        return result

    @contextmanager
    def _write_window(self):
        self._index_runtime.suspend()
        try:
            yield
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as write_error:
            try:
                self._index_runtime.reopen(attempts=5)
            except (OSError, sqlite3.Error, RuntimeError, ValueError) as reopen_error:
                write_error.add_note(f"runtime reopen also failed: {reopen_error}")
                raise write_error.with_traceback(write_error.__traceback__)
            raise
        self._index_runtime.reopen(attempts=5)

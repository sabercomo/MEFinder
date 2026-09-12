"""Publish automatic alignment writes through the shared index runtime."""

from __future__ import annotations

import sqlite3
from importlib.util import find_spec
from contextlib import contextmanager

from ..embedding_models import (
    model_component_installed,
    resolve_alignment_thresholds,
)
from ..preferences import read_preferences, resolve_preferences_path
from ..embedding_runtime import (
    SemanticAlignmentCancelled,
    begin_embedding_run,
)
from ..lifecycle import DurableOperationClosedError
from ..text_alignment import InvalidAlignmentRequest, generate_alignment


class TextAlignmentRejected(ValueError):
    """The requested pair cannot be aligned."""


class TextAlignmentFailed(RuntimeError):
    """Alignment computation or index publication failed."""


class TextAlignmentCancelled(Exception):
    """The in-flight alignment was cancelled by the user."""


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
        cache_dir = self._paths.runtime_root / "components" / "text-alignment" / "models"
        if not model_component_installed(cache_dir, model_id):
            # The managed model component is a settings-UI download. Starting a
            # generation job without it must fail clearly and locally — never
            # trigger a hidden network download from inside the job.
            raise TextAlignmentFailed(
                "对齐计算组件未安装：请在设置 → 译本对齐 中下载模型后再生成。"
            )
        missing = [name for name in ("numpy", "fastembed", "onnxruntime") if find_spec(name) is None]
        if missing:
            raise TextAlignmentFailed(
                "对齐计算运行时未安装：请安装包含对齐组件的版本后再生成。"
            )
        begin_embedding_run()
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
            except (SemanticAlignmentCancelled, DurableOperationClosedError) as exc:
                # Both mean "the run stopped because the app is shutting down or
                # the user cancelled" — a cancellation, not a parse/data failure.
                # A queued alignment that never started (DurableOperationClosed)
                # must not surface the misleading "请检查两本文献的解析文本".
                raise TextAlignmentCancelled(str(exc)) from exc
            except InvalidAlignmentRequest as exc:
                raise TextAlignmentRejected(str(exc)) from exc
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                raise TextAlignmentFailed(str(exc)) from exc
        return result

    @contextmanager
    def _write_window(self):
        # Alignment writes touch only alignment_runs/alignment_links(+members)
        # and segment tables; search-visible data (paragraphs, pages, catalog)
        # is unchanged, so the live engine keeps serving across the write
        # transactions. Rollback-journal locking bounds any reader wait to the
        # writer's short EXCLUSIVE commit (both sides run busy_timeout); closing
        # the engine here would 503 every overlapping search for the whole
        # window with no consistency benefit.
        yield

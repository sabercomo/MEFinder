"""Publish automatic alignment writes through the shared index runtime."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager

from ..embedding_models import (
    model_component_installed,
    resolve_alignment_thresholds,
)
from ..runtime_location import component_runtime_root
from ..preferences import read_preferences, resolve_preferences_path
from ..embedding_runtime import (
    SemanticAlignmentCancelled,
    begin_embedding_run,
    embedding_cancel_requested,
)
from ..lifecycle import DurableOperationClosedError
from ..alignment_compute import (
    AlignmentComputeError,
    CANCELLED,
    COMPONENT_MISSING,
    PROTOCOL_INCOMPATIBLE,
    SubprocessAlignmentComputeRunner,
)
from ..text_alignment import InvalidAlignmentRequest, generate_alignment


class TextAlignmentRejected(ValueError):
    """The requested pair cannot be aligned."""


class TextAlignmentFailed(RuntimeError):
    """Alignment computation or index publication failed."""


class TextAlignmentCancelled(Exception):
    """The in-flight alignment was cancelled by the user."""


_PROBE_MESSAGES = {
    COMPONENT_MISSING: "对齐计算运行时未安装：请安装包含对齐组件的版本后再生成。",
    PROTOCOL_INCOMPATIBLE: "对齐计算进程版本不兼容，请更新应用后再生成。",
}


def _probe_message(exc: AlignmentComputeError) -> str:
    return _PROBE_MESSAGES.get(exc.code, str(exc))


def build_compute_runner(*, task_id, cancel_check):
    """Default factory: an out-of-process compute runner for this runtime.

    Injectable so tests can substitute a stub or a runner with a custom launch
    command. The runner owns the NumPy/ONNX/fastembed stack in a separate
    process; the coordinator (main process) keeps identity checks, write
    coordination and result publication.
    """

    return SubprocessAlignmentComputeRunner(task_id=task_id, cancel_check=cancel_check)


class TextAlignmentCoordinator:
    def __init__(
        self,
        paths,
        index_runtime,
        durable_operations,
        *,
        compute_runner_factory=build_compute_runner,
    ) -> None:
        self._paths = paths
        self._index_runtime = index_runtime
        self._durable_operations = durable_operations
        self._compute_runner_factory = compute_runner_factory

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
        cache_dir = component_runtime_root(self._paths.runtime_root) / "components" / "text-alignment" / "models"
        if not model_component_installed(cache_dir, model_id):
            # The managed model component is a settings-UI download. Starting a
            # generation job without it must fail clearly and locally — never
            # trigger a hidden network download from inside the job.
            raise TextAlignmentFailed(
                "对齐计算组件未安装：请在设置 → 译本对齐 中下载模型后再生成。"
            )
        begin_embedding_run()
        # The compute runs out of process. Capability is checked by probing that
        # external runtime — NOT by a main-process find_spec — and a failed probe
        # is a clear, local error: the coordinator never silently falls back to
        # in-process computation.
        runner = self._compute_runner_factory(
            task_id=uuid.uuid4().hex, cancel_check=embedding_cancel_requested
        )
        try:
            runner.probe()
        except AlignmentComputeError as exc:
            raise TextAlignmentFailed(_probe_message(exc)) from exc
        with self._index_runtime.mutation():
            try:
                with self._durable_operations.operation():
                    result = generate_alignment(
                        self._paths.index_path,
                        document_group_id,
                        pivot_source_file_id,
                        target_source_file_id,
                        force=force,
                        model_cache_dir=cache_dir,
                        embedding_model_id=model_id,
                        alignment_thresholds=thresholds,
                        write_window=self._write_window,
                        compute_runner=runner,
                    )
            except (SemanticAlignmentCancelled, DurableOperationClosedError) as exc:
                # Both mean "the run stopped because the app is shutting down or
                # the user cancelled" — a cancellation, not a parse/data failure.
                # A queued alignment that never started (DurableOperationClosed)
                # must not surface the misleading "请检查两本文献的解析文本".
                raise TextAlignmentCancelled(str(exc)) from exc
            except InvalidAlignmentRequest as exc:
                raise TextAlignmentRejected(str(exc)) from exc
            except AlignmentComputeError as exc:
                # A cancelled compute (user cancel or app shutdown killed the
                # worker) is a cancellation; every other compute failure —
                # crash, protocol mismatch, missing component surfacing late —
                # is a clear failure. Neither publishes a half-built result.
                if exc.code == CANCELLED:
                    raise TextAlignmentCancelled(str(exc)) from exc
                raise TextAlignmentFailed(str(exc)) from exc
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                raise TextAlignmentFailed(str(exc)) from exc
        return result

    @contextmanager
    def _write_window(self):
        # Alignment writes touch only alignment_runs/alignment_links(+members)
        # and segment tables; search-visible data (paragraphs, pages, catalog)
        # is unchanged, so the live engine keeps serving across the write
        # transactions. SQLite locks the whole database file, not per table: a
        # first-time large-book segmentation write overflows the page cache and
        # escalates from RESERVED to a held EXCLUSIVE lock *before* commit, so an
        # overlapping search read can be blocked for much of the write, not just
        # the final commit. That block is a bounded wait, not an immediate
        # failure: both sides run a 30s busy_timeout, so a reader waits the lock
        # out and serves *as long as the writer clears the lock within that
        # window* — it is not a guarantee, a write that stayed locked past 30s
        # would still surface to the reader as a lock error (503). Keeping the
        # engine open is therefore strictly better than closing it (which would
        # 503 every overlapping search for the whole window with no consistency
        # benefit), but it converts unavailability into a bounded wait rather
        # than eliminating it. The spilling-write availability path is pinned by
        # tests/test_alignment_write_window_availability.py (which forces the
        # EXCLUSIVE escalation with a shrunk page cache); the real-default-cache
        # big-book lock-wait duration is measured separately, not by that test.
        yield

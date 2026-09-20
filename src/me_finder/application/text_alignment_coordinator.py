"""Publish automatic alignment writes through the shared index runtime."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

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
    enter_embedding_run,
    exit_embedding_run,
)
from ..managed_alignment_runtime import (
    ComputeUnavailable,
    compute_admission,
    resolve_installed_runtime_launch,
)
from ..lifecycle import DurableOperationClosedError
from ..alignment_compute import (
    AlignmentComputeError,
    CANCELLED,
    COMPONENT_MISSING,
    PROTOCOL_INCOMPATIBLE,
    WORKER_START_FAILED,
    SubprocessAlignmentComputeRunner,
)
from ..text_alignment import InvalidAlignmentRequest, generate_alignment


class TextAlignmentRejected(ValueError):
    """The requested pair cannot be aligned."""


class TextAlignmentFailed(RuntimeError):
    """Alignment computation or index publication failed on the data."""


class TextAlignmentComponentUnavailable(TextAlignmentFailed):
    """The external compute runtime/component is missing, incompatible or could
    not start — a distinct, user-actionable condition (not a parse failure).

    Subclasses TextAlignmentFailed so existing handlers still catch it, while a
    caller that wants the specific, showable reason can catch this first.
    """


class TextAlignmentCancelled(Exception):
    """The in-flight alignment was cancelled by the user."""


# Compute failures that mean "the external runtime is unavailable" (as opposed
# to a computation that ran but failed on the data). These carry a showable,
# user-actionable message to the HTTP/task layer.
_COMPONENT_CODES = {COMPONENT_MISSING, PROTOCOL_INCOMPATIBLE, WORKER_START_FAILED}

_COMPONENT_MESSAGES = {
    COMPONENT_MISSING: "对齐计算运行时未安装：请安装包含对齐组件的版本后再生成。",
    PROTOCOL_INCOMPATIBLE: "对齐计算进程版本不兼容，请更新应用后再生成。",
    WORKER_START_FAILED: "对齐计算进程无法启动：请检查应用安装是否完整。",
}


def _map_compute_error(exc: AlignmentComputeError) -> Exception:
    """Translate a compute-seam error into the coordinator's exception, keeping
    cancellation as cancellation and component problems distinct and showable."""

    if exc.code == CANCELLED:
        return TextAlignmentCancelled(str(exc))
    if exc.code in _COMPONENT_CODES:
        return TextAlignmentComponentUnavailable(_COMPONENT_MESSAGES.get(exc.code, str(exc)))
    return TextAlignmentFailed(str(exc))


def build_compute_runner(*, task_id, cancel_check, runtime_root=None):
    """Default factory: an out-of-process compute runner for this runtime.

    Injectable so tests can substitute a stub or a runner with a custom launch
    command. The runner owns the NumPy/ONNX/fastembed stack in a separate
    process; the coordinator (main process) keeps identity checks, write
    coordination and result publication.

    When an independent alignment compute runtime is installed under the runtime
    root, the runner launches *that* isolated interpreter — so the compute phase
    needs no numeric stack in the main process. Otherwise it falls back to the
    2A behaviour (the main runtime's own interpreter). A component failure never
    silently falls back to in-process computation; that is enforced by the
    runner and the coordinator's error mapping, not here.
    """

    launch = (
        resolve_installed_runtime_launch(runtime_root)
        if runtime_root is not None
        else None
    )
    if launch is not None:
        return SubprocessAlignmentComputeRunner(
            task_id=task_id,
            cancel_check=cancel_check,
            launch_command=launch.command,
            env=launch.env,
            cwd=Path(launch.cwd),
        )
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
        reviewed_body_ranges=None,
        expected_segment_set_ids=None,
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
            raise TextAlignmentComponentUnavailable(
                "对齐计算组件未安装：请在设置 → 译本对齐 中下载模型后再生成。"
            )
        with self._index_runtime.mutation():
            # A queued request must not clear cancellation of the run that owns
            # this lock. Reset before admission so shutdown after admission
            # cannot have its cancellation signal erased.
            begin_embedding_run()
            # Mark the compute task active so an uninstall of the alignment
            # runtime requested mid-computation defers until it finishes,
            # instead of removing the runtime out from under it.
            enter_embedding_run()
            try:
                # Admit this task through the runtime's shared compute lease: it
                # refuses (component-unavailable) if an install/upgrade/uninstall
                # is under way, and holds the lease so such an operation waits for
                # this task to finish rather than swapping/deleting the runtime
                # mid-compute — across application instances, not just this one.
                with compute_admission(self._paths.runtime_root), \
                        self._durable_operations.operation():
                    runner = self._compute_runner_factory(
                        task_id=uuid.uuid4().hex,
                        cancel_check=embedding_cancel_requested,
                        runtime_root=self._paths.runtime_root,
                    )
                    # Probe the external runtime *inside* the durable operation
                    # and mutation lock: it spawns a process, so it must be
                    # covered by the shutdown drain (close waits for the active
                    # operation) and be cancellable (the probe polls the same
                    # cancel signal), or a close during probe would leave an
                    # orphan. Capability is checked by probing that runtime —
                    # NOT by a main-process find_spec — and there is no silent
                    # fall back to in-process computation.
                    runner.probe()
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
                        reviewed_body_ranges=reviewed_body_ranges,
                        expected_segment_set_ids=expected_segment_set_ids,
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
            except ComputeUnavailable as exc:
                # The runtime is being installed / upgraded / uninstalled: a
                # showable, retryable component condition — not a data failure.
                raise TextAlignmentComponentUnavailable(str(exc)) from exc
            except AlignmentComputeError as exc:
                # Cancellation (user or shutdown) stays a cancellation; a missing
                # or incompatible or unstartable runtime is a distinct, showable
                # component error; every other compute failure is a plain
                # failure. None of these publish a half-built result.
                raise _map_compute_error(exc) from exc
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                raise TextAlignmentFailed(str(exc)) from exc
            finally:
                exit_embedding_run()
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

"""Thread-safe in-memory state for document import jobs."""

from __future__ import annotations

import threading
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from ..mineru_api import MinerUError


Job = Dict[str, object]
JobContext = Dict[str, object]


class ImportJobCancelled(RuntimeError):
    """Raised cooperatively after the user stops a background import."""


class ImportJobStore:
    """Own import-job state and keep compound transitions atomic."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._contexts: Dict[str, JobContext] = {}
        self._cancelled_job_ids: set[str] = set()
        self._pending_source_ids: set[str] = set()
        self._deleting_source_ids: set[str] = set()
        self._lock = threading.RLock()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Keep a caller's multi-step state transition under the store lock."""

        with self._lock:
            yield

    def restore_job(
        self,
        job_id: str,
        job: Mapping[str, object],
        context: Mapping[str, object],
    ) -> None:
        with self._lock:
            self._jobs[job_id] = deepcopy(dict(job))
            self._contexts[job_id] = deepcopy(dict(context))

    def register_background_job(self, job: Mapping[str, object]) -> None:
        job_id = str(job.get("job_id") or "")
        with self._lock:
            self._jobs[job_id] = deepcopy(dict(job))

    def register_background_job_unless_processing(
        self,
        job_id_prefix: str,
        job: Mapping[str, object],
    ) -> Optional[Job]:
        """Atomically register a singleton background job for one prefix."""

        job_id = str(job.get("job_id") or "")
        with self._lock:
            running = next(
                (
                    item
                    for item in self._jobs.values()
                    if str(item.get("job_id") or "").startswith(job_id_prefix)
                    and item.get("status") == "processing"
                ),
                None,
            )
            if running is not None:
                return deepcopy(running)
            self._jobs[job_id] = deepcopy(dict(job))
            return None

    def add_import_job(
        self,
        job: Mapping[str, object],
        context: Mapping[str, object],
        *,
        consume_reservation: bool = False,
    ) -> None:
        job_id = str(job.get("job_id") or "")
        source_file_id = str(job.get("source_file_id") or "")
        with self._lock:
            if source_file_id in self._deleting_source_ids:
                raise MinerUError("该文献正在删除，不能开始解析。")
            if (
                source_file_id in self._pending_source_ids
                and not consume_reservation
            ):
                raise MinerUError("同一文献正在准备导入。")
            if self._job_for_source_locked(
                source_file_id,
                statuses=("processing", "cancelling"),
            ):
                raise MinerUError("同一文献已有解析任务正在运行。")
            self._jobs[job_id] = deepcopy(dict(job))
            self._contexts[job_id] = deepcopy(dict(context))

    def update_job(self, job_id: str, updates: Mapping[str, object]) -> bool:
        """Update a job and report whether it has durable recovery context."""

        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(deepcopy(dict(updates)))
            return job_id in self._contexts

    def has_recovery_context(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._contexts

    def update_context(
        self,
        job_id: str,
        updates: Mapping[str, object],
    ) -> None:
        with self._lock:
            self._contexts[job_id].update(deepcopy(dict(updates)))

    def switch_job_route(
        self,
        job_id: str,
        *,
        parse_route: str,
        force_mineru: bool,
        vision_provider_id: Optional[str],
        provider_name: Optional[str],
    ) -> None:
        with self._lock:
            context = self._contexts.get(job_id)
            job = self._jobs.get(job_id)
            if context is None or job is None:
                raise MinerUError("导入任务的恢复信息不存在。")
            context["force_mineru"] = bool(force_mineru)
            context["vision_provider_id"] = vision_provider_id
            job["parse_route"] = parse_route
            job["provider_id"] = vision_provider_id
            job["provider_name"] = provider_name

    def job_snapshot(self, job_id: str) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(str(job_id))
            return deepcopy(job) if job is not None else None

    def job_and_context_snapshot(
        self,
        job_id: str,
    ) -> Optional[Tuple[Job, JobContext]]:
        with self._lock:
            job = self._jobs.get(job_id)
            context = self._contexts.get(job_id)
            if job is None or context is None:
                return None
            return deepcopy(job), deepcopy(context)

    def job_for_source(
        self,
        source_file_id: str,
        *,
        statuses: Sequence[str],
    ) -> Optional[Job]:
        with self._lock:
            job = self._job_for_source_locked(source_file_id, statuses=statuses)
            return deepcopy(job) if job is not None else None

    def processing_job_with_prefix(self, job_id_prefix: str) -> Optional[Job]:
        with self._lock:
            job = next(
                (
                    item
                    for item in self._jobs.values()
                    if str(item.get("job_id") or "").startswith(job_id_prefix)
                    and item.get("status") == "processing"
                ),
                None,
            )
            return deepcopy(job) if job is not None else None

    def active_source_ids(self) -> set[str]:
        with self._lock:
            return {
                str(job["source_file_id"])
                for job in self._jobs.values()
                if job.get("status") in {"processing", "cancelling"}
                and job.get("source_file_id")
            }

    def has_active_jobs(self) -> bool:
        with self._lock:
            return any(
                job.get("status") in {"processing", "cancelling"}
                for job in self._jobs.values()
            )

    def resumable_snapshots(self) -> List[Tuple[Job, JobContext]]:
        with self._lock:
            return [
                (deepcopy(job), deepcopy(self._contexts[job_id]))
                for job_id, job in self._jobs.items()
                if str(job.get("status") or "") in {"paused", "failed"}
                and job_id in self._contexts
            ]

    def resume_candidate(self, job_id: str) -> Tuple[Job, JobContext]:
        """Validate current resume eligibility and return isolated inputs."""

        with self._lock:
            job, context = self._resumable_job_locked(job_id)
            return deepcopy(job), deepcopy(context)

    def retry_replacement_candidate(
        self,
        job_id: str,
        *,
        statuses: Sequence[str],
    ) -> Tuple[Job, JobContext]:
        """Revalidate a retry source while its durable replacement is locked."""

        with self._lock:
            job = self._jobs.get(job_id)
            context = self._contexts.get(job_id)
            if (
                job is None
                or context is None
                or str(job.get("status") or "") not in set(statuses)
            ):
                raise MinerUError(
                    "原导入任务不存在或状态已变化，请刷新后重试。"
                )
            return deepcopy(job), deepcopy(context)

    def begin_resume(
        self,
        job_id: str,
        updates: Mapping[str, object],
    ) -> Tuple[Job, JobContext]:
        with self._lock:
            job, context = self._resumable_job_locked(job_id)
            job.update(deepcopy(dict(updates)))
            return deepcopy(job), deepcopy(context)

    def ensure_not_cancelled(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._cancelled_job_ids:
                raise ImportJobCancelled("用户已停止导入任务。")

    def request_dismissal(self, job_id: str) -> str:
        """Mark active work cancelled; leave inactive state until journal deletion."""

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return "dismissed"
            if str(job.get("status") or "") in {"processing", "cancelling"}:
                self._cancelled_job_ids.add(job_id)
                job.update(
                    status="cancelling",
                    phase="cancelling",
                    can_resume=False,
                    message="正在停止后台解析，不会再提交新的页面…",
                )
                return "cancelling"
            return "dismissed"

    def finish_cancelled_job(self, job_id: str) -> None:
        with self._lock:
            self._cancelled_job_ids.discard(job_id)
            self._jobs.pop(job_id, None)
            self._contexts.pop(job_id, None)

    def fail_cancelled_job(
        self,
        job_id: str,
        updates: Mapping[str, object],
    ) -> None:
        """Expose a durable cancellation-cleanup failure as inactive work."""

        with self._lock:
            self._jobs[job_id].update(deepcopy(dict(updates)))
            self._cancelled_job_ids.discard(job_id)

    def reserve_source(self, source_file_id: str) -> None:
        with self._lock:
            self._reserve_source_locked(source_file_id)

    def replace_reservation(
        self,
        previous_source_file_id: str,
        source_file_id: str,
    ) -> None:
        with self._lock:
            self._reserve_source_locked(source_file_id)
            self._pending_source_ids.discard(previous_source_file_id)

    def release_reservation(self, source_file_id: str) -> None:
        with self._lock:
            self._pending_source_ids.discard(str(source_file_id or ""))

    def release_reservations(self, source_file_ids: Sequence[str]) -> None:
        with self._lock:
            for source_file_id in source_file_ids:
                self._pending_source_ids.discard(str(source_file_id or ""))

    def begin_source_deletion(self, source_file_id: str) -> None:
        with self._lock:
            if source_file_id in self._deleting_source_ids:
                raise MinerUError("该文献正在删除，请勿重复操作。")
            if source_file_id in self._pending_source_ids:
                raise MinerUError("该文献正在准备导入，请稍后再删除。")
            if self._job_for_source_locked(
                source_file_id,
                statuses=("processing",),
            ):
                raise MinerUError(
                    "该文献仍在解析中，请等待任务结束后再删除。"
                )
            self._deleting_source_ids.add(source_file_id)

    def end_source_deletion(self, source_file_id: str) -> None:
        with self._lock:
            self._deleting_source_ids.discard(source_file_id)

    def source_job_ids(self, source_file_ids: Sequence[str]) -> List[str]:
        source_ids = {str(value) for value in source_file_ids}
        with self._lock:
            return [
                job_id
                for job_id, job in self._jobs.items()
                if str(job.get("source_file_id") or "") in source_ids
            ]

    def remove_job(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            self._contexts.pop(job_id, None)

    def remove_jobs(self, job_ids: Sequence[str]) -> None:
        with self._lock:
            for job_id in job_ids:
                self._jobs.pop(job_id, None)
                self._contexts.pop(job_id, None)

    def target_is_referenced(self, target: Path) -> bool:
        resolved = Path(target).resolve()
        with self._lock:
            return any(
                Path(context.get("target") or "").resolve() == resolved
                for context in self._contexts.values()
                if context.get("target")
            )

    def _reserve_source_locked(self, source_file_id: str) -> None:
        if source_file_id in self._deleting_source_ids:
            raise MinerUError("该文献正在删除，不能开始解析。")
        if source_file_id in self._pending_source_ids:
            raise MinerUError("同一文献正在准备导入。")
        if self._job_for_source_locked(
            source_file_id,
            statuses=("processing", "cancelling"),
        ):
            raise MinerUError("同一文献已有解析任务正在运行。")
        self._pending_source_ids.add(source_file_id)

    def _resumable_job_locked(
        self,
        job_id: str,
    ) -> Tuple[Job, JobContext]:
        job = self._jobs.get(job_id)
        context = self._contexts.get(job_id)
        if not job or not context:
            raise MinerUError("待继续的导入任务不存在。")
        if (
            str(job.get("status") or "") not in {"paused", "failed"}
            or not job.get("can_resume")
        ):
            raise MinerUError("该导入任务当前不能继续。")
        source_file_id = str(context.get("source_file_id") or "")
        if source_file_id in self._deleting_source_ids:
            raise MinerUError("该文献正在删除，不能继续解析。")
        if source_file_id in self._pending_source_ids:
            raise MinerUError("同一文献正在准备导入。")
        if self._job_for_source_locked(
            source_file_id,
            statuses=("processing", "cancelling"),
            excluded_job_id=job_id,
        ):
            raise MinerUError("同一文献已有解析任务正在运行。")
        return job, context

    def _job_for_source_locked(
        self,
        source_file_id: str,
        *,
        statuses: Sequence[str],
        excluded_job_id: Optional[str] = None,
    ) -> Optional[Job]:
        allowed = set(statuses)
        return next(
            (
                job
                for job_id, job in self._jobs.items()
                if job_id != excluded_job_id
                and job.get("source_file_id") == source_file_id
                and job.get("status") in allowed
            ),
            None,
        )

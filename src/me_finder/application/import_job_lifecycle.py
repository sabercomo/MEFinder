"""Durable state transitions for document import jobs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, Mapping, NamedTuple, Optional, Sequence

from ..import_job_journal import ImportJobJournal
from ..import_resume import ResumeManifestError, sha256_file
from ..mineru_api import MinerUError
from .import_job_store import ImportJobStore, Job, JobContext


FailureStageResolver = Callable[..., Optional[str]]
JobUpdater = Callable[..., None]


class ImportJobCleanupFailed(RuntimeError):
    """Raised after a cancellation cleanup failure becomes durable."""


class ImportJobRetrySwapFailed(RuntimeError):
    """Raised when retry replacement compensation also cannot finish."""


class ImportResumeTransition(NamedTuple):
    """Inputs selected by one atomic durable resume transition."""

    job: Job
    context: JobContext
    target: Path
    retry_index_only: bool


class ImportJobLifecycle:
    """Keep journal and in-memory import-job state in a safe order."""

    def __init__(
        self,
        journal: ImportJobJournal,
        store: ImportJobStore,
        *,
        hash_file: Callable[[Path], str] = sha256_file,
    ) -> None:
        self._journal = journal
        self._store = store
        self._hash_file = hash_file

    def restore_startup_jobs(
        self,
        infer_failure_stage: FailureStageResolver,
    ) -> None:
        raw_jobs = self._journal.list_jobs()
        raw_job_ids = {
            str(saved_job.get("job_id") or "") for saved_job in raw_jobs
        }
        skipped_jobs: Dict[str, str] = {}
        replacement_lineage_ids = {
            str(saved_job.get("replacement_lineage_id") or "")
            for saved_job in raw_jobs
            if saved_job.get("replacement_lineage_id")
        }
        replacement_lineages: Dict[str, list[Mapping[str, object]]] = {}
        job_lineage_ids: Dict[str, str] = {}
        for saved_job in raw_jobs:
            saved_job_id = str(saved_job.get("job_id") or "")
            lineage_id = str(
                saved_job.get("replacement_lineage_id")
                or (
                    saved_job_id
                    if saved_job_id in replacement_lineage_ids
                    else ""
                )
            )
            if lineage_id:
                replacement_lineages.setdefault(lineage_id, []).append(
                    saved_job
                )
                job_lineage_ids[saved_job_id] = lineage_id
            elif str(saved_job.get("status") or "") == "cancelling":
                skipped_jobs[saved_job_id] = "cancelled"
        lineage_obsolete_jobs: Dict[str, list[tuple[str, str]]] = {}
        cancelling_lineage_anchors: Dict[str, str] = {}
        for lineage_id, lineage_jobs in replacement_lineages.items():
            committed_candidates = [
                item
                for item in lineage_jobs
                if not item.get("replaces_job_id")
                or str(item.get("replaces_job_id") or "")
                not in raw_job_ids
            ]
            committed = max(
                committed_candidates,
                key=lambda item: (
                    int(item.get("replacement_generation") or 0),
                    str(item.get("last_updated") or ""),
                    str(item.get("job_id") or ""),
                ),
            )
            committed_job_id = str(committed.get("job_id") or "")
            if str(committed.get("status") or "") == "cancelling":
                skipped_jobs[committed_job_id] = "cancelled"
                cancelling_lineage_anchors[lineage_id] = committed_job_id
            for replacement in lineage_jobs:
                replacement_job_id = str(replacement.get("job_id") or "")
                if replacement_job_id != committed_job_id:
                    reason = (
                        "uncommitted retry replacement"
                        if str(replacement.get("replaces_job_id") or "")
                        in raw_job_ids
                        else "superseded retry replacement"
                    )
                    skipped_jobs[replacement_job_id] = reason
                    lineage_obsolete_jobs.setdefault(lineage_id, []).append(
                        (replacement_job_id, reason)
                    )
        blocked_lineage_ids: set[str] = set()
        for saved_job_id, reason in tuple(skipped_jobs.items()):
            if saved_job_id in job_lineage_ids:
                continue
            try:
                self._journal.delete_job(saved_job_id)
            except (OSError, ValueError) as exc:
                logging.warning(
                    "failed to remove %s journal %s: %s",
                    reason,
                    saved_job_id,
                    exc,
                )
                lineage_id = job_lineage_ids.get(saved_job_id)
                if lineage_id:
                    blocked_lineage_ids.add(lineage_id)
        for lineage_id in replacement_lineages:
            cleanup_failed = False
            for saved_job_id, reason in sorted(
                lineage_obsolete_jobs.get(lineage_id, [])
            ):
                try:
                    self._journal.delete_job(saved_job_id)
                except (OSError, ValueError) as exc:
                    logging.warning(
                        "failed to remove %s journal %s: %s",
                        reason,
                        saved_job_id,
                        exc,
                    )
                    blocked_lineage_ids.add(lineage_id)
                    cleanup_failed = True
                    break
            if cleanup_failed:
                continue
            cancelling_anchor = cancelling_lineage_anchors.get(lineage_id)
            if cancelling_anchor:
                try:
                    self._journal.delete_job(cancelling_anchor)
                except (OSError, ValueError) as exc:
                    logging.warning(
                        "failed to remove cancelled journal %s: %s",
                        cancelling_anchor,
                        exc,
                    )
                    blocked_lineage_ids.add(lineage_id)
        for lineage_id in blocked_lineage_ids:
            logging.warning(
                "skipping retry lineage %s after cleanup failure",
                lineage_id,
            )
            for saved_job in replacement_lineages[lineage_id]:
                skipped_jobs[
                    str(saved_job.get("job_id") or "")
                ] = "blocked retry lineage"
        for saved_job in self._journal.load_startup_jobs(
            skip_job_ids=tuple(skipped_jobs),
        ):
            saved_job_id = str(saved_job.get("job_id") or "")
            saved_context = saved_job.get("context")
            if not isinstance(saved_context, dict):
                continue
            target_text = str(saved_context.get("target") or "")
            if not saved_job_id or not target_text:
                continue
            restored_job = {
                key: value
                for key, value in saved_job.items()
                if key
                not in {
                    "context",
                    "file_hash",
                    "job_log_spec_version",
                    "replaces_job_id",
                    "replacement_lineage_id",
                    "replacement_generation",
                }
            }
            restored_failure_stage = infer_failure_stage(
                restored_job,
                is_pdf=bool(saved_context.get("is_pdf")),
            )
            if restored_failure_stage:
                restored_job["failure_stage"] = restored_failure_stage
                restored_job["can_resume"] = True
                if str(restored_job.get("status") or "") == "failed":
                    restored_job["phase"] = "index_failed"
                try:
                    self._journal.update_job(
                        saved_job_id,
                        failure_stage=restored_failure_stage,
                        phase=restored_job.get("phase"),
                        can_resume=True,
                    )
                except (KeyError, OSError, ValueError, ResumeManifestError):
                    logging.warning(
                        "failed to upgrade legacy index-retry journal %s",
                        saved_job_id,
                    )
            self._store.restore_job(
                saved_job_id,
                restored_job,
                {
                    "target": Path(target_text),
                    "source_file_id": str(
                        saved_context.get("source_file_id") or ""
                    ),
                    "profile": dict(saved_context.get("profile") or {}),
                    "is_pdf": bool(saved_context.get("is_pdf")),
                    "force_mineru": bool(saved_context.get("force_mineru")),
                    "vision_provider_id": saved_context.get("provider_id"),
                    "file_hash": str(saved_job.get("file_hash") or ""),
                },
            )

    def add_job(
        self,
        job: Mapping[str, object],
        context: Mapping[str, object],
        *,
        target: Path,
        source_file_id: str,
        profile: Mapping[str, object],
        is_pdf: bool,
        force_mineru: bool,
        provider_id: Optional[str],
        total_pages: int,
        consume_reservation: bool,
        replaces_job_id: Optional[str] = None,
    ) -> None:
        job_id = str(job.get("job_id") or "")
        self._store.add_import_job(
            job,
            context,
            consume_reservation=consume_reservation,
        )
        try:
            record = self._journal.save_job(
                job,
                target=target,
                source_file_id=source_file_id,
                profile=profile,
                is_pdf=is_pdf,
                force_mineru=force_mineru,
                provider_id=provider_id,
                total_pages=total_pages,
                replaces_job_id=replaces_job_id,
            )
        except Exception:
            self._store.remove_job(job_id)
            raise
        self._store.update_context(
            job_id,
            {"file_hash": str(record.get("file_hash") or "")},
        )

    def replace_job_for_retry(
        self,
        previous_job_id: str,
        job: Mapping[str, object],
        context: Mapping[str, object],
        *,
        previous_statuses: Sequence[str],
        target: Path,
        source_file_id: str,
        profile: Mapping[str, object],
        is_pdf: bool,
        force_mineru: bool,
        provider_id: Optional[str],
        total_pages: int,
    ) -> None:
        """Durably swap a retry source before the replacement is enqueued."""

        job_id = str(job.get("job_id") or "")
        with self._store.atomic():
            self._store.retry_replacement_candidate(
                previous_job_id,
                statuses=previous_statuses,
            )
            self.add_job(
                job,
                context,
                target=target,
                source_file_id=source_file_id,
                profile=profile,
                is_pdf=is_pdf,
                force_mineru=force_mineru,
                provider_id=provider_id,
                total_pages=total_pages,
                consume_reservation=False,
                replaces_job_id=previous_job_id,
            )
            try:
                replacement_record = self._journal.get_job(job_id)
                if replacement_record is None:
                    raise KeyError(job_id)
                self._journal.commit_retry_replacement(
                    lineage_id=str(
                        replacement_record["replacement_lineage_id"]
                    ),
                    replacement_job_id=job_id,
                    predecessor_job_id=previous_job_id,
                )
            except (KeyError, OSError, ValueError) as swap_error:
                try:
                    if not self._journal.delete_job(job_id):
                        raise KeyError(job_id)
                except (KeyError, OSError, ValueError) as rollback_error:
                    logging.error(
                        "failed to roll back retry replacement journal %s: %s",
                        job_id,
                        rollback_error,
                    )
                    raise ImportJobRetrySwapFailed(
                        "重试任务替换失败，且新任务记录回滚失败："
                        f"{swap_error}; {rollback_error}"
                    ) from swap_error
                finally:
                    self._store.remove_job(job_id)
                raise
            self._store.remove_job(previous_job_id)

    def update_job(self, job_id: str, **updates: object) -> None:
        persisted_updates = dict(updates)
        progress = updates.get("progress")
        if isinstance(progress, dict):
            resume = progress.get("resume")
            for field in ("total_pages", "completed_pages", "failed_pages"):
                if field in progress:
                    persisted_updates[field] = progress[field]
                elif isinstance(resume, dict) and field in resume:
                    persisted_updates[field] = resume[field]
        status = str(updates.get("status") or "")
        if "can_resume" not in updates:
            if status == "completed":
                persisted_updates["can_resume"] = False
            elif status == "failed":
                persisted_updates["can_resume"] = True
            elif status == "processing":
                persisted_updates["can_resume"] = False
        with self._store.atomic():
            if status in {"completed", "failed"}:
                self._store.ensure_not_cancelled(job_id)
            durable = self._store.has_recovery_context(job_id)
            if durable:
                if status == "completed":
                    if not self._journal.delete_job(job_id):
                        raise KeyError(job_id)
                else:
                    self._journal.update_job(job_id, **persisted_updates)
            self._store.update_job(job_id, persisted_updates)

    def progress_job(
        self,
        job_id: str,
        update: Dict[str, object],
        update_job: JobUpdater,
    ) -> None:
        self._store.ensure_not_cancelled(job_id)
        phase = str(update.get("phase") or "")
        message = "正在处理…"
        if phase == "mineru_processing":
            if update.get("waiting_for_credential"):
                message = (
                    f"等待可用的 MinerU 账号：{update.get('completed', 0)}/"
                    f"{update.get('total', 0)} 个分段已完成"
                )
            else:
                message = (
                    f"MinerU 解析中：{update.get('completed', 0)}/"
                    f"{update.get('total', 0)} 个分段"
                )
        elif phase == "vision_processing":
            provider_name = str(update.get("provider_name") or "其他视觉 API")
            message = (
                f"{provider_name} 解析中："
                f"{update.get('completed', 0)}/{update.get('total', 0)} 页"
            )
        elif phase == "local_ocr_processing":
            provider_name = str(update.get("provider_name") or "本地 OCR")
            message = (
                f"{provider_name} 识别中："
                f"{update.get('completed', 0)}/{update.get('total', 0)} 页"
            )
        elif phase == "rebuilding_index":
            message = "正在重建本地 SQLite 索引…"
        updates: Dict[str, object] = {
            "phase": phase,
            "message": message,
            "progress": update,
        }
        if update.get("provider_id"):
            updates["provider_id"] = update["provider_id"]
        if update.get("provider_name"):
            updates["provider_name"] = update["provider_name"]
        update_job(job_id, **updates)

    def ensure_not_cancelled(self, job_id: str) -> None:
        self._store.ensure_not_cancelled(job_id)

    def finish_cancelled_job(self, job_id: str) -> None:
        with self._store.atomic():
            try:
                deleted = self._journal.delete_job(job_id)
            except OSError as exc:
                detail = str(exc).strip() or type(exc).__name__
                updates: Dict[str, object] = {
                    "status": "failed",
                    "phase": "cancellation_cleanup_failed",
                    "failure_stage": "cleanup",
                    "can_resume": False,
                    "error": f"取消任务清理失败：{detail}",
                    "message": (
                        "任务已停止，但持久化记录清理失败："
                        f"{detail}。"
                    ),
                }
                try:
                    self._journal.update_job(job_id, **updates)
                except (KeyError, OSError, ValueError, ResumeManifestError) as state_exc:
                    state_detail = str(state_exc).strip() or type(state_exc).__name__
                    updates["error"] = (
                        f"取消任务清理失败：{detail}；"
                        f"失败状态未能写入：{state_detail}"
                    )
                    updates["message"] = str(updates["error"])
                    self._store.fail_cancelled_job(job_id, updates)
                    raise ImportJobCleanupFailed(
                        f"导入任务 {job_id} 取消清理失败："
                        f"{detail}；状态写入失败：{state_detail}"
                    ) from state_exc
                self._store.fail_cancelled_job(job_id, updates)
                raise ImportJobCleanupFailed(
                    f"导入任务 {job_id} 取消清理失败：{detail}"
                ) from exc
            if not deleted:
                self._store.finish_cancelled_job(job_id)
                return
            self._store.finish_cancelled_job(job_id)

    def switch_route(
        self,
        job_id: str,
        *,
        parse_route: str,
        force_mineru: bool,
        vision_provider_id: Optional[str],
        provider_name: Optional[str],
    ) -> None:
        with self._store.atomic():
            self._journal.switch_parser_route(
                job_id,
                parse_route=parse_route,
                force_mineru=bool(force_mineru),
                provider_id=vision_provider_id,
                provider_name=provider_name,
            )
            self._store.switch_job_route(
                job_id,
                parse_route=parse_route,
                force_mineru=force_mineru,
                vision_provider_id=vision_provider_id,
                provider_name=provider_name,
            )

    def validated_target(
        self,
        job_id: str,
        context: Mapping[str, object],
        update_job: JobUpdater,
    ) -> Path:
        target = Path(context["target"])
        record = self._journal.get_job(job_id)
        expected_hash = str(
            context.get("file_hash")
            or (record or {}).get("file_hash")
            or ""
        )
        if not target.is_file():
            message = "待恢复的原始文件已不存在。"
        elif not expected_hash:
            message = "待恢复任务缺少文件校验信息，不能安全继续。"
        elif self._hash_file(target) != expected_hash:
            message = "原始文件内容已经变化，旧断点不会继续使用。"
        else:
            if isinstance(context, dict):
                context["file_hash"] = expected_hash
            return target
        update_job(
            job_id,
            status="failed",
            phase="failed",
            can_resume=False,
            message=message,
        )
        raise MinerUError(message)

    def begin_resume(
        self,
        job_id: str,
        *,
        infer_failure_stage: FailureStageResolver,
        validate_target: Callable[[str, Mapping[str, object]], Path],
    ) -> ImportResumeTransition:
        with self._store.atomic():
            job, context = self._store.resume_candidate(job_id)
            target = validate_target(job_id, context)
            retry_index_only = (
                infer_failure_stage(
                    job,
                    is_pdf=bool(context.get("is_pdf")),
                )
                == "index"
            )
            next_phase = "rebuilding_index" if retry_index_only else "stored"
            next_message = (
                "正在重新建立索引，不会再次调用解析 API…"
                if retry_index_only
                else "正在从上次断点继续…"
            )
            state_updates: Dict[str, object] = {
                "status": "processing",
                "phase": next_phase,
                "can_resume": False,
                "message": next_message,
                "vision_failed": False,
                "mineru_failed": False,
                "mineru_interrupted": False,
                "can_retry_with_provider": False,
                "retry_provider_id": None,
                "retry_provider_name": None,
                "needs_provider_config": False,
            }
            journal_updates = dict(state_updates)
            if retry_index_only:
                journal_updates["failure_stage"] = "index"
            self._journal.update_job(job_id, **journal_updates)
            restored, context_snapshot = self._store.begin_resume(
                job_id,
                state_updates,
            )
        return ImportResumeTransition(
            restored,
            context_snapshot,
            target,
            retry_index_only,
        )

    def dismiss_job(self, job_id: str) -> str:
        with self._store.atomic():
            result = self._store.request_dismissal(job_id)
            if result == "cancelling":
                self._journal.update_job(
                    job_id,
                    status="cancelling",
                    phase="cancelling",
                    can_resume=False,
                    message="正在停止后台解析，不会再提交新的页面…",
                )
                return result
            self._journal.delete_job(job_id)
            self._store.remove_job(job_id)
            return result

    def purge_source_jobs(self, source_file_ids: Sequence[str]) -> list[str]:
        stale_job_ids = self._store.source_job_ids(source_file_ids)
        warnings: list[str] = []
        removed_job_ids: list[str] = []
        for job_id in stale_job_ids:
            try:
                self._journal.delete_job(job_id)
            except (OSError, ValueError) as exc:
                logging.warning(
                    "failed to remove stale import journal %s: %s",
                    job_id,
                    exc,
                )
                warnings.append(f"{job_id}: {exc}")
            removed_job_ids.append(job_id)
        self._store.remove_jobs(removed_job_ids)
        return warnings

    def rollback_unqueued_batch(self, queued_items: Sequence[Job]) -> None:
        for queued in queued_items:
            queued_job_id = str(queued["job_id"])
            try:
                self._journal.delete_job(queued_job_id)
            except OSError:
                logging.warning(
                    "failed to remove unqueued batch journal %s",
                    queued_job_id,
                )
            self._store.remove_job(queued_job_id)

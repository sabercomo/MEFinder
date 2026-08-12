"""Execution policies for batches of already-created import jobs."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Protocol, Sequence

from ..import_queue import ImportQueueClosedError, ImportQueueFullError
from ..mineru_api import MinerUError
from .import_job_lifecycle import ImportJobCleanupFailed
from .import_job_store import ImportJobCancelled


BatchItem = Dict[str, object]


class TaskQueuePort(Protocol):
    def submit(self, task, *args: object) -> None:
        ...


class ImportBatchJobPort(Protocol):
    """Job operations needed after every batch job has been created."""

    def update_import_job(self, job_id: str, **updates: object) -> None:
        ...

    def prepare_import_job(
        self,
        job_id: str,
        target: Path,
        source_file_id: str,
        profile: Dict[str, object],
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
    ) -> bool:
        ...

    def index_registered_pdf(
        self,
        job_id: str,
        source_file_id: str,
        *,
        backup_existing: bool = False,
    ) -> None:
        ...

    def rebuild_runtime_index(
        self,
        job_id: str,
        expected_source_ids: Optional[List[str]] = None,
    ) -> set[str]:
        ...

    def fail_import_at_index(
        self,
        job_id: str,
        exc: Exception,
        *,
        parsed: bool = False,
    ) -> None:
        ...

    def fail_import_at_queue(self, job_id: str) -> None:
        ...

    def finalize_import_job(
        self,
        job_id: str,
        source_file_id: str,
        is_pdf: bool,
    ) -> None:
        ...

    def ensure_import_not_cancelled(self, job_id: str) -> None:
        ...

    def finish_cancelled_import_job(self, job_id: str) -> None:
        ...


class ImportBatchExecutor:
    """Submit and run batches whose durable jobs already exist."""

    def __init__(self, task_queue: TaskQueuePort) -> None:
        self._task_queue = task_queue

    @staticmethod
    def _finish_cancelled(jobs: ImportBatchJobPort, job_id: str) -> None:
        try:
            jobs.finish_cancelled_import_job(job_id)
        except ImportJobCleanupFailed:
            logging.error(
                "cancelled import job cleanup failed for %s",
                job_id,
                exc_info=True,
            )

    @staticmethod
    def _finish_if_cancelled(jobs: ImportBatchJobPort, job_id: str) -> bool:
        try:
            jobs.ensure_import_not_cancelled(job_id)
        except ImportJobCancelled:
            ImportBatchExecutor._finish_cancelled(jobs, job_id)
            return True
        return False

    @staticmethod
    def _fail_if_active(
        jobs: ImportBatchJobPort,
        job_id: str,
        exc: Exception,
        *,
        parsed: bool = False,
    ) -> None:
        try:
            jobs.fail_import_at_index(job_id, exc, parsed=parsed)
        except ImportJobCancelled:
            ImportBatchExecutor._finish_cancelled(jobs, job_id)
            return
        ImportBatchExecutor._finish_if_cancelled(jobs, job_id)

    def submit_native(
        self,
        queued_items: Sequence[BatchItem],
        *,
        jobs: ImportBatchJobPort,
    ) -> None:
        try:
            self._task_queue.submit(
                self._run_native_batch,
                queued_items,
                jobs,
            )
        except (ImportQueueFullError, ImportQueueClosedError):
            for item in queued_items:
                jobs.fail_import_at_queue(str(item["job_id"]))

    @staticmethod
    def _run_native_batch(
        queued_items: Sequence[BatchItem],
        jobs: ImportBatchJobPort,
    ) -> None:
        job_ids = [str(item["job_id"]) for item in queued_items]
        batch_size = len(job_ids)
        pdf_only = all(bool(item["is_pdf"]) for item in queued_items)
        active_items: List[BatchItem] = []
        for item in queued_items:
            job_id = str(item["job_id"])
            if ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
                continue
            jobs.update_import_job(
                job_id,
                phase="text_parsing" if pdf_only else "rebuilding_index",
                message=(
                    f"正在逐份解析并写入索引（共 {batch_size} 个 PDF）…"
                    if pdf_only
                    else f"正在批量建立索引（共 {batch_size} 个文件）…"
                ),
                parse_route="native",
            )
            if not ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
                active_items.append(item)
        if pdf_only:
            for item in active_items:
                job_id = str(item["job_id"])
                try:
                    jobs.index_registered_pdf(
                        job_id,
                        str(item["source_file_id"]),
                        backup_existing=False,
                    )
                except ImportJobCancelled:
                    ImportBatchExecutor._finish_cancelled(jobs, job_id)
                    continue
                except Exception as exc:
                    if not ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
                        ImportBatchExecutor._fail_if_active(jobs, job_id, exc)
                    continue
                if ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
                    continue
                try:
                    jobs.finalize_import_job(
                        job_id,
                        str(item["source_file_id"]),
                        True,
                    )
                except ImportJobCancelled:
                    ImportBatchExecutor._finish_cancelled(jobs, job_id)
                    continue
                ImportBatchExecutor._finish_if_cancelled(jobs, job_id)
            return

        while active_items:
            active_items = [
                item
                for item in active_items
                if not ImportBatchExecutor._finish_if_cancelled(
                    jobs,
                    str(item["job_id"]),
                )
            ]
            if not active_items:
                return
            anchor_job_id = str(active_items[0]["job_id"])
            expected_source_ids = [
                str(item["source_file_id"])
                for item in active_items
                if bool(item["is_pdf"])
            ]
            try:
                missing_source_ids = jobs.rebuild_runtime_index(
                    anchor_job_id,
                    expected_source_ids,
                )
                break
            except ImportJobCancelled:
                ImportBatchExecutor._finish_cancelled(jobs, anchor_job_id)
                active_items = active_items[1:]
            except Exception as exc:
                for item in active_items:
                    job_id = str(item["job_id"])
                    if ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
                        continue
                    ImportBatchExecutor._fail_if_active(jobs, job_id, exc)
                return
        for item in active_items:
            job_id = str(item["job_id"])
            if ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
                continue
            source_file_id = str(item["source_file_id"])
            if source_file_id in missing_source_ids:
                ImportBatchExecutor._fail_if_active(
                    jobs,
                    job_id,
                    MinerUError(
                        f"{item.get('display_file_name') or Path(item['target']).name} "
                        "未能进入索引：重建后未找到文献记录。"
                    ),
                )
                continue
            try:
                jobs.finalize_import_job(
                    job_id,
                    source_file_id,
                    bool(item["is_pdf"]),
                )
            except ImportJobCancelled:
                ImportBatchExecutor._finish_cancelled(jobs, job_id)
                continue
            ImportBatchExecutor._finish_if_cancelled(jobs, job_id)

    def submit_remote(
        self,
        queued_items: Sequence[BatchItem],
        *,
        jobs: ImportBatchJobPort,
    ) -> None:
        commit_lock = threading.Lock()
        for item in queued_items:
            try:
                self._task_queue.submit(
                    self._run_remote_item,
                    item,
                    jobs,
                    commit_lock,
                )
            except (ImportQueueFullError, ImportQueueClosedError):
                jobs.fail_import_at_queue(str(item["job_id"]))

    @staticmethod
    def _run_remote_item(
        item: Mapping[str, object],
        jobs: ImportBatchJobPort,
        commit_lock: threading.Lock,
    ) -> None:
        job_id = str(item["job_id"])
        source_file_id = str(item["source_file_id"])
        if ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
            return
        try:
            succeeded = jobs.prepare_import_job(
                job_id,
                Path(item["target"]),
                source_file_id,
                dict(item["profile"]),
                bool(item["is_pdf"]),
                bool(item["force_mineru"]),
                (
                    str(item["vision_provider_id"])
                    if item.get("vision_provider_id")
                    else None
                ),
            )
        except ImportJobCancelled:
            ImportBatchExecutor._finish_cancelled(jobs, job_id)
            return
        if ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
            return
        if not succeeded:
            return
        jobs.update_import_job(
            job_id,
            phase="rebuilding_index",
            message="解析完成，正在写入本地索引…",
        )
        if ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
            return
        try:
            with commit_lock:
                if ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
                    return
                jobs.index_registered_pdf(
                    job_id,
                    source_file_id,
                    backup_existing=False,
                )
        except ImportJobCancelled:
            ImportBatchExecutor._finish_cancelled(jobs, job_id)
            return
        except Exception as exc:
            if not ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
                ImportBatchExecutor._fail_if_active(
                    jobs,
                    job_id,
                    exc,
                    parsed=True,
                )
            return
        if ImportBatchExecutor._finish_if_cancelled(jobs, job_id):
            return
        try:
            jobs.finalize_import_job(
                job_id,
                source_file_id,
                bool(item["is_pdf"]),
            )
        except ImportJobCancelled:
            ImportBatchExecutor._finish_cancelled(jobs, job_id)
            return
        ImportBatchExecutor._finish_if_cancelled(jobs, job_id)

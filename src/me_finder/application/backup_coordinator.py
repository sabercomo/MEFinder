"""Coordinate runtime backup export and restore outside HTTP handlers."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Dict, Mapping, Protocol

from ..app_context import AppPaths
from ..backup_service import restore_backup, write_backup
from ..import_queue import ImportQueueClosedError, ImportQueueFullError
from ..mineru_api import MinerUError
from ..pdf_import_service import import_config_lock


class BackupIndexPort(Protocol):
    def mutation(self) -> AbstractContextManager[None]:
        ...


class BackupJobsPort(Protocol):
    def register_background_job(self, job: Mapping[str, object]) -> None:
        ...

    def submit_background_task(
        self, task: Callable[..., None], *args: object
    ) -> None:
        ...

    def rebuild_runtime_index(self, job_id: str) -> set[str]:
        ...

    def update_import_job(self, job_id: str, **updates: object) -> None:
        ...


class DurableOperationsPort(Protocol):
    def operation(self) -> AbstractContextManager[None]:
        ...


BackupRoot = Callable[[], Path]
BackupWriter = Callable[..., Path]
BackupRestorer = Callable[..., Dict[str, object]]
ConfigLock = Callable[[], AbstractContextManager[None]]


class BackupQueueError(MinerUError):
    """A registered restore job could not enter the background queue."""


class BackupCoordinator:
    """Export curated state and restore it under the runtime mutation lock."""

    def __init__(
        self,
        paths: AppPaths,
        index_runtime: BackupIndexPort,
        durable_operations: DurableOperationsPort,
        jobs: BackupJobsPort,
        *,
        app_data_root: BackupRoot,
        write: BackupWriter = write_backup,
        restore: BackupRestorer = restore_backup,
        config_lock: ConfigLock = import_config_lock,
    ) -> None:
        self._paths = paths
        self._index_runtime = index_runtime
        self._durable_operations = durable_operations
        self._jobs = jobs
        self._app_data_root = app_data_root
        self._write = write
        self._restore = restore
        self._config_lock = config_lock

    def export(self, *, output_dir: Path | None = None) -> Dict[str, object]:
        app_data_root = self._app_data_root()
        destination = (
            Path(output_dir) if output_dir is not None else app_data_root / "backups"
        )
        target = self._write(
            self._paths.runtime_root,
            destination,
            app_data_root=app_data_root,
        )
        return {
            "ok": True,
            "path": str(target),
            "size_bytes": target.stat().st_size,
        }

    def start_restore(self, source_path: str) -> str:
        path = Path(str(source_path)).expanduser()
        if not path.is_file():
            raise MinerUError("备份文件不存在。")
        if path.suffix.lower() != ".zip":
            raise MinerUError("请选择 .zip 备份文件。")

        job_id = f"restore-{uuid.uuid4().hex[:12]}"
        self._jobs.register_background_job(
            {
                "job_id": job_id,
                "status": "processing",
                "phase": "restoring_backup",
                "message": "正在恢复备份并重建索引…",
            }
        )
        try:
            self._jobs.submit_background_task(
                self._run_restore_job,
                job_id,
                path,
            )
        except (ImportQueueFullError, ImportQueueClosedError) as exc:
            self._jobs.update_import_job(
                job_id,
                status="failed",
                phase="queue_failed",
                message="备份恢复任务未能进入队列。",
            )
            raise BackupQueueError(
                "备份恢复任务暂时无法启动，文件未更改。"
            ) from exc
        return job_id

    def _run_restore_job(self, job_id: str, path: Path) -> None:
        try:
            with (
                self._durable_operations.operation(),
                self._index_runtime.mutation(),
                self._config_lock(),
            ):
                summary = self._restore(
                    self._paths.runtime_root,
                    path.read_bytes(),
                    app_data_root=self._app_data_root(),
                )
                self._jobs.update_import_job(
                    job_id,
                    phase="rebuilding_index",
                    message=(
                        f"已恢复 {summary['count']} 项，正在重建索引…"
                    ),
                )
                self._jobs.rebuild_runtime_index(job_id)
            self._jobs.update_import_job(
                job_id,
                status="completed",
                phase="completed",
                message=(
                    f"备份已恢复并重建索引：{summary['count']} 项"
                ),
            )
        except (
            MinerUError,
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            self._jobs.update_import_job(
                job_id,
                status="failed",
                phase="failed",
                message=f"文件已恢复，但索引重建失败：{exc}",
            )

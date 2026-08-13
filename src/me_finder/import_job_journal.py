"""Persistent, JSON-only descriptions of import jobs.

The worker queue intentionally remains in-memory because Python callables
cannot be restored safely.  This journal stores only the data needed to let a
user resume a job after restarting the app; loading it never submits work.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from .import_resume import (
    ResumeManifestError,
    atomic_write_json,
    fsync_directory,
    load_json_object,
    quarantine_corrupt_manifest,
    sha256_file,
)


JOB_LOG_SPEC_VERSION = 1
DEFAULT_IMPORT_JOB_DIR = Path("corpus/processed/import_jobs")
_RECOVERABLE_RUNNING_STATUSES = frozenset({"queued", "processing"})
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JOB_DISPLAY_FIELDS = (
    "phase",
    "message",
    "file_name",
    "parse_route",
    "provider_id",
    "provider_name",
    "detected_pdf_type",
    "size_bytes",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _page_list(values: Optional[Sequence[object]]) -> List[object]:
    return list(values or [])


class ImportJobJournal:
    """Store resumable import job descriptions as one JSON file per job."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._lock = threading.RLock()

    def save_job(
        self,
        job: Mapping[str, object],
        *,
        target: Path,
        source_file_id: str,
        profile: Mapping[str, object],
        is_pdf: bool,
        force_mineru: bool = False,
        provider_id: Optional[str] = None,
        total_pages: int = 0,
        completed_pages: Optional[Sequence[object]] = None,
        failed_pages: Optional[Sequence[object]] = None,
        replaces_job_id: Optional[str] = None,
    ) -> Dict[str, object]:
        """Persist one serializable job description and its retry context.

        Only known display fields are copied from ``job``.  In particular,
        callables and other runtime objects are never serialized.
        """

        job_id = str(job.get("job_id") or "").strip()
        path = self._job_path(job_id)
        target_path = Path(target).expanduser().resolve()
        if not target_path.is_file():
            raise FileNotFoundError(target_path)
        target_stat = target_path.stat()
        status = str(job.get("status") or "queued").strip().lower()
        record: Dict[str, object] = {
            "job_log_spec_version": JOB_LOG_SPEC_VERSION,
            "job_id": job_id,
            "source_file_id": str(source_file_id),
            "file_type": "pdf" if is_pdf else "docx",
            "status": status,
            "can_resume": status in _RECOVERABLE_RUNNING_STATUSES,
            "context": {
                "target": str(target_path),
                "source_file_id": str(source_file_id),
                "profile": dict(profile),
                "is_pdf": bool(is_pdf),
                "force_mineru": bool(force_mineru),
                "provider_id": str(provider_id) if provider_id else None,
            },
            "file_hash": sha256_file(target_path),
            "file_size": int(target_stat.st_size),
            "file_mtime_ns": int(target_stat.st_mtime_ns),
            "total_pages": max(0, int(total_pages or 0)),
            "completed_pages": _page_list(completed_pages),
            "failed_pages": _page_list(failed_pages),
            "last_updated": _utc_now_iso(),
        }
        for field in _JOB_DISPLAY_FIELDS:
            if field in job:
                record[field] = job[field]
        with self._lock:
            if replaces_job_id:
                predecessor_id = str(replaces_job_id)
                predecessor = self._load_path(
                    self._job_path(predecessor_id),
                    quarantine=False,
                )
                lineage_id = str(
                    (predecessor or {}).get("replacement_lineage_id")
                    or predecessor_id
                )
                record["replaces_job_id"] = predecessor_id
                record["replacement_lineage_id"] = lineage_id
                record["replacement_generation"] = 1 + max(
                    (
                        int(candidate.get("replacement_generation") or 0)
                        for candidate in self.list_jobs()
                        if (
                            str(
                                candidate.get("replacement_lineage_id")
                                or candidate.get("job_id")
                                or ""
                            )
                            == lineage_id
                        )
                    ),
                    default=0,
                )
            atomic_write_json(path, record)
        return dict(record)

    def commit_retry_replacement(
        self,
        *,
        lineage_id: str,
        replacement_job_id: str,
        predecessor_job_id: str,
    ) -> None:
        """Delete stale lineage members, then commit by deleting predecessor."""

        lineage = str(lineage_id).strip()
        if not _SAFE_JOB_ID.fullmatch(lineage):
            raise ValueError("invalid retry lineage id")
        replacement_id = str(replacement_job_id).strip()
        predecessor_id = str(predecessor_job_id).strip()
        replacement_path = self._job_path(replacement_id)
        predecessor_path = self._job_path(predecessor_id)
        with self._lock:
            if not replacement_path.is_file():
                raise KeyError(replacement_id)
            if not predecessor_path.is_file():
                raise KeyError(predecessor_id)
            sibling_ids = [
                str(record["job_id"])
                for record in self.list_jobs()
                if str(record.get("replacement_lineage_id") or "") == lineage
                and str(record.get("job_id") or "")
                not in {replacement_id, predecessor_id}
            ]
            for sibling_id in sibling_ids:
                if not self.delete_job(sibling_id):
                    raise KeyError(sibling_id)
            if not self.delete_job(predecessor_id):
                raise KeyError(predecessor_id)

    def update_job(self, job_id: str, **updates: object) -> Dict[str, object]:
        """Atomically merge top-level updates into an existing job record."""

        path = self._job_path(job_id)
        with self._lock:
            record = self._load_path(path, quarantine=False)
            if record is None:
                raise KeyError(job_id)
            if "job_id" in updates and str(updates["job_id"]) != str(record["job_id"]):
                raise ValueError("job_id cannot be changed")
            record.update(updates)
            record["job_id"] = str(record["job_id"])
            record["last_updated"] = _utc_now_iso()
            atomic_write_json(path, record)
            return dict(record)

    def switch_parser_route(
        self,
        job_id: str,
        *,
        parse_route: str,
        force_mineru: bool,
        provider_id: Optional[str],
        provider_name: Optional[str],
    ) -> Dict[str, object]:
        """Atomically switch both displayed and resumable parser route."""

        path = self._job_path(job_id)
        with self._lock:
            record = self._load_path(path, quarantine=False)
            if record is None:
                raise KeyError(job_id)
            context = record.get("context")
            if not isinstance(context, Mapping):
                raise ValueError("import job context is invalid")
            merged_context = dict(context)
            merged_context.update(
                {
                    "force_mineru": bool(force_mineru),
                    "provider_id": provider_id,
                }
            )
            record["context"] = merged_context
            record["parse_route"] = str(parse_route)
            record["provider_id"] = provider_id
            record["provider_name"] = provider_name
            record["last_updated"] = _utc_now_iso()
            atomic_write_json(path, record)
            return dict(record)

    def get_job(self, job_id: str) -> Optional[Dict[str, object]]:
        """Load one job; quarantine and hide a damaged record."""

        path = self._job_path(job_id)
        with self._lock:
            return self._load_path(path, quarantine=True)

    def list_jobs(self) -> List[Dict[str, object]]:
        """Return every readable job, isolating corrupt files."""

        with self._lock:
            jobs: List[Dict[str, object]] = []
            if not self.directory.is_dir():
                return jobs
            for path in sorted(self.directory.glob("*.json")):
                record = self._load_path(path, quarantine=True)
                if record is not None:
                    jobs.append(record)
            return jobs

    def delete_job(self, job_id: str) -> bool:
        """Remove a completed or explicitly dismissed job description."""

        path = self._job_path(job_id)
        with self._lock:
            if not path.exists():
                return False
            path.unlink()
            fsync_directory(path.parent)
            return True

    def load_startup_jobs(
        self,
        *,
        skip_job_ids: Sequence[str] = (),
    ) -> List[Dict[str, object]]:
        """Restore display state without submitting jobs or performing I/O.

        Jobs interrupted while queued or processing become ``paused``.  A
        missing target becomes a terminal failure.  This method deliberately
        has no reference to :class:`ImportTaskQueue`, so loading state can
        never trigger an upload by accident.

        Skipped jobs are excluded before target checks or journal updates.
        """

        restored: List[Dict[str, object]] = []
        skipped = {str(job_id) for job_id in skip_job_ids}
        with self._lock:
            for record in self.list_jobs():
                if str(record.get("job_id") or "") in skipped:
                    continue
                context = record.get("context")
                target_text = (
                    str(context.get("target") or "")
                    if isinstance(context, Mapping)
                    else ""
                )
                target = Path(target_text).expanduser() if target_text else None
                updates: Dict[str, object] = {}
                if target is None or not target.is_file():
                    updates = {
                        "status": "failed",
                        "phase": "failed",
                        "can_resume": False,
                        "error": "待恢复的原始文件不存在。",
                        "message": "待恢复的原始文件不存在。",
                    }
                else:
                    target_stat = target.stat()
                    metadata_changed = bool(
                        int(record.get("file_size") or -1)
                        != int(target_stat.st_size)
                        or int(record.get("file_mtime_ns") or -1)
                        != int(target_stat.st_mtime_ns)
                    )
                    if (
                        metadata_changed
                        and sha256_file(target)
                        != str(record.get("file_hash") or "")
                    ):
                        updates = {
                            "status": "failed",
                            "phase": "failed",
                            "can_resume": False,
                            "error": "待恢复文件的内容已经变化，旧断点不会继续使用。",
                            "message": "待恢复文件的内容已经变化，旧断点不会继续使用。",
                        }
                    elif metadata_changed:
                        updates = {
                            "file_size": int(target_stat.st_size),
                            "file_mtime_ns": int(target_stat.st_mtime_ns),
                        }
                if (
                    "status" not in updates
                    and str(record.get("status") or "").lower()
                    in _RECOVERABLE_RUNNING_STATUSES
                ):
                    updates.update(
                        {
                            "status": "paused",
                            "phase": "paused",
                            "can_resume": True,
                            "message": "上次导入被中断，可手动继续。",
                        }
                    )
                if updates:
                    record = self.update_job(str(record["job_id"]), **updates)
                restored.append(record)
        return restored

    def _job_path(self, job_id: str) -> Path:
        value = str(job_id).strip()
        if not _SAFE_JOB_ID.fullmatch(value):
            raise ValueError("invalid import job id")
        return self.directory / f"{value}.json"

    @staticmethod
    def _load_path(
        path: Path,
        *,
        quarantine: bool,
    ) -> Optional[Dict[str, object]]:
        try:
            record = load_json_object(path)
        except ResumeManifestError:
            if quarantine:
                quarantine_corrupt_manifest(path)
                return None
            raise
        if record is None:
            return None
        job_id = str(record.get("job_id") or "").strip()
        context = record.get("context")
        replaces_job_id = record.get("replaces_job_id")
        replacement_lineage_id = record.get("replacement_lineage_id")
        replacement_generation = record.get("replacement_generation")
        valid_schema = bool(
            record.get("job_log_spec_version") == JOB_LOG_SPEC_VERSION
            and _SAFE_JOB_ID.fullmatch(job_id)
            and path.stem == job_id
            and isinstance(record.get("status"), str)
            and isinstance(record.get("source_file_id"), str)
            and record.get("file_type") in {"pdf", "docx"}
            and isinstance(record.get("file_hash"), str)
            and _SHA256.fullmatch(str(record.get("file_hash") or ""))
            and isinstance(record.get("file_size"), int)
            and int(record.get("file_size") or 0) >= 0
            and isinstance(record.get("file_mtime_ns"), int)
            and int(record.get("file_mtime_ns") or 0) >= 0
            and isinstance(context, Mapping)
            and isinstance(context.get("target"), str)
            and bool(str(context.get("target") or "").strip())
            and isinstance(context.get("source_file_id"), str)
            and isinstance(context.get("profile"), Mapping)
            and isinstance(context.get("is_pdf"), bool)
            and isinstance(context.get("force_mineru"), bool)
            and (
                (
                    replaces_job_id is None
                    and replacement_lineage_id is None
                    and replacement_generation is None
                )
                or (
                    isinstance(replaces_job_id, str)
                    and bool(_SAFE_JOB_ID.fullmatch(replaces_job_id))
                    and replaces_job_id != job_id
                    and isinstance(replacement_lineage_id, str)
                    and bool(
                        _SAFE_JOB_ID.fullmatch(replacement_lineage_id)
                    )
                    and replacement_lineage_id != job_id
                    and isinstance(replacement_generation, int)
                    and not isinstance(replacement_generation, bool)
                    and replacement_generation > 0
                )
            )
        )
        if not valid_schema:
            if quarantine:
                quarantine_corrupt_manifest(path)
                return None
            raise ResumeManifestError(f"导入任务清单结构无效：{path}")
        return record

"""SQLite source of truth for resumable document and slice jobs."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .slicing import SliceDescriptor


LEDGER_SCHEMA_VERSION = 1
DOCUMENT_STATUSES = frozenset(
    {
        "preparing",
        "queued",
        "running",
        "waiting",
        "retryable_failure",
        "permanent_failure",
        "cancelled",
        "validated",
        "published",
    }
)
SLICE_STATUSES = frozenset(
    {
        "queued",
        "running",
        "submitted",
        "waiting",
        "completed",
        "retryable_failure",
        "permanent_failure",
        "cancelled",
    }
)


SCHEMA_V1 = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS document_jobs (
    id TEXT PRIMARY KEY,
    source_file_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    parser_model TEXT,
    options_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    total_pages INTEGER NOT NULL,
    total_slices INTEGER NOT NULL DEFAULT 0,
    completed_pages INTEGER NOT NULL DEFAULT 0,
    completed_slices INTEGER NOT NULL DEFAULT 0,
    publish_status TEXT NOT NULL DEFAULT 'not_published',
    published_export_path TEXT,
    error_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_jobs_resume
ON document_jobs(source_file_id, source_sha256, provider_id, options_fingerprint, updated_at);

CREATE TABLE IF NOT EXISTS slice_jobs (
    id TEXT PRIMARY KEY,
    document_job_id TEXT NOT NULL REFERENCES document_jobs(id) ON DELETE CASCADE,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    global_page_offset INTEGER NOT NULL,
    slice_path TEXT NOT NULL,
    slice_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    credential_id TEXT,
    remote_task_id TEXT,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    result_path TEXT,
    result_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(document_job_id, page_start, page_end)
);

CREATE INDEX IF NOT EXISTS idx_slice_jobs_schedule
ON slice_jobs(document_job_id, status, page_start);
CREATE INDEX IF NOT EXISTS idx_slice_jobs_remote
ON slice_jobs(provider_id, remote_task_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DocumentJob:
    id: str
    source_file_id: str
    document_id: str
    source_path: str
    source_sha256: str
    provider_id: str
    parser_model: Optional[str]
    options_fingerprint: str
    status: str
    total_pages: int
    total_slices: int
    completed_pages: int
    completed_slices: int
    publish_status: str
    published_export_path: Optional[str]
    error_summary: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SliceJob:
    id: str
    document_job_id: str
    page_start: int
    page_end: int
    global_page_offset: int
    slice_path: str
    slice_sha256: str
    size_bytes: int
    provider_id: str
    credential_id: Optional[str]
    remote_task_id: Optional[str]
    status: str
    attempt_count: int
    last_error: Optional[str]
    result_path: Optional[str]
    result_sha256: Optional[str]
    created_at: str
    updated_at: str

    @property
    def page_count(self) -> int:
        return self.page_end - self.page_start + 1


_DOCUMENT_UPDATE_FIELDS = frozenset(
    {
        "status",
        "total_slices",
        "completed_pages",
        "completed_slices",
        "publish_status",
        "published_export_path",
        "error_summary",
    }
)
_SLICE_UPDATE_FIELDS = frozenset(
    {
        "slice_path",
        "slice_sha256",
        "size_bytes",
        "credential_id",
        "remote_task_id",
        "status",
        "attempt_count",
        "last_error",
        "result_path",
        "result_sha256",
    }
)


class JobLedger:
    """A separate ledger survives atomic rebuilds of the search index DB."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def migrate(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > LEDGER_SCHEMA_VERSION:
                raise RuntimeError(
                    f"parser job ledger schema {version} is newer than supported "
                    f"version {LEDGER_SCHEMA_VERSION}"
                )
            if version < 1:
                connection.executescript(SCHEMA_V1)
                connection.execute("PRAGMA user_version = 1")
            connection.commit()

    def create_document_job(
        self,
        *,
        source_file_id: str,
        document_id: str,
        source_path: Path,
        source_sha256: str,
        provider_id: str,
        parser_model: Optional[str],
        options_fingerprint: str,
        total_pages: int,
    ) -> DocumentJob:
        job_id = uuid.uuid4().hex
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO document_jobs(
                    id, source_file_id, document_id, source_path, source_sha256,
                    provider_id, parser_model, options_fingerprint, status,
                    total_pages, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?, ?, ?)
                """,
                (
                    job_id,
                    source_file_id,
                    document_id,
                    str(Path(source_path).resolve()),
                    source_sha256,
                    provider_id,
                    parser_model,
                    options_fingerprint,
                    int(total_pages),
                    now,
                    now,
                ),
            )
        return self.get_document_job(job_id)

    def find_resumable_job(
        self,
        *,
        source_file_id: str,
        source_sha256: str,
        provider_id: str,
        options_fingerprint: str,
    ) -> Optional[DocumentJob]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM document_jobs
                WHERE source_file_id = ? AND source_sha256 = ?
                  AND provider_id = ? AND options_fingerprint = ?
                  AND status NOT IN ('cancelled', 'permanent_failure', 'published')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (source_file_id, source_sha256, provider_id, options_fingerprint),
            ).fetchone()
        return _document_from_row(row) if row is not None else None

    def get_document_job(self, job_id: str) -> DocumentJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM document_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _document_from_row(row)

    def add_slices(
        self, job_id: str, descriptors: Iterable[SliceDescriptor], provider_id: str
    ) -> List[SliceJob]:
        now = _now()
        descriptor_list = list(descriptors)
        with self._connect() as connection:
            for descriptor in descriptor_list:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO slice_jobs(
                        id, document_job_id, page_start, page_end,
                        global_page_offset, slice_path, slice_sha256, size_bytes,
                        provider_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        job_id,
                        descriptor.page_start,
                        descriptor.page_end,
                        descriptor.global_page_offset,
                        str(Path(descriptor.path).resolve()),
                        descriptor.sha256,
                        descriptor.size_bytes,
                        provider_id,
                        now,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE document_jobs SET total_slices = ?, status = 'queued',
                    updated_at = ? WHERE id = ?
                """,
                (len(descriptor_list), now, job_id),
            )
        return self.list_slice_jobs(job_id)

    def get_slice_job(self, slice_id: str) -> SliceJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM slice_jobs WHERE id = ?", (slice_id,)
            ).fetchone()
        if row is None:
            raise KeyError(slice_id)
        return _slice_from_row(row)

    def list_slice_jobs(self, job_id: str) -> List[SliceJob]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM slice_jobs WHERE document_job_id = ? "
                "ORDER BY page_start, page_end",
                (job_id,),
            ).fetchall()
        return [_slice_from_row(row) for row in rows]

    def update_document(self, job_id: str, **updates: object) -> DocumentJob:
        invalid = set(updates) - _DOCUMENT_UPDATE_FIELDS
        if invalid:
            raise ValueError(f"unsupported document job fields: {sorted(invalid)}")
        if "status" in updates and updates["status"] not in DOCUMENT_STATUSES:
            raise ValueError("invalid document job status")
        if not updates:
            return self.get_document_job(job_id)
        values = dict(updates)
        values["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE document_jobs SET {assignments} WHERE id = ?",
                (*values.values(), job_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(job_id)
        return self.get_document_job(job_id)

    def update_slice(self, slice_id: str, **updates: object) -> SliceJob:
        invalid = set(updates) - _SLICE_UPDATE_FIELDS
        if invalid:
            raise ValueError(f"unsupported slice job fields: {sorted(invalid)}")
        if "status" in updates and updates["status"] not in SLICE_STATUSES:
            raise ValueError("invalid slice job status")
        if not updates:
            return self.get_slice_job(slice_id)
        values = dict(updates)
        values["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE slice_jobs SET {assignments} WHERE id = ?",
                (*values.values(), slice_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(slice_id)
        return self.get_slice_job(slice_id)

    def refresh_progress(self, job_id: str) -> DocumentJob:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS completed_slices,
                       COALESCE(SUM(page_end - page_start + 1), 0) AS completed_pages
                FROM slice_jobs
                WHERE document_job_id = ? AND status = 'completed'
                """,
                (job_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE document_jobs
                SET completed_slices = ?, completed_pages = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(row[0]), int(row[1]), _now(), job_id),
            )
        return self.get_document_job(job_id)


def _document_from_row(row: sqlite3.Row) -> DocumentJob:
    return DocumentJob(**{key: row[key] for key in row.keys()})


def _slice_from_row(row: sqlite3.Row) -> SliceJob:
    return SliceJob(**{key: row[key] for key in row.keys()})

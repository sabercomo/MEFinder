"""SQLite source of truth for resumable document and slice jobs."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from .slicing import SliceDescriptor


LEDGER_SCHEMA_VERSION = 3
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

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS parser_credentials (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    daily_page_budget INTEGER,
    max_concurrency_override INTEGER,
    current_in_flight INTEGER NOT NULL DEFAULT 0,
    pages_used_today INTEGER NOT NULL DEFAULT 0,
    usage_date TEXT,
    cooldown_until TEXT,
    last_401_at TEXT,
    last_429_at TEXT,
    health_status TEXT NOT NULL DEFAULT 'healthy',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_parser_credentials_provider
ON parser_credentials(provider_id, enabled, health_status);
"""

# MinerU's "priority parsing pages" are not an account quota.  Keep the three
# v2 columns in place so an existing SQLite file can be upgraded without a
# destructive table rebuild, but erase their obsolete scheduler state and stop
# exposing or reading them in application code.
SCHEMA_V3 = """
UPDATE parser_credentials
SET daily_page_budget = NULL, pages_used_today = 0, usage_date = NULL;
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


@dataclass(frozen=True)
class CredentialRecord:
    id: str
    provider_id: str
    display_name: str
    secret_ref: str
    enabled: int
    max_concurrency_override: Optional[int]
    current_in_flight: int
    cooldown_until: Optional[str]
    last_401_at: Optional[str]
    last_429_at: Optional[str]
    health_status: str
    created_at: str
    updated_at: str

    @property
    def is_enabled(self) -> bool:
        return bool(self.enabled)


@dataclass(frozen=True)
class CredentialPageAttribution:
    """One successfully normalized slice attributed to its credential."""

    credential_id: str
    provider_id: str
    display_name: str
    document_job_id: str
    document_id: str
    source_file_id: str
    source_path: str
    page_start: int
    page_end: int
    completed_at: str

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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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
                version = 1
            if version < 2:
                connection.executescript(SCHEMA_V2)
                connection.execute("PRAGMA user_version = 2")
                version = 2
            if version < 3:
                connection.executescript(SCHEMA_V3)
                connection.execute("PRAGMA user_version = 3")
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

    def upsert_credential(
        self,
        *,
        credential_id: str,
        provider_id: str,
        display_name: str,
        secret_ref: str,
        enabled: bool = True,
        max_concurrency_override: Optional[int] = None,
    ) -> CredentialRecord:
        if max_concurrency_override is not None and max_concurrency_override < 1:
            raise ValueError("max_concurrency_override must be positive")
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO parser_credentials(
                    id, provider_id, display_name, secret_ref, enabled,
                    max_concurrency_override, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    provider_id = excluded.provider_id,
                    display_name = excluded.display_name,
                    secret_ref = excluded.secret_ref,
                    enabled = excluded.enabled,
                    max_concurrency_override = excluded.max_concurrency_override,
                    updated_at = excluded.updated_at
                """,
                (
                    credential_id,
                    provider_id,
                    display_name,
                    secret_ref,
                    1 if enabled else 0,
                    max_concurrency_override,
                    now,
                    now,
                ),
            )
        return self.get_credential(credential_id)

    def get_credential(self, credential_id: str) -> CredentialRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM parser_credentials WHERE id = ?", (credential_id,)
            ).fetchone()
        if row is None:
            raise KeyError(credential_id)
        return _credential_from_row(row)

    def list_credentials(self, provider_id: str) -> List[CredentialRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM parser_credentials WHERE provider_id = ? ORDER BY id",
                (provider_id,),
            ).fetchall()
        return [_credential_from_row(row) for row in rows]

    def delete_credential(self, credential_id: str, provider_id: str) -> bool:
        """Delete an idle credential while preserving historical attribution."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT provider_id, current_in_flight FROM parser_credentials "
                "WHERE id = ?",
                (credential_id,),
            ).fetchone()
            if row is None or str(row["provider_id"]) != provider_id:
                raise KeyError(credential_id)
            active_slice = connection.execute(
                """
                SELECT 1 FROM slice_jobs
                WHERE credential_id = ?
                  AND remote_task_id IS NOT NULL
                  AND status IN (
                    'running', 'submitted', 'waiting', 'retryable_failure'
                  )
                LIMIT 1
                """,
                (credential_id,),
            ).fetchone()
            if int(row["current_in_flight"]) > 0 or active_slice is not None:
                return False
            connection.execute(
                "DELETE FROM parser_credentials WHERE id = ?",
                (credential_id,),
            )
        return True

    def try_reserve_credential(
        self,
        credential_id: str,
        *,
        provider_max_concurrency: int,
        now_iso: str,
    ) -> Optional[CredentialRecord]:
        """Atomically reserve one concurrency slot for a new remote task."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM parser_credentials WHERE id = ?", (credential_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            record = _credential_from_row(row)
            concurrency = record.max_concurrency_override or provider_max_concurrency
            eligible = bool(
                record.is_enabled
                and record.health_status not in {"unauthorized", "disabled"}
                and (not record.cooldown_until or record.cooldown_until <= now_iso)
                and record.current_in_flight < concurrency
            )
            if not eligible:
                connection.rollback()
                return None
            connection.execute(
                """
                UPDATE parser_credentials
                SET current_in_flight = current_in_flight + 1, updated_at = ?
                WHERE id = ?
                """,
                (now_iso, credential_id),
            )
            connection.commit()
        return self.get_credential(credential_id)

    def release_credential(
        self,
        credential_id: str,
    ) -> CredentialRecord:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM parser_credentials WHERE id = ?", (credential_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(credential_id)
            connection.execute(
                """
                UPDATE parser_credentials
                SET current_in_flight = MAX(0, current_in_flight - 1),
                    updated_at = ? WHERE id = ?
                """,
                (now, credential_id),
            )
            connection.commit()
        return self.get_credential(credential_id)

    def update_credential_health(
        self,
        credential_id: str,
        *,
        enabled: Optional[bool] = None,
        health_status: Optional[str] = None,
        cooldown_until: Optional[str] = None,
        last_401_at: Optional[str] = None,
        last_429_at: Optional[str] = None,
    ) -> CredentialRecord:
        values: Dict[str, object] = {"updated_at": _now()}
        if enabled is not None:
            values["enabled"] = 1 if enabled else 0
        if health_status is not None:
            values["health_status"] = health_status
        if cooldown_until is not None:
            values["cooldown_until"] = cooldown_until
        if last_401_at is not None:
            values["last_401_at"] = last_401_at
        if last_429_at is not None:
            values["last_429_at"] = last_429_at
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE parser_credentials SET {assignments} WHERE id = ?",
                (*values.values(), credential_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(credential_id)
        return self.get_credential(credential_id)

    def set_credential_in_flight(
        self, credential_id: str, count: int
    ) -> CredentialRecord:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE parser_credentials SET current_in_flight = ?, updated_at = ? "
                "WHERE id = ?",
                (max(0, int(count)), _now(), credential_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(credential_id)
        return self.get_credential(credential_id)

    def recover_credential(self, credential_id: str) -> CredentialRecord:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE parser_credentials
                SET enabled = 1, health_status = 'healthy', cooldown_until = NULL,
                    updated_at = ? WHERE id = ?
                """,
                (_now(), credential_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(credential_id)
        return self.get_credential(credential_id)

    def active_remote_counts(self, provider_id: str) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sj.credential_id, COUNT(*)
                FROM slice_jobs AS sj
                JOIN document_jobs AS dj ON dj.id = sj.document_job_id
                WHERE sj.provider_id = ? AND sj.credential_id IS NOT NULL
                  AND sj.remote_task_id IS NOT NULL
                  AND sj.status IN ('submitted', 'waiting', 'retryable_failure')
                  AND dj.status NOT IN (
                    'cancelled', 'permanent_failure', 'validated', 'published'
                  )
                GROUP BY sj.credential_id
                """,
                (provider_id,),
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def completed_page_counts(self, provider_id: str) -> Dict[str, int]:
        """Return successful local page totals used only for fair scheduling."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT credential_id, SUM(page_end - page_start + 1)
                FROM slice_jobs
                WHERE provider_id = ? AND credential_id IS NOT NULL
                  AND status = 'completed'
                GROUP BY credential_id
                """,
                (provider_id,),
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def list_credential_page_attributions(
        self, provider_id: str
    ) -> List[CredentialPageAttribution]:
        """List successful book/page attribution without reading any secret."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sj.credential_id, sj.provider_id,
                       COALESCE(pc.display_name, sj.credential_id) AS display_name,
                       dj.id AS document_job_id, dj.document_id, dj.source_file_id,
                       dj.source_path, sj.page_start, sj.page_end,
                       sj.updated_at AS completed_at
                FROM slice_jobs AS sj
                JOIN document_jobs AS dj ON dj.id = sj.document_job_id
                LEFT JOIN parser_credentials AS pc ON pc.id = sj.credential_id
                WHERE sj.provider_id = ? AND sj.credential_id IS NOT NULL
                  AND sj.status = 'completed'
                  AND dj.status IN ('validated', 'published')
                ORDER BY sj.credential_id, dj.created_at, sj.page_start
                """,
                (provider_id,),
            ).fetchall()
        return [
            CredentialPageAttribution(
                credential_id=str(row["credential_id"]),
                provider_id=str(row["provider_id"]),
                display_name=str(row["display_name"]),
                document_job_id=str(row["document_job_id"]),
                document_id=str(row["document_id"]),
                source_file_id=str(row["source_file_id"]),
                source_path=str(row["source_path"]),
                page_start=int(row["page_start"]),
                page_end=int(row["page_end"]),
                completed_at=str(row["completed_at"]),
            )
            for row in rows
        ]


def _document_from_row(row: sqlite3.Row) -> DocumentJob:
    return DocumentJob(**{key: row[key] for key in row.keys()})


def _slice_from_row(row: sqlite3.Row) -> SliceJob:
    return SliceJob(**{key: row[key] for key in row.keys()})


def _credential_from_row(row: sqlite3.Row) -> CredentialRecord:
    return CredentialRecord(
        id=str(row["id"]),
        provider_id=str(row["provider_id"]),
        display_name=str(row["display_name"]),
        secret_ref=str(row["secret_ref"]),
        enabled=int(row["enabled"]),
        max_concurrency_override=(
            int(row["max_concurrency_override"])
            if row["max_concurrency_override"] is not None
            else None
        ),
        current_in_flight=int(row["current_in_flight"]),
        cooldown_until=row["cooldown_until"],
        last_401_at=row["last_401_at"],
        last_429_at=row["last_429_at"],
        health_status=str(row["health_status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )

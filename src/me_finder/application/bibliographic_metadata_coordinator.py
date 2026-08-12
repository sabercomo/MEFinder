"""Durable bibliographic metadata writes and batch recognition workflow."""

from __future__ import annotations

import copy
import threading
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from ..app_context import AppPaths
from ..bibliographic_metadata import (
    METADATA_FIELDS,
    canonical_metadata,
    manual_metadata,
    metadata_missing_fields,
    update_metadata_in_database,
)
from ..import_queue import ImportQueueClosedError, ImportQueueFullError
from ..pdf_import_service import locked_import_config, save_import_config


class BibliographicQueryPort(Protocol):
    def configured_document(
        self, source_file_id: str
    ) -> Tuple[Path, Dict[str, object], Dict[str, object]]:
        ...

    def detect_bibliographic_metadata(
        self,
        source_file_id: str,
        *,
        force: bool = False,
    ) -> Dict[str, object]:
        ...

    def batch_metadata_candidates(
        self,
        *,
        additional_active_source_ids: Iterable[str] = (),
    ) -> List[Dict[str, object]]:
        ...


class MetadataIndexPort(Protocol):
    def mutation(self) -> AbstractContextManager[None]:
        ...

    def suspend(self) -> None:
        ...

    def reopen(self, *, attempts: int = 1) -> bool:
        ...


class DurableOperationsPort(Protocol):
    def operation(self) -> AbstractContextManager[None]:
        ...


class MetadataJobsPort(Protocol):
    def processing_job_with_prefix(
        self, job_id_prefix: str
    ) -> Optional[Dict[str, object]]:
        ...

    def register_background_job_unless_processing(
        self,
        job_id_prefix: str,
        job: Mapping[str, object],
    ) -> Optional[Dict[str, object]]:
        ...

    def submit_background_task(
        self, task: Callable[..., None], *args: object
    ) -> None:
        ...

    def update_import_job(self, job_id: str, **updates: object) -> None:
        ...


LockedConfig = Callable[
    [Path], AbstractContextManager[Dict[str, object]]
]
SaveConfig = Callable[[Path, Dict[str, object]], None]
UpdateDatabase = Callable[
    [Path, str, Mapping[str, object]], Dict[str, int]
]
CanonicalizeMetadata = Callable[
    [Mapping[str, object]], Dict[str, object]
]
MissingMetadataFields = Callable[[Mapping[str, object]], List[str]]
BuildManualMetadata = Callable[
    [Mapping[str, object], Optional[Mapping[str, object]]],
    Dict[str, object],
]


class BibliographicMetadataError(ValueError):
    """The requested metadata mutation has no configured document target."""


class BibliographicMetadataQueueError(RuntimeError):
    """A registered batch job could not enter the background queue."""

    def __init__(self, job_id: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.job_id = job_id


class BibliographicMetadataCoordinator:
    """Coordinate metadata writes without depending on HTTP or lookup APIs."""

    def __init__(
        self,
        paths: AppPaths,
        queries: BibliographicQueryPort,
        index_runtime: MetadataIndexPort,
        durable_operations: DurableOperationsPort,
        jobs: MetadataJobsPort,
        *,
        lock_config: LockedConfig = locked_import_config,
        save_config: SaveConfig = save_import_config,
        update_database: UpdateDatabase = update_metadata_in_database,
        canonicalize: CanonicalizeMetadata = canonical_metadata,
        missing_fields: MissingMetadataFields = metadata_missing_fields,
        build_manual_metadata: BuildManualMetadata = manual_metadata,
        metadata_fields: Sequence[str] = METADATA_FIELDS,
    ) -> None:
        self.paths = paths
        self._queries = queries
        self._index_runtime = index_runtime
        self._durable_operations = durable_operations
        self._jobs = jobs
        self._lock_config = lock_config
        self._save_config = save_config
        self._update_database = update_database
        self._canonicalize = canonicalize
        self._missing_fields = missing_fields
        self._build_manual_metadata = build_manual_metadata
        self._metadata_fields = tuple(metadata_fields)
        self._metadata_lock = threading.Lock()

    @property
    def config_path(self) -> Path:
        return self.paths.config_root / "pdf_imports.json"

    def persist_detected(
        self,
        source_file_id: str,
        payload: Mapping[str, object],
    ) -> Dict[str, object]:
        """Persist canonical metadata inside one shutdown-safe mutation."""

        with self._durable_operations.operation():
            return self._persist(source_file_id, payload)

    def save_manual(
        self,
        source_file_id: str,
        payload: Mapping[str, object],
    ) -> Dict[str, object]:
        _config_path, _config, document = self._queries.configured_document(
            source_file_id
        )
        metadata = self._build_manual_metadata(payload, document)
        return self.persist_detected(source_file_id, metadata)

    def start_batch(
        self,
        *,
        additional_active_source_ids: Iterable[str] = (),
    ) -> Dict[str, object]:
        running = self._jobs.processing_job_with_prefix("batchmeta-")
        if running:
            return {
                "job_id": running["job_id"],
                "already_running": True,
            }

        candidates = self._queries.batch_metadata_candidates(
            additional_active_source_ids=additional_active_source_ids
        )
        if not candidates:
            return {
                "job_id": None,
                "candidates": 0,
            }

        job_id = f"batchmeta-{uuid.uuid4().hex[:12]}"
        running = self._jobs.register_background_job_unless_processing(
            "batchmeta-",
            {
                "job_id": job_id,
                "status": "processing",
                "phase": "metadata_recognition",
                "message": f"准备识别 {len(candidates)} 部文献…",
            },
        )
        if running:
            return {
                "job_id": running["job_id"],
                "already_running": True,
            }
        try:
            self._jobs.submit_background_task(
                self._run_batch_job,
                job_id,
                candidates,
            )
        except (ImportQueueFullError, ImportQueueClosedError) as exc:
            self._jobs.update_import_job(
                job_id,
                status="failed",
                phase="queue_failed",
                message="批量识别任务未能进入处理队列。",
            )
            raise BibliographicMetadataQueueError(job_id, exc) from exc
        return {
            "job_id": job_id,
            "candidates": len(candidates),
            "already_running": False,
        }

    def _persist(
        self,
        source_file_id: str,
        payload: Mapping[str, object],
    ) -> Dict[str, object]:
        with self._metadata_lock, self._index_runtime.mutation():
            with self._lock_config(self.config_path) as config:
                document = next(
                    (
                        item
                        for item in config.get("documents", [])
                        if item.get("source_file_id") == source_file_id
                    ),
                    None,
                )
                if not document:
                    raise BibliographicMetadataError(
                        "PDF 配置中找不到该文献。"
                    )
                original_config = copy.deepcopy(config)
                metadata = self._canonicalize(payload)
                if not metadata.get("metadata_missing_fields"):
                    metadata["metadata_missing_fields"] = self._missing_fields(
                        metadata
                    )
                for field in self._metadata_fields:
                    document[field] = metadata.get(field)
                for field in (
                    "document_type",
                    "metadata_status",
                    "metadata_source",
                    "metadata_confidence",
                    "metadata_evidence",
                    "metadata_conflicts",
                    "metadata_missing_fields",
                ):
                    document[field] = metadata.get(field)
                document["publication_year"] = metadata.get("publish_year")
                document["bibliographic_metadata"] = metadata
                self._save_config(self.config_path, config)
                self._index_runtime.suspend()
                database_updated = False
                runtime_reopened = False
                try:
                    self._update_database(
                        self.paths.index_path,
                        source_file_id,
                        metadata,
                    )
                    database_updated = True
                    self._index_runtime.reopen()
                    runtime_reopened = True
                    return metadata
                finally:
                    if not runtime_reopened:
                        if not database_updated:
                            self._save_config(
                                self.config_path,
                                original_config,
                            )
                        self._index_runtime.reopen()

    def _run_batch_job(
        self,
        job_id: str,
        candidates: List[Dict[str, object]],
    ) -> None:
        updated = 0
        unchanged = 0
        failures: List[Dict[str, object]] = []
        total = len(candidates)
        compare_fields = self._metadata_fields + (
            "document_type",
            "metadata_status",
        )
        for index, item in enumerate(candidates):
            source_file_id = str(item.get("source_file_id"))
            title = str(item.get("title") or source_file_id)
            self._jobs.update_import_job(
                job_id,
                phase="metadata_recognition",
                message=(
                    f"正在识别 {index + 1}/{total}：{title}"
                ),
            )
            try:
                before = self._canonicalize(
                    item.get("bibliographic_metadata") or item
                )
                detected = self._queries.detect_bibliographic_metadata(
                    source_file_id
                )
                if any(
                    detected.get(field) != before.get(field)
                    for field in compare_fields
                ):
                    self.persist_detected(source_file_id, detected)
                    updated += 1
                else:
                    unchanged += 1
            except Exception as exc:
                failures.append(
                    {
                        "source_file_id": source_file_id,
                        "title": title,
                        "error": str(exc),
                    }
                )

        summary = (
            f"批量识别完成：更新 {updated} 部，无变化 {unchanged} 部"
        )
        if failures:
            summary += f"，失败 {len(failures)} 部"
        self._jobs.update_import_job(
            job_id,
            status="completed",
            phase="completed",
            message=summary,
            batch_updated=updated,
            batch_unchanged=unchanged,
            batch_failures=failures,
        )

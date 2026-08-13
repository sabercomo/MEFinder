"""Coordinate document deletion with import jobs and the live index."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Protocol, Sequence

from ..app_context import AppPaths
from ..document_deletion import DocumentDeletionService
from ..mineru_api import MinerUError


class DeletionIndexPort(Protocol):
    def mutation(self) -> AbstractContextManager[None]:
        ...

    def suspend(self) -> None:
        ...

    def reopen(self, *, attempts: int = 1) -> bool:
        ...


class DurableOperationsPort(Protocol):
    def operation(self) -> AbstractContextManager[None]:
        ...


class DeletionJobsPort(Protocol):
    def begin_source_deletion(self, source_file_id: str) -> None:
        ...

    def end_source_deletion(self, source_file_id: str) -> None:
        ...

    def purge_source_jobs(self, source_file_ids: Sequence[str]) -> List[str]:
        ...


class DeletionServicePort(Protocol):
    def remove(
        self,
        source_file_id: str,
        *,
        delete_generated_artifacts: bool,
        delete_internal_copy: bool,
    ) -> Dict[str, object]:
        ...

    def remove_many(
        self,
        source_file_ids: Sequence[str],
        *,
        delete_generated_artifacts: bool,
        internal_copy_ids: Iterable[str],
    ) -> Dict[str, object]:
        ...


DeletionServiceFactory = Callable[[Path, Path], DeletionServicePort]
RemovalOperation = Callable[[], Dict[str, object]]
RemovedSourceIds = Callable[[Mapping[str, object]], Sequence[str]]
Failure = Dict[str, str]


class BatchDeletionConflict(MinerUError):
    """No requested source could be reserved for deletion."""

    def __init__(self, message: str, failures: Sequence[Failure]) -> None:
        super().__init__(message)
        self.failures = [dict(item) for item in failures]


class DocumentDeletionRejected(ValueError):
    """The deletion was rejected by document/config validation."""

    def __init__(self, message: str, failures: Sequence[Failure] = ()) -> None:
        super().__init__(message)
        self.failures = [dict(item) for item in failures]


class DocumentDeletionFailed(RuntimeError):
    """The deletion or subsequent runtime reopen failed."""

    def __init__(self, message: str, failures: Sequence[Failure] = ()) -> None:
        super().__init__(message)
        self.failures = [dict(item) for item in failures]


class DocumentDeletionCoordinator:
    """Keep document removal and runtime publication in one application flow."""

    def __init__(
        self,
        paths: AppPaths,
        index_runtime: DeletionIndexPort,
        durable_operations: DurableOperationsPort,
        jobs: DeletionJobsPort,
        *,
        service_factory: DeletionServiceFactory = DocumentDeletionService,
    ) -> None:
        self._paths = paths
        self._index_runtime = index_runtime
        self._durable_operations = durable_operations
        self._jobs = jobs
        self._service_factory = service_factory

    def remove(
        self,
        source_file_id: str,
        *,
        delete_generated_artifacts: bool = True,
        delete_internal_copy: bool = False,
    ) -> Dict[str, object]:
        self._jobs.begin_source_deletion(source_file_id)
        try:
            service = self._service_factory(
                self._paths.runtime_root,
                self._paths.index_path,
            )
            return self._perform_removal(
                lambda: service.remove(
                    source_file_id,
                    delete_generated_artifacts=delete_generated_artifacts,
                    delete_internal_copy=delete_internal_copy,
                ),
                removed_source_ids=lambda _result: [source_file_id],
            )
        finally:
            self._jobs.end_source_deletion(source_file_id)

    def remove_many(
        self,
        source_file_ids: Sequence[object],
        *,
        delete_generated_artifacts: bool = True,
        internal_copy_source_ids: Iterable[object] = (),
    ) -> Dict[str, object]:
        requested: List[str] = []
        for value in source_file_ids:
            source_file_id = str(value or "").strip()
            if source_file_id and source_file_id not in requested:
                requested.append(source_file_id)
        internal_ids = {
            str(value or "").strip() for value in internal_copy_source_ids
        }

        failures: List[Failure] = []
        accepted: List[str] = []
        for source_file_id in requested:
            try:
                self._jobs.begin_source_deletion(source_file_id)
                accepted.append(source_file_id)
            except MinerUError as exc:
                failures.append(
                    {"source_id": source_file_id, "error": str(exc)}
                )
        if not accepted:
            raise BatchDeletionConflict(
                failures[0]["error"] if failures else "没有可移除的文献。",
                failures,
            )

        try:
            service = self._service_factory(
                self._paths.runtime_root,
                self._paths.index_path,
            )
            result = self._perform_removal(
                lambda: service.remove_many(
                    accepted,
                    delete_generated_artifacts=delete_generated_artifacts,
                    internal_copy_ids=[
                        source_file_id
                        for source_file_id in accepted
                        if source_file_id in internal_ids
                    ],
                ),
                removed_source_ids=lambda value: list(
                    value.get("removed_source_ids") or []
                ),
                failures=failures,
            )
            result["failures"] = [
                *failures,
                *list(result.get("failures") or []),
            ]
            return result
        finally:
            for source_file_id in accepted:
                self._jobs.end_source_deletion(source_file_id)

    def _perform_removal(
        self,
        operation: RemovalOperation,
        *,
        removed_source_ids: RemovedSourceIds,
        failures: Sequence[Failure] = (),
    ) -> Dict[str, object]:
        result: Dict[str, object] = {}
        committed = False
        removal_error: Exception | None = None
        with self._index_runtime.mutation():
            try:
                self._index_runtime.suspend()
                with self._durable_operations.operation():
                    result = operation()
                committed = True
            except (
                ValueError,
                OSError,
                sqlite3.Error,
                json.JSONDecodeError,
            ) as exc:
                removal_error = DocumentDeletionRejected(
                    str(exc),
                    failures,
                )
            except RuntimeError as exc:
                logging.exception("document removal failed")
                removal_error = DocumentDeletionFailed(
                    str(exc),
                    failures,
                )
            finally:
                try:
                    self._index_runtime.reopen()
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    logging.exception(
                        "document removed but search index reload failed"
                    )
                    if removal_error is None:
                        removal_error = DocumentDeletionFailed(
                            "文献已删除，但索引重新载入失败；请重启应用。",
                            failures,
                        )

        if committed:
            warnings = self._jobs.purge_source_jobs(
                list(removed_source_ids(result))
            )
            if warnings:
                result["cleanup_warnings"] = [
                    *list(result.get("cleanup_warnings") or []),
                    *warnings,
                ]
        if removal_error is not None:
            raise removal_error
        return result

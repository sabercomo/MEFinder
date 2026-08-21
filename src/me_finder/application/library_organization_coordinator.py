"""Coordinate folder and document-group writes with the live index."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Dict, Protocol, Sequence

from ..app_context import AppPaths
from ..database import (
    assign_sources_to_document_group,
    create_document_group,
    create_folder,
    delete_document_group,
    delete_folder,
    move_sources_to_folder,
    rename_document_group,
    rename_folder,
    update_source_version_metadata,
)


class OrganizationIndexPort(Protocol):
    def mutation(self) -> AbstractContextManager[None]:
        ...

    def suspend(self) -> None:
        ...

    def reopen(self, *, attempts: int = 1) -> bool:
        ...


class DurableOperationsPort(Protocol):
    def operation(self) -> AbstractContextManager[None]:
        ...


class LibraryOrganizationRejected(ValueError):
    """The requested organization mutation was invalid."""


class LibraryOrganizationFailed(RuntimeError):
    """The organization mutation or runtime publication failed."""


OrganizationWrite = Callable[[Path], Dict[str, object]]


class LibraryOrganizationCoordinator:
    """Publish organization-only database changes through ``IndexRuntime``."""

    def __init__(
        self,
        paths: AppPaths,
        index_runtime: OrganizationIndexPort,
        durable_operations: DurableOperationsPort,
    ) -> None:
        self._paths = paths
        self._index_runtime = index_runtime
        self._durable_operations = durable_operations

    def create_folder(self, name: object) -> Dict[str, object]:
        return self._write(lambda path: create_folder(name, path))

    def rename_folder(self, folder_id: object, name: object) -> Dict[str, object]:
        return self._write(lambda path: rename_folder(folder_id, name, path))

    def delete_folder(self, folder_id: object) -> Dict[str, object]:
        return self._write(lambda path: delete_folder(folder_id, path))

    def move_sources(
        self, source_file_ids: Sequence[object], folder_id: object
    ) -> Dict[str, object]:
        return self._write(
            lambda path: move_sources_to_folder(source_file_ids, folder_id, path)
        )

    def create_document_group(self, title: object) -> Dict[str, object]:
        return self._write(lambda path: create_document_group(title, path))

    def rename_document_group(
        self, document_group_id: object, title: object
    ) -> Dict[str, object]:
        return self._write(
            lambda path: rename_document_group(document_group_id, title, path)
        )

    def delete_document_group(
        self, document_group_id: object
    ) -> Dict[str, object]:
        return self._write(
            lambda path: delete_document_group(document_group_id, path)
        )

    def assign_group(
        self, source_file_ids: Sequence[object], document_group_id: object
    ) -> Dict[str, object]:
        return self._write(
            lambda path: assign_sources_to_document_group(
                source_file_ids, document_group_id, path
            )
        )

    def update_version_label(
        self, source_file_id: object, version_label: object
    ) -> Dict[str, object]:
        return self._write(
            lambda path: update_source_version_metadata(
                source_file_id,
                {"version_label": version_label},
                path,
            )
        )

    def _write(self, operation: OrganizationWrite) -> Dict[str, object]:
        with self._index_runtime.mutation():
            self._index_runtime.suspend()
            try:
                with self._durable_operations.operation():
                    result = operation(self._paths.index_path)
            except (ValueError, OSError, sqlite3.Error) as exc:
                self._index_runtime.reopen(attempts=5)
                raise LibraryOrganizationRejected(str(exc)) from exc
            try:
                self._index_runtime.reopen(attempts=5)
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise LibraryOrganizationFailed(
                    "资料组织已写入，但索引未能重新加载。"
                ) from exc
        return result

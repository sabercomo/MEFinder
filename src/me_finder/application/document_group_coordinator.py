"""Publish DocumentGroup membership changes through ``IndexRuntime``.

Group writes touch the same index file the web server reads, so every mutation
runs inside ``index_runtime.mutation()`` with the runtime suspended, then reopens
the read connection. Reads (``list``) need no suspension.
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Dict, List, Protocol

from ..document_groups import (
    add_group_member,
    create_document_group,
    delete_document_group,
    list_document_groups,
    remove_group_member,
    rename_document_group,
    set_document_group_base,
    set_member_version_label,
)


class DocumentGroupIndexPort(Protocol):
    def mutation(self) -> AbstractContextManager[None]: ...

    def suspend(self) -> None: ...

    def reopen(self, *, attempts: int = 1) -> bool: ...


class DurableOperationsPort(Protocol):
    def operation(self) -> AbstractContextManager[None]: ...


class DocumentGroupRejected(ValueError):
    """The requested group mutation was invalid."""


class DocumentGroupFailed(RuntimeError):
    """The mutation or index reload failed."""


GroupWrite = Callable[[Path], Dict[str, object]]


class DocumentGroupCoordinator:
    def __init__(self, paths, index_runtime, durable_operations) -> None:
        self._paths = paths
        self._index_runtime = index_runtime
        self._durable_operations = durable_operations

    def create(self, title: object) -> Dict[str, object]:
        return self._write(lambda path: create_document_group(title, path))

    def rename(self, document_group_id: object, title: object) -> Dict[str, object]:
        return self._write(
            lambda path: rename_document_group(document_group_id, title, path)
        )

    def delete(self, document_group_id: object) -> Dict[str, object]:
        return self._write(
            lambda path: delete_document_group(document_group_id, path)
        )

    def add_member(
        self,
        document_group_id: object,
        source_file_id: object,
        version_label: object = None,
    ) -> Dict[str, object]:
        return self._write(
            lambda path: add_group_member(
                document_group_id, source_file_id, path, version_label
            )
        )

    def remove_member(self, source_file_id: object) -> Dict[str, object]:
        return self._write(lambda path: remove_group_member(source_file_id, path))

    def set_base(
        self, document_group_id: object, base_source_file_id: object
    ) -> Dict[str, object]:
        return self._write(
            lambda path: set_document_group_base(
                document_group_id, base_source_file_id, path
            )
        )

    def set_version_label(
        self, source_file_id: object, version_label: object
    ) -> Dict[str, object]:
        return self._write(
            lambda path: set_member_version_label(
                source_file_id, version_label, path
            )
        )

    def list(self) -> List[Dict[str, object]]:
        return list_document_groups(self._paths.index_path)

    def _write(self, operation: GroupWrite) -> Dict[str, object]:
        with self._index_runtime.mutation():
            self._index_runtime.suspend()
            try:
                with self._durable_operations.operation():
                    result = operation(self._paths.index_path)
            except ValueError as exc:
                self._index_runtime.reopen(attempts=5)
                raise DocumentGroupRejected(str(exc)) from exc
            except (OSError, sqlite3.Error) as exc:
                self._index_runtime.reopen(attempts=5)
                raise DocumentGroupFailed(str(exc)) from exc
            try:
                self._index_runtime.reopen(attempts=5)
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise DocumentGroupFailed(
                    "作品组已写入，但索引未能重新加载。"
                ) from exc
        return result

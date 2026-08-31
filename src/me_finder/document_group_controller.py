"""Transport-neutral JSON responses for DocumentGroup mutations and reads."""

from __future__ import annotations

from typing import Dict, Mapping, Tuple

from .application.document_group_coordinator import (
    DocumentGroupCoordinator,
    DocumentGroupFailed,
    DocumentGroupRejected,
)

GroupResponse = Tuple[int, Dict[str, object]]


class DocumentGroupController:
    def __init__(self, coordinator: DocumentGroupCoordinator) -> None:
        self._groups = coordinator

    @staticmethod
    def _success(result: Dict[str, object]) -> GroupResponse:
        return 200, {"ok": True, "result": result, "event": "library_changed"}

    def create(self, payload: object) -> GroupResponse:
        return self._run(payload, lambda value: self._groups.create(value.get("title")))

    def combine(self, payload: object) -> GroupResponse:
        return self._run(
            payload,
            lambda value: self._groups.combine(
                value.get("title"),
                value.get("source_file_ids"),
                value.get("base_source_file_id"),
            ),
        )

    def rename(self, payload: object) -> GroupResponse:
        return self._run(
            payload,
            lambda value: self._groups.rename(
                value.get("document_group_id"), value.get("title")
            ),
        )

    def delete(self, payload: object) -> GroupResponse:
        return self._run(
            payload,
            lambda value: self._groups.delete(value.get("document_group_id")),
        )

    def add_member(self, payload: object) -> GroupResponse:
        return self._run(
            payload,
            lambda value: self._groups.add_member(
                value.get("document_group_id"),
                value.get("source_file_id"),
                value.get("version_label"),
            ),
        )

    def remove_member(self, payload: object) -> GroupResponse:
        return self._run(
            payload,
            lambda value: self._groups.remove_member(value.get("source_file_id")),
        )

    def set_base(self, payload: object) -> GroupResponse:
        return self._run(
            payload,
            lambda value: self._groups.set_base(
                value.get("document_group_id"), value.get("base_source_file_id")
            ),
        )

    def set_version_label(self, payload: object) -> GroupResponse:
        return self._run(
            payload,
            lambda value: self._groups.set_version_label(
                value.get("source_file_id"), value.get("version_label")
            ),
        )

    def list(self, _payload: object = None) -> GroupResponse:
        return 200, {"ok": True, "document_groups": self._groups.list()}

    def _run(self, payload: object, operation) -> GroupResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "invalid request"}
        try:
            return self._success(operation(payload))
        except DocumentGroupRejected as exc:
            return 400, {"error": str(exc)}
        except DocumentGroupFailed as exc:
            return 500, {"error": str(exc)}

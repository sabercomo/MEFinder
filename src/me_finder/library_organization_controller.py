"""Transport-neutral JSON responses for library organization mutations."""

from __future__ import annotations

from typing import Dict, Mapping, Tuple

from .application.library_organization_coordinator import (
    LibraryOrganizationCoordinator,
    LibraryOrganizationFailed,
    LibraryOrganizationRejected,
)


OrganizationResponse = Tuple[int, Dict[str, object]]


class LibraryOrganizationController:
    def __init__(self, organization: LibraryOrganizationCoordinator) -> None:
        self._organization = organization

    @staticmethod
    def _success(result: Dict[str, object]) -> OrganizationResponse:
        return 200, {"ok": True, "result": result, "event": "library_changed"}

    def create_folder(self, payload: object) -> OrganizationResponse:
        return self._run(
            payload,
            lambda value: self._organization.create_folder(value.get("name")),
        )

    def rename_folder(self, payload: object) -> OrganizationResponse:
        return self._run(
            payload,
            lambda value: self._organization.rename_folder(
                value.get("folder_id"), value.get("name")
            ),
        )

    def delete_folder(self, payload: object) -> OrganizationResponse:
        return self._run(
            payload,
            lambda value: self._organization.delete_folder(value.get("folder_id")),
        )

    def move_sources(self, payload: object) -> OrganizationResponse:
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("source_file_ids"), list
        ):
            return 400, {"error": "invalid request"}
        return self._run(
            payload,
            lambda value: self._organization.move_sources(
                value["source_file_ids"], value.get("folder_id")
            ),
        )

    def create_document_group(self, payload: object) -> OrganizationResponse:
        return self._run(
            payload,
            lambda value: self._organization.create_document_group(
                value.get("title")
            ),
        )

    def rename_document_group(self, payload: object) -> OrganizationResponse:
        return self._run(
            payload,
            lambda value: self._organization.rename_document_group(
                value.get("document_group_id"), value.get("title")
            ),
        )

    def delete_document_group(self, payload: object) -> OrganizationResponse:
        return self._run(
            payload,
            lambda value: self._organization.delete_document_group(
                value.get("document_group_id")
            ),
        )

    def assign_group(self, payload: object) -> OrganizationResponse:
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("source_file_ids"), list
        ):
            return 400, {"error": "invalid request"}
        return self._run(
            payload,
            lambda value: self._organization.assign_group(
                value["source_file_ids"], value.get("document_group_id")
            ),
        )

    def update_version_label(self, payload: object) -> OrganizationResponse:
        return self._run(
            payload,
            lambda value: self._organization.update_version_label(
                value.get("source_file_id"), value.get("version_label")
            ),
        )

    def _run(
        self,
        payload: object,
        operation,
    ) -> OrganizationResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "invalid request"}
        try:
            return self._success(operation(payload))
        except LibraryOrganizationRejected as exc:
            return 400, {"error": str(exc)}
        except LibraryOrganizationFailed as exc:
            return 500, {"error": str(exc)}

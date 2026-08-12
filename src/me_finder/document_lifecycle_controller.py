"""Transport-neutral JSON responses for document lifecycle mutations."""

from __future__ import annotations

from typing import Dict, Mapping, Tuple

from .application.document_deletion_coordinator import (
    BatchDeletionConflict,
    DocumentDeletionCoordinator,
    DocumentDeletionFailed,
    DocumentDeletionRejected,
)
from .mineru_api import MinerUError


DocumentLifecycleResponse = Tuple[int, Dict[str, object]]


class DocumentLifecycleController:
    """Validate removal requests and preserve public deletion responses."""

    def __init__(self, deletion: DocumentDeletionCoordinator) -> None:
        self._deletion = deletion

    def remove(self, payload: object) -> DocumentLifecycleResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "invalid request"}
        source_file_id = str(payload.get("source_id") or "")
        if not source_file_id:
            return 400, {"error": "invalid request"}
        try:
            result = self._deletion.remove(
                source_file_id,
                delete_generated_artifacts=bool(
                    payload.get("delete_generated_artifacts", True)
                ),
                delete_internal_copy=bool(
                    payload.get("delete_internal_copy", False)
                ),
            )
        except MinerUError as exc:
            return 409, {"error": str(exc)}
        except DocumentDeletionRejected as exc:
            return 400, {"error": str(exc) or "删除文献失败。"}
        except DocumentDeletionFailed as exc:
            return 500, {"error": str(exc) or "删除文献失败。"}
        return 200, {
            "ok": True,
            "result": result,
            "event": "library_changed",
        }

    def remove_batch(self, payload: object) -> DocumentLifecycleResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "invalid request"}
        source_file_ids = payload.get("source_ids")
        if not isinstance(source_file_ids, list) or not source_file_ids:
            return 400, {"error": "invalid request"}
        try:
            result = self._deletion.remove_many(
                source_file_ids,
                delete_generated_artifacts=bool(
                    payload.get("delete_generated_artifacts", True)
                ),
                internal_copy_source_ids=(
                    payload.get("internal_copy_source_ids") or []
                ),
            )
        except BatchDeletionConflict as exc:
            return 409, {"error": str(exc), "failures": exc.failures}
        except DocumentDeletionRejected as exc:
            return 400, {
                "error": str(exc) or "删除文献失败。",
                "failures": exc.failures,
            }
        except DocumentDeletionFailed as exc:
            return 500, {
                "error": str(exc) or "删除文献失败。",
                "failures": exc.failures,
            }
        return 200, {
            "ok": True,
            "result": result,
            "event": "library_changed",
        }

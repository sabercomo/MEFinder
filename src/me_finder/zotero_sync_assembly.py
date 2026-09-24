"""Wire the Zotero source sync onto the existing import, deletion and metadata
pipelines, and expose it over the local HTTP API.

Kept out of ``web_runtime`` so the composition root only makes one call. The
sync never parses or removes anything itself: new files go through
``DocumentImportCoordinator.import_local`` (same queue, resume journal, MinerU
quota and retry as a manual import), removals through
``DocumentDeletionCoordinator.remove_many`` and metadata through the
bibliographic metadata coordinator.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple

from .application.bibliographic_metadata_coordinator import BibliographicMetadataError
from .app_context import AppPaths
from .bibliographic_metadata import update_metadata_in_database
from .import_resume import sha256_file
from .persistence.zotero_sync_store import ZoteroSyncStore
from .preferences import read_preferences, resolve_preferences_path
from .vision_api import resolve_vision_config_path, vision_config_summary
from .zotero_sync import ZoteroSyncPorts, ZoteroSyncService


PARSE_MODE_LABELS = {
    "auto": "自动选择",
    "mineru": "强制 MinerU",
    "mineru-local": "本地 MinerU",
    "general-local-model": "通用本地模型",
    "vision": "其他视觉 API",
}

Response = Tuple[int, object]


class ZoteroSyncController:
    """HTTP adapter for the Zotero settings page."""

    def __init__(self, service: ZoteroSyncService, parse_mode: Callable[[], str]) -> None:
        self._service = service
        self._parse_mode = parse_mode

    def overview(self, _params: Mapping[str, object]) -> Response:
        overview = self._service.overview()
        mode = self._parse_mode()
        overview["pdf_parse_mode"] = {"mode": mode, "label": PARSE_MODE_LABELS.get(mode, mode)}
        return 200, overview

    def status(self, _params: Mapping[str, object]) -> Response:
        return 200, self._service.status()

    def preview(self, payload: object) -> Response:
        collections = payload.get("collections") if isinstance(payload, Mapping) else None
        if not isinstance(collections, list) or len(collections) > 500:
            return 400, {"error": "collections 必须是分类 key 列表"}
        return 200, self._service.preview(collections)

    def sync(self, _payload: object) -> Response:
        return 200, self._service.start_sync("manual")


def assemble_zotero_sync(
    paths: AppPaths,
    *,
    index_runtime,
    durable_operations,
    document_imports,
    import_orchestrator,
    deletion_coordinator,
    metadata_coordinator,
) -> Tuple[ZoteroSyncService, "ZoteroSyncController"]:
    """Build the sync service and its HTTP controller."""

    root = paths.runtime_root

    def preferences() -> Mapping[str, object]:
        return read_preferences(resolve_preferences_path(root))

    def parse_mode() -> str:
        return str(preferences().get("pdf_parse_mode") or "auto")

    def import_files(files: Sequence[Path]) -> Mapping[str, object]:
        mode = parse_mode()
        provider: Optional[str] = ""
        if mode == "vision":
            provider = vision_config_summary(resolve_vision_config_path(root)).get("default_provider_id")
            if not provider:
                raise ValueError("PDF 解析方式是其他视觉 API，但还没有可用的接口")
        return document_imports.import_local(
            [str(path) for path in files],
            [Path(path).parent for path in files],
            pdf_parse_mode=mode,
            vision_provider_id=provider or "",
        )

    def remove_documents(source_ids: Sequence[str]) -> Mapping[str, object]:
        return deletion_coordinator.remove_many(
            list(source_ids),
            delete_generated_artifacts=True,
            internal_copy_source_ids=list(source_ids),
        )

    def apply_metadata(source_id: str, metadata: Mapping[str, object]) -> object:
        try:
            return metadata_coordinator.persist_detected(source_id, metadata)
        except BibliographicMetadataError:
            # EPUB/Word documents have no PDF import-config entry; write the
            # catalog metadata directly, the same way the coordinator does.
            with durable_operations.operation(), index_runtime.mutation():
                index_runtime.suspend()
                try:
                    return update_metadata_in_database(paths.index_path, source_id, metadata)
                finally:
                    if not index_runtime.reopen():
                        logging.warning("index reopen after Zotero metadata update failed")

    service = ZoteroSyncService(
        ZoteroSyncStore(paths.index_path),
        ZoteroSyncPorts(
            read_preferences=preferences,
            sources=lambda: index_runtime.catalog()["source_files"],
            import_files=import_files,
            job_status=lambda job_id: import_orchestrator.job_status(job_id),
            remove_documents=remove_documents,
            apply_metadata=apply_metadata,
            hash_file=lambda path: sha256_file(Path(path)),
            parse_mode_label=lambda: PARSE_MODE_LABELS.get(parse_mode(), ""),
        ),
    )
    return service, ZoteroSyncController(service, parse_mode)

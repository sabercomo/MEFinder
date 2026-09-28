"""Assemble library queries, work groups, and source opening."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

from .app_context import AppContext
from .application.document_group_coordinator import DocumentGroupCoordinator
from .application.document_query_service import DocumentQueryError
from .document_group_controller import DocumentGroupController
from .import_assembly import ImportAssembly
from .library_query_controller import LibraryQueryController
from .mineru_api import MinerUError
from .preferences import resolve_preferences_path


@dataclass(frozen=True)
class LibraryAssembly:
    document_group_controller: DocumentGroupController
    library_query_controller: LibraryQueryController
    open_source_file: Callable[..., Dict[str, object]]


def assemble_library(
    context: AppContext,
    imports: ImportAssembly,
    *,
    open_pdf_with_platform: Callable[..., object],
    open_path_with_default_app: Callable[..., object],
    native_pdf_opener: object | None,
) -> LibraryAssembly:
    """Build library controllers and the source-opening callback."""

    root = context.paths.runtime_root
    index_runtime = imports.index_runtime
    durable_operations = imports.durable_operations
    document_queries = imports.document_queries
    page_mapping_coordinator = imports.page_mapping_coordinator
    document_group_coordinator = DocumentGroupCoordinator(
        context.paths,
        index_runtime,
        durable_operations,
    )
    document_group_controller = DocumentGroupController(
        document_group_coordinator
    )
    def open_source_file(source_id: str, page: object = None) -> Dict[str, object]:
        try:
            target = document_queries.source_path(source_id)
        except DocumentQueryError as exc:
            # DesktopShellController preserves the existing 400 response for
            # user-facing source lookup failures by handling MinerUError.
            raise MinerUError(str(exc)) from exc
        suffix = target.suffix.lower()
        if suffix == ".pdf":
            return open_pdf_with_platform(
                target,
                page,
                preferences_path=resolve_preferences_path(root),
                native_pdf_opener=native_pdf_opener,
            )
        open_path_with_default_app(target)
        return {"ok": True, "app": "system_default", "page_jump": False, "file": target.name}

    library_query_controller = LibraryQueryController(
        document_queries,
        index_runtime,
        additional_active_source_ids=(
            page_mapping_coordinator.active_source_ids
        ),
    )
    return LibraryAssembly(
        document_group_controller=document_group_controller,
        library_query_controller=library_query_controller,
        open_source_file=open_source_file,
    )


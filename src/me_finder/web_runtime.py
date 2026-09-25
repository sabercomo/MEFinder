"""Compose domain assemblies, route tables, and runtime lifecycle hooks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from . import alignment_assembly, import_assembly
from .app_context import AppContext
from .alignment_assembly import assemble_alignment
from .import_assembly import assemble_import
from .library_assembly import assemble_library
from .component_catalog import ComponentCatalog
from .managed_mineru import ManagedMinerU
from .settings_assembly import assemble_desktop_shell, assemble_settings
from .http_routes import (
    assemble_archive_routes,
    assemble_bibliography_routes,
    assemble_import_routes,
    assemble_library_routes,
    assemble_parser_settings_routes,
    assemble_preference_routes,
    assemble_reader_routes,
    assemble_shell_routes,
    assemble_source_routes,
)
from .zotero_sync_assembly import assemble_zotero_sync
from .zotero_sync import ZoteroSyncService
from .tasks.runtime_lifecycle import RuntimeLifecycle

@dataclass(frozen=True)
class ApplicationRuntime:
    """Everything the HTTP composition root needs from a built application."""

    index_path: Path
    root: Path
    index_runtime: import_assembly.IndexRuntime
    data_root_admission: import_assembly.DataRootAdmissionGate
    document_imports: import_assembly.DocumentImportCoordinator
    controller_get_routes: Mapping[str, Callable[..., tuple[int, object]]]
    controller_post_routes: Mapping[str, Callable[..., tuple[int, object]]]
    shell_get_routes: Mapping[str, Callable[..., tuple[int, object]]]
    shell_post_routes: Mapping[str, Callable[..., tuple[int, object]]]
    begin_shutdown: Callable[[], None]
    close_runtime: Callable[..., bool]
    wait_for_durable_operations: Callable[..., bool]
    submit_background_task: Callable[..., object]
    import_orchestrator: import_assembly.ImportOrchestrator
    import_job_controller: import_assembly.ImportJobController
    structured_reader_controller: alignment_assembly.StructuredReaderController
    archive_transfer_controller: import_assembly.ArchiveTransferController
    document_queries: import_assembly.DocumentQueryService
    backup_coordinator: import_assembly.BackupCoordinator
    deletion_coordinator: import_assembly.DocumentDeletionCoordinator
    metadata_coordinator: import_assembly.BibliographicMetadataCoordinator
    page_mapping_coordinator: import_assembly.PageMappingCoordinator
    bibliographic_metadata_controller: import_assembly.BibliographicMetadataController
    page_mapping_controller: import_assembly.PageMappingController
    component_catalog: ComponentCatalog
    managed_mineru: ManagedMinerU
    document_lifecycle_controller: import_assembly.DocumentLifecycleController
    zotero_sync: ZoteroSyncService


def build_application_runtime(
    context: AppContext,
    *,
    native_pdf_opener: object | None = None,
    native_theme_setter: object | None = None,
    update_service: object | None = None,
    native_directory_chooser: object | None = None,
    native_export_directory_chooser: object | None = None,
    native_scan_directory_chooser: object | None = None,
    native_backup_file_chooser: object | None = None,
    app_data_root: Path | None = None,
    default_app_data_root: Path | None = None,
    open_pdf_with_platform: Callable[..., object],
    open_path_with_default_app: Callable[..., object],
    open_external_cnki_url: Callable[..., object],
    open_mineru_token_page: Callable[..., object],
) -> ApplicationRuntime:
    """Construct services, controllers and route tables for one runtime root."""

    index_path = context.paths.index_path
    root = context.paths.runtime_root
    imports = assemble_import(context)
    index_runtime = imports.index_runtime
    import_task_queue = imports.import_task_queue
    data_root_admission = imports.data_root_admission
    durable_operations = imports.durable_operations
    document_queries = imports.document_queries
    import_orchestrator = imports.import_orchestrator
    document_imports = imports.document_imports
    import_job_controller = imports.import_job_controller
    metadata_coordinator = imports.metadata_coordinator
    page_mapping_coordinator = imports.page_mapping_coordinator
    backup_coordinator = imports.backup_coordinator
    archive_transfer_controller = imports.archive_transfer_controller
    deletion_coordinator = imports.deletion_coordinator
    bibliographic_metadata_controller = imports.bibliographic_metadata_controller
    page_mapping_controller = imports.page_mapping_controller
    document_lifecycle_controller = imports.document_lifecycle_controller
    library = assemble_library(
        context,
        imports,
        open_pdf_with_platform=open_pdf_with_platform,
        open_path_with_default_app=open_path_with_default_app,
        native_pdf_opener=native_pdf_opener,
    )
    document_group_controller = library.document_group_controller
    alignment = assemble_alignment(context, imports)
    text_alignment_controller = alignment.text_alignment_controller
    translation_work_controller = alignment.translation_work_controller
    structured_reader_controller = alignment.structured_reader_controller

    library_query_controller = library.library_query_controller
    settings = assemble_settings(
        context, imports, native_theme_setter=native_theme_setter,
    )
    preferences_controller = settings.preferences_controller
    parser_settings_controller = settings.parser_settings_controller
    managed = settings.managed
    component_catalog = managed.catalog
    managed_mineru = managed.mineru
    library_get_routes, library_post_routes = assemble_library_routes(
        library_query_controller,
        document_group_controller,
        page_mapping_controller,
        document_lifecycle_controller,
    )
    preference_get_routes, preference_post_routes = assemble_preference_routes(
        preferences_controller
    )
    parser_get_routes, parser_post_routes = assemble_parser_settings_routes(
        parser_settings_controller
    )
    bibliography_get_routes, bibliography_post_routes = (
        assemble_bibliography_routes(bibliographic_metadata_controller)
    )
    import_get_routes, import_post_routes = assemble_import_routes(
        import_job_controller
    )
    reader_get_routes, reader_post_routes = assemble_reader_routes(
        structured_reader_controller,
        text_alignment_controller,
        translation_work_controller,
    )
    archive_get_routes, archive_post_routes = assemble_archive_routes(
        archive_transfer_controller
    )
    zotero_sync, zotero_sync_controller = assemble_zotero_sync(
        context.paths, index_runtime=index_runtime, durable_operations=durable_operations,
        document_imports=document_imports, import_orchestrator=import_orchestrator,
        deletion_coordinator=deletion_coordinator, metadata_coordinator=metadata_coordinator,
        import_capacity=lambda: import_task_queue.free_slots,
    )
    source_get_routes, source_post_routes = assemble_source_routes(zotero_sync_controller)
    controller_get_routes = (
        library_get_routes | preference_get_routes | parser_get_routes | bibliography_get_routes
        | import_get_routes | reader_get_routes | archive_get_routes | source_get_routes
    )
    controller_post_routes = (
        library_post_routes | preference_post_routes | parser_post_routes | bibliography_post_routes
        | import_post_routes | reader_post_routes | archive_post_routes | source_post_routes
    )

    desktop_shell_controller, _open_mineru_token_route = assemble_desktop_shell(
        context,
        imports,
        library,
        update_service=update_service,
        native_directory_chooser=native_directory_chooser,
        native_export_directory_chooser=native_export_directory_chooser,
        native_scan_directory_chooser=native_scan_directory_chooser,
        native_backup_file_chooser=native_backup_file_chooser,
        open_external_cnki_url=open_external_cnki_url,
        open_mineru_token_page=open_mineru_token_page,
    )
    shell_get_routes, shell_post_routes = assemble_shell_routes(
        desktop_shell_controller,
        _open_mineru_token_route,
    )


    lifecycle = RuntimeLifecycle(imports, managed, zotero_sync)
    lifecycle.start(index_path)
    return ApplicationRuntime(
        zotero_sync=zotero_sync,
        index_path=index_path,
        root=root,
        index_runtime=index_runtime,
        data_root_admission=data_root_admission,
        document_imports=document_imports,
        controller_get_routes=controller_get_routes,
        controller_post_routes=controller_post_routes,
        shell_get_routes=shell_get_routes,
        shell_post_routes=shell_post_routes,
        begin_shutdown=lifecycle.begin_shutdown,
        close_runtime=lifecycle.close_runtime,
        wait_for_durable_operations=durable_operations.wait,
        submit_background_task=import_task_queue.submit,
        import_orchestrator=import_orchestrator,
        import_job_controller=import_job_controller,
        structured_reader_controller=structured_reader_controller,
        archive_transfer_controller=archive_transfer_controller,
        document_queries=document_queries,
        backup_coordinator=backup_coordinator,
        deletion_coordinator=deletion_coordinator,
        metadata_coordinator=metadata_coordinator,
        page_mapping_coordinator=page_mapping_coordinator,
        bibliographic_metadata_controller=bibliographic_metadata_controller,
        page_mapping_controller=page_mapping_controller,
        component_catalog=component_catalog,
        managed_mineru=managed_mineru,
        document_lifecycle_controller=document_lifecycle_controller,
    )

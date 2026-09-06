"""Application composition root: build every service, controller and route table.

Split out of :mod:`me_finder.web` so the HTTP composition root stays a thin
adapter.  :func:`build_application_runtime` constructs the whole application
runtime once, in a single scope (so the existing late-bound wiring lambdas keep
working unchanged), and returns an immutable :class:`ApplicationRuntime`.
Platform/OS helpers (native choosers, PDF openers) are injected by the caller so
this module carries no desktop-specific code.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping

from . import __version__
from .app_context import AppContext
from .application.backup_coordinator import BackupCoordinator
from .application.bibliographic_metadata_coordinator import BibliographicMetadataCoordinator
from .application.data_root_admission import DataRootAdmissionGate
from .application.document_deletion_coordinator import DocumentDeletionCoordinator
from .application.document_group_coordinator import DocumentGroupCoordinator
from .application.document_import_coordinator import DocumentImportCoordinator
from .application.document_query_service import (
    DocumentQueryError,
    DocumentQueryService,
)
from .application.import_orchestrator import ImportOrchestrator
from .application.index_runtime import IndexRuntime
from .application.page_mapping_coordinator import PageMappingCoordinator
from .application.text_alignment_coordinator import TextAlignmentCoordinator
from .archive_transfer_controller import ArchiveTransferController
from .backup_service import (
    restore_backup,
    write_backup,
)
from .bibliographic_metadata import (
    METADATA_FIELDS,
    canonical_metadata,
    detect_pdf_bibliographic_metadata,
    manual_metadata,
    metadata_missing_fields,
    update_metadata_in_database,
)
from .bibliographic_metadata_controller import BibliographicMetadataController
from .book_metadata_lookup import lookup_book
from .cnki_citation import parse_cnki_journal_citation
from .component_catalog import ComponentCatalog
from .crossref_lookup import lookup_crossref
from .data_location import migrate_data_root
from .database import replace_source_in_database
from .desktop_shell_controller import DesktopShellController
from .document_deletion import DocumentDeletionService
from .document_export_service import export_indexed_pdf
from .document_group_controller import DocumentGroupController
from .document_lifecycle_controller import DocumentLifecycleController
from .import_job_controller import ImportJobController
from .import_job_journal import (
    DEFAULT_IMPORT_JOB_DIR,
    ImportJobJournal,
)
from .import_queue import ImportTaskQueue
from .import_resume import sha256_file
from .http_routes import (
    assemble_archive_routes,
    assemble_bibliography_routes,
    assemble_import_routes,
    assemble_library_routes,
    assemble_parser_settings_routes,
    assemble_preference_routes,
    assemble_reader_routes,
    assemble_shell_routes,
)
from .journal_metadata_lookup import (
    fetch_cnki_candidate,
    lookup_cnki_journal,
)
from .large_document.job_ledger import JobLedger
from .large_document.mineru_accounts import (
    MinerUAccountService,
    resolve_mineru_accounts_path,
)
from .library_query_controller import LibraryQueryController
from .lifecycle import DurableOperationGate
from .local_ocr_installer import (
    LOCAL_OCR_MANIFEST_FILE,
    LocalOCRInstaller,
)
from .local_ocr_settings import resolve_local_ocr_config_path
from .macos_update import check_macos_update
from .managed_embedding_models import ManagedEmbeddingModels
from .managed_mineru import ManagedMinerU
from .mineru_api import (
    MinerUError,
    load_mineru_config,
    mineru_config_summary,
    normalize_mineru_token,
    read_mineru_config_data,
    resolve_mineru_config_path,
    save_mineru_config,
    test_mineru_connection,
    test_mineru_credential,
)
from .mineru_local_settings import mineru_local_config_summary
from .page_mapping_controller import PageMappingController
from .parser_settings_controller import ParserSettingsController
from .parser_statistics import build_parser_statistics
from .pdf_extractors import extract_pdf_source
from .document_file_store import copy_local_document
from .import_config_store import (
    import_config_lock,
    load_import_config,
    locked_import_config,
    save_import_config,
)
from .index_publisher import rebuild_local_index
from .pdf_import_service import (
    detect_imported_pdf,
    scan_directories_for_documents,
)
from .pdf_parser_adapters import (
    parse_pdf_with_local_ocr,
    parse_pdf_with_mineru,
    parse_pdf_with_provider,
)
from .persistence import SQLiteDocumentReadRepository
from .preferences import (
    read_preferences,
    resolve_preferences_path,
    save_preferences,
)
from .preferences_controller import PreferencesController
from .runtime_page_mapping import apply_mapping_to_database
from .search import SearchEngine
from .structured_reader import (
    get_document_citation,
    get_document_window,
)
from .structured_reader_controller import StructuredReaderController
from .text_alignment import (
    list_alignment_targets,
    locate_alignment,
)
from .text_alignment_controller import TextAlignmentController
from .vision_api import (
    delete_vision_provider,
    discover_vision_models,
    resolve_vision_config_path,
    save_vision_policy,
    save_vision_provider,
    test_vision_provider,
    vision_config_summary,
)

@dataclass(frozen=True)
class ApplicationRuntime:
    """Everything the HTTP composition root needs from a built application."""

    index_path: Path
    root: Path
    index_runtime: object
    data_root_admission: object
    document_imports: object
    controller_get_routes: Mapping[str, object]
    controller_post_routes: Mapping[str, object]
    shell_get_routes: Mapping[str, object]
    shell_post_routes: Mapping[str, object]
    begin_shutdown: Callable[[], None]
    close_runtime: Callable[..., bool]
    wait_for_durable_operations: Callable[..., bool]
    submit_background_task: Callable[..., object]
    import_orchestrator: object
    import_job_controller: object
    structured_reader_controller: object
    archive_transfer_controller: object
    document_queries: object
    backup_coordinator: object
    deletion_coordinator: object
    metadata_coordinator: object
    page_mapping_coordinator: object
    bibliographic_metadata_controller: object
    page_mapping_controller: object
    component_catalog: object
    managed_mineru: object
    document_lifecycle_controller: object


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
    app_data_directory = resolve_preferences_path(root).parent
    app_data_root = context.paths.app_data_root
    default_app_data_root = context.paths.default_app_data_root
    index_runtime = IndexRuntime(
        context.paths,
        engine_factory=lambda path: SearchEngine(path),
        rebuild_index=lambda runtime_root, on_progress, *, database_path: (
            rebuild_local_index(
                runtime_root,
                on_progress,
                database_path=database_path,
            )
        ),
        replace_source=lambda extracted, path, *, backup_existing: (
            replace_source_in_database(
                extracted,
                path,
                backup_existing=backup_existing,
            )
        ),
    )
    cnki_lookup_lock = threading.Lock()
    import_task_queue = ImportTaskQueue(worker_count=2)
    import_job_journal = ImportJobJournal(root / DEFAULT_IMPORT_JOB_DIR)
    data_root_admission = DataRootAdmissionGate()
    durable_operations = DurableOperationGate()
    mineru_job_ledger = JobLedger(root / "data" / "parser_jobs.sqlite3")
    mineru_account_service = MinerUAccountService(
        ledger=mineru_job_ledger,
        config_path=resolve_mineru_accounts_path(root),
    )
    document_queries = DocumentQueryService(
        context.paths,
        index_runtime,
        repository=SQLiteDocumentReadRepository(),
        active_source_ids=lambda: import_orchestrator.active_source_ids(),
        config_loader=lambda path: load_import_config(path),
        metadata_detector=(
            lambda path, pages, document, *, force=False: (
                detect_pdf_bibliographic_metadata(
                    path,
                    pages,
                    document,
                    force=force,
                )
            )
        ),
    )
    import_orchestrator = ImportOrchestrator(
        context.paths,
        index_runtime,
        durable_operations,
        import_task_queue,
        import_job_journal,
        parse_with_mineru=lambda *args, **kwargs: parse_pdf_with_mineru(
            *args, **kwargs
        ),
        parse_with_provider=lambda *args, **kwargs: parse_pdf_with_provider(
            *args, **kwargs
        ),
        parse_with_local_ocr=lambda *args, **kwargs: parse_pdf_with_local_ocr(
            *args, **kwargs
        ),
        extract_pdf=lambda *args, **kwargs: extract_pdf_source(*args, **kwargs),
        detect_metadata=document_queries.detect_bibliographic_metadata,
        persist_metadata=(
            lambda source_id, payload: metadata_coordinator.persist_detected(
                source_id, payload
            )
        ),
    )
    document_imports = DocumentImportCoordinator(
        context.paths,
        import_orchestrator,
        detect_pdf=lambda path: detect_imported_pdf(path),
        copy_local=lambda runtime_root, path: copy_local_document(
            runtime_root,
            path,
        ),
        hash_file=lambda path: sha256_file(path),
    )
    import_job_controller = ImportJobController(
        import_orchestrator,
        source_record=lambda source_id: index_runtime.source(source_id),
        source_path=lambda source_id: document_queries.source_path(source_id),
        detect_pdf=lambda path: detect_imported_pdf(path),
        vision_summary=(
            lambda: vision_config_summary(resolve_vision_config_path(root))
        ),
        local_mineru_summary=(
            lambda: mineru_local_config_summary(
                resolve_mineru_config_path(root)
            )
        ),
    )
    metadata_coordinator = BibliographicMetadataCoordinator(
        context.paths,
        document_queries,
        index_runtime,
        durable_operations,
        import_orchestrator,
        lock_config=lambda path: locked_import_config(path),
        save_config=lambda path, data: save_import_config(path, data),
        update_database=(
            lambda path, source_id, metadata: update_metadata_in_database(
                path,
                source_id,
                metadata,
            )
        ),
        canonicalize=lambda payload: canonical_metadata(payload),
        missing_fields=lambda payload: metadata_missing_fields(payload),
        build_manual_metadata=(
            lambda payload, document: manual_metadata(payload, document)
        ),
        metadata_fields=METADATA_FIELDS,
    )
    page_mapping_coordinator = PageMappingCoordinator(
        context.paths,
        index_runtime,
        durable_operations,
        document_queries,
        import_orchestrator,
        extract_pdf=(
            lambda *args, **kwargs: extract_pdf_source(*args, **kwargs)
        ),
        config_lock=lambda: import_config_lock(),
        load_config=lambda path: load_import_config(path),
        save_config=lambda path, data: save_import_config(path, data),
        apply_mapping=(
            lambda *args, **kwargs: apply_mapping_to_database(
                *args,
                **kwargs,
            )
        ),
    )
    backup_coordinator = BackupCoordinator(
        context.paths,
        index_runtime,
        durable_operations,
        import_orchestrator,
        app_data_root=lambda: app_data_directory,
        write=lambda *args, **kwargs: write_backup(*args, **kwargs),
        restore=lambda *args, **kwargs: restore_backup(*args, **kwargs),
        config_lock=lambda: import_config_lock(),
    )
    archive_transfer_controller = ArchiveTransferController(
        backup_coordinator,
        database_path=index_path,
        runtime_root=root,
        document_output_dir=app_data_directory / "exports",
        export_document=(
            lambda **kwargs: export_indexed_pdf(**kwargs)
        ),
    )
    deletion_coordinator = DocumentDeletionCoordinator(
        context.paths,
        index_runtime,
        durable_operations,
        import_orchestrator,
        service_factory=(
            lambda runtime_root, database_path: DocumentDeletionService(
                runtime_root,
                database_path,
            )
        ),
    )
    bibliographic_metadata_controller = BibliographicMetadataController(
        document_queries,
        metadata_coordinator,
        additional_active_source_ids=(
            page_mapping_coordinator.active_source_ids
        ),
        lookup_lock=cnki_lookup_lock,
        parse_cnki_citation=(
            lambda citation: parse_cnki_journal_citation(citation)
        ),
        lookup_cnki=lambda metadata: lookup_cnki_journal(metadata),
        fetch_cnki_candidate=(
            lambda candidate: fetch_cnki_candidate(candidate)
        ),
        lookup_google_books=lambda metadata: lookup_book(metadata),
        lookup_crossref=lambda metadata: lookup_crossref(metadata),
    )
    page_mapping_controller = PageMappingController(
        page_mapping_coordinator
    )
    document_lifecycle_controller = DocumentLifecycleController(
        deletion_coordinator
    )
    document_group_coordinator = DocumentGroupCoordinator(
        context.paths,
        index_runtime,
        durable_operations,
    )
    document_group_controller = DocumentGroupController(
        document_group_coordinator
    )
    text_alignment_coordinator = TextAlignmentCoordinator(
        context.paths,
        index_runtime,
        durable_operations,
    )
    text_alignment_controller = TextAlignmentController(
        text_alignment_coordinator,
        index_runtime.run_when_ready,
        list_targets=(
            lambda *args, **kwargs: list_alignment_targets(*args, **kwargs)
        ),
        locate=(
            lambda *args, **kwargs: locate_alignment(*args, **kwargs)
        ),
        log_exception=lambda message: logging.exception(message),
    )
    structured_reader_controller = StructuredReaderController(
        index_runtime.run_when_ready,
        get_window=(
            lambda *args, **kwargs: get_document_window(*args, **kwargs)
        ),
        get_citation=(
            lambda *args, **kwargs: get_document_citation(*args, **kwargs)
        ),
        log_exception=lambda message: logging.exception(message),
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
    preferences_controller = PreferencesController(
        resolve_preferences_path(root),
        index_runtime,
        native_theme_setter=native_theme_setter,
        read=lambda path: read_preferences(path),
        save=lambda payload, path: save_preferences(payload, path),
        scan_directories=(
            lambda directories, imported_names: scan_directories_for_documents(
                directories,
                imported_names,
            )
        ),
    )
    component_catalog = ComponentCatalog(root, LOCAL_OCR_MANIFEST_FILE)
    managed_embedding_models = ManagedEmbeddingModels(root)
    managed_mineru = ManagedMinerU(
        root,
        resolve_mineru_config_path(root),
        manifest_path=component_catalog.manifest_path,
        catalog_summary=component_catalog.summary,
    )
    local_ocr_installer = LocalOCRInstaller(
        root,
        resolve_local_ocr_config_path(root),
        manifest_path=component_catalog.manifest_path,
        catalog_summary=component_catalog.summary,
    )
    if os.environ.get("ME_FINDER_DESKTOP_SHELL", "").strip().lower() in {
        "macos",
        "win32",
        "linux",
    }:
        component_catalog.start_background_check(
            on_updated=lambda: (
                local_ocr_installer.refresh_manifest(),
                managed_mineru.refresh_manifest(),
            )
        )
    managed_mineru.start_installed_if_managed()
    parser_settings_controller = ParserSettingsController(
        context.paths,
        mineru_account_service,
        test_mineru_credential=(
            lambda *args, **kwargs: test_mineru_credential(*args, **kwargs)
        ),
        test_mineru_connection=(
            lambda *args, **kwargs: test_mineru_connection(*args, **kwargs)
        ),
        discover_vision_models=(
            lambda *args, **kwargs: discover_vision_models(*args, **kwargs)
        ),
        test_vision_provider=(
            lambda *args, **kwargs: test_vision_provider(*args, **kwargs)
        ),
        resolve_mineru_config=(
            lambda runtime_root: resolve_mineru_config_path(runtime_root)
        ),
        read_mineru_config=(
            lambda path: read_mineru_config_data(path)
        ),
        load_mineru=lambda path: load_mineru_config(path),
        normalize_mineru=lambda token: normalize_mineru_token(token),
        summarize_mineru=lambda path: mineru_config_summary(path),
        save_mineru=(
            lambda payload, path: save_mineru_config(payload, path)
        ),
        build_statistics=(
            lambda database_path, **kwargs: build_parser_statistics(
                database_path,
                **kwargs,
            )
        ),
        resolve_vision_config=(
            lambda runtime_root: resolve_vision_config_path(runtime_root)
        ),
        summarize_vision=lambda path: vision_config_summary(path),
        save_vision=(
            lambda payload, path: save_vision_provider(payload, path)
        ),
        delete_vision=(
            lambda provider_id, path: delete_vision_provider(
                provider_id,
                path,
            )
        ),
        save_vision_fallback=(
            lambda payload, path: save_vision_policy(payload, path)
        ),
        managed_components={
            local_ocr_installer.component_id: local_ocr_installer,
            managed_mineru.component_id: managed_mineru,
            managed_embedding_models.component_id: managed_embedding_models,
        },
    )
    parser_settings_controller.migrate_legacy_mineru_account()
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
    )
    archive_get_routes, archive_post_routes = assemble_archive_routes(
        archive_transfer_controller
    )
    controller_get_routes = (
        library_get_routes
        | preference_get_routes
        | parser_get_routes
        | bibliography_get_routes
        | import_get_routes
        | reader_get_routes
        | archive_get_routes
    )
    controller_post_routes = (
        library_post_routes
        | preference_post_routes
        | parser_post_routes
        | bibliography_post_routes
        | import_post_routes
        | reader_post_routes
        | archive_post_routes
    )

    desktop_shell_controller = DesktopShellController(
        current_version=__version__,
        desktop_shell=os.environ.get("ME_FINDER_DESKTOP_SHELL", ""),
        check_macos_update=lambda current_version: check_macos_update(
            current_version
        ),
        open_source=lambda source_id, page: open_source_file(source_id, page),
        open_cnki=lambda value: open_external_cnki_url(value),
        durable_operations=durable_operations,
        data_root_migration=data_root_admission.migration,
        has_active_uploads=document_imports.has_active_uploads,
        has_active_jobs=import_orchestrator.has_active_jobs,
        runtime_mutation=index_runtime.mutation,
        migrate_data_root=lambda current_root, target_root, default_root: (
            index_runtime.run_when_ready(
                lambda _database_path: migrate_data_root(
                    current_root,
                    target_root,
                    default_root,
                )
            )
        ),
        update_service=update_service,
        native_directory_chooser=native_directory_chooser,
        native_export_directory_chooser=native_export_directory_chooser,
        native_scan_directory_chooser=native_scan_directory_chooser,
        native_backup_file_chooser=native_backup_file_chooser,
        app_data_root=app_data_root,
        default_app_data_root=default_app_data_root,
    )
    def _open_mineru_token_route():
        try:
            open_mineru_token_page()
            return (200, {"ok": True})
        except Exception:  # noqa: BLE001 - surface a friendly toast, log details
            logging.exception("打开 MinerU Token 页面失败")
            return (500, {"ok": False, "error": "打开 MinerU 失败，请手动访问。"})

    shell_get_routes, shell_post_routes = assemble_shell_routes(
        desktop_shell_controller,
        _open_mineru_token_route,
    )


    def begin_shutdown() -> None:
        """Reject new writes and stop accepting background work."""

        durable_operations.begin_shutdown()
        index_runtime.begin_shutdown()
        import_task_queue.shutdown(wait=False)

    def close_runtime(timeout: float = 2.0) -> bool:
        """Release the SQLite handle this handler holds open.

        The desktop app keeps its index open until the process exits, but a
        caller that outlives one handler -- notably a test using a temporary
        directory -- must be able to let go of the file.  Windows refuses to
        delete a database that still has an open connection.
        """

        begin_shutdown()
        document_imports.close()
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        durable_stopped = durable_operations.wait(timeout=timeout)
        if not durable_stopped:
            logging.warning(
                "durable mutations are still committing; runtime engine kept open"
            )
            return False
        remaining = (
            None if deadline is None else max(0.0, deadline - time.monotonic())
        )
        workers_stopped = import_task_queue.shutdown(wait=True, timeout=remaining)
        if not workers_stopped:
            # Keep the engine alive for the accepted task.  A long-lived caller
            # can retry close_runtime after it checkpoints; a desktop process
            # releases all handles immediately when it exits.
            logging.warning(
                "background imports are still stopping; runtime engine kept open"
            )
            return False
        managed_mineru.close()
        index_runtime.close()
        return True


    return ApplicationRuntime(
        index_path=index_path,
        root=root,
        index_runtime=index_runtime,
        data_root_admission=data_root_admission,
        document_imports=document_imports,
        controller_get_routes=controller_get_routes,
        controller_post_routes=controller_post_routes,
        shell_get_routes=shell_get_routes,
        shell_post_routes=shell_post_routes,
        begin_shutdown=begin_shutdown,
        close_runtime=close_runtime,
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

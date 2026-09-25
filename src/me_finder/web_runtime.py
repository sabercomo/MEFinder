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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping

from . import __version__, translation_works
from .app_context import AppContext
from .alignment_assembly import assemble_alignment
from .application.document_group_coordinator import DocumentGroupCoordinator
from .application.document_query_service import (
    DocumentQueryError,
)
from .data_location import migrate_data_root
from .desktop_shell_controller import DesktopShellController
from .document_group_controller import DocumentGroupController
from .import_assembly import assemble_import
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
from .library_query_controller import LibraryQueryController
from .macos_update import check_macos_update
from .zotero_sync_assembly import assemble_zotero_sync
from .managed_component_assembly import assemble_managed_components
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
from .parser_settings_controller import ParserSettingsController
from .parser_statistics import build_parser_statistics
from .pdf_import_service import (
    scan_directories_for_documents,
)
from .preferences import (
    read_preferences,
    resolve_preferences_path,
    save_preferences,
)
from .preferences_controller import PreferencesController
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
    zotero_sync: object = None


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
    app_data_root = context.paths.app_data_root
    default_app_data_root = context.paths.default_app_data_root
    imports = assemble_import(context)
    index_runtime = imports.index_runtime
    import_task_queue = imports.import_task_queue
    data_root_admission = imports.data_root_admission
    durable_operations = imports.durable_operations
    mineru_account_service = imports.mineru_account_service
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
    document_group_coordinator = DocumentGroupCoordinator(
        context.paths,
        index_runtime,
        durable_operations,
    )
    document_group_controller = DocumentGroupController(
        document_group_coordinator
    )
    alignment = assemble_alignment(context, imports)
    text_alignment_controller = alignment.text_alignment_controller
    translation_work_controller = alignment.translation_work_controller
    structured_reader_controller = alignment.structured_reader_controller

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
    managed = assemble_managed_components(root)
    component_catalog = managed.catalog
    managed_mineru = managed.mineru
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
        managed_components=managed.registry,
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

        # A translation alignment runs its multi-minute embedding inside a
        # durable operation; shutdown waits for that operation to drain. Signal
        # the embedding loop to stop at its next batch so the wait returns in
        # seconds instead of blocking the whole app close on a full-book run.
        from .embedding_runtime import request_embedding_cancel

        request_embedding_cancel()
        zotero_sync.stop()
        managed.embedding_models.begin_shutdown()
        managed.alignment_runtime.begin_shutdown()
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
        # Cancel and reap any in-flight alignment-runtime install/verify process
        # before reporting shutdown complete, so nothing is left behind.
        if not managed.embedding_models.close(timeout=remaining):
            return False
        if not managed.alignment_runtime.close(timeout=remaining):
            return False
        index_runtime.close()
        return True


    zotero_sync.start_scheduler()
    translation_works.start_body_bounds_warm_up(index_path)
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

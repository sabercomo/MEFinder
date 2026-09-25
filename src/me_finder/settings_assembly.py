"""Assemble preferences, parser settings, and desktop shell adapters."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Dict

from . import __version__
from .app_context import AppContext
from .data_location import migrate_data_root
from .desktop_shell_controller import DesktopShellController
from .import_assembly import ImportAssembly
from .library_assembly import LibraryAssembly
from .macos_update import check_macos_update
from .managed_component_assembly import ManagedComponents, assemble_managed_components
from .mineru_api import (
    load_mineru_config, mineru_config_summary, normalize_mineru_token,
    read_mineru_config_data, resolve_mineru_config_path, save_mineru_config,
    test_mineru_connection, test_mineru_credential,
)
from .parser_settings_controller import ParserSettingsController
from .parser_statistics import build_parser_statistics
from .pdf_import_service import scan_directories_for_documents
from .preferences import read_preferences, resolve_preferences_path, save_preferences
from .preferences_controller import PreferencesController
from .vision_api import (
    delete_vision_provider, discover_vision_models, resolve_vision_config_path,
    save_vision_policy, save_vision_provider, test_vision_provider,
    vision_config_summary,
)


@dataclass(frozen=True)
class SettingsAssembly:
    preferences_controller: PreferencesController
    parser_settings_controller: ParserSettingsController
    managed: ManagedComponents


def assemble_settings(
    context: AppContext,
    imports: ImportAssembly,
    *,
    native_theme_setter: object | None,
) -> SettingsAssembly:
    """Build local settings and managed components in the original order."""

    root = context.paths.runtime_root
    index_runtime = imports.index_runtime
    mineru_account_service = imports.mineru_account_service
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
    return SettingsAssembly(
        preferences_controller=preferences_controller,
        parser_settings_controller=parser_settings_controller,
        managed=managed,
    )


def assemble_desktop_shell(
    context: AppContext,
    imports: ImportAssembly,
    library: LibraryAssembly,
    *,
    update_service: object | None,
    native_directory_chooser: object | None,
    native_export_directory_chooser: object | None,
    native_scan_directory_chooser: object | None,
    native_backup_file_chooser: object | None,
    open_external_cnki_url: Callable[..., object],
    open_mineru_token_page: Callable[..., object],
) -> tuple[DesktopShellController, Callable[[], tuple[int, Dict[str, object]]]]:
    """Wire desktop actions after the HTTP domain controllers are ready."""

    index_runtime = imports.index_runtime
    durable_operations = imports.durable_operations
    data_root_admission = imports.data_root_admission
    document_imports = imports.document_imports
    import_orchestrator = imports.import_orchestrator
    open_source_file = library.open_source_file
    app_data_root = context.paths.app_data_root
    default_app_data_root = context.paths.default_app_data_root
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

    return desktop_shell_controller, _open_mineru_token_route

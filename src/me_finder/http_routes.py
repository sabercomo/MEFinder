"""HTTP route assembly grouped by existing product domains.

Each function receives the controllers owned by one domain and returns that
domain's GET and POST maps.  The application composition root merges the maps
explicitly; there is no dependency container or runtime route registry.
"""

from __future__ import annotations

from collections.abc import Callable


Route = Callable[..., tuple[int, object]]
RouteMap = dict[str, Route]
RoutePair = tuple[RouteMap, RouteMap]


def assemble_library_routes(
    library_query_controller,
    document_group_controller,
    page_mapping_controller,
    document_lifecycle_controller,
) -> RoutePair:
    get_routes = {
        "/api/index-meta": lambda _params: library_query_controller.index_metadata(),
        "/api/sources": lambda _params: library_query_controller.sources(),
        "/api/library": lambda params: library_query_controller.library(
            (params.get("view") or [""])[0]
        ),
        "/api/library/document": lambda params: library_query_controller.document(
            (params.get("source_id") or [""])[0]
        ),
        "/api/calibration-library": (
            lambda _params: library_query_controller.calibration_library()
        ),
        "/api/document-groups": lambda _params: document_group_controller.list(),
    }
    post_routes = {
        "/api/document-groups/create": document_group_controller.create,
        "/api/document-groups/combine": document_group_controller.combine,
        "/api/document-groups/rename": document_group_controller.rename,
        "/api/document-groups/delete": document_group_controller.delete,
        "/api/document-groups/add-member": document_group_controller.add_member,
        "/api/document-groups/remove-member": document_group_controller.remove_member,
        "/api/document-groups/set-base": document_group_controller.set_base,
        "/api/document-groups/version-label": document_group_controller.set_version_label,
        "/api/calibration": page_mapping_controller.calibrate,
        "/api/auto-page-mapping/detect": page_mapping_controller.detect,
        "/api/auto-page-mapping/apply": page_mapping_controller.apply,
        "/api/auto-page-mapping/accept": page_mapping_controller.accept,
        "/api/documents/remove": document_lifecycle_controller.remove,
        "/api/documents/remove-batch": document_lifecycle_controller.remove_batch,
    }
    return get_routes, post_routes


def assemble_preference_routes(preferences_controller) -> RoutePair:
    get_routes = {
        "/api/preferences": lambda _params: preferences_controller.preferences(),
        "/api/scan-directories": (
            lambda _params: preferences_controller.scan_directories()
        ),
    }
    post_routes = {
        "/api/preferences": preferences_controller.save_preferences,
    }
    return get_routes, post_routes


def assemble_parser_settings_routes(parser_settings_controller) -> RoutePair:
    get_routes = {
        "/api/mineru-accounts": (
            lambda _params: parser_settings_controller.mineru_accounts()
        ),
        "/api/mineru-statistics": (
            lambda _params: parser_settings_controller.mineru_statistics()
        ),
        "/api/parser-statistics": (
            lambda _params: parser_settings_controller.parser_statistics()
        ),
        "/api/components": (
            lambda _params: parser_settings_controller.component_diagnostics()
        ),
        "/api/text-alignment/models": (
            lambda _params: parser_settings_controller.text_alignment_models_component()
        ),
        "/api/mineru-config": (
            lambda _params: parser_settings_controller.mineru_config()
        ),
        "/api/mineru-local/component": (
            lambda _params: parser_settings_controller.managed_mineru_local_component()
        ),
        "/api/local-ocr": (
            lambda _params: parser_settings_controller.local_ocr_config()
        ),
        "/api/vision-providers": (
            lambda _params: parser_settings_controller.vision_providers()
        ),
        "/api/general-model": (
            lambda _params: parser_settings_controller.general_model_config()
        ),
    }
    post_routes = {
        "/api/mineru-accounts": parser_settings_controller.save_mineru_account,
        "/api/mineru-accounts/test": parser_settings_controller.test_mineru_account,
        "/api/mineru-accounts/service": parser_settings_controller.save_mineru_service,
        "/api/mineru-config": parser_settings_controller.save_mineru_config,
        "/api/mineru-config/test": (
            lambda _payload: parser_settings_controller.test_mineru_config()
        ),
        "/api/mineru-local": parser_settings_controller.save_mineru_local_config,
        "/api/mineru-local/test": parser_settings_controller.test_mineru_local_config,
        "/api/mineru-local/component": (
            parser_settings_controller.manage_mineru_local_component
        ),
        "/api/local-ocr": parser_settings_controller.save_local_ocr_config,
        "/api/local-ocr/test": parser_settings_controller.test_local_ocr_config,
        "/api/local-ocr/component": parser_settings_controller.manage_local_ocr_component,
        "/api/text-alignment/models": (
            parser_settings_controller.manage_text_alignment_models_component
        ),
        "/api/vision-providers": parser_settings_controller.update_vision_providers,
        "/api/vision-providers/models": parser_settings_controller.vision_models,
        "/api/vision-providers/test": parser_settings_controller.test_vision_provider,
        "/api/general-model": parser_settings_controller.save_general_model,
        "/api/general-model/models": parser_settings_controller.general_model_models,
        "/api/general-model/test": (
            parser_settings_controller.test_general_model_connection
        ),
    }
    return get_routes, post_routes


def assemble_bibliography_routes(bibliographic_metadata_controller) -> RoutePair:
    get_routes = {
        "/api/bibliographic-metadata": (
            lambda params: bibliographic_metadata_controller.metadata(
                (params.get("source_id") or [None])[0]
            )
        ),
    }
    post_routes = {
        "/api/bibliographic-metadata/batch-detect": (
            bibliographic_metadata_controller.batch_detect
        ),
        "/api/bibliographic-metadata/parse-cnki-citation": (
            bibliographic_metadata_controller.parse_cnki_citation
        ),
        "/api/bibliographic-metadata/lookup-cnki": (
            bibliographic_metadata_controller.lookup_cnki
        ),
        "/api/bibliographic-metadata/cnki-candidate": (
            bibliographic_metadata_controller.cnki_candidate
        ),
        "/api/bibliographic-metadata/lookup-google-books": (
            bibliographic_metadata_controller.lookup_google_books
        ),
        "/api/bibliographic-metadata/lookup-crossref": (
            bibliographic_metadata_controller.lookup_crossref
        ),
        "/api/bibliographic-metadata/detect": bibliographic_metadata_controller.detect,
        "/api/bibliographic-metadata/save": bibliographic_metadata_controller.save,
    }
    return get_routes, post_routes


def assemble_import_routes(import_job_controller) -> RoutePair:
    get_routes = {
        "/api/import-status": lambda params: import_job_controller.status(
            (params.get("job_id") or [None])[0]
        ),
        "/api/import-resumable": lambda _params: import_job_controller.resumable(),
    }
    post_routes = {
        "/api/mineru-reparse": import_job_controller.reparse_with_mineru,
        "/api/import-retry-mineru": import_job_controller.retry_with_mineru,
        "/api/import-retry-mineru-local": (
            import_job_controller.retry_with_local_mineru
        ),
        "/api/import-retry": import_job_controller.retry_with_provider,
        "/api/import-resume": import_job_controller.resume,
        "/api/import-resume-dismiss": import_job_controller.dismiss,
    }
    return get_routes, post_routes


def assemble_reader_routes(
    structured_reader_controller,
    text_alignment_controller,
) -> RoutePair:
    get_routes = {
        "/api/document/pages": structured_reader_controller.pages,
        "/api/text-alignments/targets": text_alignment_controller.targets,
    }
    post_routes = {
        "/api/document/citation": structured_reader_controller.citation,
        "/api/text-alignments/generate": text_alignment_controller.generate,
        "/api/text-alignments/locate": text_alignment_controller.locate,
    }
    return get_routes, post_routes


def assemble_archive_routes(archive_transfer_controller) -> RoutePair:
    get_routes = {}
    post_routes = {
        "/api/backup/export": archive_transfer_controller.export_backup,
        "/api/document/export": archive_transfer_controller.export_document,
        "/api/document/export-markdown": (
            archive_transfer_controller.export_document_markdown
        ),
        "/api/document/export-epub": archive_transfer_controller.export_document_epub,
        "/api/backup/import": archive_transfer_controller.restore_backup,
    }
    return get_routes, post_routes


def assemble_shell_routes(
    desktop_shell_controller,
    open_mineru_token_route: Callable[[], tuple[int, object]],
) -> RoutePair:
    get_routes = {
        "/api/update/status": desktop_shell_controller.update_status,
        "/api/macos-update": desktop_shell_controller.macos_update,
        "/api/data-location": desktop_shell_controller.data_location,
    }
    post_routes = {
        "/api/update/check": desktop_shell_controller.check_for_updates,
        "/api/update/download": (
            lambda _payload: desktop_shell_controller.download_update()
        ),
        "/api/update/install": desktop_shell_controller.install_update,
        "/api/scan-directories/choose": (
            lambda _payload: desktop_shell_controller.choose_scan_directories()
        ),
        "/api/backup/import/choose": (
            lambda _payload: desktop_shell_controller.choose_backup_file()
        ),
        "/api/export-directory/choose": (
            lambda _payload: desktop_shell_controller.choose_export_directory()
        ),
        "/api/data-location/choose": (
            lambda _payload: desktop_shell_controller.choose_data_location()
        ),
        "/api/data-location/migrate": desktop_shell_controller.migrate_data_location,
        "/api/open-source": desktop_shell_controller.open_source,
        "/api/bibliographic-metadata/open-cnki": desktop_shell_controller.open_cnki,
        "/api/open-mineru-token": lambda _payload: open_mineru_token_route(),
    }
    return get_routes, post_routes

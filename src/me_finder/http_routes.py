"""HTTP route assembly grouped by existing product domains.

Each function receives the controllers owned by one domain and returns that
domain's GET and POST maps; transport policy is declared inline via ``route``.
The HTTP composition root merges the maps into ``http_route_table.RouteTable``.
"""

from __future__ import annotations

from collections.abc import Callable

from .http_route_table import CNKI_CITATION_BODY, CNKI_LOOKUP_BODY, RoutePair, mutating, route


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
        "/api/calibration-library": lambda _params: library_query_controller.calibration_library(),
        "/api/document-groups": lambda _params: document_group_controller.list(),
    }
    post_routes = {
        "/api/document-groups/create": mutating(document_group_controller.create),
        "/api/document-groups/combine": document_group_controller.combine,
        "/api/document-groups/rename": mutating(document_group_controller.rename),
        "/api/document-groups/delete": mutating(document_group_controller.delete),
        "/api/document-groups/add-member": mutating(document_group_controller.add_member),
        "/api/document-groups/move-members": document_group_controller.move_members,
        "/api/document-groups/remove-member": mutating(document_group_controller.remove_member),
        "/api/document-groups/set-base": mutating(document_group_controller.set_base),
        "/api/document-groups/version-label": mutating(document_group_controller.set_version_label),
        "/api/calibration": mutating(page_mapping_controller.calibrate),
        "/api/auto-page-mapping/detect": page_mapping_controller.detect,
        "/api/auto-page-mapping/apply": mutating(page_mapping_controller.apply),
        "/api/auto-page-mapping/accept": mutating(page_mapping_controller.accept),
        "/api/documents/remove": mutating(document_lifecycle_controller.remove),
        "/api/documents/remove-batch": mutating(document_lifecycle_controller.remove_batch),
    }
    return get_routes, post_routes


def assemble_preference_routes(preferences_controller) -> RoutePair:
    get_routes = {
        "/api/preferences": lambda _params: preferences_controller.preferences(),
        "/api/scan-directories": lambda _params: preferences_controller.scan_directories(),
    }
    post_routes = {
        "/api/preferences": mutating(preferences_controller.save_preferences),
    }
    return get_routes, post_routes


def assemble_parser_settings_routes(parser_settings_controller) -> RoutePair:
    get_routes = {
        "/api/mineru-accounts": lambda _params: parser_settings_controller.mineru_accounts(),
        "/api/mineru-statistics": lambda _params: parser_settings_controller.mineru_statistics(),
        "/api/parser-statistics": lambda _params: parser_settings_controller.parser_statistics(),
        "/api/components": lambda _params: parser_settings_controller.component_diagnostics(),
        "/api/text-alignment/models": (
            lambda _params: parser_settings_controller.text_alignment_models_component()
        ),
        "/api/text-alignment/runtime": (
            lambda _params: parser_settings_controller.text_alignment_runtime_component()
        ),
        "/api/mineru-config": lambda _params: parser_settings_controller.mineru_config(),
        "/api/mineru-local/component": (
            lambda _params: parser_settings_controller.managed_mineru_local_component()
        ),
        "/api/local-ocr": lambda _params: parser_settings_controller.local_ocr_config(),
        "/api/vision-providers": (
            lambda _params: parser_settings_controller.vision_providers()
        ),
        "/api/general-model": (
            lambda _params: parser_settings_controller.general_model_config()
        ),
    }
    post_routes = {
        "/api/mineru-accounts": mutating(parser_settings_controller.save_mineru_account),
        "/api/mineru-accounts/test": parser_settings_controller.test_mineru_account,
        "/api/mineru-accounts/service": mutating(parser_settings_controller.save_mineru_service),
        "/api/mineru-config": mutating(parser_settings_controller.save_mineru_config),
        "/api/mineru-config/test": (
            lambda _payload: parser_settings_controller.test_mineru_config()
        ),
        "/api/mineru-local": mutating(parser_settings_controller.save_mineru_local_config),
        "/api/mineru-local/test": parser_settings_controller.test_mineru_local_config,
        "/api/mineru-local/component": mutating(
            parser_settings_controller.manage_mineru_local_component
        ),
        "/api/local-ocr": mutating(parser_settings_controller.save_local_ocr_config),
        "/api/local-ocr/test": parser_settings_controller.test_local_ocr_config,
        "/api/local-ocr/component": mutating(parser_settings_controller.manage_local_ocr_component),
        "/api/text-alignment/models": mutating(
            parser_settings_controller.manage_text_alignment_models_component
        ),
        "/api/text-alignment/runtime": (
            parser_settings_controller.manage_text_alignment_runtime_component
        ),
        "/api/vision-providers": mutating(parser_settings_controller.update_vision_providers),
        "/api/vision-providers/models": parser_settings_controller.vision_models,
        "/api/vision-providers/test": parser_settings_controller.test_vision_provider,
        "/api/general-model": mutating(parser_settings_controller.save_general_model),
        "/api/general-model/models": parser_settings_controller.general_model_models,
        "/api/general-model/test": (
            parser_settings_controller.test_general_model_connection
        ),
    }
    return get_routes, post_routes


def assemble_bibliography_routes(bibliographic_metadata_controller) -> RoutePair:
    bib = bibliographic_metadata_controller
    get_routes = {
        "/api/bibliographic-metadata": (
            lambda params: bib.metadata((params.get("source_id") or [None])[0])
        ),
    }
    post_routes = {
        "/api/bibliographic-metadata/batch-detect": mutating(bib.batch_detect),
        "/api/bibliographic-metadata/parse-cnki-citation": route(bib.parse_cnki_citation, **CNKI_CITATION_BODY),
        "/api/bibliographic-metadata/lookup-cnki": route(bib.lookup_cnki, **CNKI_LOOKUP_BODY),
        "/api/bibliographic-metadata/cnki-candidate": route(bib.cnki_candidate, **CNKI_LOOKUP_BODY),
        "/api/bibliographic-metadata/lookup-google-books": bib.lookup_google_books,
        "/api/bibliographic-metadata/lookup-crossref": bib.lookup_crossref,
        "/api/bibliographic-metadata/detect": bib.detect,
        "/api/bibliographic-metadata/save": mutating(bib.save),
    }
    return get_routes, post_routes


def assemble_source_routes(zotero_sync_controller) -> RoutePair:
    """来源：从 Zotero 分类同步文献（只读本机 Local API）。"""

    get_routes = {
        "/api/zotero/overview": zotero_sync_controller.overview,
        "/api/zotero/status": zotero_sync_controller.status,
    }
    post_routes = {
        "/api/zotero/preview": zotero_sync_controller.preview,
        "/api/zotero/sync": zotero_sync_controller.sync,
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
        "/api/mineru-reparse": mutating(import_job_controller.reparse_with_mineru),
        "/api/import-retry-mineru": mutating(import_job_controller.retry_with_mineru),
        "/api/import-retry-mineru-local": mutating(import_job_controller.retry_with_local_mineru),
        "/api/import-retry": mutating(import_job_controller.retry_with_provider),
        "/api/import-resume": mutating(import_job_controller.resume),
        "/api/import-resume-dismiss": mutating(import_job_controller.dismiss),
    }
    return get_routes, post_routes


def assemble_reader_routes(
    structured_reader_controller,
    text_alignment_controller,
    translation_work_controller,
) -> RoutePair:
    works = translation_work_controller
    get_routes = {
        "/api/document/pages": route(structured_reader_controller.pages, keep_blank_query=True),
        "/api/document/outline": structured_reader_controller.outline,
        "/api/text-alignments/targets": text_alignment_controller.targets,
        "/api/text-alignments/status": text_alignment_controller.status,
        "/api/text-alignments/current": text_alignment_controller.current,
        "/api/text-alignments/links": works.links,
        "/api/text-alignments/body-range/segments": (
            text_alignment_controller.body_range_segments
        ),
        "/api/translation-works/overview": works.overview,
        "/api/translation-works/reading-position": works.reading_position,
        "/api/translation-works/suggestion-dismissals": works.suggestion_dismissals,
    }
    post_routes = {
        "/api/document/citation": route(structured_reader_controller.citation, max_body_bytes=16 * 1024, oversize_error="引文请求内容过大。"),
        "/api/text-alignments/generate": text_alignment_controller.generate,
        "/api/text-alignments/start": text_alignment_controller.start,
        "/api/text-alignments/cancel": text_alignment_controller.cancel,
        "/api/text-alignments/locate": text_alignment_controller.locate,
        "/api/text-alignments/body-range": text_alignment_controller.body_ranges,
        "/api/text-alignments/review-candidates": works.review_candidates,
        "/api/text-alignments/corrections/save": works.save_correction,
        "/api/text-alignments/corrections/defer": works.defer_review,
        "/api/translation-works/reading-position": works.save_reading_position,
        "/api/translation-works/dismiss-suggestion": works.dismiss_suggestion,
    }
    return get_routes, post_routes


def assemble_archive_routes(archive_transfer_controller) -> RoutePair:
    get_routes = {}
    post_routes = {
        "/api/backup/export": mutating(archive_transfer_controller.export_backup),
        "/api/document/export": mutating(archive_transfer_controller.export_document),
        "/api/document/export-markdown": (
            archive_transfer_controller.export_document_markdown
        ),
        "/api/document/export-epub": archive_transfer_controller.export_document_epub,
        "/api/backup/import": mutating(archive_transfer_controller.restore_backup),
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
        "/api/export-directory/choose": mutating(
            lambda _payload: desktop_shell_controller.choose_export_directory()
        ),
        "/api/data-location/choose": (
            lambda payload: desktop_shell_controller.choose_data_location(payload)
        ),
        "/api/data-location/migrate": desktop_shell_controller.migrate_data_location,
        "/api/data-location/switch": desktop_shell_controller.switch_data_location,
        "/api/open-source": desktop_shell_controller.open_source,
        "/api/bibliographic-metadata/open-cnki": route(desktop_shell_controller.open_cnki, **CNKI_LOOKUP_BODY),
        "/api/open-mineru-token": lambda _payload: open_mineru_token_route(),
    }
    return get_routes, post_routes

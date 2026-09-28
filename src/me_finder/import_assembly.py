"""Assemble import, storage, backup, and document mutation services."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .app_context import AppContext
from .application.backup_coordinator import BackupCoordinator
from .application.bibliographic_metadata_coordinator import BibliographicMetadataCoordinator
from .application.data_root_admission import DataRootAdmissionGate
from .application.document_heading_enrichment import DocumentHeadingEnrichment
from .application.document_deletion_coordinator import DocumentDeletionCoordinator
from .application.document_import_coordinator import DocumentImportCoordinator
from .application.document_query_service import DocumentQueryService
from .application.import_orchestrator import ImportOrchestrator
from .application.index_runtime import IndexRuntime
from .application.page_mapping_coordinator import PageMappingCoordinator
from .archive_transfer_controller import ArchiveTransferController, build_archive_transfer_controller
from .backup_service import restore_backup, write_backup
from .bibliographic_metadata import (
    METADATA_FIELDS, canonical_metadata, detect_pdf_bibliographic_metadata,
    manual_metadata, metadata_missing_fields, update_metadata_in_database,
)
from .bibliographic_metadata_controller import BibliographicMetadataController
from .book_metadata_lookup import lookup_book
from .cnki_citation import parse_cnki_journal_citation
from .crossref_lookup import lookup_crossref
from .database import replace_source_in_database
from .document_deletion import DocumentDeletionService
from .document_lifecycle_controller import DocumentLifecycleController
from .document_file_store import copy_local_document
from .import_config_store import import_config_lock, load_import_config, locked_import_config, save_import_config
from .import_job_controller import ImportJobController
from .import_job_journal import DEFAULT_IMPORT_JOB_DIR, ImportJobJournal
from .import_queue import ImportTaskQueue
from .import_resume import sha256_file
from .index_publisher import rebuild_local_index
from .journal_metadata_lookup import fetch_cnki_candidate, lookup_cnki_journal
from .large_document.job_ledger import JobLedger
from .large_document.mineru_accounts import MinerUAccountService, resolve_mineru_accounts_path
from .lifecycle import DurableOperationGate
from .mineru_api import resolve_mineru_config_path
from .mineru_local_settings import mineru_local_config_summary
from .page_mapping_controller import PageMappingController
from .pdf_extractors import extract_pdf_source
from .pdf_import_service import detect_imported_pdf
from .pdf_parser_adapters import parse_pdf_with_local_ocr, parse_pdf_with_mineru, parse_pdf_with_provider
from .persistence import SQLiteDocumentHeadingStore, SQLiteDocumentReadRepository
from .preferences import read_preferences, resolve_preferences_path, record_backup_export
from .runtime_page_mapping import apply_mapping_to_database
from .search import SearchEngine
from .vision_api import resolve_vision_config_path, vision_config_summary


@dataclass(frozen=True)
class ImportAssembly:
    index_runtime: IndexRuntime
    import_task_queue: ImportTaskQueue
    data_root_admission: DataRootAdmissionGate
    durable_operations: DurableOperationGate
    mineru_account_service: MinerUAccountService
    document_queries: DocumentQueryService
    import_orchestrator: ImportOrchestrator
    document_imports: DocumentImportCoordinator
    import_job_controller: ImportJobController
    metadata_coordinator: BibliographicMetadataCoordinator
    page_mapping_coordinator: PageMappingCoordinator
    backup_coordinator: BackupCoordinator
    archive_transfer_controller: ArchiveTransferController
    deletion_coordinator: DocumentDeletionCoordinator
    bibliographic_metadata_controller: BibliographicMetadataController
    page_mapping_controller: PageMappingController
    document_lifecycle_controller: DocumentLifecycleController


def assemble_import(context: AppContext) -> ImportAssembly:
    """Build import and document mutation services in their original order."""

    index_path = context.paths.index_path
    root = context.paths.runtime_root
    app_data_directory = resolve_preferences_path(root).parent
    index_runtime = IndexRuntime(
        context.paths,
        engine_factory=lambda path: SearchEngine(path),
        script_folding_enabled=lambda: read_preferences(resolve_preferences_path(root))["script_folding"],
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
        parse_with_mineru=parse_pdf_with_mineru,
        parse_with_provider=parse_pdf_with_provider,
        parse_with_local_ocr=parse_pdf_with_local_ocr,
        extract_pdf=extract_pdf_source,
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
        lock_config=locked_import_config,
        save_config=save_import_config,
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
        load_config=load_import_config,
        save_config=save_import_config,
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
        write=write_backup,
        restore=restore_backup,
        config_lock=lambda: import_config_lock(),
    )
    archive_transfer_controller = build_archive_transfer_controller(
        backup_coordinator,
        database_path=index_path,
        runtime_root=root,
        document_output_dir=app_data_directory / "exports",
        prepare_document_export=DocumentHeadingEnrichment(
            store=SQLiteDocumentHeadingStore(index_path),
            runtime_root=root,
            durable_operations=durable_operations,
            index_runtime=index_runtime,
        ).enrich,
        record_backup_export=record_backup_export,  # 导出成功后记进本机偏好
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
    return ImportAssembly(
        index_runtime=index_runtime,
        import_task_queue=import_task_queue,
        data_root_admission=data_root_admission,
        durable_operations=durable_operations,
        mineru_account_service=mineru_account_service,
        document_queries=document_queries,
        import_orchestrator=import_orchestrator,
        document_imports=document_imports,
        import_job_controller=import_job_controller,
        metadata_coordinator=metadata_coordinator,
        page_mapping_coordinator=page_mapping_coordinator,
        backup_coordinator=backup_coordinator,
        archive_transfer_controller=archive_transfer_controller,
        deletion_coordinator=deletion_coordinator,
        bibliographic_metadata_controller=bibliographic_metadata_controller,
        page_mapping_controller=page_mapping_controller,
        document_lifecycle_controller=document_lifecycle_controller,
    )

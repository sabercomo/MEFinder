"""Local web interface — iOS-style SPA shell."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import CancelledError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Union
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .app_context import AppContext
from .application import SearchRequest
from .application.backup_coordinator import BackupCoordinator
from .application.bibliographic_metadata_coordinator import BibliographicMetadataCoordinator
from .application.document_query_service import (
    DocumentQueryError,
    DocumentQueryService,
)
from .application.document_deletion_coordinator import DocumentDeletionCoordinator
from .application.data_root_admission import (
    DataRootAdmissionError,
    DataRootAdmissionGate,
)
from .application.document_import_coordinator import DocumentImportCoordinator
from .application.import_orchestrator import (
    ImportJobCancelled,
    ImportOrchestrator,
)
from .application.index_runtime import IndexRuntime
from .application.page_mapping_coordinator import PageMappingCoordinator
from .application.document_group_coordinator import DocumentGroupCoordinator
from .document_group_controller import DocumentGroupController
from .document_groups import DocumentGroupNotFound, resolve_document_group_source_ids
from .database import DEFAULT_DATABASE_PATH, replace_source_in_database
from .data_location import migrate_data_root
from .bibliographic_metadata_controller import BibliographicMetadataController
from .archive_transfer_controller import ArchiveTransferController
from .desktop_shell_controller import DesktopShellController
from .document_lifecycle_controller import DocumentLifecycleController
from .import_job_controller import ImportJobController
from .library_query_controller import LibraryQueryController
from .page_mapping_controller import PageMappingController
from .parser_settings_controller import ParserSettingsController
from .preferences_controller import PreferencesController
from .structured_reader_controller import StructuredReaderController
from .bibliographic_metadata import (
    METADATA_FIELDS,
    canonical_metadata,
    detect_pdf_bibliographic_metadata,
    manual_metadata,
    metadata_missing_fields,
    update_metadata_in_database,
)
from .book_metadata_lookup import lookup_book
from .cnki_citation import parse_cnki_journal_citation
from .crossref_lookup import lookup_crossref
from .journal_metadata_lookup import (
    fetch_cnki_candidate,
    lookup_cnki_journal,
)
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
from .large_document.job_ledger import JobLedger
from .mineru_local_settings import mineru_local_config_summary
from .large_document.mineru_accounts import (
    MinerUAccountService,
    resolve_mineru_accounts_path,
)
from .local_ocr_installer import LocalOCRInstaller
from .local_ocr_settings import resolve_local_ocr_config_path
from .parser_statistics import build_parser_statistics
from .macos_update import check_macos_update
from .vision_api import (
    VisionAPIError,
    delete_vision_provider,
    discover_vision_models,
    resolve_vision_config_path,
    save_vision_policy,
    save_vision_provider,
    test_vision_provider,
    vision_config_summary,
)
from .preferences import read_preferences, resolve_preferences_path, save_preferences
from .document_deletion import DocumentDeletionService
from .document_export_service import export_indexed_pdf
from .pdf_extractors import extract_pdf_source
from .pdf_import_service import (
    copy_local_document,
    detect_imported_pdf,
    import_config_lock,
    locked_import_config,
    parse_pdf_with_mineru,
    parse_pdf_with_local_ocr,
    parse_pdf_with_provider,
    rebuild_local_index,
    load_import_config,
    save_import_config,
    scan_directories_for_documents,
)
from .runtime_page_mapping import apply_mapping_to_database
from .backup_service import restore_backup, write_backup
from .import_job_journal import DEFAULT_IMPORT_JOB_DIR, ImportJobJournal
from .import_queue import ImportTaskQueue
from .import_resume import sha256_file
from .http_range import InvalidByteRange, parse_byte_range
from .lifecycle import DurableOperationGate
from .search import SearchEngine
from .chunked_upload import (
    ChunkedUploadError,
)
from .structured_reader import get_document_citation, get_document_window


MAX_JSON_REQUEST_BYTES = 1024 * 1024
SOURCE_STREAM_CHUNK_BYTES = 1024 * 1024
RAW_BODY_POST_PATHS = frozenset(
    {
        "/api/import",
        "/api/import-upload/chunk",
    }
)
DATA_ROOT_MUTATING_POST_PATHS = frozenset(
    {
        "/api/preferences",
        "/api/mineru-accounts",
        "/api/mineru-accounts/service",
        "/api/mineru-config",
        "/api/mineru-local",
        "/api/local-ocr",
        "/api/local-ocr/component",
        "/api/vision-providers",
        "/api/import",
        "/api/import-upload/start",
        "/api/import-upload/chunk",
        "/api/import-upload/cancel",
        "/api/import-upload/finish",
        "/api/mineru-reparse",
        "/api/import-retry-mineru",
        "/api/import-retry-mineru-local",
        "/api/import-retry",
        "/api/import-resume",
        "/api/import-resume-dismiss",
        "/api/bibliographic-metadata/batch-detect",
        "/api/export-directory/choose",
        "/api/backup/export",
        "/api/document/export",
        "/api/backup/import",
        "/api/import-local",
        "/api/calibration",
        "/api/bibliographic-metadata/save",
        "/api/auto-page-mapping/apply",
        "/api/auto-page-mapping/accept",
        "/api/documents/remove",
        "/api/documents/remove-batch",
        "/api/document-groups/create",
        "/api/document-groups/rename",
        "/api/document-groups/delete",
        "/api/document-groups/add-member",
        "/api/document-groups/remove-member",
        "/api/document-groups/set-base",
        "/api/document-groups/version-label",
    }
)


class ManagedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading server with an observable, bounded request drain.

    ``ThreadingMixIn.server_close`` normally waits forever for non-daemon
    handlers.  That can strand a Windows WebView2 process (and its updater) if
    a client disappears during a native dialog, network lookup, or upload.
    Track accepted handlers ourselves so the desktop adapter can wait for a
    bounded interval without closing the runtime out from under live requests.
    """

    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, **kwargs) -> None:
        self._handler_condition = threading.Condition()
        self._active_handlers = 0
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        with self._handler_condition:
            self._active_handlers += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._handler_condition:
                self._active_handlers -= 1
                self._handler_condition.notify_all()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._handler_condition:
                self._active_handlers -= 1
                self._handler_condition.notify_all()

    def wait_for_handlers(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._handler_condition:
            while self._active_handlers:
                if deadline is None:
                    self._handler_condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._handler_condition.wait(remaining)
            return True


def find_adobe_pdf_app() -> Optional[Path]:
    """Find an installed Adobe Acrobat/Reader executable on Windows."""

    if sys.platform != "win32":
        return None
    candidate_paths = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if not base:
            continue
        candidate_paths.extend(
            [
                Path(base) / "Adobe" / "Acrobat DC" / "Acrobat" / "Acrobat.exe",
                Path(base) / "Adobe" / "Acrobat Reader DC" / "Reader" / "AcroRd32.exe",
                Path(base) / "Adobe" / "Acrobat" / "Acrobat.exe",
                Path(base) / "Adobe" / "Acrobat Reader" / "Reader" / "AcroRd32.exe",
            ]
        )
    registry_paths = _adobe_paths_from_registry()
    for path in registry_paths + candidate_paths:
        if path and Path(path).exists():
            return Path(path)
    return None


def _adobe_paths_from_registry() -> List[Path]:
    paths: List[Path] = []
    try:
        import winreg  # type: ignore
    except Exception:
        return paths
    registry_keys = [
        (winreg.HKEY_CLASSES_ROOT, r"AcroExch.Document.DC\shell\Open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"AcroExch.Document\shell\Open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"Applications\Acrobat.exe\shell\open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"Applications\AcroRd32.exe\shell\open\command"),
    ]
    for hive, key_name in registry_keys:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                command = str(winreg.QueryValueEx(key, "")[0])
        except OSError:
            continue
        match = re.search(r'"([^"]+\.exe)"', command, flags=re.IGNORECASE)
        if not match:
            match = re.search(r"([A-Za-z]:\\[^\s]+\.exe)", command, flags=re.IGNORECASE)
        if match:
            paths.append(Path(match.group(1)))
    return paths


def find_default_adobe_pdf_app() -> Optional[Path]:
    """Return Adobe only when Windows currently associates PDF files with it."""

    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore
    except Exception:
        return None

    association_keys = [
        (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice",
            "ProgId",
        ),
        (winreg.HKEY_CLASSES_ROOT, r".pdf", ""),
    ]
    prog_id = ""
    for hive, key_name, value_name in association_keys:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                prog_id = str(winreg.QueryValueEx(key, value_name)[0]).strip()
        except OSError:
            continue
        if prog_id:
            break

    normalized = prog_id.casefold()
    if not any(marker in normalized for marker in ("acroexch", "acrobat", "acrord")):
        return None
    return find_adobe_pdf_app()


def open_path_with_default_app(target: Path) -> None:
    """Open a local file with the platform's default application."""

    target = Path(target)
    if sys.platform == "win32":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return
    command = ["open", str(target)] if sys.platform == "darwin" else ["xdg-open", str(target)]
    subprocess.Popen(command, close_fds=True)


def open_external_cnki_url(value: object) -> None:
    """Open one validated public CNKI page in the system browser."""

    url = str(value or "").strip()
    parsed = urlparse(url)
    if (
        len(url) > 4096
        or parsed.scheme != "https"
        or parsed.hostname != "oversea.cnki.net"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"/kns8s/search", "/kcms2/article/abstract"}
    ):
        raise ValueError("知网页面地址无效。")
    if sys.platform == "win32":
        os.startfile(url)  # type: ignore[attr-defined]
        return
    command = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
    subprocess.Popen(command, close_fds=True)


def open_path_in_macos_preview(target: Path) -> None:
    """Open a local file explicitly in Preview.app."""

    if sys.platform != "darwin":
        raise RuntimeError("预览.app 仅在 macOS 上可用。")
    subprocess.Popen(["open", "-a", "Preview", str(Path(target))], close_fds=True)


def _normalized_pdf_page(page: object) -> Optional[int]:
    try:
        page_number = int(page) if page not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return page_number if page_number is not None and page_number > 0 else None


def open_pdf_in_adobe(target: Path, page_number: Optional[int]) -> Optional[Dict[str, object]]:
    """Use Adobe page jumping only when Adobe is the Windows PDF default."""

    if sys.platform != "win32" or page_number is None:
        return None
    adobe = find_default_adobe_pdf_app()
    if adobe is None:
        return None
    target = Path(target)
    try:
        subprocess.Popen(
            [str(adobe), "/A", f"page={page_number}", str(target)],
            close_fds=True,
        )
    except Exception:
        # Page jumping is a convenience: a broken Adobe install, an antivirus
        # block or a group policy must never stop the PDF from opening at all.
        # Returning None lets the caller use the user's default reader.
        logging.exception("Adobe page jump failed; falling back to the default PDF app")
        return None
    return {
        "ok": True,
        "app": str(adobe),
        "viewer_mode": "adobe",
        "page_jump": True,
        "file": target.name,
        "page": page_number,
    }


NativePDFOpener = Callable[[Path, Optional[int]], Dict[str, object]]
NativeThemeSetter = Callable[[str], None]
NativeDirectoryChooser = Callable[[], Optional[str]]
NativeExportDirectoryChooser = Callable[[], Optional[str]]
NativeScanDirectoryChooser = Callable[[], Optional[Union[str, Sequence[str]]]]
NativeBackupFileChooser = Callable[[], Optional[str]]


def open_pdf_with_platform(
    target: Path,
    page: object = None,
    *,
    preferences_path: Path | None = None,
    native_pdf_opener: NativePDFOpener | None = None,
) -> Dict[str, object]:
    """Open one PDF according to the persisted platform preference."""

    target = Path(target)
    page_number = _normalized_pdf_page(page)
    preferences = read_preferences(preferences_path)
    open_mode = str(preferences.get("pdf_open_mode") or "native")

    if open_mode == "system":
        if sys.platform == "darwin":
            open_path_in_macos_preview(target)
            app = "preview"
        else:
            adobe_result = open_pdf_in_adobe(target, page_number)
            if adobe_result is not None:
                return adobe_result
            open_path_with_default_app(target)
            app = "system_default"
        return {
            "ok": True,
            "app": app,
            "viewer_mode": "system",
            "page_jump": False,
            "file": target.name,
            "page": page_number,
        }

    native_error: Exception | None = None
    if native_pdf_opener is not None:
        try:
            native_result = native_pdf_opener(target, page_number)
            actual_page = page_number
            page_count = native_result.get("page_count")
            if page_number is not None:
                try:
                    returned_page = int(native_result.get("page") or page_number)
                except (TypeError, ValueError):
                    returned_page = page_number
                actual_page = returned_page if returned_page > 0 else page_number
            return {
                "ok": True,
                "app": (
                    "pdfkit"
                    if sys.platform == "darwin"
                    else "webview2"
                    if sys.platform == "win32"
                    else "native"
                ),
                "viewer_mode": "native",
                "page_jump": bool(actual_page),
                "file": target.name,
                "page": actual_page,
                "requested_page": page_number,
                "page_count": page_count,
                "page_adjusted": (
                    page_number is not None and actual_page != page_number
                ),
            }
        except CancelledError:
            # Application shutdown invalidated an in-flight native-window
            # request. Do not turn cancellation into an external PDF launch.
            raise
        except Exception as exc:
            native_error = exc
            logging.exception("native PDF reader failed; falling back to an external app")

    # Native reader failures still retain Adobe's page-jump behavior.
    adobe_result = open_pdf_in_adobe(target, page_number)
    if adobe_result is not None:
        return adobe_result

    if native_error is not None and sys.platform == "darwin":
        open_path_in_macos_preview(target)
        app = "preview"
    else:
        open_path_with_default_app(target)
        app = "system_default"
    return {
        "ok": True,
        "app": app,
        "viewer_mode": "system",
        "page_jump": False,
        "fallback": native_error is not None,
        "file": target.name,
        "page": page_number,
    }


_PACKAGE_DIR = Path(__file__).resolve().parent


def _load_asset(relative: str) -> str:
    return (_PACKAGE_DIR / relative).read_text(encoding="utf-8")


def _load_asset_dir(relative: str, suffix: str) -> str:
    """按文件名排序拼接一个资源目录。

    app.js 已按功能拆分到 static/js/，用数字前缀锁定加载顺序（00-state 最先、
    90-init 最后）。这些文件共享同一个全局作用域，拼接结果与拆分前逐字节相同，
    所以顺序必须严格按文件名排序，改名或加新文件时要注意前缀。
    """

    directory = _PACKAGE_DIR / relative
    if not directory.is_dir():
        return ""
    parts = [
        path.read_text(encoding="utf-8")
        for path in sorted(directory.glob(f"*{suffix}"), key=lambda p: p.name)
    ]
    return "".join(parts)


def _load_app_js() -> str:
    """优先使用拆分后的 static/js/；单文件 app.js 仅作为回退保留。"""

    bundled = _load_asset_dir("static/js", ".js")
    return bundled if bundled else _load_asset("static/app.js")


def _load_app_css() -> str:
    """优先使用拆分后的 static/css/；单文件 app.css 仅作为回退保留。

    与 static/js/ 同理，按数字前缀（00-themes 最先、90-dialogs-toast 最后）
    的文件名排序拼接，结果与拆分前逐字节相同，改名或加新文件时要注意前缀。
    """

    bundled = _load_asset_dir("static/css", ".css")
    return bundled if bundled else _load_asset("static/app.css")


HTML = (
    _load_asset("templates/index.html")
    .replace("/*__APP_CSS__*/", _load_app_css(), 1)
    .replace("/*__READER_CSS__*/", _load_asset("static/reader.css"), 1)
    .replace("//__APP_JS__", _load_app_js(), 1)
    .replace("//__READER_JS__", _load_asset("static/reader.js"), 1)
    .replace("__APP_VERSION__", __version__)
)


def render_html(theme: str) -> str:
    """Inject the persisted theme before the browser paints the first frame."""

    marker = '<html lang="zh-CN" data-theme="frost-blue">'
    desktop_shell = os.environ.get("ME_FINDER_DESKTOP_SHELL", "").strip().lower()
    shell_attribute = (
        f' data-desktop-shell="{desktop_shell}"'
        if desktop_shell in {"macos", "win32"}
        else ""
    )
    replacement = f'<html lang="zh-CN" data-theme="{theme}"{shell_attribute}>'
    return HTML.replace(marker, replacement, 1)


def make_handler(
    index_path: Path,
    *,
    app_context: AppContext | None = None,
    native_pdf_opener: NativePDFOpener | None = None,
    native_theme_setter: NativeThemeSetter | None = None,
    update_service: object | None = None,
    native_directory_chooser: NativeDirectoryChooser | None = None,
    native_export_directory_chooser: NativeExportDirectoryChooser | None = None,
    native_scan_directory_chooser: NativeScanDirectoryChooser | None = None,
    native_backup_file_chooser: NativeBackupFileChooser | None = None,
    app_data_root: Path | None = None,
    default_app_data_root: Path | None = None,
):
    # Keep the existing ``index_path`` entry point while allowing desktop and
    # future adapters to inject every process-level path explicitly.
    context = app_context or AppContext.create(
        Path.cwd(),
        index_path=index_path,
        app_data_root=app_data_root,
        default_app_data_root=default_app_data_root,
    )
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
    local_ocr_installer = LocalOCRInstaller(
        root,
        resolve_local_ocr_config_path(root),
    )
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
        summarize_local_ocr_installer=local_ocr_installer.summary,
        manage_local_ocr_installer=local_ocr_installer.perform,
    )
    parser_settings_controller.migrate_legacy_mineru_account()
    controller_get_routes = {
        "/api/index-meta": (
            lambda _params: library_query_controller.index_metadata()
        ),
        "/api/sources": lambda _params: library_query_controller.sources(),
        "/api/library": (
            lambda params: library_query_controller.library(
                (params.get("view") or [""])[0]
            )
        ),
        "/api/library/document": (
            lambda params: library_query_controller.document(
                (params.get("source_id") or [""])[0]
            )
        ),
        "/api/calibration-library": (
            lambda _params: library_query_controller.calibration_library()
        ),
        "/api/document-groups": (
            lambda _params: document_group_controller.list()
        ),
        "/api/preferences": (
            lambda _params: preferences_controller.preferences()
        ),
        "/api/scan-directories": (
            lambda _params: preferences_controller.scan_directories()
        ),
        "/api/mineru-accounts": (
            lambda _params: parser_settings_controller.mineru_accounts()
        ),
        "/api/mineru-statistics": (
            lambda _params: parser_settings_controller.mineru_statistics()
        ),
        "/api/parser-statistics": (
            lambda _params: parser_settings_controller.parser_statistics()
        ),
        "/api/mineru-config": (
            lambda _params: parser_settings_controller.mineru_config()
        ),
        "/api/local-ocr": (
            lambda _params: parser_settings_controller.local_ocr_config()
        ),
        "/api/vision-providers": (
            lambda _params: parser_settings_controller.vision_providers()
        ),
        "/api/bibliographic-metadata": (
            lambda params: bibliographic_metadata_controller.metadata(
                (params.get("source_id") or [None])[0]
            )
        ),
        "/api/import-status": (
            lambda params: import_job_controller.status(
                (params.get("job_id") or [None])[0]
            )
        ),
        "/api/import-resumable": (
            lambda _params: import_job_controller.resumable()
        ),
        "/api/document/pages": structured_reader_controller.pages,
    }
    controller_post_routes = {
        "/api/preferences": preferences_controller.save_preferences,
        "/api/mineru-accounts": (
            parser_settings_controller.save_mineru_account
        ),
        "/api/mineru-accounts/test": (
            parser_settings_controller.test_mineru_account
        ),
        "/api/mineru-accounts/service": (
            parser_settings_controller.save_mineru_service
        ),
        "/api/mineru-config": parser_settings_controller.save_mineru_config,
        "/api/mineru-config/test": (
            lambda _payload: parser_settings_controller.test_mineru_config()
        ),
        "/api/mineru-local": (
            parser_settings_controller.save_mineru_local_config
        ),
        "/api/mineru-local/test": (
            parser_settings_controller.test_mineru_local_config
        ),
        "/api/local-ocr": (
            parser_settings_controller.save_local_ocr_config
        ),
        "/api/local-ocr/test": (
            parser_settings_controller.test_local_ocr_config
        ),
        "/api/local-ocr/component": (
            parser_settings_controller.manage_local_ocr_component
        ),
        "/api/vision-providers": (
            parser_settings_controller.update_vision_providers
        ),
        "/api/vision-providers/models": (
            parser_settings_controller.vision_models
        ),
        "/api/vision-providers/test": (
            parser_settings_controller.test_vision_provider
        ),
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
        "/api/bibliographic-metadata/detect": (
            bibliographic_metadata_controller.detect
        ),
        "/api/bibliographic-metadata/save": (
            bibliographic_metadata_controller.save
        ),
        "/api/document-groups/create": document_group_controller.create,
        "/api/document-groups/rename": document_group_controller.rename,
        "/api/document-groups/delete": document_group_controller.delete,
        "/api/document-groups/add-member": document_group_controller.add_member,
        "/api/document-groups/remove-member": (
            document_group_controller.remove_member
        ),
        "/api/document-groups/set-base": document_group_controller.set_base,
        "/api/document-groups/version-label": (
            document_group_controller.set_version_label
        ),
        "/api/calibration": page_mapping_controller.calibrate,
        "/api/auto-page-mapping/detect": page_mapping_controller.detect,
        "/api/auto-page-mapping/apply": page_mapping_controller.apply,
        "/api/auto-page-mapping/accept": page_mapping_controller.accept,
        "/api/documents/remove": document_lifecycle_controller.remove,
        "/api/documents/remove-batch": (
            document_lifecycle_controller.remove_batch
        ),
        "/api/mineru-reparse": import_job_controller.reparse_with_mineru,
        "/api/import-retry-mineru": import_job_controller.retry_with_mineru,
        "/api/import-retry-mineru-local": (
            import_job_controller.retry_with_local_mineru
        ),
        "/api/import-retry": import_job_controller.retry_with_provider,
        "/api/import-resume": import_job_controller.resume,
        "/api/import-resume-dismiss": import_job_controller.dismiss,
        "/api/document/citation": structured_reader_controller.citation,
        "/api/backup/export": archive_transfer_controller.export_backup,
        "/api/document/export": archive_transfer_controller.export_document,
        "/api/document/export-markdown": (
            archive_transfer_controller.export_document_markdown
        ),
        "/api/backup/import": archive_transfer_controller.restore_backup,
    }

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
    shell_get_routes = {
        "/api/update/status": desktop_shell_controller.update_status,
        "/api/macos-update": desktop_shell_controller.macos_update,
        "/api/data-location": desktop_shell_controller.data_location,
    }
    shell_post_routes = {
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
        "/api/data-location/migrate": (
            desktop_shell_controller.migrate_data_location
        ),
        "/api/open-source": desktop_shell_controller.open_source,
        "/api/bibliographic-metadata/open-cnki": (
            desktop_shell_controller.open_cnki
        ),
    }

    class Handler(BaseHTTPRequestHandler):
        _POST_ROUTE_TABLE = {
            "/api/search": "_post_search",
        }

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            content_length: int | None = None,
            send_body: bool = True,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body) if content_length is None else content_length))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _send_json(self, data: object, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _discard_small_request_body(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                return
            if 0 < length <= MAX_JSON_REQUEST_BYTES:
                self.rfile.read(length)

        def _validated_request_host(
            self,
        ) -> tuple[Optional[tuple[str, int]], Optional[int]]:
            values = self.headers.get_all("Host") or []
            if len(values) != 1:
                return None, 400
            value = str(values[0]).strip()
            try:
                parsed = urlparse(f"//{value}")
                port = parsed.port or 80
            except ValueError:
                return None, 400
            hostname = str(parsed.hostname or "").casefold()
            if (
                not hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.params
                or parsed.query
                or parsed.fragment
            ):
                return None, 400
            if (
                hostname not in {"127.0.0.1", "localhost", "::1"}
                or port != self.server.server_port
            ):
                return None, 421
            return (hostname, port), None

        def _request_origin_is_trusted(
            self,
            authority: tuple[str, int],
        ) -> bool:
            values = self.headers.get_all("Origin") or []
            if not values:
                return True
            if len(values) != 1:
                return False
            try:
                parsed = urlparse(str(values[0]).strip())
                port = parsed.port or 80
            except ValueError:
                return False
            return bool(
                parsed.scheme == "http"
                and str(parsed.hostname or "").casefold() == authority[0]
                and port == authority[1]
                and parsed.username is None
                and parsed.password is None
                and not parsed.path
                and not parsed.params
                and not parsed.query
                and not parsed.fragment
            )

        def _request_target_matches(
            self,
            authority: tuple[str, int],
        ) -> bool:
            parsed = urlparse(self.path)
            if not parsed.scheme and not parsed.netloc:
                return True
            try:
                port = parsed.port or 80
            except ValueError:
                return False
            return bool(
                parsed.scheme == "http"
                and str(parsed.hostname or "").casefold() == authority[0]
                and port == authority[1]
                and parsed.username is None
                and parsed.password is None
            )

        def _reject_untrusted_request(self, *, send_body: bool = True) -> bool:
            authority, host_error = self._validated_request_host()
            if host_error is None and not self._request_target_matches(authority):
                host_error = 421
            if host_error is None and self._request_origin_is_trusted(authority):
                return False
            status = host_error or 403
            message = (
                "Host 请求头无效。"
                if status == 400
                else "请求目标不是当前本地服务。"
                if status == 421
                else "请求来源不受信任。"
            )
            body = json.dumps(
                {"error": message},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(
                status,
                body if send_body else b"",
                "application/json; charset=utf-8",
                content_length=len(body),
                send_body=send_body,
            )
            return True

        def _post_search(self, payload: object) -> None:
            try:
                # Resolve a document_group_id scope to member source_file_ids at the
                # transport boundary; SearchService / search.py never see DocumentGroups.
                if isinstance(payload, dict) and str(
                    payload.get("document_group_id") or ""
                ).strip():
                    if str(payload.get("source_file_id") or "").strip():
                        raise ValueError(
                            "source_file_id 与 document_group_id 不能同时指定。"
                        )
                    member_ids = resolve_document_group_source_ids(
                        payload["document_group_id"], index_path
                    )
                    payload = dict(payload)
                    payload.pop("document_group_id", None)
                    payload["source_file_ids"] = member_ids
                request = SearchRequest.from_payload(payload)
            except DocumentGroupNotFound as exc:
                self._send_json({"error": str(exc)}, status=404)
                return
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            result = index_runtime.search(request)
            if result is None:
                self._send_json(
                    {"error": "索引正在重建，请稍候再搜索。"},
                    status=503,
                )
                return
            self._send_json(result)

        def do_GET(self) -> None:
            if self._reject_untrusted_request():
                return
            parsed = urlparse(self.path)
            controller_route = controller_get_routes.get(parsed.path)
            if controller_route is not None:
                params = parse_qs(
                    parsed.query,
                    keep_blank_values=(parsed.path == "/api/document/pages"),
                )
                status, payload = controller_route(params)
                self._send_json(payload, status=status)
                return
            shell_route = shell_get_routes.get(parsed.path)
            if shell_route is not None:
                status, payload = shell_route()
                self._send_json(payload, status=status)
                return
            if parsed.path in {"/", "/index.html", "/reader", "/reader/"}:
                preferences_path = resolve_preferences_path(root)
                theme = read_preferences(preferences_path)["theme"]
                body = render_html(theme).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return
            if parsed.path.startswith("/static/brands/"):
                name = parsed.path.rsplit("/", 1)[-1]
                icon_path = _PACKAGE_DIR / "static" / "brands" / name
                if re.fullmatch(r"[a-z0-9][a-z0-9-]*\.svg", name) and icon_path.is_file():
                    self._send(200, icon_path.read_bytes(), "image/svg+xml")
                else:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            if parsed.path == "/api/calibration":
                config_path = root / "config" / "pdf_imports.json"
                if not config_path.exists():
                    self._send_json({"documents": []})
                    return
                config = load_import_config(config_path)
                params = parse_qs(parsed.query)
                sid = (params.get("source_id") or [None])[0]
                if sid:
                    doc = next((d for d in config.get("documents", []) if d.get("source_file_id") == sid), None)
                    self._send_json(doc or {"error": "not found"})
                else:
                    self._send_json(config)
                return
            if parsed.path.startswith("/source/"):
                self._send_source(parsed.path)
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_HEAD(self) -> None:
            if self._reject_untrusted_request(send_body=False):
                return
            parsed = urlparse(self.path)
            if parsed.path.startswith("/source/"):
                self._send_source(parsed.path, send_body=False)
                return
            if parsed.path in {"/", "/index.html", "/reader", "/reader/"}:
                preferences_path = resolve_preferences_path(root)
                theme = read_preferences(preferences_path)["theme"]
                content_length = len(render_html(theme).encode("utf-8"))
                self._send(200, b"", "text/html; charset=utf-8", content_length=content_length, send_body=False)
                return
            self._send(404, b"", "text/plain; charset=utf-8", send_body=False)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if self._reject_untrusted_request():
                self._discard_small_request_body()
                return
            content_type = str(self.headers.get("Content-Type") or "")
            media_type = content_type.partition(";")[0].strip().casefold()
            if parsed.path in RAW_BODY_POST_PATHS:
                invalid_content_type = media_type in {
                    "",
                    "text/plain",
                    "application/x-www-form-urlencoded",
                    "multipart/form-data",
                }
                content_type_error = "不支持此上传 Content-Type。"
            else:
                invalid_content_type = media_type != "application/json"
                content_type_error = "JSON 请求必须使用 application/json。"
            if invalid_content_type:
                self._discard_small_request_body()
                self._send_json(
                    {"error": content_type_error},
                    status=415,
                )
                return
            if parsed.path not in DATA_ROOT_MUTATING_POST_PATHS:
                self._do_POST()
                return
            try:
                with data_root_admission.operation():
                    self._do_POST()
            except DataRootAdmissionError as exc:
                self._discard_small_request_body()
                self._send_json({"error": str(exc)}, status=409)

        def _do_POST(self) -> None:
            parsed = urlparse(self.path)
            if index_runtime.closing:
                self._discard_small_request_body()
                self._send_json({"error": "应用正在关闭。"}, status=503)
                return
            if parsed.path == "/api/import":
                filename = unquote(self.headers.get("X-File-Name", ""))
                suffix = Path(filename).suffix.lower()
                if suffix not in {".pdf", ".docx"}:
                    self._send_json(
                        {"error": "只支持 PDF 或 DOCX 文件。"},
                        status=400,
                    )
                    return
                try:
                    pdf_parse_mode, vision_provider_id = (
                        DocumentImportCoordinator.validate_parse_options(
                            self.headers.get("X-PDF-Parse-Mode", "auto"),
                            self.headers.get("X-Vision-Provider-ID", ""),
                        )
                    )
                    length = int(self.headers.get("Content-Length", "0"))
                    result = document_imports.import_stream(
                        filename,
                        length,
                        self.rfile,
                        pdf_parse_mode=pdf_parse_mode,
                        vision_provider_id=vision_provider_id,
                    )
                    self._send_json(result)
                except (MinerUError, VisionAPIError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except OSError:
                    logging.exception("legacy import request failed")
                    self._send_json(
                        {"error": "导入失败，请查看 desktop.log。"},
                        status=500,
                    )
                except Exception:
                    logging.exception("legacy import request failed")
                    self._send_json({"error": "导入失败，请查看 desktop.log。"}, status=500)
                return
            if parsed.path == "/api/import-upload/chunk":
                try:
                    upload_id = str(self.headers.get("X-Upload-ID", ""))
                    offset = int(self.headers.get("X-Upload-Offset", "-1"))
                    length = int(self.headers.get("Content-Length", "0"))
                    progress = document_imports.append_chunk(
                        upload_id,
                        offset,
                        length,
                        self.rfile,
                    )
                    self._send_json(progress)
                except ChunkedUploadError as exc:
                    self._send_json({"error": str(exc)}, status=exc.status)
                except (TypeError, ValueError):
                    self._send_json({"error": "上传分块请求无效。"}, status=400)
                except Exception:
                    logging.exception("chunked import request failed")
                    self._send_json(
                        {"error": "上传分块失败，请查看 desktop.log。"},
                        status=500,
                    )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                self._send_json({"error": "Content-Length 无效。"}, status=400)
                return
            if length < 0:
                self._send_json({"error": "Content-Length 无效。"}, status=400)
                return
            if length > MAX_JSON_REQUEST_BYTES:
                self._send_json({"error": "JSON 请求内容过大。"}, status=413)
                return
            if parsed.path == "/api/document/citation" and length > 16 * 1024:
                self._send_json({"error": "引文请求内容过大。"}, status=413)
                return
            if parsed.path == "/api/bibliographic-metadata/parse-cnki-citation" and length > 32 * 1024:
                self.rfile.read(length)
                self._send_json({"error": "知网引用文字过大，请只粘贴一条引文。"}, status=413)
                return
            if (
                parsed.path
                in {
                    "/api/import-upload/start",
                    "/api/import-upload/finish",
                    "/api/import-upload/cancel",
                }
                and length > 64 * 1024
            ):
                self._send_json({"error": "上传控制请求过大。"}, status=413)
                return
            if (
                parsed.path
                in {
                    "/api/bibliographic-metadata/lookup-cnki",
                    "/api/bibliographic-metadata/cnki-candidate",
                    "/api/bibliographic-metadata/open-cnki",
                }
                and length > 32 * 1024
            ):
                self._send_json({"error": "知网题录请求内容过大。"}, status=413)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json({"error": "请求格式无效。"}, status=400)
                return
            if parsed.path == "/api/import-upload/start":
                if not isinstance(payload, dict):
                    self._send_json({"error": "上传开始请求必须是 JSON 对象。"}, status=400)
                    return
                filename = str(payload.get("file_name") or payload.get("filename") or "")
                try:
                    total_size = int(payload.get("size") or 0)
                    result = document_imports.start_chunked(
                        filename,
                        total_size,
                        pdf_parse_mode=payload.get("parse_mode", "auto"),
                        vision_provider_id=payload.get("provider_id", ""),
                        import_kind=payload.get("import_kind", "document"),
                    )
                    self._send_json(result)
                except ChunkedUploadError as exc:
                    self._send_json({"error": str(exc)}, status=exc.status)
                except (MinerUError, ValueError, OSError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception:
                    logging.exception("chunked import session start failed")
                    self._send_json(
                        {"error": "无法开始上传，请查看 desktop.log。"},
                        status=500,
                    )
                return
            if parsed.path == "/api/import-upload/cancel":
                if not isinstance(payload, dict):
                    self._send_json({"error": "上传取消请求必须是 JSON 对象。"}, status=400)
                    return
                try:
                    result = document_imports.cancel_chunked(
                        str(payload.get("upload_id") or "")
                    )
                    self._send_json(result)
                except ChunkedUploadError as exc:
                    self._send_json({"error": str(exc)}, status=exc.status)
                return
            if parsed.path == "/api/import-upload/finish":
                if not isinstance(payload, dict):
                    self._send_json({"error": "上传完成请求必须是 JSON 对象。"}, status=400)
                    return
                upload_id = str(payload.get("upload_id") or "")
                try:
                    result = document_imports.finish_chunked(upload_id)
                    self._send_json(result)
                except ChunkedUploadError as exc:
                    self._send_json({"error": str(exc)}, status=exc.status)
                except (MinerUError, VisionAPIError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except OSError:
                    logging.exception("chunked import finalization failed")
                    self._send_json(
                        {"error": "导入失败，请查看 desktop.log。"},
                        status=500,
                    )
                except Exception:
                    logging.exception("chunked import finalization failed")
                    self._send_json(
                        {"error": "导入失败，请查看 desktop.log。"},
                        status=500,
                    )
                return
            route_method = self._POST_ROUTE_TABLE.get(parsed.path)
            if route_method is not None:
                getattr(self, route_method)(payload)
                return
            controller_route = controller_post_routes.get(parsed.path)
            if controller_route is not None:
                status, response = controller_route(payload)
                self._send_json(response, status=status)
                return
            shell_route = shell_post_routes.get(parsed.path)
            if shell_route is not None:
                status, response = shell_route(payload)
                self._send_json(response, status=status)
                return
            if parsed.path == "/api/import-local":
                if not isinstance(payload, dict):
                    self._send_json(
                        {"error": "本地导入请求必须是 JSON 对象。"},
                        status=400,
                    )
                    return
                raw_paths = payload.get("paths")
                if not isinstance(raw_paths, list) or not raw_paths:
                    self._send_json({"error": "没有选择要导入的文件。"}, status=400)
                    return
                if len(raw_paths) > 50:
                    self._send_json(
                        {"error": "一次最多批量导入 50 个文件，请分批选择。"},
                        status=400,
                    )
                    return
                try:
                    pdf_parse_mode, vision_provider_id = (
                        DocumentImportCoordinator.validate_parse_options(
                            payload.get("pdf_parse_mode", "auto"),
                            payload.get("vision_provider_id", ""),
                        )
                    )
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                try:
                    preferences = read_preferences(
                        resolve_preferences_path(root)
                    )
                    allowed_bases = [
                        Path(item).resolve()
                        for item in preferences.get("scan_directories") or []
                    ]
                    result = document_imports.import_local(
                        raw_paths,
                        allowed_bases,
                        pdf_parse_mode=pdf_parse_mode,
                        vision_provider_id=vision_provider_id,
                    )
                except OSError:
                    logging.exception("local import request failed")
                    self._send_json(
                        {"error": "导入失败，请查看 desktop.log。"},
                        status=500,
                    )
                    return
                self._send_json(result)
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def _send_source(self, request_path: str, send_body: bool = True) -> None:
            source_id = unquote(request_path[len("/source/") :])
            record = index_runtime.source(source_id)
            if not record:
                self._send(404, b"Unknown source", "text/plain; charset=utf-8")
                return
            relative_path = str(record.get("relative_path") or "")
            target = (root / relative_path).resolve()
            if target != root and root not in target.parents:
                self._send(403, b"Forbidden", "text/plain; charset=utf-8")
                return
            if target.suffix.lower() not in {".pdf", ".doc", ".docx"} or not target.exists():
                self._send(404, b"Source not found", "text/plain; charset=utf-8")
                return
            content_type = {
                ".pdf": "application/pdf",
                ".doc": "application/msword",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }.get(target.suffix.lower(), "application/octet-stream")
            file_size = target.stat().st_size
            try:
                requested_range = parse_byte_range(
                    self.headers.get("Range"),
                    file_size,
                )
            except InvalidByteRange:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if requested_range is None:
                status = 200
                start = 0
                content_length = file_size
            else:
                status = 206
                start = requested_range.start
                content_length = requested_range.length

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(content_length))
            if requested_range is not None:
                self.send_header(
                    "Content-Range",
                    f"bytes {requested_range.start}-{requested_range.end}/{file_size}",
                )
            self.end_headers()
            if not send_body or content_length == 0:
                return

            try:
                with target.open("rb") as stream:
                    stream.seek(start)
                    remaining = content_length
                    while remaining:
                        chunk = stream.read(min(SOURCE_STREAM_CHUNK_BYTES, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # Closing a PDF tab while a range is streaming is normal and
                # should not produce a server traceback.
                return

        def log_message(self, format: str, *args) -> None:
            return

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
        index_runtime.close()
        return True

    Handler.begin_shutdown = staticmethod(begin_shutdown)
    Handler.close_runtime = staticmethod(close_runtime)
    Handler.wait_for_durable_operations = staticmethod(durable_operations.wait)
    Handler._submit_background_task = staticmethod(import_task_queue.submit)
    Handler.import_orchestrator = import_orchestrator
    Handler.document_imports = document_imports
    Handler.import_job_controller = import_job_controller
    Handler.structured_reader_controller = structured_reader_controller
    Handler.archive_transfer_controller = archive_transfer_controller
    Handler.data_root_admission = data_root_admission
    Handler.index_runtime = index_runtime
    Handler.document_queries = document_queries
    Handler.backup_coordinator = backup_coordinator
    Handler.deletion_coordinator = deletion_coordinator
    Handler.metadata_coordinator = metadata_coordinator
    Handler.page_mapping_coordinator = page_mapping_coordinator
    Handler.bibliographic_metadata_controller = bibliographic_metadata_controller
    Handler.page_mapping_controller = page_mapping_controller
    Handler.document_lifecycle_controller = document_lifecycle_controller
    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, index_path: Path = DEFAULT_DATABASE_PATH) -> None:
    if str(host).casefold() not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("本地 Web 服务只能绑定 loopback 地址。")
    handler = make_handler(index_path)
    server = ManagedThreadingHTTPServer((host, port), handler)
    print(f"ME Finder running at http://{host}:{port}/")
    try:
        server.serve_forever()
    finally:
        handler.begin_shutdown()
        server.server_close()
        handlers_stopped = server.wait_for_handlers(timeout=5.0)
        handler.wait_for_durable_operations()
        if not handlers_stopped:
            handlers_stopped = server.wait_for_handlers(timeout=2.0)
        if handlers_stopped:
            handler.close_runtime()
        else:
            logging.warning(
                "active HTTP handlers did not finish; runtime kept open"
            )

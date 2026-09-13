"""Local web interface — iOS-style SPA shell."""

from __future__ import annotations

import logging
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from .app_context import AppContext
from .web_runtime import build_application_runtime
from .application.data_root_admission import DataRootAdmissionError
from .application.document_import_coordinator import DocumentImportCoordinator
from .document_groups import (
    DocumentGroupNotFound,
    resolve_document_group_source_ids,
)
from .database import DEFAULT_DATABASE_PATH
from .mineru_api import MinerUError
from .vision_api import VisionAPIError
from .preferences import (
    read_preferences,
    resolve_preferences_path,
)
from .native_document_open import (  # noqa: F401 - re-exported for compatibility
    NativeBackupFileChooser,
    NativeDirectoryChooser,
    NativeExportDirectoryChooser,
    NativePDFOpener,
    NativeScanDirectoryChooser,
    NativeThemeSetter,
    find_adobe_pdf_app,  # noqa: F401
    find_default_adobe_pdf_app,  # noqa: F401
    open_external_cnki_url,
    open_mineru_token_page,
    open_path_in_macos_preview,  # noqa: F401
    open_path_with_default_app,
    open_pdf_in_adobe,  # noqa: F401
    open_pdf_with_platform,
)
from .import_config_store import load_import_config
from .chunked_upload import ChunkedUploadError
from .web_assets import (
    HTML,  # noqa: F401 - re-exported for tests and desktop bootstrap
    _PACKAGE_DIR,
    render_html,
)
from .web_http import (
    MAX_JSON_REQUEST_BYTES,  # noqa: F401 - re-exported for request-limit tests
    WebHTTPContext,
    make_http_handler,
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
    runtime = build_application_runtime(
        context,
        native_pdf_opener=native_pdf_opener,
        native_theme_setter=native_theme_setter,
        update_service=update_service,
        native_directory_chooser=native_directory_chooser,
        native_export_directory_chooser=native_export_directory_chooser,
        native_scan_directory_chooser=native_scan_directory_chooser,
        native_backup_file_chooser=native_backup_file_chooser,
        app_data_root=app_data_root,
        default_app_data_root=default_app_data_root,
        open_pdf_with_platform=open_pdf_with_platform,
        open_path_with_default_app=open_path_with_default_app,
        open_external_cnki_url=open_external_cnki_url,
        open_mineru_token_page=open_mineru_token_page,
    )
    Handler = make_http_handler(
        WebHTTPContext(
            index_path=runtime.index_path,
            root=runtime.root,
            index_runtime=runtime.index_runtime,
            data_root_admission=runtime.data_root_admission,
            document_imports=runtime.document_imports,
            controller_get_routes=runtime.controller_get_routes,
            controller_post_routes=runtime.controller_post_routes,
            shell_get_routes=runtime.shell_get_routes,
            shell_post_routes=runtime.shell_post_routes,
            render_html=render_html,
            package_dir=_PACKAGE_DIR,
            read_preferences=read_preferences,
            resolve_preferences_path=resolve_preferences_path,
            load_import_config=load_import_config,
            resolve_document_group_source_ids=(
                resolve_document_group_source_ids
            ),
            validate_parse_options=(
                DocumentImportCoordinator.validate_parse_options
            ),
            data_root_admission_error=DataRootAdmissionError,
            document_group_not_found_error=DocumentGroupNotFound,
            chunked_upload_error=ChunkedUploadError,
            mineru_error=MinerUError,
            vision_api_error=VisionAPIError,
        )
    )
    Handler.begin_shutdown = staticmethod(runtime.begin_shutdown)
    Handler.close_runtime = staticmethod(runtime.close_runtime)
    Handler.wait_for_durable_operations = staticmethod(
        runtime.wait_for_durable_operations
    )
    Handler._submit_background_task = staticmethod(runtime.submit_background_task)
    Handler.import_orchestrator = runtime.import_orchestrator
    Handler.document_imports = runtime.document_imports
    Handler.import_job_controller = runtime.import_job_controller
    Handler.structured_reader_controller = runtime.structured_reader_controller
    Handler.archive_transfer_controller = runtime.archive_transfer_controller
    Handler.data_root_admission = runtime.data_root_admission
    Handler.index_runtime = runtime.index_runtime
    Handler.document_queries = runtime.document_queries
    Handler.backup_coordinator = runtime.backup_coordinator
    Handler.deletion_coordinator = runtime.deletion_coordinator
    Handler.metadata_coordinator = runtime.metadata_coordinator
    Handler.page_mapping_coordinator = runtime.page_mapping_coordinator
    Handler.bibliographic_metadata_controller = (
        runtime.bibliographic_metadata_controller
    )
    Handler.page_mapping_controller = runtime.page_mapping_controller
    Handler.component_catalog = runtime.component_catalog
    Handler.managed_mineru = runtime.managed_mineru
    Handler.document_lifecycle_controller = runtime.document_lifecycle_controller
    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, index_path: Path = DEFAULT_DATABASE_PATH) -> None:
    if str(host).casefold() not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("本地 Web 服务只能绑定 loopback 地址。")
    handler = make_handler(index_path)
    server = ManagedThreadingHTTPServer((host, port), handler)
    print(f"MEFinder running at http://{host}:{port}/")
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

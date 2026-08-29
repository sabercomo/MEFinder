"""Local web interface — iOS-style SPA shell."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import CancelledError
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Union
from urllib.parse import urlparse

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
from .pdf_import_service import load_import_config
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
    if any(marker in normalized for marker in ("acroexch", "acrobat", "acrord")):
        return find_adobe_pdf_app()

    try:
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT,
            rf"{prog_id}\shell\open\command",
        ) as key:
            command = str(winreg.QueryValueEx(key, "")[0])
    except OSError:
        return None

    match = re.search(r'"([^"]+\.exe)"', command, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"([A-Za-z]:\\[^\s]+\.exe)", command, flags=re.IGNORECASE)
    if not match:
        return None

    executable = Path(match.group(1))
    if executable.name.casefold() not in {"acrobat.exe", "acrord32.exe"}:
        return None
    return executable


def open_path_with_default_app(target: Path) -> None:
    """Open a local file with the platform's default application."""

    target = Path(target)
    if sys.platform == "win32":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return
    command = ["open", str(target)] if sys.platform == "darwin" else ["xdg-open", str(target)]
    subprocess.Popen(command, close_fds=True)


MINERU_TOKEN_URL = "https://mineru.net/apiManage/token"


def open_mineru_token_page() -> None:
    """Open the MinerU API-token page in the system browser (fixed URL)."""

    if sys.platform == "win32":
        os.startfile(MINERU_TOKEN_URL)  # type: ignore[attr-defined]
        return
    command = (
        ["open", MINERU_TOKEN_URL]
        if sys.platform == "darwin"
        else ["xdg-open", MINERU_TOKEN_URL]
    )
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

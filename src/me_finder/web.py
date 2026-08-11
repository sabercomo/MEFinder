"""Local web interface — iOS-style SPA shell."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import CancelledError
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .app_context import AppContext
from .application import SearchRequest, SearchService
from .database import DEFAULT_DATABASE_PATH, replace_source_in_database
from .data_location import (
    DataLocationError,
    data_location_summary,
    migrate_data_root,
    proposed_data_root,
)
from .auto_page_mapping import has_manual_mapping
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
from .crossref_lookup import CrossrefLookupError, lookup_crossref
from .foreign_book_lookup import BookLookupError
from .journal_metadata_lookup import (
    CNKILookupError,
    fetch_cnki_candidate,
    lookup_cnki_journal,
)
from .mineru_api import (
    MinerUError,
    mineru_config_summary,
    resolve_mineru_config_path,
    save_mineru_config,
    test_mineru_connection,
)
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
from .calibration_library import (
    build_calibration_library,
    build_library,
    build_library_detail,
    summarize_library,
)
from .document_deletion import DocumentDeletionService
from .pdf_extractors import extract_pdf_source
from .pdf_page_mapping import normalize_manual_mapping_segments
from .pdf_import_service import (
    copy_local_document,
    cleanup_stale_document_storage_files,
    detect_imported_pdf,
    document_storage_error,
    document_storage_target,
    import_config_lock,
    locked_import_config,
    parse_pdf_with_mineru,
    parse_pdf_with_provider,
    rebuild_local_index,
    reuse_registered_pdf_copy,
    release_document_storage_target,
    load_import_config,
    register_pdf,
    save_import_config,
    scan_directories_for_documents,
)
from .runtime_page_mapping import apply_mapping_to_database, normalize_auto_segments
from .backup_service import restore_backup, write_backup
from .import_job_journal import DEFAULT_IMPORT_JOB_DIR, ImportJobJournal
from .import_queue import ImportTaskQueue
from .import_resume import ResumeManifestError, sha256_file
from .http_range import InvalidByteRange, parse_byte_range
from .lifecycle import DurableOperationGate
from .search import SearchEngine
from .chunked_upload import (
    ChunkedUploadError,
    ChunkedUploadStore,
)
from .structured_reader import (
    CitationPositionNotFound,
    InvalidCitationRange,
    InvalidPagination,
    InvalidSourceId,
    SourceNotFound,
    StructuredReaderError,
    UnsupportedSourceType,
    get_document_citation,
    get_document_window,
)


MAX_JSON_REQUEST_BYTES = 1024 * 1024
SOURCE_STREAM_CHUNK_BYTES = 1024 * 1024


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
NativeScanDirectoryChooser = Callable[[], Optional[Union[str, Sequence[str]]]]


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
    native_scan_directory_chooser: NativeScanDirectoryChooser | None = None,
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
    app_data_root = context.paths.app_data_root
    default_app_data_root = context.paths.default_app_data_root
    engine = SearchEngine(index_path)
    runtime = {
        "engine": engine,
        "source_files": {
            str(item.get("source_file_id")): item
            for item in engine.index.get("source_files", [])
            if item.get("source_file_id")
        },
        "index_metadata": engine.index.get("metadata", {}),
        "rebuilding": False,
        "closing": False,
    }
    runtime_lock = threading.RLock()
    # Re-entrant because a config mutation may hold the lock while calling the
    # shared rebuild helper, which acquires the same lock.
    rebuild_lock = threading.RLock()
    metadata_lock = threading.Lock()
    cnki_lookup_lock = threading.Lock()
    import_jobs: Dict[str, Dict[str, object]] = {}
    import_job_contexts: Dict[str, Dict[str, object]] = {}
    import_jobs_lock = threading.RLock()
    import_task_queue = ImportTaskQueue(worker_count=2)
    import_job_journal = ImportJobJournal(root / DEFAULT_IMPORT_JOB_DIR)
    chunked_uploads = ChunkedUploadStore(root / "corpus" / ".upload-staging")
    deleting_import_sources: set[str] = set()
    pending_import_sources: set[str] = set()
    calibration_active_sources: set[str] = set()
    search_service = SearchService()
    durable_operations = DurableOperationGate()

    def infer_import_failure_stage(
        job: Dict[str, object],
        *,
        is_pdf: bool,
    ) -> Optional[str]:
        explicit = str(job.get("failure_stage") or "").strip()
        if explicit:
            return explicit
        if not is_pdf:
            return None
        phase = str(job.get("phase") or "").strip()
        message = str(job.get("message") or "")
        if phase == "index_failed" or any(
            marker in message
            for marker in (
                "文件已解析，但批量重建索引失败",
                "UNIQUE constraint failed: source_files.source_file_id",
            )
        ):
            # v0.2.2 did not persist failure_stage. Recognizing its exact
            # failure text prevents an upgrade from calling MinerU again.
            return "index"
        return None

    for saved_job in import_job_journal.load_startup_jobs():
        saved_context = saved_job.get("context")
        if not isinstance(saved_context, dict):
            continue
        saved_job_id = str(saved_job.get("job_id") or "")
        target_text = str(saved_context.get("target") or "")
        if not saved_job_id or not target_text:
            continue
        restored_job = {
            key: value
            for key, value in saved_job.items()
            if key not in {"context", "file_hash", "job_log_spec_version"}
        }
        restored_failure_stage = infer_import_failure_stage(
            restored_job,
            is_pdf=bool(saved_context.get("is_pdf")),
        )
        if restored_failure_stage:
            restored_job["failure_stage"] = restored_failure_stage
            restored_job["can_resume"] = True
            if str(restored_job.get("status") or "") == "failed":
                restored_job["phase"] = "index_failed"
            try:
                import_job_journal.update_job(
                    saved_job_id,
                    failure_stage=restored_failure_stage,
                    phase=restored_job.get("phase"),
                    can_resume=True,
                )
            except (KeyError, OSError, ValueError, ResumeManifestError):
                logging.warning(
                    "failed to upgrade legacy index-retry journal %s",
                    saved_job_id,
                )
        import_jobs[saved_job_id] = restored_job
        import_job_contexts[saved_job_id] = {
            "target": Path(target_text),
            "source_file_id": str(saved_context.get("source_file_id") or ""),
            "profile": dict(saved_context.get("profile") or {}),
            "is_pdf": bool(saved_context.get("is_pdf")),
            "force_mineru": bool(saved_context.get("force_mineru")),
            "vision_provider_id": saved_context.get("provider_id"),
            "file_hash": str(saved_job.get("file_hash") or ""),
        }

    def update_import_job(job_id: str, **updates: object) -> None:
        persisted_updates = dict(updates)
        progress = updates.get("progress")
        if isinstance(progress, dict):
            resume = progress.get("resume")
            for field in (
                "total_pages",
                "completed_pages",
                "failed_pages",
            ):
                if field in progress:
                    persisted_updates[field] = progress[field]
                elif isinstance(resume, dict) and field in resume:
                    persisted_updates[field] = resume[field]
        status = str(updates.get("status") or "")
        if "can_resume" not in updates:
            if status == "completed":
                persisted_updates["can_resume"] = False
            elif status == "failed":
                persisted_updates["can_resume"] = True
            elif status == "processing":
                persisted_updates["can_resume"] = False
        with import_jobs_lock:
            job = import_jobs.get(job_id)
            if job is not None:
                job.update(persisted_updates)
        try:
            import_job_journal.update_job(job_id, **persisted_updates)
            if status == "completed":
                import_job_journal.delete_job(job_id)
        except (KeyError, OSError, ValueError, ResumeManifestError):
            # Non-import background jobs do not have a durable journal entry.
            pass

    def progress_import_job(job_id: str, update: Dict[str, object]) -> None:
        phase = str(update.get("phase") or "")
        message = "正在处理…"
        if phase == "mineru_processing":
            message = f"MinerU 解析中：{update.get('completed', 0)}/{update.get('total', 0)} 个分段"
        elif phase == "vision_processing":
            provider_name = str(update.get("provider_name") or "其他视觉 API")
            message = (
                f"{provider_name} 解析中："
                f"{update.get('completed', 0)}/{update.get('total', 0)} 页"
            )
        elif phase == "rebuilding_index":
            message = "正在重建本地 SQLite 索引…"
        update_import_job(job_id, phase=phase, message=message, progress=update)

    def switch_import_job_route(
        job_id: str,
        *,
        parse_route: str,
        force_mineru: bool,
        vision_provider_id: Optional[str],
        provider_name: Optional[str],
    ) -> None:
        """Atomically persist the paid-parser route, then mirror it in memory."""

        import_job_journal.switch_parser_route(
            job_id,
            parse_route=parse_route,
            force_mineru=bool(force_mineru),
            provider_id=vision_provider_id,
            provider_name=provider_name,
        )
        with import_jobs_lock:
            context = import_job_contexts.get(job_id)
            job = import_jobs.get(job_id)
            if context is None or job is None:
                raise MinerUError("导入任务的恢复信息不存在。")
            context["force_mineru"] = bool(force_mineru)
            context["vision_provider_id"] = vision_provider_id
            job["parse_route"] = parse_route
            job["provider_id"] = vision_provider_id
            job["provider_name"] = provider_name

    def validated_import_target(
        job_id: str,
        context: Dict[str, object],
    ) -> Path:
        """Reject a missing or replaced source before any paid parser is queued."""

        target = Path(context["target"])
        record = import_job_journal.get_job(job_id)
        expected_hash = str(
            context.get("file_hash")
            or (record or {}).get("file_hash")
            or ""
        )
        if not target.is_file():
            message = "待恢复的原始文件已不存在。"
        elif not expected_hash:
            message = "待恢复任务缺少文件校验信息，不能安全继续。"
        elif sha256_file(target) != expected_hash:
            message = "原始文件内容已经变化，旧断点不会继续使用。"
        else:
            context["file_hash"] = expected_hash
            return target
        update_import_job(
            job_id,
            status="failed",
            phase="failed",
            can_resume=False,
            message=message,
        )
        raise MinerUError(message)

    def reload_runtime_index() -> bool:
        new_engine = SearchEngine(index_path)
        with runtime_lock:
            if runtime["closing"]:
                new_engine.close()
                runtime["rebuilding"] = False
                return False
            old_engine = runtime["engine"]
            runtime["engine"] = new_engine
            runtime["source_files"] = {
                str(item.get("source_file_id")): item
                for item in new_engine.index.get("source_files", [])
                if item.get("source_file_id")
            }
            runtime["index_metadata"] = new_engine.index.get("metadata", {})
            if hasattr(old_engine, "close"):
                old_engine.close()
        return True

    def recover_runtime_index() -> bool:
        """Reopen the current DB unless shutdown has made runtime terminal."""

        recovered_engine = SearchEngine(index_path)
        with runtime_lock:
            if runtime["closing"]:
                recovered_engine.close()
                runtime["rebuilding"] = False
                return False
            old_engine = runtime["engine"]
            runtime["engine"] = recovered_engine
            runtime["source_files"] = {
                str(item.get("source_file_id")): item
                for item in recovered_engine.index.get("source_files", [])
                if item.get("source_file_id")
            }
            runtime["index_metadata"] = recovered_engine.index.get("metadata", {})
            runtime["rebuilding"] = False
            if hasattr(old_engine, "close"):
                old_engine.close()
        return True

    def latest_pdf_import_runs() -> Dict[str, Dict[str, object]]:
        connection = sqlite3.connect(str(index_path))
        try:
            rows = connection.execute("SELECT source_file_id, payload_json FROM pdf_import_runs ORDER BY row_id").fetchall()
        finally:
            connection.close()
        result: Dict[str, Dict[str, object]] = {}
        for source_id, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            result[str(source_id)] = payload
        return result

    def library_context() -> Tuple[List, List, List, List, set]:
        config_path = root / "config" / "pdf_imports.json"
        config = load_import_config(config_path)
        with runtime_lock:
            current_engine = runtime["engine"]
            sources = list(current_engine.index.get("source_files", []))
            volumes = list(current_engine.index.get("volumes", []))
            works = list(current_engine.index.get("works", []))
            active = set(calibration_active_sources)
        with import_jobs_lock:
            for job in import_jobs.values():
                if job.get("status") == "processing" and job.get("source_file_id"):
                    active.add(str(job["source_file_id"]))
        return sources, volumes, works, config.get("documents", []), active

    def calibration_library_data() -> Dict[str, object]:
        sources, volumes, _works, documents, active = library_context()
        return build_calibration_library(
            root,
            sources,
            volumes,
            documents,
            latest_runs=latest_pdf_import_runs(),
            active_source_ids=active,
        )

    def library_data() -> Dict[str, object]:
        sources, volumes, works, documents, active = library_context()
        return build_library(
            root,
            sources,
            volumes,
            works,
            documents,
            latest_runs=latest_pdf_import_runs(),
            active_source_ids=active,
        )

    def rebuild_runtime_index(
        job_id: str,
        expected_source_ids: Optional[List[str]] = None,
    ) -> set[str]:
        with rebuild_lock:
            update_import_job(job_id, phase="rebuilding_index", message="正在重建本地 SQLite 索引…")
            with runtime_lock:
                runtime["rebuilding"] = True
                old_engine = runtime["engine"]
                if hasattr(old_engine, "close"):
                    old_engine.close()
            try:
                rebuild_local_index(
                    root,
                    lambda update: progress_import_job(job_id, update),
                    database_path=index_path,
                )
                new_engine = SearchEngine(index_path)
                new_source_files = {
                    str(item.get("source_file_id")): item
                    for item in new_engine.index.get("source_files", [])
                    if item.get("source_file_id")
                }
                indexed_source_ids = set(new_source_files)
                with runtime_lock:
                    if runtime["closing"]:
                        new_engine.close()
                        runtime["rebuilding"] = False
                    else:
                        runtime["engine"] = new_engine
                        runtime["source_files"] = new_source_files
                        runtime["index_metadata"] = new_engine.index.get("metadata", {})
                        runtime["rebuilding"] = False
            except Exception:
                with runtime_lock:
                    closing = bool(runtime["closing"])
                    if closing:
                        runtime["rebuilding"] = False
                if closing:
                    raise
                recovered_engine = SearchEngine(index_path)
                with runtime_lock:
                    if runtime["closing"]:
                        recovered_engine.close()
                        runtime["rebuilding"] = False
                        raise RuntimeError(
                            "应用正在关闭，不再恢复运行时索引。"
                        )
                    runtime["engine"] = recovered_engine
                    runtime["source_files"] = {
                        str(item.get("source_file_id")): item
                        for item in runtime["engine"].index.get("source_files", [])
                        if item.get("source_file_id")
                    }
                    runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                    runtime["rebuilding"] = False
                raise
        expected = {
            str(source_id)
            for source_id in (expected_source_ids or [])
            if str(source_id)
        }
        return expected.difference(indexed_source_ids)

    def configured_pdf_for_index(
        source_file_id: str,
    ) -> Tuple[Path, Dict[str, object]]:
        config_path = root / "config" / "pdf_imports.json"
        config = load_import_config(config_path)
        document = next(
            (
                item
                for item in config.get("documents", [])
                if isinstance(item, dict)
                and str(item.get("source_file_id") or "") == source_file_id
            ),
            None,
        )
        if document is None:
            raise MinerUError(f"PDF 配置中找不到该文献：{source_file_id}")
        file_name = str(document.get("file_name") or "").strip()
        if not file_name:
            raise MinerUError("PDF 配置缺少文件名。")
        path = Path(file_name)
        if not path.is_absolute():
            path = root / "corpus" / "raw_pdf" / path
        if not path.is_file():
            raise MinerUError(f"PDF 原文件不存在：{path.name}")
        return path, document

    def restore_runtime_after_index_write() -> bool:
        new_engine = None
        for attempt in range(5):
            try:
                new_engine = SearchEngine(index_path)
                break
            except Exception:
                if new_engine is not None and hasattr(new_engine, "close"):
                    new_engine.close()
                new_engine = None
                if attempt == 4:
                    # Keep the stale catalog object available to non-search
                    # views, but never expose its closed SQLite connection as
                    # a usable search engine.
                    with runtime_lock:
                        runtime["rebuilding"] = True
                    raise
                time.sleep(0.05 * (2**attempt))
        assert new_engine is not None
        source_files = {
            str(item.get("source_file_id")): item
            for item in new_engine.index.get("source_files", [])
            if item.get("source_file_id")
        }
        index_metadata = new_engine.index.get("metadata", {})
        with runtime_lock:
            if runtime["closing"]:
                new_engine.close()
                runtime["source_files"] = source_files
                runtime["index_metadata"] = index_metadata
                runtime["rebuilding"] = False
                return False
            runtime["engine"] = new_engine
            runtime["source_files"] = source_files
            runtime["index_metadata"] = index_metadata
            runtime["rebuilding"] = False
        return True

    def index_registered_pdf(
        job_id: str,
        source_file_id: str,
        *,
        backup_existing: bool = False,
    ) -> None:
        """Extract and transactionally replace one PDF without a full rebuild."""

        with rebuild_lock:
            update_import_job(
                job_id,
                phase="text_parsing",
                message="正在读取 PDF 文本并写入本地索引…",
            )
            path, document = configured_pdf_for_index(source_file_id)
            display_name = str(
                document.get("original_file_name") or path.name
            )
            try:
                extracted = extract_pdf_source(
                    path,
                    root,
                    document,
                    parsed_dir=root / "corpus" / "parsed" / "pdf",
                )
            except Exception as exc:
                raise MinerUError(
                    f"{display_name} 未能进入索引：{type(exc).__name__}: {exc}"
                ) from exc
            extracted_sources = [
                item
                for item in extracted.get("source_files", [])
                if isinstance(item, dict)
            ]
            if (
                len(extracted_sources) != 1
                or str(extracted_sources[0].get("source_file_id") or "")
                != source_file_id
            ):
                raise MinerUError(
                    f"{display_name} 未能进入索引：解析结果缺少对应的文献记录。"
                )

            update_import_job(
                job_id,
                phase="rebuilding_index",
                message="正在写入本地 SQLite 索引…",
            )
            with durable_operations.operation():
                with runtime_lock:
                    runtime["rebuilding"] = True
                    old_engine = runtime["engine"]
                try:
                    if hasattr(old_engine, "close"):
                        old_engine.close()
                    replace_source_in_database(
                        extracted,
                        index_path,
                        backup_existing=backup_existing,
                    )
                except Exception as write_error:
                    try:
                        restore_runtime_after_index_write()
                    except Exception:
                        logging.exception(
                            "index write failed and the previous runtime index "
                            "could not be reopened"
                        )
                    raise write_error.with_traceback(write_error.__traceback__)
                else:
                    restore_runtime_after_index_write()
                with runtime_lock:
                    indexed = source_file_id in runtime["source_files"]
                if not indexed:
                    raise MinerUError(
                        f"{display_name} 未能进入索引：写入后未找到文献记录。"
                    )

    def fail_import_at_index(
        job_id: str,
        exc: Exception,
        *,
        parsed: bool = False,
    ) -> None:
        prefix = "文件已解析，但" if parsed else ""
        detail = str(exc).strip() or type(exc).__name__
        update_import_job(
            job_id,
            status="failed",
            phase="index_failed",
            failure_stage="index",
            can_resume=True,
            vision_failed=False,
            mineru_failed=False,
            mineru_interrupted=False,
            can_retry_with_provider=False,
            retry_provider_id=None,
            retry_provider_name=None,
            needs_provider_config=False,
            message=(
                f"{prefix}索引更新失败：{detail}。"
                "可点击“重新建立索引”重试，不会重新上传或调用解析 API。"
            ),
        )

    def finalize_import_job(
        job_id: str,
        source_file_id: str,
        is_pdf: bool,
    ) -> None:
        metadata_note = ""
        if is_pdf:
            update_import_job(
                job_id,
                phase="metadata_recognition",
                message="索引已建立，正在自动识别书目信息…",
            )
            try:
                metadata = detect_bibliographic_metadata(source_file_id)
                metadata = persist_bibliographic_metadata(source_file_id, metadata)
                missing = list(
                    metadata.get("metadata_missing_fields")
                    or metadata_missing_fields(metadata)
                )
                labels = {
                    "author": "作者",
                    "title": "书名",
                    "translator": "译者",
                    "publisher": "出版社",
                    "publish_place": "出版地",
                    "publish_year": "出版年份",
                    "journal_name": "出版刊物",
                    "volume": "卷次",
                    "issue": "期号",
                    "page_range": "页码",
                }
                if metadata.get("document_type") == "thesis":
                    labels.update({"title": "篇名", "publisher": "学校", "publish_year": "年份"})
                missing_labels = [labels[field] for field in missing if field in labels]
                metadata_note = "；书目信息已自动填入"
                if missing_labels:
                    metadata_note += "，缺少" + "、".join(missing_labels)
                update_import_job(
                    job_id,
                    bibliographic_metadata=metadata,
                    bibliographic_missing_fields=missing,
                )
            except Exception as metadata_exc:
                metadata_note = "；书目信息自动识别未完成，可在文献库中重试"
                update_import_job(job_id, bibliographic_error=str(metadata_exc))
        update_import_job(
            job_id,
            status="completed",
            phase="completed",
            message="导入完成，已自动更新索引" + metadata_note,
        )

    def prepare_import_job(
        job_id: str,
        target: Path,
        source_file_id: str,
        profile: Dict[str, object],
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
    ) -> bool:
        use_vision = bool(is_pdf and vision_provider_id)
        try:
            use_mineru = is_pdf and not use_vision and (
                force_mineru or str(profile.get("detected_pdf_type")) != "native_text"
            )
            if use_vision:
                provider_name = str(
                    import_jobs.get(job_id, {}).get("provider_name")
                    or "其他视觉 API"
                )
                update_import_job(
                    job_id,
                    phase="vision_processing",
                    message=f"正在使用 {provider_name} 逐页解析 PDF…",
                    parse_route="vision",
                )
                parse_pdf_with_provider(
                    root,
                    target,
                    source_file_id,
                    str(vision_provider_id),
                    on_progress=lambda update: progress_import_job(job_id, update),
                )
            elif use_mineru:
                message = (
                    "已选择 MinerU 在线解析，正在上传 PDF…"
                    if force_mineru
                    else "文本层不可靠，正在自动提交 MinerU…"
                )
                update_import_job(job_id, phase="mineru_submitting", message=message, parse_route="mineru")
                try:
                    parse_pdf_with_mineru(
                        root,
                        target,
                        source_file_id,
                        on_progress=lambda update: progress_import_job(job_id, update),
                    )
                except Exception as mineru_exc:
                    # A transient interruption (network timeout / non-fallback
                    # MinerUError) keeps a resumable checkpoint; a permanent failure
                    # does not. Either way an AUTOMATIC switch to a paid provider now
                    # happens iff the user enabled it — the transient/permanent split
                    # no longer blocks auto-switch, it only shapes the no-auto-switch
                    # message and whether the checkpoint is kept for a free resume.
                    transient = (
                        not isinstance(mineru_exc, MinerUError)
                        or not mineru_exc.allow_parser_fallback
                    )
                    config_path = resolve_vision_config_path(root)
                    try:
                        summary = vision_config_summary(config_path)
                    except VisionAPIError:
                        summary = {
                            "providers": [],
                            "default_provider_id": None,
                            "auto_fallback_from_mineru": False,
                        }
                    providers = [
                        item
                        for item in summary.get("providers", [])
                        if isinstance(item, dict)
                        and item.get("enabled")
                        and item.get("configured")
                    ]
                    fallback = providers[0] if providers else None
                    auto_fallback = bool(
                        summary.get("auto_fallback_from_mineru")
                        and fallback
                    )
                    if not auto_fallback:
                        if transient:
                            update_import_job(
                                job_id,
                                status="failed",
                                phase="failed",
                                can_resume=True,
                                message=(
                                    f"MinerU 任务暂时中断：{mineru_exc}。断点已保存，点「继续导入」"
                                    "复用断点、不额外计费；不会自动改用其他付费接口，"
                                    "如已配置可手动改用（放弃断点、从头解析）。"
                                ),
                                mineru_interrupted=True,
                                mineru_failed=False,
                                can_retry_with_provider=bool(fallback),
                                retry_provider_id=fallback.get("id") if fallback else None,
                                retry_provider_name=fallback.get("name") if fallback else None,
                                needs_provider_config=False,
                                original_error=str(mineru_exc),
                            )
                            return False
                        message = f"MinerU 解析失败：{mineru_exc}"
                        if fallback:
                            message += (
                                f"。可手动改用 {fallback.get('name') or '其他解析 API'}；"
                                "也可在设置中开启失败后自动切换。"
                            )
                        else:
                            message += "。可在设置中配置其他解析 API 后自行切换。"
                        update_import_job(
                            job_id,
                            status="failed",
                            phase="failed",
                            message=message,
                            mineru_failed=True,
                            can_retry_with_provider=bool(fallback),
                            retry_provider_id=fallback.get("id") if fallback else None,
                            retry_provider_name=fallback.get("name") if fallback else None,
                            needs_provider_config=not bool(fallback),
                            mineru_interrupted=False,
                            original_error=str(mineru_exc),
                        )
                        return False
                    fallback_id = str(fallback.get("id"))
                    fallback_name = str(fallback.get("name") or "其他视觉 API")
                    switch_reason = "任务暂时中断" if transient else "解析失败"
                    switch_import_job_route(
                        job_id,
                        parse_route="vision",
                        force_mineru=False,
                        vision_provider_id=fallback_id,
                        provider_name=fallback_name,
                    )
                    update_import_job(
                        job_id,
                        phase="vision_processing",
                        message=(
                            f"MinerU {switch_reason}，已按设置自动切换到 {fallback_name}…"
                        ),
                        parse_route="vision",
                        provider_id=fallback_id,
                        provider_name=fallback_name,
                        mineru_failed=True,
                        mineru_interrupted=False,
                        fallback_used=True,
                        original_error=str(mineru_exc),
                    )
                    try:
                        parse_pdf_with_provider(
                            root,
                            target,
                            source_file_id,
                            fallback_id,
                            on_progress=lambda update: progress_import_job(job_id, update),
                        )
                    except Exception as fallback_exc:
                        update_import_job(
                            job_id,
                            status="failed",
                            phase="failed",
                            message=(
                                f"MinerU {switch_reason}；自动切换到 "
                                f"{fallback_name} 后仍失败：{fallback_exc}"
                            ),
                            fallback_error=str(fallback_exc),
                            vision_failed=True,
                            can_retry_with_provider=True,
                            retry_provider_id=fallback_id,
                            retry_provider_name=fallback_name,
                            needs_provider_config=False,
                        )
                        return False
            else:
                update_import_job(
                    job_id,
                    phase="text_parsing",
                    message="原生文本，使用快速解析，正在建立索引…",
                    parse_route="native",
                )
            return True
        except Exception as exc:
            if use_vision:
                try:
                    summary = vision_config_summary(resolve_vision_config_path(root))
                except (OSError, ValueError, VisionAPIError):
                    summary = {"providers": []}
                providers = [
                    item
                    for item in summary.get("providers", [])
                    if isinstance(item, dict)
                    and item.get("enabled")
                    and item.get("configured")
                ]
                current_provider_id = str(vision_provider_id or "")
                retry_provider = next(
                    (
                        item
                        for item in providers
                        if str(item.get("id") or "") != current_provider_id
                    ),
                    providers[0] if providers else None,
                )
                update_import_job(
                    job_id,
                    status="failed",
                    phase="failed",
                    message=str(exc),
                    vision_failed=True,
                    mineru_failed=False,
                    mineru_interrupted=False,
                    can_retry_with_provider=bool(retry_provider),
                    retry_provider_id=(
                        retry_provider.get("id") if retry_provider else None
                    ),
                    retry_provider_name=(
                        retry_provider.get("name") if retry_provider else None
                    ),
                    needs_provider_config=not bool(retry_provider),
                    original_error=str(exc),
                )
                return False
            update_import_job(
                job_id,
                status="failed",
                phase="failed",
                message=str(exc),
                vision_failed=False,
                mineru_failed=False,
                mineru_interrupted=False,
                can_retry_with_provider=False,
                retry_provider_id=None,
                retry_provider_name=None,
                needs_provider_config=False,
            )
            return False

    def run_import_job(
        job_id: str,
        target: Path,
        source_file_id: str,
        profile: Dict[str, object],
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
    ) -> None:
        if not prepare_import_job(
            job_id,
            target,
            source_file_id,
            profile,
            is_pdf,
            force_mineru,
            vision_provider_id,
        ):
            return
        try:
            if is_pdf:
                index_registered_pdf(job_id, source_file_id)
            else:
                rebuild_runtime_index(job_id)
            finalize_import_job(job_id, source_file_id, is_pdf)
        except Exception as exc:
            fail_import_at_index(
                job_id,
                exc,
                parsed=bool(
                    is_pdf
                    and (
                        force_mineru
                        or vision_provider_id
                        or str(profile.get("detected_pdf_type"))
                        != "native_text"
                    )
                ),
            )

    def create_import_job(
        target: Path,
        profile: Dict[str, object],
        source_file_id: str,
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
        consume_reservation: bool = False,
        display_file_name: Optional[str] = None,
    ) -> str:
        job_id = f"import-{uuid.uuid4().hex[:12]}"
        parse_route = None
        provider_name = None
        if vision_provider_id:
            try:
                summary = vision_config_summary(resolve_vision_config_path(root))
                provider = next(
                    (
                        item
                        for item in summary.get("providers", [])
                        if isinstance(item, dict)
                        and str(item.get("id")) == str(vision_provider_id)
                    ),
                    None,
                )
                provider_name = provider.get("name") if provider else None
            except VisionAPIError:
                provider_name = None
        if is_pdf:
            parse_route = (
                "vision"
                if vision_provider_id
                else "mineru"
                if force_mineru or str(profile.get("detected_pdf_type")) != "native_text"
                else "native"
            )
        with import_jobs_lock:
            if source_file_id in deleting_import_sources:
                raise MinerUError("该文献正在删除，不能开始解析。")
            if (
                source_file_id in pending_import_sources
                and not consume_reservation
            ):
                raise MinerUError("同一文献正在准备导入。")
            already_running = next(
                (
                    other
                    for other in import_jobs.values()
                    if other.get("source_file_id") == source_file_id
                    and other.get("status") == "processing"
                ),
                None,
            )
            if already_running:
                raise MinerUError("同一文献已有解析任务正在运行。")
            job = {
                "job_id": job_id,
                "status": "processing",
                "can_resume": False,
                "phase": "stored",
                "message": "文件已保存，准备处理…",
                "file_name": (
                    Path(str(display_file_name)).name
                    if display_file_name
                    else target.name
                ),
                "size_bytes": target.stat().st_size,
                "source_file_id": source_file_id,
                "detected_pdf_type": profile.get("detected_pdf_type") if is_pdf else None,
                "parse_route": parse_route,
                "force_mineru": bool(force_mineru),
                "provider_id": vision_provider_id,
                "provider_name": provider_name,
                "vision_failed": False,
            }
            context = {
                "target": Path(target),
                "source_file_id": source_file_id,
                "profile": dict(profile),
                "is_pdf": is_pdf,
                "force_mineru": bool(force_mineru),
                "vision_provider_id": vision_provider_id,
            }
            import_jobs[job_id] = job
            import_job_contexts[job_id] = context
        try:
            record = import_job_journal.save_job(
                job,
                target=target,
                source_file_id=source_file_id,
                profile=profile,
                is_pdf=is_pdf,
                force_mineru=force_mineru,
                provider_id=vision_provider_id,
                total_pages=int(profile.get("pdf_page_count") or 0),
            )
        except Exception:
            with import_jobs_lock:
                import_jobs.pop(job_id, None)
                import_job_contexts.pop(job_id, None)
            raise
        with import_jobs_lock:
            context["file_hash"] = str(record.get("file_hash") or "")
        return job_id

    def _reserve_import_source_locked(source_file_id: str) -> None:
        """Reserve one source while its config row is registered."""

        if source_file_id in deleting_import_sources:
            raise MinerUError("该文献正在删除，不能开始解析。")
        if source_file_id in pending_import_sources:
            raise MinerUError("同一文献正在准备导入。")
        if any(
            job.get("source_file_id") == source_file_id
            and job.get("status") == "processing"
            for job in import_jobs.values()
        ):
            raise MinerUError("同一文献已有解析任务正在运行。")
        pending_import_sources.add(source_file_id)

    def register_pdf_for_import(
        target: Path,
        *,
        original_file_name: Optional[str] = None,
    ) -> Tuple[Dict[str, object], str, Path]:
        """Atomically reserve a PDF identity and register its config row."""

        predicted_source_id = f"pdf-import-{sha256_file(target)[:16]}"
        with rebuild_lock, import_jobs_lock:
            _reserve_import_source_locked(predicted_source_id)
            try:
                document = register_pdf(
                    root,
                    target,
                    original_file_name=original_file_name,
                )
                source_file_id = str(document["source_file_id"])
                if source_file_id != predicted_source_id:
                    _reserve_import_source_locked(source_file_id)
                    pending_import_sources.discard(predicted_source_id)
            except Exception:
                pending_import_sources.discard(predicted_source_id)
                raise
        target = reuse_registered_pdf_copy(root, target, document)
        return document, source_file_id, target

    def release_import_reservation(source_file_id: str) -> None:
        with import_jobs_lock:
            pending_import_sources.discard(str(source_file_id or ""))

    def release_item_reservations(items: List[Dict[str, object]]) -> None:
        with import_jobs_lock:
            for item in items:
                if item.get("source_reserved"):
                    pending_import_sources.discard(
                        str(item.get("source_file_id") or "")
                    )

    def cleanup_unreferenced_import_target(
        candidate: Optional[Path],
    ) -> bool:
        """Delete only a new corpus copy that no config or job owns."""

        if candidate is None:
            return False
        target = Path(candidate)
        try:
            resolved = target.resolve()
            allowed_roots = (
                (root / "corpus" / "raw_pdf").resolve(),
                (root / "corpus" / "raw_docx").resolve(),
            )
            if not any(parent in resolved.parents for parent in allowed_roots):
                return False
        except OSError:
            return False

        # Keep the same lock order as job creation (jobs -> import config).
        with import_jobs_lock:
            if any(
                Path(context.get("target") or "").resolve() == resolved
                for context in import_job_contexts.values()
                if context.get("target")
            ):
                return False
            if resolved.suffix.lower() == ".pdf":
                config_path = root / "config" / "pdf_imports.json"
                with locked_import_config(config_path) as config:
                    for document in config.get("documents", []):
                        if not isinstance(document, dict):
                            continue
                        configured_name = str(
                            document.get("file_name") or ""
                        ).strip()
                        if not configured_name:
                            continue
                        configured_path = Path(configured_name)
                        if not configured_path.is_absolute():
                            configured_path = (
                                root
                                / "corpus"
                                / "raw_pdf"
                                / configured_path
                            )
                        if configured_path.resolve() == resolved:
                            return False
            try:
                target.unlink(missing_ok=True)
            except OSError:
                return False
        return True

    def queue_import_job(
        job_id: str,
        target: Path,
        profile: Dict[str, object],
        source_file_id: str,
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
    ) -> None:
        import_task_queue.submit(
            run_import_job,
            job_id,
            target,
            source_file_id,
            profile,
            is_pdf,
            force_mineru,
            vision_provider_id,
        )

    def fail_import_at_queue(job_id: str) -> None:
        """Keep an unqueued task durable and explicitly resumable."""

        update_import_job(
            job_id,
            status="failed",
            phase="queue_failed",
            failure_stage="queue",
            can_resume=True,
            vision_failed=False,
            mineru_failed=False,
            mineru_interrupted=False,
            can_retry_with_provider=False,
            retry_provider_id=None,
            retry_provider_name=None,
            needs_provider_config=False,
            message=(
                "导入任务暂时无法进入处理队列。"
                "文件和任务进度已安全保留，可点击“继续导入”重试。"
            ),
        )

    def start_import_job(
        target: Path,
        profile: Dict[str, object],
        source_file_id: str,
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
        consume_reservation: bool = False,
        display_file_name: Optional[str] = None,
    ) -> str:
        job_id = create_import_job(
            target,
            profile,
            source_file_id,
            is_pdf,
            force_mineru=force_mineru,
            vision_provider_id=vision_provider_id,
            consume_reservation=consume_reservation,
            display_file_name=display_file_name,
        )
        try:
            queue_import_job(
                job_id,
                target,
                profile,
                source_file_id,
                is_pdf,
                force_mineru=force_mineru,
                vision_provider_id=vision_provider_id,
            )
        except Exception:
            fail_import_at_queue(job_id)
        return job_id

    def is_provider_retry_eligible(
        job: Dict[str, object],
        context: Optional[Dict[str, object]] = None,
    ) -> bool:
        """Authorize an EXPLICIT, user-initiated paid provider retry.

        MinerU failures and visual-provider failures qualify. A transient
        interruption also qualifies: a stuck PDF task should not be a dead end,
        so the user may explicitly switch when another provider is configured.
        This gate never enables automatic fallback. Index-stage failures never
        qualify because re-parsing cannot fix a rebuild error.
        """

        return bool(
            str(job.get("status") or "") == "failed"
            and str(job.get("failure_stage") or "") != "index"
            and (
                context is None
                or bool(context.get("is_pdf"))
            )
            and (
                job.get("mineru_failed")
                or job.get("vision_failed")
                # Upgrade jobs saved by v0.4.0 builds that did not persist the
                # explicit flag for a failed visual-provider parse.
                or (
                    str(job.get("parse_route") or "") == "vision"
                    and str(job.get("phase") or "") == "failed"
                )
                or job.get("needs_provider_config")
                or job.get("can_retry_with_provider")
                or job.get("mineru_interrupted")
            )
        )

    def public_import_job(job: Dict[str, object]) -> Dict[str, object]:
        """Return current retry choices without mutating the saved job.

        A parser failure can happen before any alternate provider exists. The
        user may then configure one from the error card, so retry availability
        is derived from current provider config, not the historical snapshot.
        """

        public_job = dict(job)
        is_legacy_vision_failure = bool(
            str(public_job.get("status") or "") == "failed"
            and str(public_job.get("parse_route") or "") == "vision"
            and str(public_job.get("phase") or "") == "failed"
            and str(public_job.get("failure_stage") or "") != "index"
        )
        if is_legacy_vision_failure:
            public_job["vision_failed"] = True
        is_parser_failure = is_provider_retry_eligible(public_job)
        if not is_parser_failure:
            if str(public_job.get("status") or "") == "failed" and (
                str(public_job.get("failure_stage") or "") == "index"
                or public_job.get("mineru_interrupted")
            ):
                public_job.update(
                    can_retry_with_provider=False,
                    retry_provider_id=None,
                    retry_provider_name=None,
                    needs_provider_config=False,
                )
            return public_job
        try:
            summary = vision_config_summary(resolve_vision_config_path(root))
        except (OSError, ValueError, VisionAPIError):
            summary = {"providers": []}
        providers = [
            item
            for item in summary.get("providers", [])
            if isinstance(item, dict)
            and item.get("enabled")
            and item.get("configured")
        ]
        preferred_provider_id = str(
            public_job.get("retry_provider_id") or ""
        )
        provider = next(
            (
                item
                for item in providers
                if str(item.get("id") or "") == preferred_provider_id
            ),
            providers[0] if providers else None,
        )
        if public_job.get("vision_failed"):
            current_provider_id = str(public_job.get("provider_id") or "")
            alternate_provider = next(
                (
                    item
                    for item in providers
                    if str(item.get("id") or "") != current_provider_id
                ),
                None,
            )
            if alternate_provider and (
                provider is None
                or str(provider.get("id") or "") == current_provider_id
            ):
                provider = alternate_provider
        public_job.update(
            can_retry_with_provider=bool(provider),
            retry_provider_id=provider.get("id") if provider else None,
            retry_provider_name=provider.get("name") if provider else None,
            needs_provider_config=not bool(provider),
        )
        return public_job

    def resumable_import_jobs() -> List[Dict[str, object]]:
        with import_jobs_lock:
            jobs = [
                (
                    job_id,
                    dict(job),
                    dict(import_job_contexts.get(job_id) or {}),
                )
                for job_id, job in import_jobs.items()
                if str(job.get("status") or "") in {"paused", "failed"}
            ]
        result: List[Dict[str, object]] = []
        for _job_id, job, context in jobs:
            public_job = public_import_job(job)
            public_job["file_type"] = (
                "pdf" if context.get("is_pdf") else "docx"
            )
            result.append(public_job)
        return sorted(
            result,
            key=lambda item: str(item.get("last_updated") or ""),
            reverse=True,
        )

    def retry_index_job(
        job_id: str,
        target: Path,
        source_file_id: str,
        is_pdf: bool,
    ) -> None:
        try:
            if is_pdf:
                index_registered_pdf(job_id, source_file_id)
            else:
                rebuild_runtime_index(job_id)
            finalize_import_job(job_id, source_file_id, is_pdf)
        except Exception as exc:
            fail_import_at_index(job_id, exc, parsed=True)

    def resume_import_job(job_id: str) -> Dict[str, object]:
        with import_jobs_lock:
            job = import_jobs.get(job_id)
            context = import_job_contexts.get(job_id)
            if not job or not context:
                raise MinerUError("待继续的导入任务不存在。")
            if (
                str(job.get("status") or "") not in {"paused", "failed"}
                or not job.get("can_resume")
            ):
                raise MinerUError("该导入任务当前不能继续。")
            source_file_id = str(context.get("source_file_id") or "")
            if source_file_id in deleting_import_sources:
                raise MinerUError("该文献正在删除，不能继续解析。")
            if source_file_id in pending_import_sources:
                raise MinerUError("同一文献正在准备导入。")
            already_running = next(
                (
                    other
                    for other_id, other in import_jobs.items()
                    if other_id != job_id
                    and other.get("source_file_id") == source_file_id
                    and other.get("status") == "processing"
                ),
                None,
            )
            if already_running:
                raise MinerUError("同一文献已有解析任务正在运行。")
            target = validated_import_target(job_id, context)
            retry_index_only = (
                infer_import_failure_stage(
                    job,
                    is_pdf=bool(context.get("is_pdf")),
                )
                == "index"
            )
            next_phase = "rebuilding_index" if retry_index_only else "stored"
            next_message = (
                "正在重新建立索引，不会再次调用解析 API…"
                if retry_index_only
                else "正在从上次断点继续…"
            )
            job.update(
                {
                    "status": "processing",
                    "phase": next_phase,
                    "can_resume": False,
                    "message": next_message,
                    "vision_failed": False,
                    "mineru_failed": False,
                    "mineru_interrupted": False,
                    "can_retry_with_provider": False,
                    "retry_provider_id": None,
                    "retry_provider_name": None,
                    "needs_provider_config": False,
                }
            )
            restored = dict(job)
        journal_updates: Dict[str, object] = {
            "status": "processing",
            "phase": next_phase,
            "can_resume": False,
            "message": next_message,
            "vision_failed": False,
            "mineru_failed": False,
            "mineru_interrupted": False,
            "can_retry_with_provider": False,
            "retry_provider_id": None,
            "retry_provider_name": None,
            "needs_provider_config": False,
        }
        if retry_index_only:
            journal_updates["failure_stage"] = "index"
        import_job_journal.update_job(job_id, **journal_updates)
        try:
            if retry_index_only:
                import_task_queue.submit(
                    retry_index_job,
                    job_id,
                    target,
                    str(context.get("source_file_id") or ""),
                    bool(context.get("is_pdf")),
                )
            else:
                queue_import_job(
                    job_id,
                    target,
                    dict(context.get("profile") or {}),
                    str(context.get("source_file_id") or ""),
                    bool(context.get("is_pdf")),
                    force_mineru=bool(context.get("force_mineru")),
                    vision_provider_id=(
                        str(context["vision_provider_id"])
                        if context.get("vision_provider_id")
                        else None
                    ),
                )
        except Exception as exc:
            fail_import_at_queue(job_id)
            raise MinerUError(
                "导入任务暂时无法进入处理队列，已保留为可继续任务。"
            ) from exc
        return restored

    def dismiss_import_job(job_id: str) -> None:
        """Forget a paused/failed task after the user explicitly dismisses it."""

        with import_jobs_lock:
            job = import_jobs.get(job_id)
            if job is None:
                # A completed task may already have removed its journal.
                import_job_journal.delete_job(job_id)
                return
            if str(job.get("status") or "") == "processing":
                raise MinerUError("正在运行的导入任务不能移除。")
            import_job_journal.delete_job(job_id)
            import_jobs.pop(job_id, None)
            import_job_contexts.pop(job_id, None)

    def start_native_import_batch(
        items: List[Dict[str, object]],
    ) -> List[str]:
        """Index PDFs independently; retain one rebuild for Word/mixed batches."""

        queued_items: List[Dict[str, object]] = []
        try:
            for item in items:
                try:
                    job_id = create_import_job(
                        Path(item["target"]),
                        dict(item["profile"]),
                        str(item["source_file_id"]),
                        bool(item["is_pdf"]),
                        consume_reservation=bool(
                            item.get("source_reserved")
                        ),
                        display_file_name=(
                            str(item["display_file_name"])
                            if item.get("display_file_name")
                            else None
                        ),
                    )
                finally:
                    if item.get("source_reserved"):
                        release_import_reservation(str(item["source_file_id"]))
                queued_items.append({**item, "job_id": job_id})
        except Exception:
            release_item_reservations(items)
            for queued in queued_items:
                queued_job_id = str(queued["job_id"])
                try:
                    import_job_journal.delete_job(queued_job_id)
                except OSError:
                    logging.warning(
                        "failed to remove unqueued batch journal %s",
                        queued_job_id,
                    )
                with import_jobs_lock:
                    import_jobs.pop(queued_job_id, None)
                    import_job_contexts.pop(queued_job_id, None)
            raise

        if not queued_items:
            return []

        def run_native_batch() -> None:
            job_ids = [str(item["job_id"]) for item in queued_items]
            batch_size = len(job_ids)
            pdf_only = all(bool(item["is_pdf"]) for item in queued_items)
            for job_id in job_ids:
                update_import_job(
                    job_id,
                    phase="text_parsing" if pdf_only else "rebuilding_index",
                    message=(
                        f"正在逐份解析并写入索引（共 {batch_size} 个 PDF）…"
                        if pdf_only
                        else f"正在批量建立索引（共 {batch_size} 个文件）…"
                    ),
                    parse_route="native",
                )
            if pdf_only:
                for item in queued_items:
                    job_id = str(item["job_id"])
                    try:
                        index_registered_pdf(
                            job_id,
                            str(item["source_file_id"]),
                            backup_existing=False,
                        )
                    except Exception as exc:
                        fail_import_at_index(job_id, exc)
                        continue
                    finalize_import_job(
                        job_id,
                        str(item["source_file_id"]),
                        True,
                    )
                return

            expected_source_ids = [
                str(item["source_file_id"])
                for item in queued_items
                if bool(item["is_pdf"])
            ]
            try:
                missing_source_ids = rebuild_runtime_index(
                    job_ids[0],
                    expected_source_ids,
                )
            except Exception as exc:
                for job_id in job_ids:
                    fail_import_at_index(job_id, exc)
                return
            for item in queued_items:
                source_file_id = str(item["source_file_id"])
                if source_file_id in missing_source_ids:
                    fail_import_at_index(
                        str(item["job_id"]),
                        MinerUError(
                            f"{item.get('display_file_name') or Path(item['target']).name} "
                            "未能进入索引："
                            "重建后未找到文献记录。"
                        ),
                    )
                    continue
                finalize_import_job(
                    str(item["job_id"]),
                    source_file_id,
                    bool(item["is_pdf"]),
                )

        try:
            import_task_queue.submit(run_native_batch)
        except Exception:
            for item in queued_items:
                fail_import_at_queue(str(item["job_id"]))
        return [str(item["job_id"]) for item in queued_items]

    def start_remote_import_batch(
        items: List[Dict[str, object]],
    ) -> List[str]:
        """Parse OCR/VLM files with two workers, then index each independently."""

        queued_items: List[Dict[str, object]] = []
        try:
            for item in items:
                vision_provider_id = (
                    str(item["vision_provider_id"])
                    if item.get("vision_provider_id")
                    else None
                )
                try:
                    job_id = create_import_job(
                        Path(item["target"]),
                        dict(item["profile"]),
                        str(item["source_file_id"]),
                        bool(item["is_pdf"]),
                        force_mineru=bool(item["force_mineru"]),
                        vision_provider_id=vision_provider_id,
                        consume_reservation=bool(
                            item.get("source_reserved")
                        ),
                        display_file_name=(
                            str(item["display_file_name"])
                            if item.get("display_file_name")
                            else None
                        ),
                    )
                finally:
                    if item.get("source_reserved"):
                        release_import_reservation(str(item["source_file_id"]))
                queued_items.append(
                    {
                        **item,
                        "job_id": job_id,
                        "vision_provider_id": vision_provider_id,
                    }
                )
        except Exception:
            release_item_reservations(items)
            for queued in queued_items:
                queued_job_id = str(queued["job_id"])
                try:
                    import_job_journal.delete_job(queued_job_id)
                except OSError:
                    logging.warning(
                        "failed to remove unqueued batch journal %s",
                        queued_job_id,
                    )
                with import_jobs_lock:
                    import_jobs.pop(queued_job_id, None)
                    import_job_contexts.pop(queued_job_id, None)
            raise

        if not queued_items:
            return []

        remote_commit_lock = threading.Lock()

        def run_remote_item(item: Dict[str, object]) -> None:
            job_id = str(item["job_id"])
            source_file_id = str(item["source_file_id"])
            succeeded = prepare_import_job(
                job_id,
                Path(item["target"]),
                source_file_id,
                dict(item["profile"]),
                bool(item["is_pdf"]),
                bool(item["force_mineru"]),
                (
                    str(item["vision_provider_id"])
                    if item.get("vision_provider_id")
                    else None
                ),
            )
            if not succeeded:
                return
            update_import_job(
                job_id,
                phase="rebuilding_index",
                message="解析完成，正在写入本地索引…",
            )
            try:
                # Each completed parser task is committed immediately.  The
                # transaction and the shared rebuild lock make a multi-GB
                # pre-import snapshot unnecessary; the second worker may keep
                # parsing another PDF meanwhile.
                with remote_commit_lock:
                    index_registered_pdf(
                        job_id,
                        source_file_id,
                        backup_existing=False,
                    )
            except Exception as exc:
                fail_import_at_index(job_id, exc, parsed=True)
                return
            finalize_import_job(
                job_id,
                source_file_id,
                bool(item["is_pdf"]),
            )

        for item in queued_items:
            try:
                import_task_queue.submit(run_remote_item, item)
            except Exception:
                fail_import_at_queue(str(item["job_id"]))
        return [str(item["job_id"]) for item in queued_items]

    def backup_app_data_root() -> Path:
        return resolve_preferences_path(root).parent

    def export_runtime_backup() -> Dict[str, object]:
        app_data = backup_app_data_root()
        dest_dir = app_data / "backups"
        target = write_backup(root, dest_dir, app_data_root=app_data)
        return {"ok": True, "path": str(target), "size_bytes": target.stat().st_size}

    def import_runtime_backup(source_path: str) -> str:
        path = Path(str(source_path)).expanduser()
        if not path.is_file():
            raise MinerUError("备份文件不存在。")
        if path.suffix.lower() != ".zip":
            raise MinerUError("请选择 .zip 备份文件。")
        job_id = f"restore-{uuid.uuid4().hex[:12]}"
        with import_jobs_lock:
            import_jobs[job_id] = {
                "job_id": job_id,
                "status": "processing",
                "phase": "restoring_backup",
                "message": "正在恢复备份并重建索引…",
            }

        def run_restore_job() -> None:
            try:
                with (
                    durable_operations.operation(),
                    rebuild_lock,
                    import_config_lock(),
                ):
                    summary = restore_backup(
                        root,
                        path.read_bytes(),
                        app_data_root=backup_app_data_root(),
                    )
                    update_import_job(
                        job_id,
                        phase="rebuilding_index",
                        message=f"已恢复 {summary['count']} 项，正在重建索引…",
                    )
                    rebuild_runtime_index(job_id)
                update_import_job(
                    job_id,
                    status="completed",
                    phase="completed",
                    message=f"备份已恢复并重建索引：{summary['count']} 项",
                )
            except Exception as exc:
                update_import_job(
                    job_id,
                    status="failed",
                    phase="failed",
                    message=f"文件已恢复，但索引重建失败：{exc}",
                )

        try:
            import_task_queue.submit(run_restore_job)
        except Exception as exc:
            update_import_job(
                job_id,
                status="failed",
                phase="queue_failed",
                message="备份恢复任务未能进入队列。",
            )
            raise MinerUError(
                "备份恢复任务暂时无法启动，文件未更改。"
            ) from exc
        return job_id

    def upload_storage_details(
        filename: str,
        length: int,
        is_pdf: bool,
    ) -> Tuple[str, Path]:
        if length <= 0 or length > 600 * 1024 * 1024:
            raise MinerUError("文件为空或超过 600 MB 限制。")
        safe_name = Path(filename).name
        if not safe_name or safe_name in {".", ".."}:
            raise MinerUError("无法识别文件名。")
        suffix = Path(safe_name).suffix.lower()
        expected = ".pdf" if is_pdf else ".docx"
        if suffix != expected:
            raise MinerUError(f"导入文件必须是 {expected}。")
        directory = root / "corpus" / ("raw_pdf" if is_pdf else "raw_docx")
        return safe_name, directory

    def store_upload(filename: str, length: int, is_pdf: bool, reader) -> Path:
        safe_name, directory = upload_storage_details(filename, length, is_pdf)
        target: Optional[Path] = None
        temp_path: Optional[Path] = None
        remaining = length
        try:
            directory.mkdir(parents=True, exist_ok=True)
            cleanup_stale_document_storage_files(directory)
            target = document_storage_target(
                directory,
                safe_name,
                shorten_long_names=is_pdf,
            )
            temp_path = directory / f".mefinder-upload-{uuid.uuid4().hex}.tmp"
            with temp_path.open("wb") as stream:
                while remaining > 0:
                    chunk = reader.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise MinerUError("上传数据不完整。")
                    stream.write(chunk)
                    remaining -= len(chunk)
            temp_path.replace(target)
        except OSError as exc:
            raise document_storage_error(safe_name, exc) from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            if target is not None:
                release_document_storage_target(target)
        assert target is not None
        return target

    def store_completed_upload(
        filename: str,
        length: int,
        is_pdf: bool,
        staged_path: Path,
    ) -> Path:
        """Atomically move a verified chunked upload into document storage."""

        safe_name, directory = upload_storage_details(filename, length, is_pdf)
        target: Optional[Path] = None
        try:
            if staged_path.stat().st_size != length:
                raise MinerUError("上传文件大小校验失败。")
            directory.mkdir(parents=True, exist_ok=True)
            cleanup_stale_document_storage_files(directory)
            target = document_storage_target(
                directory,
                safe_name,
                shorten_long_names=is_pdf,
            )
            staged_path.replace(target)
        except OSError as exc:
            raise document_storage_error(safe_name, exc) from exc
        finally:
            if target is not None:
                release_document_storage_target(target)
        assert target is not None
        return target

    def validate_upload_parse_options(
        pdf_parse_mode: object,
        vision_provider_id: object,
    ) -> Tuple[str, str]:
        mode = str(pdf_parse_mode or "auto").strip().lower()
        if mode not in {"auto", "mineru", "vision"}:
            raise MinerUError("PDF 解析方式无效。")
        provider_id = (
            str(vision_provider_id or "").strip()
            if mode == "vision"
            else ""
        )
        if mode == "vision" and not provider_id:
            raise MinerUError("请选择一个其他解析 API。")
        return mode, provider_id

    def start_stored_upload_import(
        target: Path,
        *,
        filename: str,
        is_pdf: bool,
        pdf_parse_mode: str,
        vision_provider_id: str,
        upload_id: str = "legacy",
    ) -> Dict[str, object]:
        """Run type detection and enqueue the existing import pipeline."""

        reserved_source_id = ""
        owned_target: Optional[Path] = target
        try:
            if is_pdf:
                logging.info(
                    "import type detection started upload_id=%s file=%r size=%d",
                    upload_id,
                    Path(filename).name,
                    target.stat().st_size,
                )
                profile = detect_imported_pdf(target)
                logging.info(
                    "import type detection completed upload_id=%s detected_type=%s",
                    upload_id,
                    profile.get("detected_pdf_type"),
                )
                (
                    _document,
                    source_file_id,
                    target,
                ) = register_pdf_for_import(
                    target,
                    original_file_name=filename,
                )
                reserved_source_id = source_file_id
            else:
                profile = {"detected_pdf_type": "docx"}
                source_file_id = f"docx-import-{uuid.uuid4().hex[:16]}"
            force_mineru = is_pdf and pdf_parse_mode == "mineru"
            job_id = start_import_job(
                target,
                profile,
                source_file_id,
                is_pdf,
                force_mineru=force_mineru,
                vision_provider_id=vision_provider_id or None,
                consume_reservation=bool(reserved_source_id),
                display_file_name=filename,
            )
            if reserved_source_id:
                release_import_reservation(reserved_source_id)
                reserved_source_id = ""
            parse_route = None
            if is_pdf:
                parse_route = (
                    "vision"
                    if vision_provider_id
                    else "mineru"
                    if force_mineru
                    or str(profile.get("detected_pdf_type")) != "native_text"
                    else "native"
                )
            logging.info(
                "import job queued upload_id=%s job_id=%s route=%s",
                upload_id,
                job_id,
                parse_route or "docx",
            )
            return {
                "ok": True,
                "job_id": job_id,
                "file_name": Path(filename).name,
                "source_file_id": source_file_id,
                "detected_pdf_type": (
                    profile.get("detected_pdf_type") if is_pdf else None
                ),
                "parse_route": parse_route,
                "provider_id": vision_provider_id or None,
            }
        finally:
            if reserved_source_id:
                release_import_reservation(reserved_source_id)
            cleanup_unreferenced_import_target(owned_target)

    def accept_auto_page_mapping(source_id: str) -> int:
        config_path = root / "config" / "pdf_imports.json"
        if not config_path.exists():
            raise MinerUError("PDF 导入配置不存在。")
        with runtime_lock:
            source = runtime["source_files"].get(source_id)
        if not source:
            raise MinerUError("文献未找到。")
        auto_mapping = ((source.get("pdf_profile") or {}).get("auto_page_mapping") or {})
        applied = [segment for segment in auto_mapping.get("applied_segments", []) if isinstance(segment, dict)]
        if not applied:
            raise MinerUError("没有可接受的高置信度自动映射段。")
        with locked_import_config(config_path) as config:
            document = next((doc for doc in config.get("documents", []) if doc.get("source_file_id") == source_id), None)
            if not document:
                raise MinerUError("PDF 配置中找不到该文献。")
            manual_segments = []
            for segment in applied:
                clean = {
                    "pdf_page_start": int(segment["pdf_page_start"]),
                    "pdf_page_end": int(segment["pdf_page_end"]),
                    "citation_page_start": str(segment["citation_page_start"]),
                    "number_style": str(segment.get("number_style") or "arabic"),
                    "method": "manual_segment",
                    "confidence": float(segment.get("mapping_confidence") or 0.95),
                    "label": "已接受自动页码映射",
                    "evidence": segment.get("mapping_evidence"),
                    "layout_mode": "spread"
                    if segment.get("layout_mode") == "spread"
                    else "single",
                }
                if clean["layout_mode"] == "spread":
                    clean["reading_direction"] = (
                        "rtl" if segment.get("reading_direction") == "rtl" else "ltr"
                    )
                    clean["gutter_x"] = segment.get("gutter_x") or 0.5
                manual_segments.append(clean)
            document.setdefault("page_mapping", {})
            document["page_mapping"]["segments"] = manual_segments
            document["page_mapping"]["validated_by"] = "auto_mapping_accepted"
            document["page_mapping"]["mapping_status"] = "manual_mapped"
            document["page_mapping"]["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_import_config(config_path, config)
        return len(manual_segments)

    def apply_manual_page_mapping(
        source_id: str,
        segments: List[Dict[str, object]],
    ) -> None:
        with durable_operations.operation():
            _apply_manual_page_mapping(source_id, segments)

    def _apply_manual_page_mapping(
        source_id: str,
        segments: List[Dict[str, object]],
    ) -> None:
        """Persist manual mapping and rebuild as one deletion-safe mutation."""

        cleaned_segments = normalize_manual_mapping_segments(segments)
        with rebuild_lock:
            config_path = root / "config" / "pdf_imports.json"
            if not config_path.exists():
                raise MinerUError("PDF 导入配置不存在。")
            with locked_import_config(config_path) as config:
                document = next(
                    (
                        item
                        for item in config.get("documents", [])
                        if item.get("source_file_id") == source_id
                    ),
                    None,
                )
                if not document:
                    raise MinerUError("PDF 配置中找不到该文献。")
                document.setdefault("page_mapping", {})
                document["page_mapping"]["segments"] = cleaned_segments
                document["page_mapping"]["validated_by"] = "manual_ui"
                document["page_mapping"]["mapping_origin"] = "manual"
                document["page_mapping"]["mapping_status"] = (
                    "manual_mapped" if cleaned_segments else "unmapped"
                )
                document["page_mapping"]["updated_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                save_import_config(config_path, config)
            job_id = f"calibration-{uuid.uuid4().hex[:12]}"
            with import_jobs_lock:
                import_jobs[job_id] = {
                    "job_id": job_id,
                    "status": "processing",
                    "phase": "rebuilding_index",
                    "message": "正在应用页码校准并重建索引…",
                }
            try:
                rebuild_runtime_index(job_id)
                update_import_job(
                    job_id,
                    status="completed",
                    phase="completed",
                    message="页码校准已生效",
                )
            except Exception as exc:
                update_import_job(
                    job_id,
                    status="failed",
                    phase="failed",
                    message=str(exc),
                )
                raise

    def source_path_from_id(source_id: str) -> Path:
        with runtime_lock:
            record = runtime["source_files"].get(source_id)
        if not record:
            raise MinerUError("文献未找到。")
        relative_path = str(record.get("relative_path") or "")
        target = (root / relative_path).resolve()
        if target != root and root not in target.parents:
            raise MinerUError("拒绝打开应用目录外的文件。")
        if target.suffix.lower() not in {".pdf", ".doc", ".docx"} or not target.exists():
            raise MinerUError("原始文件不存在。")
        return target

    def detect_auto_page_mapping(source_id: str) -> Dict[str, object]:
        config_path = root / "config" / "pdf_imports.json"
        if not config_path.exists():
            raise MinerUError("PDF 导入配置不存在。")
        config = load_import_config(config_path)
        document = next((doc for doc in config.get("documents", []) if doc.get("source_file_id") == source_id), None)
        if not document:
            raise MinerUError("PDF 配置中找不到该文献。")
        try:
            path = source_path_from_id(source_id)
        except MinerUError as exc:
            if "不存在" in str(exc):
                return {
                    "source_id": source_id,
                    "mapping_status": "source_missing",
                    "failure_reasons": ["source_missing"],
                    "selected_segments": [],
                    "applied_segments": [],
                    "manual_mapping_present": has_manual_mapping(document),
                    "dry_run": True,
                }
            raise
        if path.suffix.lower() != ".pdf":
            raise MinerUError("自动页码检测只支持 PDF。")
        manual_present = has_manual_mapping(document)
        detection_config = copy.deepcopy(document)
        detection_config.setdefault("page_mapping", {})
        detection_config["page_mapping"]["segments"] = []
        detection_config["page_mapping"]["validated_by"] = None
        extracted = extract_pdf_source(path, root, detection_config, parsed_dir=None)
        sources = extracted.get("source_files", [])
        if not sources:
            raise MinerUError("无法读取文献页码证据。")
        profile = sources[0].get("pdf_profile") or {}
        result = dict(profile.get("auto_page_mapping") or {})
        result["manual_mapping_present"] = manual_present
        result["dry_run"] = True
        result["source_id"] = source_id
        result["source_file"] = path.name
        result["current_mapping"] = document.get("page_mapping") or {}
        return result

    def apply_live_auto_mapping(
        source_id: str,
        segments: List[Dict[str, object]],
        auto_mapping: Dict[str, object],
        replace_manual: bool,
    ) -> Dict[str, int]:
        with durable_operations.operation():
            return _apply_live_auto_mapping(
                source_id,
                segments,
                auto_mapping,
                replace_manual,
            )

    def _apply_live_auto_mapping(
        source_id: str,
        segments: List[Dict[str, object]],
        auto_mapping: Dict[str, object],
        replace_manual: bool,
    ) -> Dict[str, int]:
        config_path = root / "config" / "pdf_imports.json"
        with locked_import_config(config_path) as config:
            document = next((doc for doc in config.get("documents", []) if doc.get("source_file_id") == source_id), None)
            if not document:
                raise MinerUError("PDF 配置中找不到该文献。")
            manual_present = has_manual_mapping(document)
            if manual_present and not replace_manual:
                raise MinerUError("当前文献已有人工页码映射，必须明确确认后才能替换。")
            cleaned = normalize_auto_segments(segments)
            if not cleaned:
                raise MinerUError("没有可应用的自动页码区间。")
            original_config = copy.deepcopy(config)
            document.setdefault("page_mapping", {})
            document["page_mapping"]["segments"] = cleaned
            document["page_mapping"]["validated_by"] = "auto_mapping_ui"
            document["page_mapping"]["mapping_origin"] = "auto"
            document["page_mapping"]["updated_at"] = datetime.now(timezone.utc).isoformat()
            confidence_levels = {str(item.get("confidence_level") or "") for item in cleaned}
            mapping_status = "auto_mapped_high" if confidence_levels == {"high"} else "auto_mapped_medium"
            document["page_mapping"]["mapping_status"] = mapping_status
            save_import_config(config_path, config)
            with runtime_lock:
                runtime["rebuilding"] = True
                old_engine = runtime["engine"]
                if hasattr(old_engine, "close"):
                    old_engine.close()
            database_updated = False
            try:
                updated = apply_mapping_to_database(
                    index_path,
                    source_id,
                    cleaned,
                    auto_mapping=auto_mapping,
                    mapping_status=mapping_status,
                )
                database_updated = True
                reload_runtime_index()
                with runtime_lock:
                    runtime["rebuilding"] = False
                return updated
            except Exception:
                # Once the SQLite transaction committed, the new config is the
                # durable source of truth for the next rebuild.  Rolling it
                # back merely because the in-memory engine could not reload
                # would split config and DB into contradictory states.
                if not database_updated:
                    save_import_config(config_path, original_config)
                recover_runtime_index()
                raise

    def open_source_file(source_id: str, page: object = None) -> Dict[str, object]:
        target = source_path_from_id(source_id)
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

    def configured_document(source_id: str) -> Tuple[Path, Dict[str, object], Dict[str, object]]:
        config_path = root / "config" / "pdf_imports.json"
        if not config_path.exists():
            raise MinerUError("PDF 导入配置不存在。")
        config = load_import_config(config_path)
        document = next((doc for doc in config.get("documents", []) if doc.get("source_file_id") == source_id), None)
        if not document:
            raise MinerUError("PDF 配置中找不到该文献。")
        return config_path, config, document

    def front_matter_pages(source_id: str, limit: int = 20, tail: int = 8) -> List[Dict[str, object]]:
        """Front pages plus the trailing pages: Chinese colophons often sit at the back."""
        connection = sqlite3.connect(str(index_path))
        try:
            total_row = connection.execute(
                "SELECT MAX(pdf_page_index) FROM pdf_pages WHERE source_file_id = ?",
                (source_id,),
            ).fetchone()
            total = int(total_row[0]) + 1 if total_row and total_row[0] is not None else 0
            rows = connection.execute(
                "SELECT payload_json FROM pdf_pages WHERE source_file_id = ? AND (pdf_page_index < ? OR pdf_page_index >= ?) ORDER BY pdf_page_index",
                (source_id, limit, max(limit, total - tail)),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            connection.close()

    def detect_bibliographic_metadata(source_id: str, force: bool = False) -> Dict[str, object]:
        _, _, document = configured_document(source_id)
        path = source_path_from_id(source_id)
        return detect_pdf_bibliographic_metadata(
            path,
            front_matter_pages(source_id),
            document,
            force=force,
        )

    def persist_bibliographic_metadata(
        source_id: str, payload: Dict[str, object]
    ) -> Dict[str, object]:
        with durable_operations.operation():
            return _persist_bibliographic_metadata(source_id, payload)

    def _persist_bibliographic_metadata(
        source_id: str, payload: Dict[str, object]
    ) -> Dict[str, object]:
        with metadata_lock, rebuild_lock:
            config_path = root / "config" / "pdf_imports.json"
            with locked_import_config(config_path) as config:
                document = next(
                    (
                        item
                        for item in config.get("documents", [])
                        if item.get("source_file_id") == source_id
                    ),
                    None,
                )
                if not document:
                    raise MinerUError("PDF 配置中找不到该文献。")
                original_config = copy.deepcopy(config)
                metadata = canonical_metadata(payload)
                if not metadata.get("metadata_missing_fields"):
                    metadata["metadata_missing_fields"] = metadata_missing_fields(metadata)
                for field in METADATA_FIELDS:
                    document[field] = metadata.get(field)
                for field in (
                    "document_type",
                    "metadata_status",
                    "metadata_source",
                    "metadata_confidence",
                    "metadata_evidence",
                    "metadata_conflicts",
                    "metadata_missing_fields",
                ):
                    document[field] = metadata.get(field)
                document["publication_year"] = metadata.get("publish_year")
                document["bibliographic_metadata"] = metadata
                save_import_config(config_path, config)
                with runtime_lock:
                    runtime["rebuilding"] = True
                    old_engine = runtime["engine"]
                    if hasattr(old_engine, "close"):
                        old_engine.close()
                database_updated = False
                try:
                    update_metadata_in_database(index_path, source_id, metadata)
                    database_updated = True
                    reload_runtime_index()
                    with runtime_lock:
                        runtime["rebuilding"] = False
                    return metadata
                except Exception:
                    if not database_updated:
                        save_import_config(config_path, original_config)
                    recover_runtime_index()
                    raise

    def save_bibliographic_metadata(source_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        _, _, document = configured_document(source_id)
        metadata = manual_metadata(payload, document)
        return persist_bibliographic_metadata(source_id, metadata)

    def batch_metadata_candidates() -> List[Dict[str, object]]:
        """PDF sources that still need automatic bibliographic recognition.

        Besides sources with missing fields, this re-checks anything classified
        as a book/translated_book: a journal offprint whose publisher was pulled
        from a cited work looks "complete" as a book, so filtering by missing
        fields alone would never revisit that misclassification. Manually edited
        records are always left untouched.
        """
        data = library_data()
        candidates = []
        for item in data.get("items", []):
            if str(item.get("source_type") or "") != "pdf":
                continue
            nested = item.get("bibliographic_metadata")
            source = str((nested or {}).get("metadata_source") or item.get("metadata_source") or "")
            if source == "manual":
                continue
            doc_type = str(item.get("document_type") or (nested or {}).get("document_type") or "")
            if item.get("metadata_missing_fields") or doc_type in ("book", "translated_book"):
                candidates.append(item)
        return candidates

    def run_batch_metadata_job(job_id: str, candidates: List[Dict[str, object]]) -> None:
        updated = 0
        unchanged = 0
        failures: List[Dict[str, object]] = []
        total = len(candidates)
        compare_fields = tuple(METADATA_FIELDS) + ("document_type", "metadata_status")
        for index, item in enumerate(candidates):
            source_id = str(item.get("source_file_id"))
            title = str(item.get("title") or source_id)
            update_import_job(
                job_id,
                phase="metadata_recognition",
                message=f"正在识别 {index + 1}/{total}：{title}",
            )
            try:
                before = canonical_metadata(item.get("bibliographic_metadata") or item)
                detected = detect_bibliographic_metadata(source_id)
                if any(detected.get(field) != before.get(field) for field in compare_fields):
                    persist_bibliographic_metadata(source_id, detected)
                    updated += 1
                else:
                    unchanged += 1
            except Exception as exc:
                failures.append({"source_file_id": source_id, "title": title, "error": str(exc)})
        summary = f"批量识别完成：更新 {updated} 部，无变化 {unchanged} 部"
        if failures:
            summary += f"，失败 {len(failures)} 部"
        update_import_job(
            job_id,
            status="completed",
            phase="completed",
            message=summary,
            batch_updated=updated,
            batch_unchanged=unchanged,
            batch_failures=failures,
        )

    class Handler(BaseHTTPRequestHandler):
        _GET_ROUTE_TABLE = {
            "/api/document/pages": "_get_document_pages",
        }
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

        def _get_document_pages(self, parsed) -> None:
            params = parse_qs(parsed.query, keep_blank_values=True)
            source_ids = params.get("source_id", [])
            starts = params.get("start", ["0"])
            counts = params.get("count", ["20"])
            if len(source_ids) != 1 or len(starts) != 1 or len(counts) != 1:
                self._send_json(
                    {
                        "error": (
                            "source_id 必须提供一次，start 和 count "
                            "最多各提供一次。"
                        )
                    },
                    status=400,
                )
                return

            try:
                with runtime_lock:
                    if runtime["rebuilding"]:
                        result = None
                    else:
                        result = get_document_window(
                            index_path,
                            source_ids[0],
                            start=starts[0],
                            count=counts[0],
                        )
            except (
                InvalidPagination,
                InvalidSourceId,
                UnsupportedSourceType,
            ) as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            except SourceNotFound as exc:
                self._send_json({"error": str(exc)}, status=404)
                return
            except (OSError, sqlite3.Error, StructuredReaderError) as exc:
                logging.exception("structured reader data request failed")
                self._send_json(
                    {"error": "结构化阅读数据读取失败，请稍后重试。"},
                    status=500,
                )
                return

            if result is None:
                self._send_json(
                    {"error": "索引正在重建，请稍候再打开结构化阅读。"},
                    status=503,
                )
                return
            self._send_json(result)

        def _post_search(self, payload: object) -> None:
            try:
                request = SearchRequest.from_payload(payload)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            with runtime_lock:
                if runtime["rebuilding"]:
                    self._send_json(
                        {"error": "索引正在重建，请稍候再搜索。"},
                        status=503,
                    )
                    return
                result = search_service.execute(runtime["engine"], request)
            self._send_json(result)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            route_method = self._GET_ROUTE_TABLE.get(parsed.path)
            if route_method is not None:
                getattr(self, route_method)(parsed)
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
            if parsed.path == "/api/index-meta":
                with runtime_lock:
                    self._send_json(runtime["index_metadata"])
                return
            if parsed.path == "/api/mineru-config":
                config_path = resolve_mineru_config_path(root)
                try:
                    self._send_json(mineru_config_summary(config_path))
                except (MinerUError, OSError, json.JSONDecodeError):
                    self._send_json({"error": "本机 MinerU 配置文件无法读取。"}, status=500)
                return
            if parsed.path == "/api/vision-providers":
                config_path = resolve_vision_config_path(root)
                try:
                    self._send_json(vision_config_summary(config_path))
                except VisionAPIError as exc:
                    self._send_json({"error": str(exc)}, status=500)
                return
            if parsed.path == "/api/preferences":
                preferences_path = resolve_preferences_path(root)
                self._send_json(read_preferences(preferences_path))
                return
            if parsed.path == "/api/update/status":
                if update_service is None:
                    self._send_json({
                        "status": "unsupported",
                        "can_self_update": False,
                        "message": "当前运行方式不支持应用内更新。",
                    })
                else:
                    self._send_json(update_service.status())
                return
            if parsed.path == "/api/macos-update":
                desktop_shell = os.environ.get(
                    "ME_FINDER_DESKTOP_SHELL", ""
                ).strip().lower()
                if desktop_shell != "macos":
                    self._send_json(
                        {
                            "status": "unsupported",
                            "current_version": __version__,
                            "update_available": False,
                            "message": "此更新入口仅用于 macOS 应用。",
                        },
                        status=404,
                    )
                    return
                self._send_json(check_macos_update(__version__))
                return
            if parsed.path == "/api/data-location":
                desktop_shell = os.environ.get(
                    "ME_FINDER_DESKTOP_SHELL", ""
                ).strip().lower()
                if (
                    desktop_shell != "macos"
                    or app_data_root is None
                    or default_app_data_root is None
                ):
                    self._send_json(
                        {
                            "available": False,
                            "error": "数据位置选择仅适用于已打包的 macOS 应用。",
                        },
                        status=404,
                    )
                    return
                self._send_json(
                    data_location_summary(app_data_root, default_app_data_root)
                )
                return
            if parsed.path == "/api/sources":
                with runtime_lock:
                    current_engine = runtime["engine"]
                    self._send_json({
                        "source_files": current_engine.index.get("source_files", []),
                        "volumes": current_engine.index.get("volumes", []),
                        "works": current_engine.index.get("works", []),
                    })
                return
            if parsed.path == "/api/calibration-library":
                try:
                    self._send_json(calibration_library_data())
                except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                    self._send_json({"error": f"页码校准文献加载失败：{exc}"}, status=500)
                return
            if parsed.path == "/api/library":
                try:
                    requested_view = (parse_qs(parsed.query).get("view") or [""])[0]
                    payload = library_data()
                    if requested_view == "summary":
                        payload = summarize_library(payload)
                    self._send_json(payload)
                except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                    self._send_json({"error": f"文献库加载失败：{exc}"}, status=500)
                return
            if parsed.path == "/api/library/document":
                try:
                    source_id = (parse_qs(parsed.query).get("source_id") or [""])[0]
                    detail = build_library_detail(library_data(), source_id)
                    if detail is None:
                        self._send_json({"error": "文献不存在或已被移除。"}, status=404)
                    else:
                        self._send_json(detail)
                except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                    self._send_json({"error": f"文献详情加载失败：{exc}"}, status=500)
                return
            if parsed.path == "/api/scan-directories":
                try:
                    preferences = read_preferences(resolve_preferences_path(root))
                    directories = list(preferences.get("scan_directories") or [])
                    with runtime_lock:
                        sources = list(runtime["engine"].index.get("source_files", []))
                    imported_names = {
                        str(item.get("file_name")): int(item.get("size_bytes") or 0)
                        for item in sources
                        if item.get("file_name")
                    }
                    result = scan_directories_for_documents(directories, imported_names)
                    result["directories"] = directories
                    self._send_json(result)
                except Exception as exc:
                    self._send_json({"error": f"扫描文献目录失败：{exc}"}, status=500)
                return
            if parsed.path == "/api/import-status":
                params = parse_qs(parsed.query)
                job_id = (params.get("job_id") or [None])[0]
                with import_jobs_lock:
                    job = dict(import_jobs.get(str(job_id), {})) if job_id else {}
                public_job = public_import_job(job) if job else {}
                self._send_json(
                    public_job or {"error": "导入任务不存在。"},
                    status=200 if public_job else 404,
                )
                return
            if parsed.path == "/api/import-resumable":
                self._send_json({"jobs": resumable_import_jobs()})
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
            if parsed.path == "/api/bibliographic-metadata":
                params = parse_qs(parsed.query)
                sid = (params.get("source_id") or [None])[0]
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    _, _, document = configured_document(str(sid))
                    self._send_json({"ok": True, "metadata": canonical_metadata(document)})
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=404)
                return
            if parsed.path.startswith("/source/"):
                self._send_source(parsed.path)
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_HEAD(self) -> None:
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
            with runtime_lock:
                if runtime["closing"]:
                    self._send_json({"error": "应用正在关闭。"}, status=503)
                    return
            if parsed.path == "/api/import":
                filename = unquote(self.headers.get("X-File-Name", ""))
                suffix = Path(filename).suffix.lower()
                if suffix not in {".pdf", ".docx"}:
                    self._send_json({"error": "只支持 PDF 或 DOCX 文件。"}, status=400)
                    return
                try:
                    pdf_parse_mode, vision_provider_id = validate_upload_parse_options(
                        self.headers.get("X-PDF-Parse-Mode", "auto"),
                        self.headers.get("X-Vision-Provider-ID", ""),
                    )
                    length = int(self.headers.get("Content-Length", "0"))
                    logging.info(
                        "legacy import request received file=%r size=%d",
                        Path(filename).name,
                        length,
                    )
                    target = store_upload(filename, length, suffix == ".pdf", self.rfile)
                    logging.info(
                        "legacy import upload completed file=%r size=%d",
                        Path(filename).name,
                        length,
                    )
                    result = start_stored_upload_import(
                        target,
                        filename=filename,
                        is_pdf=suffix == ".pdf",
                        pdf_parse_mode=pdf_parse_mode,
                        vision_provider_id=vision_provider_id or None,
                    )
                    self._send_json(result)
                except (MinerUError, VisionAPIError, OSError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception:
                    logging.exception("legacy import request failed")
                    self._send_json({"error": "导入失败，请查看 desktop.log。"}, status=500)
                return
            if parsed.path == "/api/import-upload/chunk":
                try:
                    upload_id = str(self.headers.get("X-Upload-ID", ""))
                    offset = int(self.headers.get("X-Upload-Offset", "-1"))
                    length = int(self.headers.get("Content-Length", "0"))
                    if offset == 0:
                        logging.info(
                            "chunked import first chunk request upload_id=%s size=%d",
                            upload_id,
                            length,
                        )
                    progress = chunked_uploads.append(
                        upload_id,
                        offset,
                        length,
                        self.rfile,
                    )
                    if progress["first_chunk"]:
                        logging.info(
                            "chunked import first chunk stored upload_id=%s received=%d",
                            upload_id,
                            progress["received_size"],
                        )
                    if progress["complete"]:
                        logging.info(
                            "chunked import upload completed upload_id=%s size=%d",
                            upload_id,
                            progress["received_size"],
                        )
                    else:
                        logging.debug(
                            "chunked import progress upload_id=%s received=%d total=%d",
                            upload_id,
                            progress["received_size"],
                            progress["total_size"],
                        )
                    self._send_json({"ok": True, **progress})
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
                suffix = Path(filename).suffix.lower()
                if suffix not in {".pdf", ".docx"}:
                    self._send_json({"error": "只支持 PDF 或 DOCX 文件。"}, status=400)
                    return
                try:
                    total_size = int(payload.get("size") or 0)
                    pdf_parse_mode, vision_provider_id = validate_upload_parse_options(
                        payload.get("parse_mode", "auto"),
                        payload.get("provider_id", ""),
                    )
                    result = chunked_uploads.start(
                        filename,
                        total_size,
                        metadata={
                            "is_pdf": "1" if suffix == ".pdf" else "0",
                            "parse_mode": pdf_parse_mode,
                            "provider_id": vision_provider_id,
                        },
                    )
                    result.update({"file_name": Path(filename).name})
                    logging.info(
                        "chunked import session started upload_id=%s file=%r size=%d",
                        result["upload_id"],
                        Path(filename).name,
                        total_size,
                    )
                    self._send_json({"ok": True, **result})
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
                    cancelled = chunked_uploads.cancel(str(payload.get("upload_id") or ""))
                    self._send_json({"ok": True, "cancelled": cancelled})
                except ChunkedUploadError as exc:
                    self._send_json({"error": str(exc)}, status=exc.status)
                return
            if parsed.path == "/api/import-upload/finish":
                if not isinstance(payload, dict):
                    self._send_json({"error": "上传完成请求必须是 JSON 对象。"}, status=400)
                    return
                upload_id = str(payload.get("upload_id") or "")
                completed = None
                staged_path: Optional[Path] = None
                try:
                    completed = chunked_uploads.finish(upload_id)
                    staged_path = completed.temp_path
                    metadata = dict(completed.metadata)
                    is_pdf = metadata.get("is_pdf") == "1"
                    pdf_parse_mode, vision_provider_id = validate_upload_parse_options(
                        metadata.get("parse_mode", "auto"),
                        metadata.get("provider_id", ""),
                    )
                    logging.info(
                        "chunked import finalization started upload_id=%s file=%r size=%d",
                        completed.upload_id,
                        completed.filename,
                        completed.total_size,
                    )
                    target = store_completed_upload(
                        completed.filename,
                        completed.total_size,
                        is_pdf,
                        completed.temp_path,
                    )
                    staged_path = None
                    result = start_stored_upload_import(
                        target,
                        filename=completed.filename,
                        is_pdf=is_pdf,
                        pdf_parse_mode=pdf_parse_mode,
                        vision_provider_id=vision_provider_id,
                        upload_id=completed.upload_id,
                    )
                    self._send_json(result)
                except ChunkedUploadError as exc:
                    self._send_json({"error": str(exc)}, status=exc.status)
                except (MinerUError, VisionAPIError, OSError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception:
                    logging.exception("chunked import finalization failed")
                    self._send_json(
                        {"error": "导入失败，请查看 desktop.log。"},
                        status=500,
                    )
                finally:
                    if staged_path is not None:
                        try:
                            staged_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                return
            route_method = self._POST_ROUTE_TABLE.get(parsed.path)
            if route_method is not None:
                getattr(self, route_method)(payload)
                return
            if parsed.path == "/api/document/citation":
                if not isinstance(payload, dict):
                    self._send_json({"error": "引文请求必须是 JSON 对象。"}, status=400)
                    return
                allowed_fields = {
                    "source_id",
                    "start_anchor_id",
                    "end_anchor_id",
                }
                unexpected_fields = sorted(set(payload) - allowed_fields)
                if unexpected_fields:
                    self._send_json({"error": "引文请求包含不支持的字段。"}, status=400)
                    return
                if set(payload) != allowed_fields:
                    self._send_json(
                        {
                            "error": (
                                "source_id、start_anchor_id 和 end_anchor_id "
                                "必须各提供一次。"
                            )
                        },
                        status=400,
                    )
                    return
                try:
                    with runtime_lock:
                        if runtime["rebuilding"]:
                            citation_result = None
                        else:
                            citation_result = get_document_citation(
                                index_path,
                                payload["source_id"],
                                start_anchor_id=payload["start_anchor_id"],
                                end_anchor_id=payload["end_anchor_id"],
                            )
                except (
                    InvalidCitationRange,
                    InvalidPagination,
                    InvalidSourceId,
                    UnsupportedSourceType,
                ) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except (CitationPositionNotFound, SourceNotFound) as exc:
                    self._send_json({"error": str(exc)}, status=404)
                    return
                except (OSError, sqlite3.Error, StructuredReaderError):
                    logging.exception("structured reader citation request failed")
                    self._send_json(
                        {"error": "结构化阅读引文生成失败，请稍后重试。"},
                        status=500,
                    )
                    return
                if citation_result is None:
                    self._send_json(
                        {"error": "索引正在重建，请稍候再生成引文。"},
                        status=503,
                    )
                    return
                self._send_json(citation_result)
                return
            if parsed.path == "/api/preferences":
                preferences_path = resolve_preferences_path(root)
                try:
                    preferences = save_preferences(payload, preferences_path)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except OSError:
                    self._send_json({"error": "应用设置无法保存，请检查配置目录是否可写。"}, status=500)
                    return
                if "theme" in payload and native_theme_setter is not None:
                    try:
                        native_theme_setter(str(preferences["theme"]))
                    except Exception:
                        logging.exception("failed to apply native window theme")
                self._send_json({"ok": True, **preferences})
                return
            if parsed.path == "/api/update/check":
                if update_service is None:
                    self._send_json({"error": "当前运行方式不支持应用内更新。"}, status=400)
                    return
                self._send_json(update_service.check(
                    auto_download=payload.get("auto_download") is True
                ))
                return
            if parsed.path == "/api/update/download":
                if update_service is None:
                    self._send_json({"error": "当前运行方式不支持应用内更新。"}, status=400)
                    return
                self._send_json(update_service.download())
                return
            if parsed.path == "/api/update/install":
                if update_service is None:
                    self._send_json({"error": "当前运行方式不支持应用内更新。"}, status=400)
                    return
                self._send_json(update_service.install(payload.get("confirm_token")))
                return
            if parsed.path == "/api/scan-directories/choose":
                if native_scan_directory_chooser is None:
                    self._send_json(
                        {"error": "当前运行方式不支持打开文件夹选择器。"},
                        status=400,
                    )
                    return
                try:
                    selected_folders = native_scan_directory_chooser()
                except Exception as exc:  # noqa: BLE001 - surface any picker failure
                    self._send_json(
                        {"error": str(exc) or "打开文件夹选择器失败。"},
                        status=400,
                    )
                    return
                if not selected_folders:
                    self._send_json({"ok": True, "cancelled": True})
                    return
                if isinstance(selected_folders, (str, Path)):
                    candidates = [selected_folders]
                else:
                    candidates = list(selected_folders)
                folders: List[str] = []
                seen_folders: set[str] = set()
                for selected_folder in candidates:
                    folder = Path(str(selected_folder))
                    if not folder.is_dir():
                        self._send_json(
                            {"error": "所选路径不是文件夹。"},
                            status=400,
                        )
                        return
                    normalized = str(folder)
                    if normalized in seen_folders:
                        continue
                    seen_folders.add(normalized)
                    folders.append(normalized)
                if not folders:
                    self._send_json({"ok": True, "cancelled": True})
                    return
                self._send_json(
                    {
                        "ok": True,
                        "cancelled": False,
                        "folder": folders[0],
                        "folders": folders,
                    }
                )
                return
            if parsed.path == "/api/data-location/choose":
                if (
                    app_data_root is None
                    or default_app_data_root is None
                    or native_directory_chooser is None
                ):
                    self._send_json(
                        {"error": "当前运行方式不支持选择数据位置。"},
                        status=400,
                    )
                    return
                try:
                    selected_folder = native_directory_chooser()
                    if not selected_folder:
                        self._send_json({"ok": True, "cancelled": True})
                        return
                    target = proposed_data_root(selected_folder)
                except (DataLocationError, OSError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                self._send_json(
                    {
                        "ok": True,
                        "cancelled": False,
                        "selected_folder": str(selected_folder),
                        "target_path": str(target),
                    }
                )
                return
            if parsed.path == "/api/data-location/migrate":
                if app_data_root is None or default_app_data_root is None:
                    self._send_json(
                        {"error": "当前运行方式不支持迁移数据位置。"},
                        status=400,
                    )
                    return
                target_value = str(payload.get("target_path") or "").strip()
                if not target_value:
                    self._send_json({"error": "请先选择新的数据位置。"}, status=400)
                    return
                try:
                    with durable_operations.operation(), rebuild_lock:
                        # A job may have entered the queue while this request
                        # was waiting for the consistency locks.  Re-check only
                        # after both locks are held so migration snapshots a
                        # state that no import/rebuild can still mutate.
                        with import_jobs_lock:
                            has_active_job = any(
                                job.get("status") == "processing"
                                for job in import_jobs.values()
                            )
                        if has_active_job:
                            raise MinerUError(
                                "文献正在导入或索引正在更新，"
                                "请完成后再迁移。"
                            )
                        with runtime_lock:
                            result = migrate_data_root(
                                app_data_root,
                                Path(target_value),
                                default_app_data_root,
                            )
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=409)
                    return
                except DataLocationError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except (OSError, sqlite3.Error) as exc:
                    self._send_json(
                        {"error": f"迁移数据失败：{exc}"},
                        status=500,
                    )
                    return
                self._send_json(result)
                return
            if parsed.path == "/api/open-source":
                sid = str(payload.get("source_id") or "")
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    result = open_source_file(sid, payload.get("page"))
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"打开原文失败：{exc}"}, status=500)
                    return
                self._send_json(result)
                return
            if parsed.path == "/api/mineru-reparse":
                sid = str(payload.get("source_id") or "")
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    with runtime_lock:
                        record = runtime["source_files"].get(sid)
                    if not record or str(record.get("source_type")) != "pdf":
                        raise MinerUError("PDF 文献未找到。")
                    with import_jobs_lock:
                        running = next(
                            (
                                job for job in import_jobs.values()
                                if job.get("source_file_id") == sid and job.get("status") == "processing"
                            ),
                            None,
                        )
                    if running:
                        self._send_json({
                            "ok": True,
                            "job_id": running["job_id"],
                            "already_running": True,
                            "detected_pdf_type": running.get("detected_pdf_type"),
                        })
                        return
                    target = source_path_from_id(sid)
                    profile = detect_imported_pdf(target)
                    job_id = start_import_job(
                        target,
                        profile,
                        sid,
                        True,
                        force_mineru=True,
                        display_file_name=str(record.get("file_name") or ""),
                    )
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"提交 MinerU 解析失败：{exc}"}, status=500)
                    return
                self._send_json({
                    "ok": True,
                    "job_id": job_id,
                    "already_running": False,
                    "detected_pdf_type": profile.get("detected_pdf_type"),
                })
                return
            if parsed.path == "/api/import-retry":
                previous_job_id = str(payload.get("job_id") or "").strip()
                provider_id = str(payload.get("provider_id") or "").strip()
                if not previous_job_id or not provider_id:
                    self._send_json(
                        {"error": "缺少原任务或备用解析接口。"},
                        status=400,
                    )
                    return
                with import_jobs_lock:
                    previous_job = import_jobs.get(previous_job_id)
                    saved_context = import_job_contexts.get(previous_job_id)
                    context = dict(saved_context) if saved_context else None
                if not previous_job or not context:
                    self._send_json({"error": "原导入任务不存在。"}, status=404)
                    return
                if previous_job.get("status") != "failed":
                    self._send_json(
                        {"error": "只有失败的导入任务可以切换接口重试。"},
                        status=400,
                    )
                    return
                if not is_provider_retry_eligible(previous_job, context):
                    self._send_json(
                        {
                            "error": (
                                "该任务不是可切换接口的 PDF 解析失败；"
                                "索引失败和 Word 导入不能改走视觉解析 API。"
                            )
                        },
                        status=400,
                    )
                    return
                try:
                    target = validated_import_target(previous_job_id, context)
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                try:
                    summary = vision_config_summary(resolve_vision_config_path(root))
                    provider = next(
                        (
                            item
                            for item in summary.get("providers", [])
                            if isinstance(item, dict)
                            and str(item.get("id")) == provider_id
                            and item.get("enabled")
                            and item.get("configured")
                        ),
                        None,
                    )
                    if provider is None:
                        raise VisionAPIError("所选备用解析接口不可用。")
                    job_id = start_import_job(
                        target,
                        dict(context["profile"]),
                        str(context["source_file_id"]),
                        bool(context["is_pdf"]),
                        vision_provider_id=provider_id,
                        display_file_name=str(
                            previous_job.get("file_name") or ""
                        ),
                    )
                    dismiss_import_job(previous_job_id)
                except (MinerUError, VisionAPIError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                self._send_json(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "provider_id": provider_id,
                        "provider_name": provider.get("name"),
                        "parse_route": "vision",
                    }
                )
                return
            if parsed.path == "/api/import-resume":
                job_id = str(payload.get("job_id") or "").strip()
                if not job_id:
                    self._send_json({"error": "缺少待继续的导入任务。"}, status=400)
                    return
                try:
                    resumed = resume_import_job(job_id)
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                self._send_json(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "parse_route": resumed.get("parse_route"),
                        "provider_id": resumed.get("provider_id"),
                        "provider_name": resumed.get("provider_name"),
                    }
                )
                return
            if parsed.path == "/api/import-resume-dismiss":
                job_id = str(payload.get("job_id") or "").strip()
                if not job_id:
                    self._send_json({"error": "缺少待移除的导入任务。"}, status=400)
                    return
                try:
                    dismiss_import_job(job_id)
                except (MinerUError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                self._send_json({"ok": True, "job_id": job_id})
                return
            if parsed.path == "/api/bibliographic-metadata/batch-detect":
                with import_jobs_lock:
                    running = next(
                        (
                            job for job in import_jobs.values()
                            if str(job.get("job_id", "")).startswith("batchmeta-")
                            and job.get("status") == "processing"
                        ),
                        None,
                    )
                if running:
                    self._send_json({"ok": True, "job_id": running["job_id"], "already_running": True})
                    return
                try:
                    candidates = batch_metadata_candidates()
                except Exception as exc:
                    self._send_json({"error": f"筛选待识别文献失败：{exc}"}, status=500)
                    return
                if not candidates:
                    self._send_json({"ok": True, "job_id": None, "candidates": 0})
                    return
                job_id = f"batchmeta-{uuid.uuid4().hex[:12]}"
                with import_jobs_lock:
                    import_jobs[job_id] = {
                        "job_id": job_id,
                        "status": "processing",
                        "phase": "metadata_recognition",
                        "message": f"准备识别 {len(candidates)} 部文献…",
                    }
                try:
                    import_task_queue.submit(
                        run_batch_metadata_job,
                        job_id,
                        candidates,
                    )
                except Exception as exc:
                    update_import_job(
                        job_id,
                        status="failed",
                        phase="queue_failed",
                        message="批量识别任务未能进入处理队列。",
                    )
                    self._send_json(
                        {"error": str(exc), "job_id": job_id},
                        status=503,
                    )
                    return
                self._send_json({"ok": True, "job_id": job_id, "candidates": len(candidates), "already_running": False})
                return
            if parsed.path == "/api/backup/export":
                try:
                    self._send_json(export_runtime_backup())
                except (OSError, ValueError) as exc:
                    self._send_json({"error": f"导出备份失败：{exc}"}, status=500)
                return
            if parsed.path == "/api/backup/import":
                source_path = str(payload.get("path") or "").strip()
                if not source_path:
                    self._send_json({"error": "请填写备份文件路径。"}, status=400)
                    return
                try:
                    job_id = import_runtime_backup(source_path)
                except (MinerUError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except OSError as exc:
                    self._send_json({"error": f"读取备份失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True, "job_id": job_id})
                return
            if parsed.path == "/api/import-local":
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
                pdf_parse_mode = str(payload.get("pdf_parse_mode") or "auto").strip().lower()
                if pdf_parse_mode not in {"auto", "mineru", "vision"}:
                    self._send_json({"error": "PDF 解析方式无效。"}, status=400)
                    return
                vision_provider_id = (
                    str(payload.get("vision_provider_id") or "").strip()
                    if pdf_parse_mode == "vision"
                    else ""
                )
                if pdf_parse_mode == "vision" and not vision_provider_id:
                    self._send_json({"error": "请选择一个其他解析 API。"}, status=400)
                    return
                preferences = read_preferences(resolve_preferences_path(root))
                allowed_bases = [Path(item).resolve() for item in preferences.get("scan_directories") or []]
                prepared_items: List[Dict[str, object]] = []
                prepared_source_ids: set[str] = set()
                import_errors: List[Dict[str, object]] = []
                for raw in raw_paths:
                    item_reserved_source_id = ""
                    owned_target: Optional[Path] = None
                    try:
                        source_path = Path(str(raw)).resolve()
                        # 只允许导入配置目录内的文件，防止任意路径读取。
                        if not any(
                            base == source_path or base in source_path.parents
                            for base in allowed_bases
                        ):
                            raise MinerUError("不在已配置的文献目录内。")
                        if not source_path.is_file():
                            raise MinerUError("文件不存在。")
                        target = copy_local_document(root, source_path)
                        owned_target = target
                        is_pdf = target.suffix.lower() == ".pdf"
                        if is_pdf:
                            profile = detect_imported_pdf(target)
                            predicted_source_id = (
                                f"pdf-import-{sha256_file(target)[:16]}"
                            )
                            if predicted_source_id in prepared_source_ids:
                                raise MinerUError(
                                    "同一批次中已有内容相同的文献。"
                                )
                            (
                                document,
                                source_file_id,
                                target,
                            ) = register_pdf_for_import(
                                target,
                                original_file_name=source_path.name,
                            )
                            item_reserved_source_id = source_file_id
                        else:
                            profile = {"detected_pdf_type": "docx"}
                            source_file_id = f"docx-import-{uuid.uuid4().hex[:16]}"
                        if source_file_id in prepared_source_ids:
                            raise MinerUError("同一批次中已有内容相同的文献。")
                        with import_jobs_lock:
                            if any(
                                job.get("source_file_id") == source_file_id
                                and job.get("status") == "processing"
                                for job in import_jobs.values()
                            ):
                                raise MinerUError("同一文献已有解析任务正在运行。")
                        force_mineru = is_pdf and pdf_parse_mode == "mineru"
                        parse_route = None
                        if is_pdf:
                            parse_route = (
                                "vision"
                                if vision_provider_id
                                else "mineru"
                                if force_mineru or str(profile.get("detected_pdf_type")) != "native_text"
                                else "native"
                            )
                        prepared_items.append(
                            {
                                "target": target,
                                "owned_target": owned_target,
                                "profile": profile,
                                "source_file_id": source_file_id,
                                "source_reserved": is_pdf,
                                "is_pdf": is_pdf,
                                "force_mineru": force_mineru,
                                "vision_provider_id": vision_provider_id or None,
                                "parse_route": parse_route,
                                "display_file_name": source_path.name,
                                "response": {
                                    "path": str(raw),
                                    "file_name": source_path.name,
                                    "size_bytes": target.stat().st_size,
                                    "source_file_id": source_file_id,
                                    "detected_pdf_type": (
                                        profile.get("detected_pdf_type")
                                        if is_pdf
                                        else None
                                    ),
                                    "file_type": "pdf" if is_pdf else "docx",
                                    "parse_route": parse_route,
                                    "provider_id": vision_provider_id or None,
                                },
                            }
                        )
                        prepared_source_ids.add(source_file_id)
                        item_reserved_source_id = ""
                    except (MinerUError, VisionAPIError, OSError, ValueError) as exc:
                        if item_reserved_source_id:
                            release_import_reservation(item_reserved_source_id)
                        cleanup_unreferenced_import_target(owned_target)
                        import_errors.append({"path": str(raw), "error": str(exc)})
                    except Exception:
                        if item_reserved_source_id:
                            release_import_reservation(item_reserved_source_id)
                        cleanup_unreferenced_import_target(owned_target)
                        release_item_reservations(prepared_items)
                        raise

                try:
                    native_pdf_items = [
                        item
                        for item in prepared_items
                        if item["is_pdf"] and item["parse_route"] == "native"
                    ]
                    word_items = [
                        item
                        for item in prepared_items
                        if not item["is_pdf"]
                    ]
                    remote_items = [
                        item
                        for item in prepared_items
                        if item["is_pdf"] and item["parse_route"] != "native"
                    ]
                    native_pdf_job_ids = start_native_import_batch(
                        native_pdf_items
                    )
                    for item, job_id in zip(
                        native_pdf_items,
                        native_pdf_job_ids,
                    ):
                        item["response"]["job_id"] = job_id
                    word_job_ids = start_native_import_batch(word_items)
                    for item, job_id in zip(word_items, word_job_ids):
                        item["response"]["job_id"] = job_id
                    remote_job_ids = start_remote_import_batch(remote_items)
                    for item, job_id in zip(remote_items, remote_job_ids):
                        item["response"]["job_id"] = job_id
                finally:
                    release_item_reservations(prepared_items)
                    for item in prepared_items:
                        cleanup_unreferenced_import_target(
                            Path(item["owned_target"])
                            if item.get("owned_target")
                            else None
                        )
                jobs = [dict(item["response"]) for item in prepared_items]
                self._send_json({"ok": True, "jobs": jobs, "errors": import_errors})
                return
            if parsed.path == "/api/mineru-config":
                config_path = resolve_mineru_config_path(root)
                try:
                    summary = save_mineru_config(payload, config_path)
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except (OSError, json.JSONDecodeError):
                    self._send_json({"error": "本机配置文件无法保存，请检查应用目录是否可写。"}, status=500)
                    return
                self._send_json({"ok": True, **summary})
                return
            if parsed.path == "/api/mineru-config/test":
                config_path = resolve_mineru_config_path(root)
                try:
                    result = test_mineru_connection(config_path)
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except (OSError, ValueError):
                    self._send_json(
                        {"error": "无法读取本机 MinerU 配置，请检查配置目录。"},
                        status=500,
                    )
                    return
                self._send_json(result)
                return
            if parsed.path == "/api/vision-providers":
                config_path = resolve_vision_config_path(root)
                action = str(payload.get("action") or "").strip().lower()
                try:
                    if action == "save_provider":
                        provider = payload.get("provider")
                        if not isinstance(provider, dict):
                            raise VisionAPIError("解析接口配置格式无效。")
                        summary = save_vision_provider(provider, config_path)
                    elif action == "delete_provider":
                        summary = delete_vision_provider(
                            str(payload.get("provider_id") or ""),
                            config_path,
                        )
                    elif action == "save_policy":
                        summary = save_vision_policy(payload, config_path)
                    else:
                        raise VisionAPIError("不支持的配置操作。")
                except VisionAPIError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except (OSError, json.JSONDecodeError):
                    self._send_json(
                        {"error": "其他解析 API 配置无法保存，请检查配置目录。"},
                        status=500,
                    )
                    return
                self._send_json({"ok": True, **summary})
                return
            if parsed.path == "/api/vision-providers/models":
                provider = payload.get("provider")
                if not isinstance(provider, dict):
                    self._send_json(
                        {
                            "error": "解析接口配置格式无效。",
                            "manual_entry_allowed": True,
                        },
                        status=400,
                    )
                    return
                try:
                    result = discover_vision_models(
                        provider,
                        resolve_vision_config_path(root),
                    )
                except VisionAPIError as exc:
                    self._send_json(
                        {
                            "error": str(exc),
                            "manual_entry_allowed": True,
                        },
                        status=400,
                    )
                    return
                self._send_json({"ok": True, **result})
                return
            if parsed.path == "/api/vision-providers/test":
                provider_id = str(payload.get("provider_id") or "").strip()
                try:
                    result = test_vision_provider(
                        provider_id,
                        resolve_vision_config_path(root),
                    )
                except VisionAPIError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                self._send_json(result)
                return
            if parsed.path == "/api/calibration":
                sid = str(payload.get("source_id") or "")
                segments = payload.get("segments", [])
                if not sid or not isinstance(segments, list):
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    apply_manual_page_mapping(sid, segments)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"校准已保存，但索引重建失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True, "rebuilt": True})
                return
            if parsed.path == "/api/auto-page-mapping/detect":
                sid = str(payload.get("source_id") or "")
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    with rebuild_lock:
                        with runtime_lock:
                            calibration_active_sources.add(sid)
                        result = detect_auto_page_mapping(sid)
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"页码自动检测失败：{exc}"}, status=500)
                    return
                finally:
                    with runtime_lock:
                        calibration_active_sources.discard(sid)
                self._send_json({"ok": True, "result": result})
                return
            if parsed.path == "/api/documents/remove":
                sid = str(payload.get("source_id") or "")
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                with import_jobs_lock:
                    if sid in deleting_import_sources:
                        self._send_json(
                            {"error": "该文献正在删除，请勿重复操作。"},
                            status=409,
                        )
                        return
                    if sid in pending_import_sources:
                        self._send_json(
                            {"error": "该文献正在准备导入，请稍后再删除。"},
                            status=409,
                        )
                        return
                    running_import = next(
                        (
                            job
                            for job in import_jobs.values()
                            if job.get("source_file_id") == sid
                            and job.get("status") == "processing"
                        ),
                        None,
                    )
                    if running_import is not None:
                        self._send_json(
                            {"error": "该文献仍在解析中，请等待任务结束后再删除。"},
                            status=409,
                        )
                        return
                    deleting_import_sources.add(sid)
                    stale_job_ids = [
                        job_id
                        for job_id, job in import_jobs.items()
                        if job.get("source_file_id") == sid
                    ]
                result: Dict[str, object] = {}
                removal_committed = False
                removal_error: Optional[Exception] = None
                removal_error_status = 400
                journal_cleanup_warnings: List[str] = []
                try:
                    with rebuild_lock:
                        try:
                            with runtime_lock:
                                runtime["rebuilding"] = True
                                old_engine = runtime["engine"]
                                if hasattr(old_engine, "close"):
                                    old_engine.close()
                            with durable_operations.operation():
                                result = DocumentDeletionService(
                                    root, index_path
                                ).remove(
                                    sid,
                                    delete_generated_artifacts=bool(
                                        payload.get(
                                            "delete_generated_artifacts", True
                                        )
                                    ),
                                    delete_internal_copy=bool(
                                        payload.get("delete_internal_copy", False)
                                    ),
                                )
                            removal_committed = True
                        except (
                            ValueError,
                            OSError,
                            sqlite3.Error,
                            json.JSONDecodeError,
                        ) as exc:
                            removal_error = exc
                        except Exception as exc:
                            logging.exception("failed to remove document")
                            removal_error = exc
                            removal_error_status = 500
                        finally:
                            try:
                                recover_runtime_index()
                            except Exception as exc:
                                logging.exception(
                                    "document removed but search index reload failed"
                                )
                                with runtime_lock:
                                    runtime["rebuilding"] = False
                                if removal_error is None:
                                    removal_error = RuntimeError(
                                        "文献已删除，但索引重新载入失败；请重启应用。"
                                    )
                                    removal_error_status = 500

                    if removal_committed:
                        for stale_job_id in stale_job_ids:
                            try:
                                import_job_journal.delete_job(stale_job_id)
                            except (OSError, ValueError) as exc:
                                logging.warning(
                                    "failed to remove stale import journal %s: %s",
                                    stale_job_id,
                                    exc,
                                )
                                journal_cleanup_warnings.append(
                                    f"{stale_job_id}: {exc}"
                                )
                        with import_jobs_lock:
                            for stale_job_id in stale_job_ids:
                                import_jobs.pop(stale_job_id, None)
                                import_job_contexts.pop(stale_job_id, None)
                        if journal_cleanup_warnings:
                            result.setdefault("cleanup_warnings", [])
                            result["cleanup_warnings"] = [
                                *list(result.get("cleanup_warnings") or []),
                                *journal_cleanup_warnings,
                            ]

                    if removal_error is not None:
                        self._send_json(
                            {"error": str(removal_error) or "删除文献失败。"},
                            status=removal_error_status,
                        )
                        return
                    self._send_json(
                        {
                            "ok": True,
                            "result": result,
                            "event": "library_changed",
                        }
                    )
                finally:
                    with import_jobs_lock:
                        deleting_import_sources.discard(sid)
                return
            if parsed.path == "/api/documents/remove-batch":
                # 逐份删除会为每份文献整份复制索引；真实语料下 61 份就是 200GB 写入。
                raw_ids = payload.get("source_ids")
                if not isinstance(raw_ids, list) or not raw_ids:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                requested: List[str] = []
                for value in raw_ids:
                    text = str(value or "").strip()
                    if text and text not in requested:
                        requested.append(text)
                internal_ids = {
                    str(value or "").strip()
                    for value in (payload.get("internal_copy_source_ids") or [])
                }
                batch_failures: List[Dict[str, str]] = []
                accepted: List[str] = []
                with import_jobs_lock:
                    for candidate in requested:
                        if candidate in deleting_import_sources:
                            batch_failures.append(
                                {"source_id": candidate, "error": "该文献正在删除，请勿重复操作。"}
                            )
                            continue
                        if candidate in pending_import_sources:
                            batch_failures.append(
                                {"source_id": candidate, "error": "该文献正在准备导入，请稍后再删除。"}
                            )
                            continue
                        if any(
                            job.get("source_file_id") == candidate
                            and job.get("status") == "processing"
                            for job in import_jobs.values()
                        ):
                            batch_failures.append(
                                {"source_id": candidate, "error": "该文献仍在解析中，请等待任务结束后再删除。"}
                            )
                            continue
                        accepted.append(candidate)
                        deleting_import_sources.add(candidate)
                    accepted_set = set(accepted)
                    stale_job_ids = [
                        job_id
                        for job_id, job in import_jobs.items()
                        if job.get("source_file_id") in accepted_set
                    ]
                if not accepted:
                    self._send_json(
                        {
                            "error": batch_failures[0]["error"] if batch_failures else "没有可移除的文献。",
                            "failures": batch_failures,
                        },
                        status=409,
                    )
                    return
                result = {}
                removal_committed = False
                removal_error: Optional[Exception] = None
                removal_error_status = 400
                journal_cleanup_warnings = []
                try:
                    with rebuild_lock:
                        try:
                            with runtime_lock:
                                runtime["rebuilding"] = True
                                old_engine = runtime["engine"]
                                if hasattr(old_engine, "close"):
                                    old_engine.close()
                            with durable_operations.operation():
                                result = DocumentDeletionService(
                                    root, index_path
                                ).remove_many(
                                    accepted,
                                    delete_generated_artifacts=bool(
                                        payload.get("delete_generated_artifacts", True)
                                    ),
                                    internal_copy_ids=[
                                        candidate
                                        for candidate in accepted
                                        if candidate in internal_ids
                                    ],
                                )
                            removal_committed = True
                        except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                            removal_error = exc
                        except Exception as exc:
                            logging.exception("failed to remove documents")
                            removal_error = exc
                            removal_error_status = 500
                        finally:
                            try:
                                recover_runtime_index()
                            except Exception:
                                logging.exception(
                                    "documents removed but search index reload failed"
                                )
                                with runtime_lock:
                                    runtime["rebuilding"] = False
                                if removal_error is None:
                                    removal_error = RuntimeError(
                                        "文献已删除，但索引重新载入失败；请重启应用。"
                                    )
                                    removal_error_status = 500

                    if removal_committed:
                        removed_ids = set(result.get("removed_source_ids") or [])
                        committed_job_ids = [
                            job_id
                            for job_id in stale_job_ids
                            if str(import_jobs.get(job_id, {}).get("source_file_id") or "")
                            in removed_ids
                        ]
                        for stale_job_id in committed_job_ids:
                            try:
                                import_job_journal.delete_job(stale_job_id)
                            except (OSError, ValueError) as exc:
                                logging.warning(
                                    "failed to remove stale import journal %s: %s",
                                    stale_job_id,
                                    exc,
                                )
                                journal_cleanup_warnings.append(f"{stale_job_id}: {exc}")
                        with import_jobs_lock:
                            for stale_job_id in committed_job_ids:
                                import_jobs.pop(stale_job_id, None)
                                import_job_contexts.pop(stale_job_id, None)
                        if journal_cleanup_warnings:
                            result["cleanup_warnings"] = [
                                *list(result.get("cleanup_warnings") or []),
                                *journal_cleanup_warnings,
                            ]

                    if removal_error is not None:
                        self._send_json(
                            {"error": str(removal_error) or "删除文献失败。", "failures": batch_failures},
                            status=removal_error_status,
                        )
                        return
                    result["failures"] = [
                        *batch_failures,
                        *list(result.get("failures") or []),
                    ]
                    self._send_json(
                        {"ok": True, "result": result, "event": "library_changed"}
                    )
                finally:
                    with import_jobs_lock:
                        for candidate in accepted:
                            deleting_import_sources.discard(candidate)
                return
            if parsed.path == "/api/bibliographic-metadata/parse-cnki-citation":
                if not isinstance(payload, dict) or set(payload) != {"citation_text"}:
                    self._send_json(
                        {"error": "请求必须只包含 citation_text。"},
                        status=400,
                    )
                    return
                try:
                    metadata = parse_cnki_journal_citation(payload["citation_text"])
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                self._send_json({"ok": True, "metadata": metadata})
                return
            if parsed.path == "/api/bibliographic-metadata/lookup-cnki":
                if not isinstance(payload, dict) or set(payload) != {"metadata"}:
                    self._send_json({"error": "请求必须只包含 metadata。"}, status=400)
                    return
                query_metadata = payload.get("metadata")
                allowed_fields = {"title", "author", "publish_year", "journal_name", "doi", "issn"}
                if not isinstance(query_metadata, dict) or set(query_metadata) - allowed_fields:
                    self._send_json({"error": "知网查询字段无效。"}, status=400)
                    return
                if not cnki_lookup_lock.acquire(blocking=False):
                    self._send_json(
                        {"error": "已有知网查询正在进行，请稍候。", "code": "lookup_busy"},
                        status=409,
                    )
                    return
                try:
                    result = lookup_cnki_journal(query_metadata)
                except CNKILookupError as exc:
                    status = {
                        "invalid_query": 400,
                        "verification_required": 403,
                        "rate_limited": 429,
                        "timeout": 504,
                    }.get(exc.code, 502)
                    self._send_json(
                        {"error": str(exc), "code": exc.code, "open_url": exc.open_url},
                        status=status,
                    )
                    return
                finally:
                    cnki_lookup_lock.release()
                self._send_json({"ok": True, **result})
                return
            if parsed.path == "/api/bibliographic-metadata/cnki-candidate":
                if not isinstance(payload, dict) or set(payload) != {"candidate"}:
                    self._send_json({"error": "请求必须只包含 candidate。"}, status=400)
                    return
                candidate = payload.get("candidate")
                if not isinstance(candidate, dict) or set(candidate) != {"record_url"}:
                    self._send_json({"error": "知网候选字段无效。"}, status=400)
                    return
                if not cnki_lookup_lock.acquire(blocking=False):
                    self._send_json(
                        {"error": "已有知网查询正在进行，请稍候。", "code": "lookup_busy"},
                        status=409,
                    )
                    return
                try:
                    result = fetch_cnki_candidate(candidate)
                except CNKILookupError as exc:
                    status = {
                        "invalid_candidate": 400,
                        "verification_required": 403,
                        "rate_limited": 429,
                        "timeout": 504,
                    }.get(exc.code, 502)
                    self._send_json(
                        {"error": str(exc), "code": exc.code, "open_url": exc.open_url},
                        status=status,
                    )
                    return
                finally:
                    cnki_lookup_lock.release()
                self._send_json({"ok": True, **result})
                return
            if parsed.path == "/api/bibliographic-metadata/lookup-google-books":
                if not isinstance(payload, dict) or set(payload) != {"metadata"}:
                    self._send_json({"error": "请求必须只包含 metadata。"}, status=400)
                    return
                query_metadata = payload.get("metadata")
                allowed_fields = {"title", "author", "publish_year", "isbn"}
                if not isinstance(query_metadata, dict) or set(query_metadata) - allowed_fields:
                    self._send_json({"error": "图书查询字段无效。"}, status=400)
                    return
                # 复用同一把外部查询锁，保证同一时刻只有一个联网元数据请求。
                if not cnki_lookup_lock.acquire(blocking=False):
                    self._send_json(
                        {"error": "已有联网查询正在进行，请稍候。", "code": "lookup_busy"},
                        status=409,
                    )
                    return
                try:
                    result = lookup_book(query_metadata)
                except BookLookupError as exc:
                    status = {
                        "invalid_query": 400,
                        "rate_limited": 429,
                        "timeout": 504,
                    }.get(exc.code, 502)
                    self._send_json(
                        {"error": str(exc), "code": exc.code, "open_url": exc.open_url},
                        status=status,
                    )
                    return
                finally:
                    cnki_lookup_lock.release()
                self._send_json({"ok": True, **result})
                return
            if parsed.path == "/api/bibliographic-metadata/lookup-crossref":
                if not isinstance(payload, dict) or set(payload) != {"metadata"}:
                    self._send_json({"error": "请求必须只包含 metadata。"}, status=400)
                    return
                query_metadata = payload.get("metadata")
                allowed_fields = {"title", "author", "publish_year", "doi"}
                if not isinstance(query_metadata, dict) or set(query_metadata) - allowed_fields:
                    self._send_json({"error": "Crossref 查询字段无效。"}, status=400)
                    return
                if not cnki_lookup_lock.acquire(blocking=False):
                    self._send_json(
                        {"error": "已有联网查询正在进行，请稍候。", "code": "lookup_busy"},
                        status=409,
                    )
                    return
                try:
                    result = lookup_crossref(query_metadata)
                except CrossrefLookupError as exc:
                    status = {
                        "invalid_query": 400,
                        "rate_limited": 429,
                        "timeout": 504,
                    }.get(exc.code, 502)
                    self._send_json(
                        {"error": str(exc), "code": exc.code, "open_url": exc.open_url},
                        status=status,
                    )
                    return
                finally:
                    cnki_lookup_lock.release()
                self._send_json({"ok": True, **result})
                return
            if parsed.path == "/api/bibliographic-metadata/open-cnki":
                if not isinstance(payload, dict) or set(payload) != {"url"}:
                    self._send_json({"error": "请求必须只包含 url。"}, status=400)
                    return
                try:
                    open_external_cnki_url(payload.get("url"))
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except OSError as exc:
                    self._send_json({"error": f"打开知网页面失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/bibliographic-metadata/detect":
                sid = str(payload.get("source_id") or "")
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    metadata = detect_bibliographic_metadata(sid, force=bool(payload.get("force")))
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"书目信息识别失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True, "metadata": metadata})
                return
            if parsed.path == "/api/bibliographic-metadata/save":
                sid = str(payload.get("source_id") or "")
                metadata_payload = payload.get("metadata") or {}
                if not sid or not isinstance(metadata_payload, dict):
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    metadata = save_bibliographic_metadata(sid, metadata_payload)
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"书目信息保存失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True, "metadata": metadata})
                return
            if parsed.path == "/api/auto-page-mapping/apply":
                sid = str(payload.get("source_id") or "")
                segments = payload.get("segments") or []
                auto_mapping = payload.get("auto_mapping") or {}
                if not sid or not isinstance(segments, list) or not isinstance(auto_mapping, dict):
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    with rebuild_lock:
                        updated = apply_live_auto_mapping(
                            sid,
                            segments,
                            auto_mapping,
                            bool(payload.get("replace_manual")),
                        )
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=409)
                    return
                except Exception as exc:
                    self._send_json({"error": f"应用自动映射失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True, "updated": updated})
                return
            if parsed.path == "/api/auto-page-mapping/accept":
                sid = str(payload.get("source_id") or "")
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                job_id = f"auto-map-{uuid.uuid4().hex[:12]}"
                try:
                    with durable_operations.operation(), rebuild_lock:
                        segment_count = accept_auto_page_mapping(sid)
                        with import_jobs_lock:
                            import_jobs[job_id] = {
                                "job_id": job_id,
                                "status": "processing",
                                "phase": "rebuilding_index",
                                "message": "正在接受自动页码映射并重建索引…",
                            }
                        rebuild_runtime_index(job_id)
                        update_import_job(
                            job_id,
                            status="completed",
                            phase="completed",
                            message="自动页码映射已接受",
                        )
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    update_import_job(job_id, status="failed", phase="failed", message=str(exc))
                    self._send_json({"error": f"自动映射已保存失败或索引重建失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True, "segment_count": segment_count, "rebuilt": True})
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def _send_source(self, request_path: str, send_body: bool = True) -> None:
            source_id = unquote(request_path[len("/source/") :])
            with runtime_lock:
                record = runtime["source_files"].get(source_id)
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
        with runtime_lock:
            runtime["closing"] = True
        import_task_queue.shutdown(wait=False)

    def close_runtime(timeout: float = 2.0) -> bool:
        """Release the SQLite handle this handler holds open.

        The desktop app keeps its index open until the process exits, but a
        caller that outlives one handler -- notably a test using a temporary
        directory -- must be able to let go of the file.  Windows refuses to
        delete a database that still has an open connection.
        """

        begin_shutdown()
        chunked_uploads.close()
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
        with runtime_lock:
            current = runtime.get("engine")
            runtime["engine"] = None
        if current is not None:
            current.close()
        return True

    Handler.begin_shutdown = staticmethod(begin_shutdown)
    Handler.close_runtime = staticmethod(close_runtime)
    Handler.wait_for_durable_operations = staticmethod(durable_operations.wait)
    Handler._submit_background_task = staticmethod(import_task_queue.submit)
    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, index_path: Path = DEFAULT_DATABASE_PATH) -> None:
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

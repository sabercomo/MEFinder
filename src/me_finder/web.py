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
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .database import DEFAULT_DATABASE_PATH
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
from .mineru_api import MinerUError, mineru_config_summary, resolve_mineru_config_path, save_mineru_config
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
from .calibration_library import build_calibration_library, build_library
from .document_deletion import DocumentDeletionService
from .pdf_extractors import extract_pdf_source
from .pdf_import_service import (
    copy_local_document,
    detect_imported_pdf,
    parse_pdf_with_mineru,
    parse_pdf_with_provider,
    rebuild_local_index,
    register_pdf,
    save_import_config,
    scan_directories_for_documents,
)
from .runtime_page_mapping import apply_mapping_to_database, normalize_auto_segments
from .backup_service import restore_backup, write_backup
from .import_queue import ImportBatchCompletion, ImportTaskQueue
from .search import SearchEngine


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


def open_path_with_default_app(target: Path) -> None:
    """Open a local file with the platform's default application."""

    target = Path(target)
    if sys.platform == "win32":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return
    command = ["open", str(target)] if sys.platform == "darwin" else ["xdg-open", str(target)]
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


NativePDFOpener = Callable[[Path, Optional[int]], Dict[str, object]]
NativeThemeSetter = Callable[[str], None]
NativeDirectoryChooser = Callable[[], Optional[str]]


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
        except Exception as exc:
            native_error = exc
            logging.exception("native PDF reader failed; falling back to an external app")

    # Adobe is only required for the Windows page-jump feature (its /A page=N
    # switch); machines without Adobe still fall back to the default PDF app.
    if sys.platform == "win32" and page_number:
        adobe = find_adobe_pdf_app()
        if adobe is not None:
            args = [str(adobe), "/A", f"page={page_number}", str(target)]
            subprocess.Popen(args, close_fds=True)
            return {
                "ok": True,
                "app": str(adobe),
                "viewer_mode": "adobe",
                "page_jump": True,
                "file": target.name,
                "page": page_number,
            }

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


HTML = (
    _load_asset("templates/index.html")
    .replace("/*__APP_CSS__*/", _load_asset("static/app.css"), 1)
    .replace("//__APP_JS__", _load_asset("static/app.js"), 1)
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
    native_pdf_opener: NativePDFOpener | None = None,
    native_theme_setter: NativeThemeSetter | None = None,
    update_service: object | None = None,
    native_directory_chooser: NativeDirectoryChooser | None = None,
    native_scan_directory_chooser: NativeDirectoryChooser | None = None,
    app_data_root: Path | None = None,
    default_app_data_root: Path | None = None,
):
    engine = SearchEngine(index_path)
    root = Path(".").resolve()
    runtime = {
        "engine": engine,
        "source_files": {
            str(item.get("source_file_id")): item
            for item in engine.index.get("source_files", [])
            if item.get("source_file_id")
        },
        "index_metadata": engine.index.get("metadata", {}),
        "rebuilding": False,
    }
    runtime_lock = threading.RLock()
    rebuild_lock = threading.Lock()
    metadata_lock = threading.Lock()
    import_jobs: Dict[str, Dict[str, object]] = {}
    import_job_contexts: Dict[str, Dict[str, object]] = {}
    import_jobs_lock = threading.RLock()
    import_task_queue = ImportTaskQueue(worker_count=2)
    calibration_active_sources: set[str] = set()

    def update_import_job(job_id: str, **updates: object) -> None:
        with import_jobs_lock:
            job = import_jobs.get(job_id)
            if job is not None:
                job.update(updates)

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

    def reload_runtime_index() -> None:
        with runtime_lock:
            new_engine = SearchEngine(index_path)
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
        config = json.loads(config_path.read_text("utf-8")) if config_path.exists() else {"documents": []}
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

    def rebuild_runtime_index(job_id: str) -> None:
        with rebuild_lock:
            update_import_job(job_id, phase="rebuilding_index", message="正在重建本地 SQLite 索引…")
            with runtime_lock:
                runtime["rebuilding"] = True
                old_engine = runtime["engine"]
                if hasattr(old_engine, "close"):
                    old_engine.close()
            try:
                rebuild_local_index(root, lambda update: progress_import_job(job_id, update))
                with runtime_lock:
                    runtime["engine"] = SearchEngine(index_path)
                    runtime["source_files"] = {
                        str(item.get("source_file_id")): item
                        for item in runtime["engine"].index.get("source_files", [])
                        if item.get("source_file_id")
                    }
                    runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                    runtime["rebuilding"] = False
            except Exception:
                with runtime_lock:
                    runtime["engine"] = SearchEngine(index_path)
                    runtime["source_files"] = {
                        str(item.get("source_file_id")): item
                        for item in runtime["engine"].index.get("source_files", [])
                        if item.get("source_file_id")
                    }
                    runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                    runtime["rebuilding"] = False
                raise

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
                }
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
        try:
            use_vision = bool(is_pdf and vision_provider_id)
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
                            original_error=str(mineru_exc),
                        )
                        return False
                    fallback_id = str(fallback.get("id"))
                    fallback_name = str(fallback.get("name") or "其他视觉 API")
                    update_import_job(
                        job_id,
                        phase="vision_processing",
                        message=(
                            f"MinerU 解析失败，已按设置自动切换到 {fallback_name}…"
                        ),
                        parse_route="vision",
                        provider_id=fallback_id,
                        provider_name=fallback_name,
                        mineru_failed=True,
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
                                f"MinerU 解析失败：{mineru_exc}；自动切换到 "
                                f"{fallback_name} 后仍失败：{fallback_exc}"
                            ),
                            fallback_error=str(fallback_exc),
                            can_retry_with_provider=True,
                            retry_provider_id=fallback_id,
                            retry_provider_name=fallback_name,
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
            update_import_job(job_id, status="failed", phase="failed", message=str(exc))
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
            rebuild_runtime_index(job_id)
            finalize_import_job(job_id, source_file_id, is_pdf)
        except Exception as exc:
            update_import_job(job_id, status="failed", phase="failed", message=str(exc))

    def create_import_job(
        target: Path,
        profile: Dict[str, object],
        source_file_id: str,
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
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
            import_jobs[job_id] = {
                "job_id": job_id,
                "status": "processing",
                "phase": "stored",
                "message": "文件已保存，准备处理…",
                "file_name": target.name,
                "source_file_id": source_file_id,
                "detected_pdf_type": profile.get("detected_pdf_type") if is_pdf else None,
                "parse_route": parse_route,
                "force_mineru": bool(force_mineru),
                "provider_id": vision_provider_id,
                "provider_name": provider_name,
            }
            import_job_contexts[job_id] = {
                "target": Path(target),
                "source_file_id": source_file_id,
                "profile": dict(profile),
                "is_pdf": is_pdf,
            }
        return job_id

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

    def start_import_job(
        target: Path,
        profile: Dict[str, object],
        source_file_id: str,
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
    ) -> str:
        job_id = create_import_job(
            target,
            profile,
            source_file_id,
            is_pdf,
            force_mineru=force_mineru,
            vision_provider_id=vision_provider_id,
        )
        queue_import_job(
            job_id,
            target,
            profile,
            source_file_id,
            is_pdf,
            force_mineru=force_mineru,
            vision_provider_id=vision_provider_id,
        )
        return job_id

    def start_native_import_batch(
        items: List[Dict[str, object]],
    ) -> List[str]:
        """Index local-text PDFs and Word files together with one rebuild."""

        queued_items: List[Dict[str, object]] = []
        for item in items:
            job_id = create_import_job(
                Path(item["target"]),
                dict(item["profile"]),
                str(item["source_file_id"]),
                bool(item["is_pdf"]),
            )
            queued_items.append({**item, "job_id": job_id})

        if not queued_items:
            return []

        def run_native_batch() -> None:
            job_ids = [str(item["job_id"]) for item in queued_items]
            batch_size = len(job_ids)
            for job_id in job_ids:
                update_import_job(
                    job_id,
                    phase="rebuilding_index",
                    message=f"正在批量建立索引（共 {batch_size} 个文件）…",
                    parse_route="native",
                )
            try:
                rebuild_runtime_index(job_ids[0])
            except Exception as exc:
                for job_id in job_ids:
                    update_import_job(
                        job_id,
                        status="failed",
                        phase="failed",
                        message=f"批量重建索引失败：{exc}",
                    )
                return
            for item in queued_items:
                finalize_import_job(
                    str(item["job_id"]),
                    str(item["source_file_id"]),
                    bool(item["is_pdf"]),
                )

        import_task_queue.submit(run_native_batch)
        return [str(item["job_id"]) for item in queued_items]

    def start_remote_import_batch(
        items: List[Dict[str, object]],
    ) -> List[str]:
        """Parse OCR/VLM files with two workers, then rebuild the index once."""

        queued_items: List[Dict[str, object]] = []
        for item in items:
            vision_provider_id = (
                str(item["vision_provider_id"])
                if item.get("vision_provider_id")
                else None
            )
            job_id = create_import_job(
                Path(item["target"]),
                dict(item["profile"]),
                str(item["source_file_id"]),
                bool(item["is_pdf"]),
                force_mineru=bool(item["force_mineru"]),
                vision_provider_id=vision_provider_id,
            )
            queued_items.append(
                {
                    **item,
                    "job_id": job_id,
                    "vision_provider_id": vision_provider_id,
                }
            )

        if not queued_items:
            return []

        def finish_remote_batch(successful_items: List[object]) -> None:
            successful = [
                dict(item)
                for item in successful_items
                if isinstance(item, dict)
            ]
            if not successful:
                return
            batch_size = len(successful)
            for item in successful:
                update_import_job(
                    str(item["job_id"]),
                    phase="rebuilding_index",
                    message=f"解析完成，正在批量更新索引（共 {batch_size} 个文件）…",
                )
            representative = str(successful[0]["job_id"])
            try:
                rebuild_runtime_index(representative)
            except Exception as exc:
                for item in successful:
                    update_import_job(
                        str(item["job_id"]),
                        status="failed",
                        phase="failed",
                        message=f"文件已解析，但批量重建索引失败：{exc}",
                    )
                return
            for item in successful:
                finalize_import_job(
                    str(item["job_id"]),
                    str(item["source_file_id"]),
                    bool(item["is_pdf"]),
                )

        group = ImportBatchCompletion(len(queued_items), finish_remote_batch)

        def run_remote_item(item: Dict[str, object]) -> None:
            succeeded = prepare_import_job(
                str(item["job_id"]),
                Path(item["target"]),
                str(item["source_file_id"]),
                dict(item["profile"]),
                bool(item["is_pdf"]),
                bool(item["force_mineru"]),
                (
                    str(item["vision_provider_id"])
                    if item.get("vision_provider_id")
                    else None
                ),
            )
            if succeeded:
                update_import_job(
                    str(item["job_id"]),
                    phase="waiting_for_batch",
                    message="解析完成，等待同批文件后统一更新索引…",
                )
            group.finish(item, succeeded)

        for item in queued_items:
            import_task_queue.submit(run_remote_item, item)
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
        summary = restore_backup(root, path.read_bytes(), app_data_root=backup_app_data_root())
        job_id = f"restore-{uuid.uuid4().hex[:12]}"
        with import_jobs_lock:
            import_jobs[job_id] = {
                "job_id": job_id,
                "status": "processing",
                "phase": "rebuilding_index",
                "message": f"已恢复 {summary['count']} 项，正在重建索引…",
            }

        def run_restore_job() -> None:
            try:
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

        threading.Thread(target=run_restore_job, daemon=True).start()
        return job_id

    def store_upload(filename: str, length: int, is_pdf: bool, reader) -> Path:
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
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / safe_name
        if target.exists():
            target = directory / f"{Path(safe_name).stem} (imported-{uuid.uuid4().hex[:8]}){suffix}"
        temp_path = directory / f".{target.name}.{uuid.uuid4().hex}.uploading"
        remaining = length
        with temp_path.open("wb") as stream:
            while remaining > 0:
                chunk = reader.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise MinerUError("上传数据不完整。")
                stream.write(chunk)
                remaining -= len(chunk)
        temp_path.replace(target)
        return target

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
        config = json.loads(config_path.read_text("utf-8"))
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
            }
            manual_segments.append(clean)
        document.setdefault("page_mapping", {})
        document["page_mapping"]["segments"] = manual_segments
        document["page_mapping"]["validated_by"] = "auto_mapping_accepted"
        document["page_mapping"]["mapping_status"] = "manual_mapped"
        document["page_mapping"]["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_import_config(config_path, config)
        return len(manual_segments)

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
        config = json.loads(config_path.read_text("utf-8"))
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
        config_path = root / "config" / "pdf_imports.json"
        config = json.loads(config_path.read_text("utf-8"))
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
        try:
            updated = apply_mapping_to_database(
                index_path,
                source_id,
                cleaned,
                auto_mapping=auto_mapping,
                mapping_status=mapping_status,
            )
            reload_runtime_index()
            with runtime_lock:
                runtime["rebuilding"] = False
            return updated
        except Exception:
            save_import_config(config_path, original_config)
            with runtime_lock:
                runtime["engine"] = SearchEngine(index_path)
                runtime["source_files"] = {
                    str(item.get("source_file_id")): item
                    for item in runtime["engine"].index.get("source_files", [])
                    if item.get("source_file_id")
                }
                runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                runtime["rebuilding"] = False
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
        config = json.loads(config_path.read_text("utf-8"))
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

    def persist_bibliographic_metadata(source_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        with metadata_lock:
            config_path, config, document = configured_document(source_id)
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
            try:
                update_metadata_in_database(index_path, source_id, metadata)
                reload_runtime_index()
                with runtime_lock:
                    runtime["rebuilding"] = False
                return metadata
            except Exception:
                save_import_config(config_path, original_config)
                with runtime_lock:
                    runtime["engine"] = SearchEngine(index_path)
                    runtime["source_files"] = {
                        str(item.get("source_file_id")): item
                        for item in runtime["engine"].index.get("source_files", [])
                        if item.get("source_file_id")
                    }
                    runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                    runtime["rebuilding"] = False
                raise

    def save_bibliographic_metadata(source_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        _, _, document = configured_document(source_id)
        metadata = manual_metadata(payload, document)
        return persist_bibliographic_metadata(source_id, metadata)

    def batch_metadata_candidates() -> List[Dict[str, object]]:
        """PDF sources with missing bibliographic fields, excluding manual ones."""
        data = library_data()
        candidates = []
        for item in data.get("items", []):
            if str(item.get("source_type") or "") != "pdf":
                continue
            if not item.get("metadata_missing_fields"):
                continue
            nested = item.get("bibliographic_metadata")
            source = str((nested or {}).get("metadata_source") or item.get("metadata_source") or "")
            if source == "manual":
                continue
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

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
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
                    self._send_json(library_data())
                except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                    self._send_json({"error": f"文献库加载失败：{exc}"}, status=500)
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
                self._send_json(job or {"error": "导入任务不存在。"}, status=200 if job else 404)
                return
            if parsed.path == "/api/calibration":
                config_path = root / "config" / "pdf_imports.json"
                if not config_path.exists():
                    self._send_json({"documents": []})
                    return
                config = json.loads(config_path.read_text("utf-8"))
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
            if parsed.path in {"/", "/index.html"}:
                preferences_path = resolve_preferences_path(root)
                theme = read_preferences(preferences_path)["theme"]
                content_length = len(render_html(theme).encode("utf-8"))
                self._send(200, b"", "text/html; charset=utf-8", content_length=content_length, send_body=False)
                return
            self._send(404, b"", "text/plain; charset=utf-8", send_body=False)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/import":
                filename = unquote(self.headers.get("X-File-Name", ""))
                suffix = Path(filename).suffix.lower()
                if suffix not in {".pdf", ".docx"}:
                    self._send_json({"error": "只支持 PDF 或 DOCX 文件。"}, status=400)
                    return
                try:
                    pdf_parse_mode = str(self.headers.get("X-PDF-Parse-Mode", "auto")).strip().lower()
                    if pdf_parse_mode not in {"auto", "mineru", "vision"}:
                        raise MinerUError("PDF 解析方式无效。")
                    vision_provider_id = (
                        str(self.headers.get("X-Vision-Provider-ID", "")).strip()
                        if pdf_parse_mode == "vision"
                        else ""
                    )
                    if pdf_parse_mode == "vision" and not vision_provider_id:
                        raise MinerUError("请选择一个其他解析 API。")
                    length = int(self.headers.get("Content-Length", "0"))
                    target = store_upload(filename, length, suffix == ".pdf", self.rfile)
                    is_pdf = suffix == ".pdf"
                    if is_pdf:
                        profile = detect_imported_pdf(target)
                        document = register_pdf(root, target)
                        source_file_id = str(document["source_file_id"])
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
                    )
                    parse_route = None
                    if is_pdf:
                        parse_route = (
                            "vision"
                            if vision_provider_id
                            else "mineru"
                            if force_mineru or str(profile.get("detected_pdf_type")) != "native_text"
                            else "native"
                        )
                    self._send_json({
                        "ok": True,
                        "job_id": job_id,
                        "file_name": target.name,
                        "source_file_id": source_file_id,
                        "detected_pdf_type": profile.get("detected_pdf_type") if is_pdf else None,
                        "parse_route": parse_route,
                        "provider_id": vision_provider_id or None,
                    })
                except (MinerUError, VisionAPIError, OSError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception:
                    self._send_json({"error": "导入失败，请查看 desktop.log。"}, status=500)
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json({"error": "请求格式无效。"}, status=400)
                return
            if parsed.path == "/api/search":
                requested_limit = payload.get("limit", 10)
                search_limit: int | str
                if str(requested_limit).strip().lower() in {"all", "0"}:
                    search_limit = "all"
                else:
                    try:
                        search_limit = int(requested_limit)
                    except (TypeError, ValueError):
                        search_limit = 10
                with runtime_lock:
                    if runtime["rebuilding"]:
                        self._send_json({"error": "索引正在重建，请稍候再搜索。"}, status=503)
                        return
                    result = runtime["engine"].search(
                        payload.get("query", ""),
                        payload.get("mode", "auto"),
                        search_limit,
                        payload.get("source_type", "all"),
                        payload.get("source_file_id"),
                    )
                self._send_json(result)
                return
            if parsed.path == "/api/preferences":
                preferences_path = resolve_preferences_path(root)
                try:
                    preferences = save_preferences(payload, preferences_path)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except OSError:
                    self._send_json({"error": "外观设置无法保存，请检查配置目录是否可写。"}, status=500)
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
                    selected_folder = native_scan_directory_chooser()
                except Exception as exc:  # noqa: BLE001 - surface any picker failure
                    self._send_json(
                        {"error": str(exc) or "打开文件夹选择器失败。"},
                        status=400,
                    )
                    return
                if not selected_folder:
                    self._send_json({"ok": True, "cancelled": True})
                    return
                folder = Path(str(selected_folder))
                if not folder.is_dir():
                    self._send_json({"error": "所选路径不是文件夹。"}, status=400)
                    return
                self._send_json(
                    {"ok": True, "cancelled": False, "folder": str(folder)}
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
                with import_jobs_lock:
                    has_active_job = any(
                        job.get("status") == "processing"
                        for job in import_jobs.values()
                    )
                if has_active_job:
                    self._send_json(
                        {"error": "文献正在导入或索引正在更新，请完成后再迁移。"},
                        status=409,
                    )
                    return
                try:
                    with runtime_lock:
                        result = migrate_data_root(
                            app_data_root,
                            Path(target_value),
                            default_app_data_root,
                        )
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
                    job_id = start_import_job(target, profile, sid, True, force_mineru=True)
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
                    context = import_job_contexts.get(previous_job_id)
                if not previous_job or not context:
                    self._send_json({"error": "原导入任务不存在。"}, status=404)
                    return
                if previous_job.get("status") != "failed":
                    self._send_json(
                        {"error": "只有失败的导入任务可以切换接口重试。"},
                        status=400,
                    )
                    return
                target = Path(context["target"])
                if not target.is_file():
                    self._send_json({"error": "待重试的 PDF 已不存在。"}, status=404)
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
                    )
                except VisionAPIError as exc:
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
                threading.Thread(
                    target=run_batch_metadata_job,
                    args=(job_id, candidates),
                    daemon=True,
                ).start()
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
                import_errors: List[Dict[str, object]] = []
                for raw in raw_paths:
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
                        is_pdf = target.suffix.lower() == ".pdf"
                        if is_pdf:
                            profile = detect_imported_pdf(target)
                            document = register_pdf(root, target)
                            source_file_id = str(document["source_file_id"])
                        else:
                            profile = {"detected_pdf_type": "docx"}
                            source_file_id = f"docx-import-{uuid.uuid4().hex[:16]}"
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
                                "profile": profile,
                                "source_file_id": source_file_id,
                                "is_pdf": is_pdf,
                                "force_mineru": force_mineru,
                                "vision_provider_id": vision_provider_id or None,
                                "parse_route": parse_route,
                                "response": {
                                    "path": str(raw),
                                    "file_name": target.name,
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
                    except (MinerUError, VisionAPIError, OSError, ValueError) as exc:
                        import_errors.append({"path": str(raw), "error": str(exc)})

                native_items = [
                    item
                    for item in prepared_items
                    if not item["is_pdf"] or item["parse_route"] == "native"
                ]
                remote_items = [
                    item
                    for item in prepared_items
                    if item["is_pdf"] and item["parse_route"] != "native"
                ]
                native_job_ids = start_native_import_batch(native_items)
                for item, job_id in zip(native_items, native_job_ids):
                    item["response"]["job_id"] = job_id
                remote_job_ids = start_remote_import_batch(remote_items)
                for item, job_id in zip(remote_items, remote_job_ids):
                    item["response"]["job_id"] = job_id
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
                sid = payload.get("source_id")
                segments = payload.get("segments", [])
                config_path = root / "config" / "pdf_imports.json"
                if not config_path.exists() or not sid:
                    self._send_json({"error": "invalid request"})
                    return
                config = json.loads(config_path.read_text("utf-8"))
                doc = next((d for d in config.get("documents", []) if d.get("source_file_id") == sid), None)
                if not doc:
                    self._send_json({"error": "document not found"})
                    return
                if "page_mapping" not in doc:
                    doc["page_mapping"] = {}
                doc["page_mapping"]["segments"] = segments
                doc["page_mapping"]["validated_by"] = "manual_ui"
                doc["page_mapping"]["mapping_origin"] = "manual"
                doc["page_mapping"]["mapping_status"] = "manual_mapped"
                doc["page_mapping"]["updated_at"] = datetime.now(timezone.utc).isoformat()
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
                    update_import_job(job_id, status="completed", phase="completed", message="页码校准已生效")
                except Exception as exc:
                    update_import_job(job_id, status="failed", phase="failed", message=str(exc))
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
                    with runtime_lock:
                        calibration_active_sources.add(sid)
                    result = detect_auto_page_mapping(sid)
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"自动检测失败：{exc}"}, status=500)
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
                with runtime_lock:
                    runtime["rebuilding"] = True
                    old_engine = runtime["engine"]
                    if hasattr(old_engine, "close"):
                        old_engine.close()
                try:
                    result = DocumentDeletionService(root, index_path).remove(
                        sid,
                        delete_generated_artifacts=bool(payload.get("delete_generated_artifacts", True)),
                        delete_internal_copy=bool(payload.get("delete_internal_copy", False)),
                    )
                    with runtime_lock:
                        runtime["engine"] = SearchEngine(index_path)
                        runtime["source_files"] = {
                            str(item.get("source_file_id")): item
                            for item in runtime["engine"].index.get("source_files", [])
                            if item.get("source_file_id")
                        }
                        runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                        runtime["rebuilding"] = False
                except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                    with runtime_lock:
                        runtime["engine"] = SearchEngine(index_path)
                        runtime["source_files"] = {
                            str(item.get("source_file_id")): item
                            for item in runtime["engine"].index.get("source_files", [])
                            if item.get("source_file_id")
                        }
                        runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                        runtime["rebuilding"] = False
                    self._send_json({"error": str(exc)}, status=400)
                    return
                self._send_json({"ok": True, "result": result, "event": "library_changed"})
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
                    segment_count = accept_auto_page_mapping(sid)
                    with import_jobs_lock:
                        import_jobs[job_id] = {
                            "job_id": job_id,
                            "status": "processing",
                            "phase": "rebuilding_index",
                            "message": "正在接受自动页码映射并重建索引…",
                        }
                    rebuild_runtime_index(job_id)
                    update_import_job(job_id, status="completed", phase="completed", message="自动页码映射已接受")
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
            body = target.read_bytes() if send_body else b""
            self._send(200, body, content_type, content_length=target.stat().st_size, send_body=send_body)

        def log_message(self, format: str, *args) -> None:
            return

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, index_path: Path = DEFAULT_DATABASE_PATH) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(index_path))
    print(f"ME Finder running at http://{host}:{port}/")
    server.serve_forever()

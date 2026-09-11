"""Unified native document/link opening for desktop hosts.

Platform knowledge (Adobe registry probes, ``open``/``xdg-open``/``startfile``,
PDF open-mode preference) lives here — never in the HTTP business layer, which
only ever receives and calls capability hooks.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Union
from urllib.parse import urlparse

from .preferences import read_preferences

NativePDFOpener = Callable[[Path, Optional[int]], Dict[str, object]]
NativeThemeSetter = Callable[[str], None]
NativeDirectoryChooser = Callable[[], Optional[str]]
NativeExportDirectoryChooser = Callable[[], Optional[str]]
NativeScanDirectoryChooser = Callable[[], Optional[Union[str, Sequence[str]]]]
NativeBackupFileChooser = Callable[[], Optional[str]]




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



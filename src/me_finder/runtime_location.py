"""Resolve MEFinder bundle, application-data, and mutable runtime roots."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .data_location import (
    default_windows_data_root,
    read_data_root,
    read_macos_data_root,
)


APP_DATA_ROOT_ENV = "ME_FINDER_APP_DATA_ROOT"
PORTABLE_MARKER = "portable.flag"
INSTALLED_MARKER = "installed.flag"


def app_root() -> Path:
    """Return bundled resources when frozen, otherwise the source root."""

    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        if sys.platform == "darwin":
            contents_dir = executable_dir.parent
            if executable_dir.name == "MacOS" and contents_dir.name == "Contents":
                return contents_dir / "Resources"
        return executable_dir
    return Path(__file__).resolve().parents[2]


def is_portable_bundle(bundle_root: Path) -> bool:
    return bool(
        getattr(sys, "frozen", False)
        and (Path(bundle_root) / PORTABLE_MARKER).is_file()
    )


def installation_kind(bundle_root: Path) -> str:
    if not getattr(sys, "frozen", False):
        return "source"
    root = Path(bundle_root)
    if is_portable_bundle(root):
        return "portable"
    if (root / INSTALLED_MARKER).is_file():
        return "installed"
    return "standalone"


def installed_data_root_override(bundle_root: Path | None = None) -> Path | None:
    """Read the data directory selected by the Windows installer, if any."""

    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    root = Path(bundle_root) if bundle_root is not None else app_root()
    if is_portable_bundle(root):
        return None
    marker = root / "data_root.txt"
    try:
        raw = marker.read_bytes()
    except OSError:
        return None
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            text = raw.decode(encoding).strip()
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode(errors="replace").strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


def local_app_data_root(
    home: Path | None = None,
    *,
    bundle_root: Path | None = None,
) -> Path:
    """Resolve the current mutable application-data root."""

    configured_root = os.environ.get(APP_DATA_ROOT_ENV, "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    if sys.platform == "win32":
        installed_root = installed_data_root_override(bundle_root)
        local_app_data = os.environ.get("LOCALAPPDATA") or None
        if home is None and local_app_data is None and installed_root is not None:
            return installed_root
        default_root = default_windows_data_root(
            home,
            local_app_data=local_app_data,
        )
        return read_data_root(
            default_root,
            fallback_root=installed_root or default_root,
        )
    user_home = Path(home) if home is not None else Path.home()
    if sys.platform == "darwin":
        return read_macos_data_root(user_home)
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / "MEFinder"
    return user_home / ".local" / "share" / "MEFinder"


def runtime_root(
    bundle_root: Path | None = None,
    *,
    app_data_root: Path | None = None,
) -> Path:
    """Return the active mutable runtime root without creating it."""

    root = Path(bundle_root) if bundle_root is not None else app_root()
    if not getattr(sys, "frozen", False) or is_portable_bundle(root):
        return root
    data_root = (
        Path(app_data_root)
        if app_data_root is not None
        else local_app_data_root(bundle_root=root)
    )
    return data_root / "runtime"

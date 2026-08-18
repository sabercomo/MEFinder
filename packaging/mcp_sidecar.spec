# -*- mode: python ; coding: utf-8 -*-
"""Build the standalone STDIO MCP sidecar as a single executable."""

import os
import sys
from pathlib import Path

# This spec lives in packaging/; resolve every repo path from the repo root so
# PyInstaller 6.x (which resolves the Analysis script relative to the spec
# directory) finds the sources regardless of the current working directory.
ROOT = Path(SPECPATH).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.windows_version_info import write_windows_version_info


project_root = ROOT
target_arch = os.environ.get("MEFINDER_TARGET_ARCH") or None
codesign_identity = os.environ.get("MEFINDER_CODESIGN_IDENTITY") or None
if codesign_identity == "-":
    # Passing "-" asks PyInstaller for a hardened ad-hoc onefile signature.
    # Its extracted python.org framework keeps a different Team ID and macOS
    # rejects the load. Let PyInstaller use its normal ad-hoc path instead;
    # build_macos.sh signs the finished sidecar explicitly with "-".
    codesign_identity = None
version_info_path = None
if sys.platform == "win32":
    version_info_path = write_windows_version_info(
        ROOT / "build" / "mcp_windows_version_info.txt",
        file_description="MEFinder MCP Server",
        internal_name="MEFinderMCP",
        original_filename="MEFinderMCP.exe",
    )

a = Analysis(
    [str(ROOT / "mefinder_mcp.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (
            str(ROOT / "docs" / "contracts" / "v0.4.4-mcp-v1-tools.json"),
            "docs/contracts",
        ),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "doctest", "test"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MEFinderMCP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=target_arch,
    codesign_identity=codesign_identity,
    icon=str(ROOT / "assets" / "app_icon.ico") if sys.platform == "win32" else None,
    version=str(version_info_path) if version_info_path is not None else None,
)

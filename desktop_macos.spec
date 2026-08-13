# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller configuration for the native macOS .app bundle."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules
from src.me_finder import __version__


project_root = Path.cwd()
stage_root = project_root / "build" / "macos-stage"
target_arch = os.environ.get("MEFINDER_TARGET_ARCH") or None
codesign_identity = os.environ.get("MEFINDER_CODESIGN_IDENTITY") or None
app_version = os.environ.get("MEFINDER_APP_VERSION") or __version__
pdfkit_hiddenimports = collect_submodules("Quartz.PDFKit")

required_stage_files = (
    stage_root / "data" / "index.sqlite3",
    stage_root / "config" / "pdf_imports.json",
    stage_root / "config" / "mineru_api.local.example.json",
    stage_root / "app_icon.icns",
)
missing_stage_files = [str(path) for path in required_stage_files if not path.is_file()]
if missing_stage_files:
    raise SystemExit(
        "macOS staging files are missing. Run ./build_macos.sh instead:\n"
        + "\n".join(missing_stage_files)
    )

a = Analysis(
    ["desktop.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        ("src/me_finder/templates", "src/me_finder/templates"),
        ("src/me_finder/static", "src/me_finder/static"),
        (str(stage_root / "data"), "data"),
        (str(stage_root / "config"), "config"),
    ],
    hiddenimports=[
        "src",
        "src.me_finder",
        "src.me_finder.web",
        "src.me_finder.search",
        "src.me_finder.database",
        "src.me_finder.indexer",
        "src.me_finder.normalization",
        "src.me_finder.extractors",
        "src.me_finder.pdf_extractors",
        "src.me_finder.pdf_page_mapping",
        "src.me_finder.pdf_import_service",
        "src.me_finder.vision_api",
        "src.me_finder.preferences",
        "src.me_finder.macos_pdf_viewer",
        "webview",
        "webview.platforms.cocoa",
        "PyObjCTools.AppHelper",
        *pdfkit_hiddenimports,
        "bottle",
        "proxy_tools",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pydoc",
        "doctest",
        "xmlrpc",
        "pdb",
        "profile",
        "pstats",
        "test",
        "clr_loader",
        "pythonnet",
        "webview.platforms.edgechromium",
        "webview.platforms.mshtml",
        "webview.platforms.qt",
        "webview.platforms.gtk",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MEFinder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=target_arch,
    codesign_identity=codesign_identity,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MEFinder",
)

app = BUNDLE(
    coll,
    name="MEFinder.app",
    icon=str(stage_root / "app_icon.icns"),
    bundle_identifier="com.sabercomo.mefinder",
    version=app_version,
    info_plist={
        "CFBundleDisplayName": "文献原句定位器",
        "CFBundleName": "MEFinder",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
    },
)

# -*- mode: python ; coding: utf-8 -*-
# Windows 桌面版（pywebview 原生窗口）onedir 构建。
# 由 Windows 绿色版或安装版发布脚本调用；开发时也可直接运行 PyInstaller。
# UPX 不要开：压缩 WebView2/.NET DLL 会导致加载失败和杀软误报。

import sys
from pathlib import Path

# This spec lives in packaging/; PyInstaller 6.x resolves the Analysis script
# (and other relative paths) against the spec's own directory, so resolve every
# repo path from the repo root instead of relying on the current directory.
ROOT = Path(SPECPATH).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.windows_version_info import write_windows_version_info


version_info_path = write_windows_version_info(ROOT / 'build' / 'windows_version_info.txt')

a = Analysis(
    [str(ROOT / 'desktop.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / 'src' / 'me_finder' / 'templates'), 'src/me_finder/templates'),
        (str(ROOT / 'src' / 'me_finder' / 'static'), 'src/me_finder/static'),
    ],
    hiddenimports=[
        'src',
        'src.me_finder',
        'src.me_finder.web',
        'src.me_finder.search',
        'src.me_finder.database',
        'src.me_finder.indexer',
        'src.me_finder.normalization',
        'src.me_finder.extractors',
        'src.me_finder.pdf_extractors',
        'src.me_finder.pdf_page_mapping',
        'src.me_finder.pdf_import_service',
        'src.me_finder.vision_api',
        'src.me_finder.preferences',
        'src.me_finder.windows_desktop',
        'src.me_finder.update_service',
        'webview',
        'clr_loader',
        'pythonnet',
        'bottle',
        'proxy_tools',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'pydoc', 'doctest',
        'xmlrpc', 'pdb', 'profile', 'pstats', 'test',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='文献原句定位器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=str(ROOT / 'assets' / 'app_icon.ico'),
    version=str(version_info_path),
)

# COLLECT 目录名保持 ASCII，避免构建脚本处理中文路径；exe 名仍是中文。
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MEFinder',
)

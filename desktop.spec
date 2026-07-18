# -*- mode: python ; coding: utf-8 -*-
# 桌面版（pywebview 原生窗口）onedir 构建。
# 用 build_desktop.cmd 构建；直接构建：py -3 -m PyInstaller desktop.spec --clean --noconfirm
# UPX 不要开：压缩 WebView2/.NET DLL 会导致加载失败和杀软误报。

a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('src/me_finder/templates', 'src/me_finder/templates'),
        ('src/me_finder/static', 'src/me_finder/static'),
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
        'src.me_finder.preferences',
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
    icon='assets/app_icon.ico',
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

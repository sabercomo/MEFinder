# Desktop Packaging Plan — ME_Finder

> **实施状态（2026-07-10）：已完成。**
> 入口 `desktop.py`，构建配置 `desktop.spec`，构建脚本 `build_desktop.cmd`（`full` 参数额外复制语料）。
> 与原计划的差异：
> - dist 文件夹名用 ASCII 的 `MEFinder`（exe 仍叫 `文献原句定位器.exe`）——中文路径会让批处理脚本编码出错；
> - SQLite 索引在后台线程加载，窗口先显示加载页（不能让窗口空白）；
> - `--noconsole` 模式下无 stdout，日志写 exe 同目录 `desktop.log`，启动失败在窗口内显示错误页；
> - UPX 关闭（压缩 WebView2/.NET DLL 会导致加载失败与杀软误报）；
> - 构建脚本只复制 `config/pdf_imports.json`，绝不复制含私钥的 `mineru_api.local.json`。
> 已验证：打包 exe 启动、加载索引、搜索命中、`/source/` 打开原文均正常，仅监听 127.0.0.1。

## Goal

Package the ME_Finder web application as a standalone Windows `.exe` that:

1. Starts the local Python HTTP backend automatically
2. Opens a native window (not the system browser) showing the UI
3. Shuts down cleanly when the window is closed
4. Binds only to `127.0.0.1` (never `0.0.0.0`)

---

## Technology Choice: pywebview

**Why pywebview over Electron, Tauri, or PySide6:**

- The UI is already HTML/CSS/JS — pywebview wraps it with zero rewrite.
- pywebview uses the system WebView2 (Edge/Chromium on Windows 10+), so no
  bundled browser engine — small binary size.
- Python backend stays as-is; pywebview runs in the same process.
- Single `pip install pywebview` dependency.

**Rejected alternatives:**

| Option    | Reason rejected |
|-----------|----------------|
| PySide6   | User requirement: do not rewrite frontend to Qt |
| Electron  | Adds ~150 MB, requires Node toolchain |
| Tauri     | Requires Rust toolchain, complex IPC for Python backend |
| CEFPython | Abandoned, no Python 3.10+ wheels |

---

## Architecture

```
┌─────────────────────────────────────────────┐
│                   main.py                    │
│                                              │
│  1. Start ThreadingHTTPServer on 127.0.0.1   │
│     (in a daemon thread)                     │
│                                              │
│  2. webview.create_window(                   │
│       title="文献原句定位器",                  │
│       url="http://127.0.0.1:{port}/",        │
│       width=1280, height=820,                │
│       min_size=(900, 600)                    │
│     )                                        │
│                                              │
│  3. webview.start()  ← blocks until closed   │
│                                              │
│  4. server.shutdown()                        │
└─────────────────────────────────────────────┘
```

The HTTP server thread is a daemon — it dies when the main thread exits.
`webview.start()` blocks until the user closes the window, then we call
`server.shutdown()` for a clean stop.

---

## Port Selection

Use port 0 (OS-assigned) to avoid conflicts:

```python
server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
port = server.server_address[1]
```

---

## File Layout (one-folder build)

```
dist/
├── 文献原句定位器.exe
├── _internal/           ← PyInstaller internals
│   └── ...
├── data/
│   └── index.sqlite3    ← required, copied by build script
└── corpus/
    └── raw_pdf/         ← optional, for "Open PDF" feature
        └── *.pdf
```

The exe looks for `data/index.sqlite3` relative to its own directory.
Corpus PDFs are optional — the "Open PDF" button works only if they're present.

---

## PyInstaller Configuration

```
pyinstaller desktop.py \
    --name "文献原句定位器" \
    --onedir \
    --console       # use --noconsole for release
    --hidden-import src.me_finder.web \
    --hidden-import src.me_finder.search \
    --hidden-import src.me_finder.indexer \
    --hidden-import src.me_finder.normalization \
    --hidden-import src.me_finder.extractors \
    --hidden-import src.me_finder.pdf_extractors \
    --hidden-import src.me_finder.pdf_page_mapping \
    --hidden-import webview
```

For release: switch `--console` to `--noconsole` and add `--icon=icon.ico`.

---

## Implementation Steps

### Step 1: Install pywebview

```
pip install pywebview
```

pywebview on Windows uses EdgeChromium (WebView2) by default. WebView2 is
pre-installed on Windows 10 21H2+ and all Windows 11.

### Step 2: Create `desktop.py` entry point

- Detect app root (`sys.executable` parent when frozen, `__file__` parent otherwise)
- `os.chdir(app_root)` so relative paths in `web.py` work
- Validate `data/index.sqlite3` exists
- Start HTTP server on `127.0.0.1:0` in daemon thread
- Create pywebview window pointing to `http://127.0.0.1:{port}/`
- `webview.start()` blocks
- On exit: `server.shutdown()`

### Step 3: Verify one-folder build

```
pyinstaller desktop.spec --clean --noconfirm
xcopy data dist\data\ /E
dist\文献原句定位器.exe
```

Verify:
- Window opens without browser
- Search works
- Close window → process exits cleanly
- Port is not exposed to LAN

### Step 4: Evaluate one-file build

One-file extracts to a temp dir on each launch — slower startup, and
`data/index.sqlite3` must still be adjacent to the exe (not inside).
Evaluate whether the UX tradeoff is acceptable.

---

## Security

- Server binds to `127.0.0.1` only — never `0.0.0.0`.
- No authentication needed for local-only access.
- `_send_source` already validates paths stay within the project root.
- pywebview window does not expose DevTools in release builds.

---

## Dependencies

| Package      | Purpose           | Size impact |
|--------------|-------------------|-------------|
| pywebview    | Native window     | ~2 MB       |
| pyinstaller  | Build exe         | dev-only    |

All other imports are Python stdlib.

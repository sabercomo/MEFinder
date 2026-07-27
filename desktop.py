"""桌面入口：用 pywebview 原生窗口包装本地 Web UI。

开发模式运行：

    py -3 desktop.py

打包后（PyInstaller onedir）exe 在自身目录查找 data/index.sqlite3。
SQLite 索引约数百 MB，首次加载需等待一段时间，因此先显示加载页，后台加载完成后再切换。
服务只绑定 127.0.0.1，端口由系统自动分配。
日志写入 exe 同目录的 desktop.log（目录不可写时退回 %TEMP%）。
"""

from __future__ import annotations

import html
import logging
import os
import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path

import webview

from src.me_finder.preferences import DEFAULT_THEME, read_preferences

APP_TITLE = "文献原句定位器"
PORTABLE_MARKER = "portable.flag"

LOADING_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  :root {{
    --app-bg: {app_bg}; --text-primary: {text_primary};
    --text-secondary: {text_secondary}; --border: {border}; --accent: {accent};
  }}
  html, body { height: 100%; margin: 0; }
  body {
    display: flex; align-items: center; justify-content: center;
    background: var(--app-bg); color: var(--text-primary);
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  .box { text-align: center; }
  .spinner {
    width: 36px; height: 36px; margin: 0 auto 18px;
    border: 3px solid var(--border); border-top-color: var(--accent);
    border-radius: 50%; animation: spin 0.9s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  h1 { font-size: 17px; font-weight: 600; margin: 0 0 6px; }
  p { font-size: 13px; color: var(--text-secondary); margin: 0; }
</style>
</head>
<body>
  <div class="box">
    <div class="spinner"></div>
    <h1>正在加载索引</h1>
    <p>首次启动约需 20&#8211;30 秒，请稍候</p>
  </div>
</body>
</html>
"""

ERROR_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  html, body {{ height: 100%; margin: 0; }}
  :root {{
    --app-bg: {app_bg}; --surface: {surface}; --text-primary: {text_primary};
    --text-secondary: {text_secondary}; --border: {border}; --danger: {danger};
  }}
  body {{
    display: flex; align-items: center; justify-content: center;
    background: var(--app-bg); color: var(--text-primary);
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }}
  .box {{ max-width: 680px; padding: 32px; }}
  h1 {{ font-size: 17px; font-weight: 600; margin: 0 0 10px; color: var(--danger); }}
  pre {{
    font-size: 12px; color: var(--text-secondary); white-space: pre-wrap;
    word-break: break-all; background: var(--surface); border-radius: 10px;
    padding: 14px; border: 1px solid var(--border);
  }}
</style>
</head>
<body>
  <div class="box">
    <h1>{title}</h1>
    <pre>{detail}</pre>
  </div>
</body>
</html>
"""


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def local_app_data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "MEFinder"


def is_portable_bundle(bundle_root: Path) -> bool:
    return bool(getattr(sys, "frozen", False) and (Path(bundle_root) / PORTABLE_MARKER).is_file())


def webview_storage_path(root: Path, portable: bool) -> str | None:
    return str(Path(root) / "webview-data") if portable else None


def prepare_runtime_root(bundle_root: Path) -> Path:
    """Keep mutable corpus/index data outside the replaceable exe folder."""

    if not getattr(sys, "frozen", False):
        return bundle_root
    if is_portable_bundle(bundle_root):
        return bundle_root
    runtime_root = local_app_data_root() / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    for folder in ("data", "config", "corpus"):
        source = bundle_root / folder
        target = runtime_root / folder
        if not source.exists() or target.exists():
            continue
        shutil.copytree(source, target)
    for relative in (
        Path("data/index.sqlite3"),
        Path("config/pdf_imports.json"),
        Path("config/mineru_api.local.example.json"),
    ):
        source = bundle_root / relative
        target = runtime_root / relative
        if source.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return runtime_root


def theme_palette(theme: str) -> dict[str, str]:
    palettes = {
        "frost-blue": ("#F5F8FC", "#FFFFFF", "#172033", "#667085", "#DDE5EF", "#1677FF"),
        "sage-ivory": ("#F7F7F1", "#FFFDF8", "#25291F", "#6E7464", "#DEE1D4", "#637A50"),
        "warm-sand": ("#FBF7F1", "#FFFCF8", "#34251E", "#7C695E", "#E7D9CC", "#B85C2B"),
        "rose-mist": ("#FDF6F8", "#FFFFFF", "#2C2528", "#71666A", "#EBDCE2", "#C9446A"),
        "lavender-purple": ("#F9F7FD", "#FFFFFF", "#282532", "#6E697A", "#DED8EB", "#7B5EC7"),
        "midnight": ("#08111D", "#111C29", "#EEF4FB", "#A8B4C4", "#2A394A", "#2485FF"),
    }
    app_bg, surface, text_primary, text_secondary, border, accent = palettes.get(
        theme, palettes[DEFAULT_THEME]
    )
    return {
        "app_bg": app_bg,
        "surface": surface,
        "text_primary": text_primary,
        "text_secondary": text_secondary,
        "border": border,
        "accent": accent,
        "danger": "#FF6673" if theme == "midnight" else "#D62C3A",
    }


def loading_html(theme: str = DEFAULT_THEME) -> str:
    rendered = LOADING_HTML
    for name, value in theme_palette(theme).items():
        rendered = rendered.replace("{" + name + "}", value)
    return rendered


def error_html(title: str, detail: str, theme: str = DEFAULT_THEME) -> str:
    return ERROR_HTML.format(
        title=html.escape(title), detail=html.escape(detail), **theme_palette(theme)
    )


def setup_logging(root: Path) -> None:
    handlers = []
    for log_dir in (root, Path(tempfile.gettempdir())):
        try:
            handlers.append(logging.FileHandler(log_dir / "desktop.log", encoding="utf-8"))
            break
        except OSError:
            continue
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers or None,
    )


def main() -> None:
    bundle_root = app_root()
    portable = is_portable_bundle(bundle_root)
    root = prepare_runtime_root(bundle_root)
    os.chdir(root)
    if getattr(sys, "frozen", False) and not portable:
        mineru_config_path = local_app_data_root() / "mineru_api.local.json"
        os.environ["ME_FINDER_MINERU_CONFIG"] = str(mineru_config_path)
        vision_config_path = local_app_data_root() / "vision_api.local.json"
        os.environ["ME_FINDER_VISION_CONFIG"] = str(vision_config_path)
        preferences_path = local_app_data_root() / "preferences.json"
        os.environ["ME_FINDER_PREFERENCES"] = str(preferences_path)
    elif portable:
        preferences_path = root / "config" / "preferences.json"
        os.environ["ME_FINDER_MINERU_CONFIG"] = str(root / "config" / "mineru_api.local.json")
        os.environ["ME_FINDER_VISION_CONFIG"] = str(root / "config" / "vision_api.local.json")
        os.environ["ME_FINDER_PREFERENCES"] = str(preferences_path)
    else:
        preferences_path = root / "config" / "preferences.json"
        os.environ.setdefault("ME_FINDER_MINERU_CONFIG", str(root / "config" / "mineru_api.local.json"))
        os.environ.setdefault("ME_FINDER_VISION_CONFIG", str(root / "config" / "vision_api.local.json"))
        os.environ.setdefault("ME_FINDER_PREFERENCES", str(preferences_path))
    theme = read_preferences(preferences_path)["theme"]
    setup_logging(root)
    logging.info("app root: %s", root)
    if root != bundle_root:
        logging.info("bundle root: %s", bundle_root)

    window = webview.create_window(
        APP_TITLE,
        html=loading_html(theme),
        width=1280,
        height=820,
        min_size=(900, 600),
    )
    state = {"server": None}

    def start_backend(win) -> None:
        try:
            index_path = root / "data" / "index.sqlite3"
            if not index_path.exists():
                logging.error("index not found: %s", index_path)
                win.load_html(error_html(
                    "未找到索引数据库 data/index.sqlite3",
                    "请把 index.sqlite3 放到：\n%s\n\n"
                    "索引数据库可在项目目录用命令生成：\n"
                    "py -3 -m src.me_finder build-index" % index_path,
                    theme,
                ))
                return
            logging.info("loading index from %s", index_path)
            from http.server import ThreadingHTTPServer

            from src.me_finder.web import make_handler

            handler = make_handler(index_path)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            state["server"] = server
            port = int(server.server_address[1])
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = "http://127.0.0.1:%d/" % port
            logging.info("backend ready at %s", url)
            win.load_url(url)
        except Exception:
            logging.exception("backend failed to start")
            win.load_html(error_html("后台启动失败", traceback.format_exc(), theme))

    webview.start(start_backend, window, storage_path=webview_storage_path(root, portable))
    server = state["server"]
    if server is not None:
        server.shutdown()
    logging.info("window closed, exiting")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("fatal error")
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                "程序启动失败，详情见 desktop.log。\n\n" + traceback.format_exc(),
                APP_TITLE,
                0x10,
            )
        except Exception:
            pass
        sys.exit(1)

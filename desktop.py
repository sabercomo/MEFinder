"""桌面入口：用 pywebview 原生窗口包装本地 Web UI。

开发模式运行：

    python3 desktop.py

打包后从 Windows onedir 目录或 macOS .app Resources 读取初始资源，
再把可变数据放到对应平台的用户数据目录。
SQLite 索引约数百 MB，首次加载需等待一段时间，因此先显示加载页，后台加载完成后再切换。
服务只绑定 127.0.0.1，端口由系统自动分配。
日志写入运行时数据目录的 desktop.log（目录不可写时退回系统临时目录）。
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

from src.me_finder import __version__
from src.me_finder.data_location import (
    default_macos_data_root,
    default_windows_data_root,
    read_data_root,
    read_macos_data_root,
)
from src.me_finder.preferences import DEFAULT_THEME, read_preferences

APP_TITLE = "文献原句定位器"
PORTABLE_MARKER = "portable.flag"
INSTALLED_MARKER = "installed.flag"
DESKTOP_SHELL_ENV = "ME_FINDER_DESKTOP_SHELL"
MACOS_TITLEBAR_HEIGHT = 28.0
MACOS_TRAFFIC_LIGHT_SAFE_WIDTH = 82.0

_macos_drag_strip_class = None

WINDOWS_BOOTSTRAP_TITLEBAR = """
<div class="windows-titlebar" role="banner" aria-label="窗口标题栏">
  <div class="windows-titlebar-drag pywebview-drag-region" ondblclick="toggleWindowsMaximize()">
    <span class="windows-titlebar-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3.5h9l5 5V20a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1Z"/><path d="M14 3.5V9h5"/><circle cx="9" cy="13.5" r="2.4"/><path d="m10.8 15.3 2.1 2.1"/></svg>
    </span>
    <span class="windows-titlebar-title">文献原句定位器</span>
  </div>
  <div class="windows-titlebar-controls">
    <button type="button" aria-label="最小化窗口" title="最小化" onclick="minimizeWindowsWindow()"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 8.5h8"/></svg></button>
    <button class="windows-maximize-button" type="button" aria-label="最大化窗口" title="最大化" onclick="toggleWindowsMaximize()"><svg class="windows-maximize-icon" viewBox="0 0 12 12" aria-hidden="true"><rect x="2.25" y="2.25" width="7.5" height="7.5"/></svg><svg class="windows-restore-icon" viewBox="0 0 12 12" aria-hidden="true"><path d="M4 3V1.75h6.25V8H9M1.75 4h6.25v6.25H1.75Z"/></svg></button>
    <button class="windows-close-button" type="button" aria-label="关闭窗口" title="关闭" onclick="closeWindowsWindow()"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="m2.25 2.25 7.5 7.5m0-7.5-7.5 7.5"/></svg></button>
  </div>
</div>
"""

WINDOWS_BOOTSTRAP_CSS = """
  html[data-desktop-shell="win32"] { --windows-titlebar-height: 40px; }
  .windows-titlebar {
    position: fixed; inset: 0 0 auto 0; z-index: 1000;
    display: flex; align-items: stretch; height: var(--windows-titlebar-height);
    color: var(--text-primary);
    background: linear-gradient(to right, var(--sidebar-bg) 0, var(--sidebar-bg) 220px, var(--app-bg) 220px, var(--app-bg) 100%);
    border-bottom: 1px solid var(--border); user-select: none;
  }
  .windows-titlebar-drag {
    display: flex; flex: 1; align-items: center; min-width: 0; padding: 0 14px;
  }
  .windows-titlebar-icon { display: grid; place-items: center; width: 18px; height: 18px; margin-right: 8px; color: var(--accent); pointer-events: none; }
  .windows-titlebar-icon svg { width: 17px; height: 17px; }
  .windows-titlebar-title { overflow: hidden; font-size: 13px; font-weight: 600; line-height: 1; text-overflow: ellipsis; white-space: nowrap; pointer-events: none; }
  .windows-titlebar-controls { display: flex; flex: 0 0 auto; height: 100%; }
  .windows-titlebar-controls button { display: grid; place-items: center; width: 46px; height: 100%; padding: 0; border: 0; color: var(--text-primary); background: transparent; }
  .windows-titlebar-controls button:hover { background: color-mix(in srgb, var(--text-primary) 9%, transparent); }
  .windows-titlebar-controls .windows-close-button:hover { color: #fff; background: #c42b1c; }
  .windows-titlebar-controls svg { width: 12px; height: 12px; fill: none; stroke: currentColor; stroke-width: 1.2; }
  .windows-restore-icon { display: none; }
  html.windows-maximized .windows-maximize-icon { display: none; }
  html.windows-maximized .windows-restore-icon { display: block; }
  body.windows-shell { box-sizing: border-box; padding-top: var(--windows-titlebar-height); }
"""

WINDOWS_BOOTSTRAP_SCRIPT = """
<script>
  window.setWindowsMaximized = function(maximized) {
    document.documentElement.classList.toggle('windows-maximized', !!maximized);
    var button = document.querySelector('.windows-maximize-button');
    if (button) {
      button.setAttribute('aria-label', maximized ? '还原窗口' : '最大化窗口');
      button.title = maximized ? '还原' : '最大化';
    }
  };
  function callWindowsWindow(method) {
    if (!window.pywebview || !window.pywebview.api || typeof window.pywebview.api[method] !== 'function') return Promise.resolve(false);
    return window.pywebview.api[method]();
  }
  function minimizeWindowsWindow() { callWindowsWindow('minimize'); }
  function toggleWindowsMaximize() { callWindowsWindow('toggle_maximize').then(window.setWindowsMaximized); }
  function closeWindowsWindow() { callWindowsWindow('close'); }
  window.addEventListener('pywebviewready', function() { callWindowsWindow('is_maximized').then(window.setWindowsMaximized); });
</script>
"""

LOADING_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  :root {{
    --app-bg: {app_bg}; --sidebar-bg: {sidebar_bg}; --text-primary: {text_primary};
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
    --app-bg: {app_bg}; --sidebar-bg: {sidebar_bg}; --surface: {surface}; --text-primary: {text_primary};
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
        executable_dir = Path(sys.executable).resolve().parent
        if sys.platform == "darwin":
            contents_dir = executable_dir.parent
            if executable_dir.name == "MacOS" and contents_dir.name == "Contents":
                return contents_dir / "Resources"
        return executable_dir
    return Path(__file__).resolve().parent


def installed_data_root_override(bundle_root: Path | None = None) -> Path | None:
    """Read the data directory chosen in the Windows installer, if any.

    The installer writes ``data_root.txt`` beside the executable so a silent
    self-update (which never shows wizard pages) keeps using the directory the
    user picked on first install instead of resetting to the default.
    """

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


def local_app_data_root(home: Path | None = None) -> Path:
    configured_root = os.environ.get("ME_FINDER_APP_DATA_ROOT", "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    if sys.platform == "win32":
        installed_root = installed_data_root_override()
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


def python_launcher() -> str:
    return "py -3" if os.name == "nt" else "python3"


def is_portable_bundle(bundle_root: Path) -> bool:
    return bool(getattr(sys, "frozen", False) and (Path(bundle_root) / PORTABLE_MARKER).is_file())


def installation_kind(bundle_root: Path) -> str:
    if not getattr(sys, "frozen", False):
        return "source"
    bundle_root = Path(bundle_root)
    if is_portable_bundle(bundle_root):
        return "portable"
    if (bundle_root / INSTALLED_MARKER).is_file():
        return "installed"
    return "standalone"


def webview_storage_path(root: Path, portable: bool) -> str:
    # Keep WebView2 cookies/cache with the selected application data root.
    # Otherwise WebView2 may silently fall back to a profile under LOCALAPPDATA.
    return str(Path(root) / "webview-data")


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
        "frost-blue": ("#F5F8FC", "#F1F5FA", "#FFFFFF", "#172033", "#667085", "#DDE5EF", "#1677FF"),
        "sage-ivory": ("#F7F7F1", "#F0F2E8", "#FFFDF8", "#25291F", "#6E7464", "#DEE1D4", "#637A50"),
        "warm-sand": ("#FBF7F1", "#F6EFE5", "#FFFCF8", "#34251E", "#7C695E", "#E7D9CC", "#B85C2B"),
        "rose-mist": ("#FDF6F8", "#FAF0F3", "#FFFFFF", "#2C2528", "#71666A", "#EBDCE2", "#C9446A"),
        "lavender-purple": ("#F9F7FD", "#F3F0F9", "#FFFFFF", "#282532", "#6E697A", "#DED8EB", "#7B5EC7"),
        "midnight": ("#08111D", "#091522", "#111C29", "#EEF4FB", "#A8B4C4", "#2A394A", "#2485FF"),
    }
    app_bg, sidebar_bg, surface, text_primary, text_secondary, border, accent = palettes.get(
        theme, palettes[DEFAULT_THEME]
    )
    return {
        "app_bg": app_bg,
        "sidebar_bg": sidebar_bg,
        "surface": surface,
        "text_primary": text_primary,
        "text_secondary": text_secondary,
        "border": border,
        "accent": accent,
        "danger": "#FF6673" if theme == "midnight" else "#D62C3A",
    }


def _decorate_bootstrap_html(rendered: str, desktop_shell: str | None) -> str:
    if str(desktop_shell or "").lower() != "win32":
        return rendered
    rendered = rendered.replace(
        '<html lang="zh-CN">',
        '<html lang="zh-CN" data-desktop-shell="win32">',
        1,
    )
    rendered = rendered.replace("</style>", WINDOWS_BOOTSTRAP_CSS + "\n</style>", 1)
    rendered = rendered.replace(
        "<body>",
        '<body class="windows-shell">\n' + WINDOWS_BOOTSTRAP_TITLEBAR,
        1,
    )
    return rendered.replace("</body>", WINDOWS_BOOTSTRAP_SCRIPT + "\n</body>", 1)


def loading_html(
    theme: str = DEFAULT_THEME, desktop_shell: str | None = None
) -> str:
    rendered = LOADING_HTML
    for name, value in theme_palette(theme).items():
        rendered = rendered.replace("{" + name + "}", value)
    return _decorate_bootstrap_html(rendered, desktop_shell)


def error_html(
    title: str,
    detail: str,
    theme: str = DEFAULT_THEME,
    desktop_shell: str | None = None,
) -> str:
    rendered = ERROR_HTML.format(
        title=html.escape(title), detail=html.escape(detail), **theme_palette(theme)
    )
    return _decorate_bootstrap_html(rendered, desktop_shell)


def _install_macos_native_drag_strip(window, native_window, appkit) -> None:
    """Keep window dragging native even if the WebKit bridge is unavailable."""

    global _macos_drag_strip_class

    if hasattr(native_window, "setMovable_"):
        native_window.setMovable_(True)

    content_view = native_window.contentView()
    if content_view is None or not hasattr(appkit, "NSView"):
        return
    if getattr(window, "_mefinder_native_drag_strip", None) is not None:
        return

    if _macos_drag_strip_class is None:
        class MEFinderNativeDragStrip(appkit.NSView):
            def acceptsFirstMouse_(self, _event):
                return True

            def mouseDownCanMoveWindow(self):
                return True

            def mouseDown_(self, event):
                host_window = self.window()
                if host_window is not None and hasattr(
                    host_window, "performWindowDragWithEvent_"
                ):
                    host_window.performWindowDragWithEvent_(event)

        _macos_drag_strip_class = MEFinderNativeDragStrip

    drag_strip = _macos_drag_strip_class.alloc().initWithFrame_(
        appkit.NSMakeRect(
            MACOS_TRAFFIC_LIGHT_SAFE_WIDTH,
            0.0,
            max(
                0.0,
                float(content_view.bounds().size.width)
                - MACOS_TRAFFIC_LIGHT_SAFE_WIDTH,
            ),
            MACOS_TITLEBAR_HEIGHT,
        )
    )
    drag_strip.setTranslatesAutoresizingMaskIntoConstraints_(False)
    content_view.addSubview_(drag_strip)
    constraints = [
        drag_strip.leadingAnchor().constraintEqualToAnchor_constant_(
            content_view.leadingAnchor(),
            MACOS_TRAFFIC_LIGHT_SAFE_WIDTH,
        ),
        drag_strip.trailingAnchor().constraintEqualToAnchor_(
            content_view.trailingAnchor()
        ),
        drag_strip.topAnchor().constraintEqualToAnchor_(content_view.topAnchor()),
        drag_strip.heightAnchor().constraintEqualToConstant_(MACOS_TITLEBAR_HEIGHT),
    ]
    appkit.NSLayoutConstraint.activateConstraints_(constraints)
    # Native views retain their children, while these Python references also
    # keep the PyObjC subclass and its constraints alive for the window lifetime.
    window._mefinder_native_drag_strip = drag_strip
    window._mefinder_native_drag_constraints = constraints


def configure_macos_titlebar(window) -> None:
    """Extend the web content into a transparent native titlebar on macOS."""

    if sys.platform != "darwin":
        return
    try:
        import AppKit

        native_window = window.native
        full_size_content = getattr(
            AppKit,
            "NSWindowStyleMaskFullSizeContentView",
            getattr(AppKit, "NSFullSizeContentViewWindowMask", 1 << 15),
        )
        native_window.setStyleMask_(native_window.styleMask() | full_size_content)
        native_window.setTitlebarAppearsTransparent_(True)
        native_window.setTitleVisibility_(getattr(AppKit, "NSWindowTitleHidden", 1))
        if hasattr(native_window, "setTitlebarSeparatorStyle_"):
            native_window.setTitlebarSeparatorStyle_(
                getattr(AppKit, "NSTitlebarSeparatorStyleNone", 0)
            )
        _install_macos_native_drag_strip(window, native_window, AppKit)
        try:
            titlebar_view = (
                native_window.contentView().superview().subviews().lastObject()
            )
            titlebar_view.setBackgroundColor_(AppKit.NSColor.clearColor())
        except Exception:
            # Older macOS releases can expose a different private titlebar view tree.
            pass
    except Exception:
        # The themed titlebar is progressive enhancement; never block app startup.
        logging.exception("failed to configure macOS titlebar")


def configure_windows_main_window(window) -> None:
    """Restore native resizing around the HTML-owned Windows titlebar."""

    if sys.platform != "win32":
        return
    try:
        from src.me_finder.windows_desktop import configure_windows_chromeless

        configure_windows_chromeless(window)
    except Exception:
        # A frame enhancement must never prevent the actual app from opening.
        logging.exception("failed to configure frameless Windows resize border")


def create_main_window(webview_module, theme: str):
    """Create the platform shell and return its optional Windows controller."""

    palette = theme_palette(theme)
    options = {
        "html": loading_html(theme, sys.platform),
        "width": 1500,
        "height": 860,
        "min_size": (960, 640),
        "resizable": True,
        "text_select": True,
        "background_color": palette["app_bg"],
    }
    controller = None
    if sys.platform == "win32":
        from src.me_finder.windows_desktop import WindowsWindowController

        controller = WindowsWindowController()
        options.update(
            {
                "js_api": controller,
                "frameless": True,
                "easy_drag": False,
                "shadow": True,
            }
        )

    window = webview_module.create_window(APP_TITLE, **options)
    if controller is not None:
        controller._bind(window)
        window.events.before_show += configure_windows_main_window
    elif sys.platform == "darwin":
        window.events.before_show += configure_macos_titlebar
    return window, controller


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
    import webview

    from src.me_finder.app_context import AppContext

    bundle_root = app_root()
    portable = is_portable_bundle(bundle_root)
    root = prepare_runtime_root(bundle_root)
    app_data_root = (
        local_app_data_root()
        if getattr(sys, "frozen", False) and not portable
        else None
    )
    default_app_data_root = None
    if (
        app_data_root is not None
        and not os.environ.get("ME_FINDER_APP_DATA_ROOT", "").strip()
    ):
        if sys.platform == "darwin":
            default_app_data_root = default_macos_data_root()
        elif sys.platform == "win32":
            default_app_data_root = default_windows_data_root(
                local_app_data=os.environ.get("LOCALAPPDATA") or None,
            )
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
    os.environ[DESKTOP_SHELL_ENV] = "macos" if sys.platform == "darwin" else sys.platform
    theme = read_preferences(preferences_path)["theme"]
    setup_logging(root)
    logging.info("app root: %s", root)
    if root != bundle_root:
        logging.info("bundle root: %s", bundle_root)

    pdf_viewer = None
    if sys.platform == "darwin":
        from src.me_finder.macos_pdf_viewer import MacOSPDFViewer

        pdf_viewer = MacOSPDFViewer()
    elif sys.platform == "win32":
        from src.me_finder.windows_desktop import WindowsPDFViewer

        pdf_viewer = WindowsPDFViewer(webview, theme)

    window, window_controller = create_main_window(webview, theme)
    if sys.platform == "darwin":
        window.events.closing += pdf_viewer.close
    native_theme_setter = None
    if sys.platform == "win32":
        def apply_native_theme(selected_theme: str) -> None:
            pdf_viewer.set_theme(selected_theme)

        native_theme_setter = apply_native_theme
        window.events.closing += pdf_viewer.close

    update_service = None
    if sys.platform == "win32":
        from src.me_finder.update_service import UpdateService

        def close_for_update() -> None:
            threading.Timer(0.8, window.destroy).start()

        update_service = UpdateService(
            __version__,
            local_app_data_root() / "updates",
            install_kind=installation_kind(bundle_root),
            on_install_started=close_for_update,
        )

    state_lock = threading.Lock()
    state = {
        "server": None,
        "handler": None,
        "closing": False,
        "pdf_viewer": pdf_viewer,
        "update_service": update_service,
    }

    def choose_folders(
        initial_directory: Path | None,
        *,
        allow_multiple: bool = False,
    ) -> list[str]:
        """Open the platform folder picker. Works on macOS and Windows alike."""

        start = initial_directory
        if start is None or not start.is_dir():
            start = Path.home()
        selection = window.create_file_dialog(
            webview.FileDialog.FOLDER,
            directory=str(start),
            allow_multiple=allow_multiple,
        )
        if not selection:
            return []
        if isinstance(selection, (str, Path)):
            selection = [selection]
        return [str(folder) for folder in selection]

    def choose_data_directory() -> str | None:
        if app_data_root is None:
            return None
        selection = choose_folders(app_data_root.parent)
        return selection[0] if selection else None

    def choose_backup_file() -> str | None:
        try:
            selection = window.create_file_dialog(
                webview.FileDialog.OPEN,
                directory=str(Path.home()),
                allow_multiple=False,
                file_types=("MEFinder 备份 (*.zip)",),
            )
        except webview.errors.WebViewException as exc:
            raise RuntimeError(str(exc)) from exc
        if not selection:
            return None
        if isinstance(selection, (str, Path)):
            return str(selection)
        return str(selection[0])

    def choose_scan_directories() -> list[str]:
        # Never start at the home folder: picking it is one click away there,
        # and scanning it would walk the user's whole personal library.
        return choose_folders(Path.home() / "Documents", allow_multiple=True)

    def start_backend(win) -> None:
        handler = None
        server = None
        server_started = False
        try:
            with state_lock:
                if state["closing"]:
                    return
            index_path = root / "data" / "index.sqlite3"
            if not index_path.exists():
                logging.error("index not found: %s", index_path)
                with state_lock:
                    closing = bool(state["closing"])
                if not closing:
                    win.load_html(error_html(
                        "未找到索引数据库 data/index.sqlite3",
                        "请把 index.sqlite3 放到：\n%s\n\n"
                        "索引数据库可在项目目录用命令生成：\n"
                        "%s -m src.me_finder build-index" % (index_path, python_launcher()),
                        theme,
                        sys.platform,
                    ))
                return
            logging.info("loading index from %s", index_path)
            from src.me_finder.web import ManagedThreadingHTTPServer, make_handler

            handler = make_handler(
                index_path,
                app_context=AppContext.create(
                    root,
                    index_path=index_path,
                    app_data_root=app_data_root,
                    default_app_data_root=default_app_data_root,
                ),
                native_pdf_opener=pdf_viewer.open if pdf_viewer is not None else None,
                native_theme_setter=native_theme_setter,
                update_service=update_service,
                native_directory_chooser=choose_data_directory,
                native_scan_directory_chooser=choose_scan_directories,
                native_backup_file_chooser=choose_backup_file,
            )
            server = ManagedThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = int(server.server_address[1])
            server_thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            # BaseServer.shutdown() blocks until serve_forever() has entered
            # its loop.  Publish the pair only after Thread.start succeeds,
            # while serializing with the close path so an immediately closed
            # WebView cannot leave behind a late-starting backend.
            with state_lock:
                if state["closing"]:
                    handler.begin_shutdown()
                else:
                    server_thread.start()
                    server_started = True
                    state["handler"] = handler
                    state["server"] = server
            if not server_started:
                server.server_close()
                handler.close_runtime()
                return
            url = "http://127.0.0.1:%d/" % port
            logging.info("backend ready at %s", url)
            with state_lock:
                closing = bool(state["closing"])
            if not closing:
                win.load_url(url)
        except Exception:
            logging.exception("backend failed to start")
            if not server_started:
                if handler is not None:
                    handler.begin_shutdown()
                if server is not None:
                    server.server_close()
                if handler is not None:
                    handler.close_runtime()
            with state_lock:
                closing = bool(state["closing"])
            if not closing:
                win.load_html(
                    error_html(
                        "后台启动失败",
                        traceback.format_exc(),
                        theme,
                        sys.platform,
                    )
                )

    webview.start(start_backend, window, storage_path=webview_storage_path(root, portable))
    with state_lock:
        state["closing"] = True
        server = state["server"]
        handler = state["handler"]
    if handler is not None:
        handler.begin_shutdown()
    if server is not None:
        server.shutdown()
        server.server_close()
    handlers_stopped = (
        server is None or server.wait_for_handlers(timeout=2.0)
    )
    if handler is not None:
        # A half-open request must not block Windows/WebView2 shutdown forever,
        # but a config+SQLite mutation that already started must reach either
        # commit or rollback before the process is allowed to disappear.
        handler.wait_for_durable_operations()
    if not handlers_stopped and server is not None:
        handlers_stopped = server.wait_for_handlers(timeout=2.0)
    if not handlers_stopped:
        logging.warning("active backend requests did not finish before desktop exit")
    if handler is not None and handlers_stopped:
        if not handler.close_runtime():
            logging.warning("backend workers did not finish before desktop exit")
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

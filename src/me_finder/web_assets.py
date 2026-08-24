"""Static front-end asset assembly for the local desktop shell."""

from __future__ import annotations

import os
from pathlib import Path

from . import __version__


_PACKAGE_DIR = Path(__file__).resolve().parent


def _load_asset(relative: str) -> str:
    return (_PACKAGE_DIR / relative).read_text(encoding="utf-8")


def _load_asset_dir(relative: str, suffix: str) -> str:
    """按文件名排序拼接一个资源目录。

    app.js 已按功能拆分到 static/js/，用数字前缀锁定加载顺序（00-state 最先、
    90-init 最后）。这些文件共享同一个全局作用域，拼接结果与拆分前逐字节相同，
    所以顺序必须严格按文件名排序，改名或加新文件时要注意前缀。
    """

    directory = _PACKAGE_DIR / relative
    if not directory.is_dir():
        return ""
    parts = [
        path.read_text(encoding="utf-8")
        for path in sorted(directory.glob(f"*{suffix}"), key=lambda p: p.name)
    ]
    return "".join(parts)


def _load_app_js() -> str:
    """优先使用拆分后的 static/js/；单文件 app.js 仅作为回退保留。"""

    bundled = _load_asset_dir("static/js", ".js")
    return bundled if bundled else _load_asset("static/app.js")


def _load_app_css() -> str:
    """优先使用拆分后的 static/css/；单文件 app.css 仅作为回退保留。

    与 static/js/ 同理，按数字前缀（00-themes 最先、90-dialogs-toast 最后）
    的文件名排序拼接，结果与拆分前逐字节相同，改名或加新文件时要注意前缀。
    """

    bundled = _load_asset_dir("static/css", ".css")
    return bundled if bundled else _load_asset("static/app.css")


HTML = (
    _load_asset("templates/index.html")
    .replace("/*__APP_CSS__*/", _load_app_css(), 1)
    .replace("/*__READER_CSS__*/", _load_asset("static/reader.css"), 1)
    .replace("//__APP_JS__", _load_app_js(), 1)
    .replace("//__READER_JS__", _load_asset("static/reader.js"), 1)
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

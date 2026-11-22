"""Static front-end asset assembly for the local desktop shell."""

from __future__ import annotations

import os
from pathlib import Path

from . import __version__


PACKAGE_DIR = Path(__file__).resolve().parent

# 随包分发的 Vue 3 全局构建（试点，见 static/vendor/README.md）；离线可用，不引入 Node 构建。
VUE_ASSET = "static/vendor/vue.global.prod.js"
KATEX_ASSET = "static/vendor/katex.min.js"


def _load_asset(relative: str) -> str:
    return (PACKAGE_DIR / relative).read_text(encoding="utf-8")


def _load_asset_dir(relative: str, suffix: str) -> str:
    """按文件名排序拼接一个资源目录。

    app.js 已按功能拆分到 static/js/，用数字前缀锁定加载顺序（00-state 最先、
    90-init 最后）。这些文件共享同一个全局作用域，拼接结果与拆分前逐字节相同，
    所以顺序必须严格按文件名排序，改名或加新文件时要注意前缀。
    """

    directory = PACKAGE_DIR / relative
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


def _load_reader_js() -> str:
    """Return the ordered reader modules shared by both application windows."""

    return _load_asset_dir("static/reader", ".js")


HTML = (
    _load_asset("templates/index.html")
    .replace("/*__APP_CSS__*/", _load_app_css(), 1)
    .replace("/*__READER_CSS__*/", _load_asset("static/reader.css"), 1)
    .replace("//__APP_JS__", _load_app_js(), 1)
    .replace("//__READER_JS__", _load_reader_js(), 1)
    .replace("__APP_VERSION__", __version__)
    # 最后插入第三方压缩代码，避免前面的占位替换扫进其正文。
    .replace("//__VUE_JS__", _load_asset(VUE_ASSET), 1)
    .replace("//__KATEX_JS__", _load_asset(KATEX_ASSET), 1)
)


READER_WINDOW_HTML = (
    _load_asset("templates/reader-window.html")
    .replace("/*__APP_CSS__*/", _load_app_css(), 1)
    .replace("/*__READER_CSS__*/", _load_asset("static/reader.css"), 1)
    .replace("//__THEME_JS__", _load_asset("static/js/05-theme-engine.js"), 1)
    .replace("//__API_JS__", _load_asset("static/js/07-api.js"), 1)
    .replace("//__ALIGNMENT_JOBS_JS__", _load_asset("static/js/15-alignment-jobs.js"), 1)
    .replace("//__MATERIAL_TAGS_JS__", _load_asset("static/js/22-material-tags.js"), 1)
    .replace("//__READER_JS__", _load_reader_js(), 1)
    .replace("//__WINDOW_JS__", _load_asset("static/reader-window.js"), 1)
)


def render_html(
    theme: str, *, reader_window: bool = False, works_navigation: str = "hidden"
) -> str:
    """Render the theme and work entry before the first paint."""

    if reader_window:
        return READER_WINDOW_HTML.replace('data-theme="frost-blue"', f'data-theme="{theme}"', 1)
    marker = '<html lang="zh-CN" data-theme="frost-blue">'
    desktop_shell = os.environ.get("ME_FINDER_DESKTOP_SHELL", "").strip().lower()
    shell_attribute = (
        f' data-desktop-shell="{desktop_shell}"'
        if desktop_shell in {"macos", "win32"}
        else ""
    )
    replacement = f'<html lang="zh-CN" data-theme="{theme}"{shell_attribute}>'
    html = HTML.replace(marker, replacement, 1)
    if works_navigation in {"visible", "readonly"}:
        title = "译本对照（只读）" if works_navigation == "readonly" else "译本对照"
        html = html.replace(
            'data-page="works" hidden title="译本对照"',
            f'data-page="works" title="{title}"',
            1,
        )
    if works_navigation == "readonly":
        tag = '<span class="sidebar-item-tag" hidden>只读</span>'
        html = html.replace(tag, tag.replace(" hidden", ""), 1)
    return html

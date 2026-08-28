"""Persistent desktop preferences shared by the web UI and pywebview shell."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Mapping


DEFAULT_THEME = "frost-blue"
DEFAULT_LIBRARY_VIEW = "list"
DEFAULT_CALIBRATION_VIEW = "grid"
DEFAULT_PDF_OPEN_MODE = "native"
DEFAULT_PDF_PARSE_MODE = "auto"
DEFAULT_DOCUMENT_EXPORT_MODE = "data_only"
DEFAULT_AUTO_UPDATE = False
DEFAULT_CITATION_STYLES = ("chinese", "gb")
DEFAULT_CITATION_STYLE = "chinese"
VALID_CITATION_STYLES = ("chinese", "gb", "chicago", "apa", "mla")
VALID_THEMES = frozenset(
    {
        "frost-blue",
        "sage-ivory",
        "warm-sand",
        "rose-mist",
        "lavender-purple",
        "midnight",
    }
)
VALID_LIBRARY_VIEWS = frozenset({"list", "grid"})
VALID_CALIBRATION_VIEWS = frozenset({"list", "grid"})
VALID_PDF_OPEN_MODES = frozenset({"native", "system"})
VALID_PDF_PARSE_MODES = frozenset({"auto", "mineru", "mineru-local", "vision"})
VALID_DOCUMENT_EXPORT_MODES = frozenset({"data_only", "with_pdf"})
# 保留旧客户端的导出偏好契约；当前界面不再读取这些选项。
# 常规 Markdown / EPUB 导出使用共享规范化层的 printed 默认策略。
DEFAULT_PAGE_MARKER_MODE = "printed"
VALID_PAGE_MARKER_MODES = frozenset({"none", "printed", "full"})
DEFAULT_EXPORT_PAGE_CLEANUP: dict[str, Any] = {
    "page_marker_mode": DEFAULT_PAGE_MARKER_MODE,
    "remove_visible_page_numbers": True,
    "remove_running_headers": True,
    "remove_running_footers": True,
}
# 文献默认语言与联网自动匹配阈值：此前只存 localStorage，换机/迁移/导入备份后
# 会静默复位。纳入 preferences.json 后随数据一起备份迁移（C-01）。
DEFAULT_LIBRARY_LANGUAGE = "chinese"
VALID_LIBRARY_LANGUAGES = frozenset({"chinese", "foreign"})
DEFAULT_ONLINE_AUTO_MATCH = 0.90
ONLINE_AUTO_MATCH_MIN = 0.80
ONLINE_AUTO_MATCH_MAX = 1.00

# ── 可扩展主题引擎（阶段 3-6）的持久化 ──
# 浅色与深色各自独立保存一份主题选择，外观模式决定跟随系统/浅/深。
# 内置 CSS 主题（VALID_THEMES）仍是首帧与原生标题栏的回退，legacy `theme`
# 字段继续只存这 6 个之一；真正的引擎状态放在独立的 `appearance` 对象里，
# 因此不影响任何既有 theme 契约与后端校验。
APPEARANCE_SCHEMA_VERSION = 2
VALID_APPEARANCE_MODES = frozenset({"system", "light", "dark"})
DEFAULT_APPEARANCE_MODE = "system"
# 每种模式的默认内置主题（也是 legacy theme 回退）。
APPEARANCE_MODE_DEFAULT_THEME = {"light": "frost-blue", "dark": "midnight"}
APPEARANCE_MODE_DEFAULT_HIGHLIGHT = {"light": "#2563B8", "dark": "#58A6FF"}
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_THEME_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_CUSTOM_THEMES = 60


def _mode_of_theme(theme: str) -> str:
    return "dark" if theme == "midnight" else "light"


def _builtin_fallback_theme(theme_id: str, custom_themes: dict, mode_hint: str) -> str:
    """把任意主题 id（内置/新预设/自定义）归约为一个内置 CSS 主题 id，
    供 legacy `theme` 字段（首帧、原生标题栏）使用。"""

    if theme_id in VALID_THEMES:
        return theme_id
    mode = mode_hint
    entry = custom_themes.get(theme_id) if isinstance(custom_themes, dict) else None
    if isinstance(entry, dict) and entry.get("mode") in {"light", "dark"}:
        mode = str(entry["mode"])
    return APPEARANCE_MODE_DEFAULT_THEME.get(mode, "frost-blue")


def resolve_native_shell_theme(
    preferences: Mapping[str, Any], *, os_prefers_dark: bool | None = None
) -> str:
    """决定原生窗口首帧/标题栏应使用的内置主题 id。

    legacy ``theme`` 字段在保存时对 ``system`` 外观模式一律退到浅色选择（保存时
    无从得知系统色）。这会让「跟随系统 + 系统深色」在原生首帧/标题栏露出浅色
    （深色 HTML 周围的一圈白边）。原生启动时可以现场探测系统色，因此这里按
    ``os_prefers_dark`` 把 ``system`` 归结为真正生效的明暗，再归约成内置主题。"""

    appearance = preferences.get("appearance") if isinstance(preferences, Mapping) else None
    legacy = str(preferences.get("theme") or DEFAULT_THEME) if isinstance(preferences, Mapping) else DEFAULT_THEME
    if not isinstance(appearance, Mapping):
        return legacy if legacy in VALID_THEMES else DEFAULT_THEME
    mode = appearance.get("mode")
    if mode not in VALID_APPEARANCE_MODES:
        mode = DEFAULT_APPEARANCE_MODE
    if mode == "system":
        mode = "dark" if os_prefers_dark else "light"
    selection = appearance.get(mode)
    if not isinstance(selection, str) or not selection:
        selection = APPEARANCE_MODE_DEFAULT_THEME.get(mode, DEFAULT_THEME)
    custom = appearance.get("custom_themes")
    return _builtin_fallback_theme(
        selection, custom if isinstance(custom, dict) else {}, mode
    )


def _normalized_theme_def(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    mode = value.get("mode")
    if mode not in {"light", "dark"}:
        return None
    accent, background, foreground = (
        value.get("accent"),
        value.get("background"),
        value.get("foreground"),
    )
    highlight = value.get("highlight", APPEARANCE_MODE_DEFAULT_HIGHLIGHT[str(mode)])
    for color in (accent, highlight, background, foreground):
        if not isinstance(color, str) or not _HEX_COLOR.match(color.strip()):
            return None
    try:
        contrast = float(value.get("contrast", 55))
    except (TypeError, ValueError):
        contrast = 55.0
    contrast = max(0, min(100, round(contrast)))
    name = str(value.get("name") or "自定义主题").strip()[:60] or "自定义主题"
    normalized = {
        "schemaVersion": APPEARANCE_SCHEMA_VERSION,
        "name": name,
        "mode": str(mode),
        "accent": str(accent).strip(),
        "highlight": str(highlight).strip(),
        "background": str(background).strip(),
        "foreground": str(foreground).strip(),
        "contrast": contrast,
    }
    for font_key in ("fontUi", "fontCode"):
        font_value = value.get(font_key)
        if isinstance(font_value, str) and 0 < len(font_value) < 200:
            normalized[font_key] = font_value
    tokens = value.get("tokens")
    if isinstance(tokens, dict):
        clean = {
            str(k): str(v)
            for k, v in tokens.items()
            if isinstance(k, str) and re.match(r"^--[a-z0-9-]+$", k) and isinstance(v, str)
        }
        if clean:
            normalized["tokens"] = clean
    return normalized


def _normalized_appearance(value: Any, legacy_theme: str) -> dict[str, Any]:
    """把 appearance 规整为可信结构；缺失或损坏时从 legacy theme 迁移。

    迁移策略：把用户当前的单一 theme 放进它所属模式的选择里，另一模式取默认，
    外观模式设为该 theme 的模式——升级后表现与升级前逐帧一致。"""

    legacy_mode = _mode_of_theme(legacy_theme)
    custom_themes: dict[str, Any] = {}
    if isinstance(value, dict):
        raw_custom = value.get("custom_themes")
        if isinstance(raw_custom, dict):
            for key, entry in raw_custom.items():
                if not isinstance(key, str) or not _THEME_ID.match(key):
                    continue
                normalized = _normalized_theme_def(entry)
                if normalized is not None:
                    normalized["id"] = key
                    custom_themes[key] = normalized
                if len(custom_themes) >= _MAX_CUSTOM_THEMES:
                    break

    def _resolve_selection(raw: Any, mode: str) -> str:
        candidate = str(raw).strip() if isinstance(raw, str) else ""
        if candidate in VALID_THEMES:
            return candidate
        if candidate in custom_themes and custom_themes[candidate].get("mode") == mode:
            return candidate
        # 新官方预设 id（前端 THEME_PRESETS 里非内置 CSS 的那些）也放行：
        # 后端不内联颜色语义，仅要求 id 合法且非空。
        if candidate and _THEME_ID.match(candidate):
            return candidate
        return APPEARANCE_MODE_DEFAULT_THEME[mode]

    if isinstance(value, dict):
        mode = value.get("mode")
        if mode not in VALID_APPEARANCE_MODES:
            mode = DEFAULT_APPEARANCE_MODE
        light = _resolve_selection(value.get("light"), "light")
        dark = _resolve_selection(value.get("dark"), "dark")
    else:
        # 首次迁移：从 legacy theme 构造。
        mode = legacy_mode
        light = legacy_theme if legacy_mode == "light" else APPEARANCE_MODE_DEFAULT_THEME["light"]
        dark = legacy_theme if legacy_mode == "dark" else APPEARANCE_MODE_DEFAULT_THEME["dark"]

    return {
        "schemaVersion": APPEARANCE_SCHEMA_VERSION,
        "mode": str(mode),
        "light": light,
        "dark": dark,
        "custom_themes": custom_themes,
    }


def _normalized_online_auto_match(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_ONLINE_AUTO_MATCH
    return max(ONLINE_AUTO_MATCH_MIN, min(ONLINE_AUTO_MATCH_MAX, round(number, 2)))
_PREFERENCES_LOCK = threading.RLock()


def resolve_preferences_path(root: Path | None = None) -> Path:
    configured = os.environ.get("ME_FINDER_PREFERENCES")
    if configured:
        return Path(configured).expanduser().resolve()
    base = (root or Path.cwd()).resolve()
    return base / "config" / "preferences.json"


def read_preferences(path: Path | None = None) -> dict[str, Any]:
    preferences_path = path or resolve_preferences_path()
    try:
        payload = json.loads(preferences_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = {}
    theme = payload.get("theme") if isinstance(payload, dict) else None
    if theme not in VALID_THEMES:
        theme = DEFAULT_THEME
    calibration_view = payload.get("calibration_view") if isinstance(payload, dict) else None
    if calibration_view not in VALID_CALIBRATION_VIEWS:
        calibration_view = DEFAULT_CALIBRATION_VIEW
    library_view = payload.get("library_view") if isinstance(payload, dict) else None
    if library_view not in VALID_LIBRARY_VIEWS:
        legacy_calibration_view = payload.get("calibration_view") if isinstance(payload, dict) else None
        library_view = (
            str(legacy_calibration_view)
            if legacy_calibration_view in VALID_CALIBRATION_VIEWS
            else DEFAULT_LIBRARY_VIEW
        )
    raw_directories = payload.get("scan_directories") if isinstance(payload, dict) else None
    scan_directories = _normalized_scan_directories(raw_directories)
    pdf_open_mode = payload.get("pdf_open_mode") if isinstance(payload, dict) else None
    if pdf_open_mode not in VALID_PDF_OPEN_MODES:
        pdf_open_mode = DEFAULT_PDF_OPEN_MODE
    pdf_parse_mode = payload.get("pdf_parse_mode") if isinstance(payload, dict) else None
    if pdf_parse_mode not in VALID_PDF_PARSE_MODES:
        pdf_parse_mode = DEFAULT_PDF_PARSE_MODE
    document_export_mode = (
        payload.get("document_export_mode") if isinstance(payload, dict) else None
    )
    if document_export_mode not in VALID_DOCUMENT_EXPORT_MODES:
        document_export_mode = DEFAULT_DOCUMENT_EXPORT_MODE
    auto_update = payload.get("auto_update") if isinstance(payload, dict) else None
    if not isinstance(auto_update, bool):
        auto_update = DEFAULT_AUTO_UPDATE
    citation_styles = _normalized_citation_styles(
        payload.get("citation_styles") if isinstance(payload, dict) else None
    )
    citation_style = str(
        payload.get("citation_style") if isinstance(payload, dict) else ""
    ).strip().lower()
    if citation_style not in citation_styles:
        citation_style = citation_styles[0] if citation_styles else DEFAULT_CITATION_STYLE
    library_language = payload.get("lib_default_language") if isinstance(payload, dict) else None
    if library_language not in VALID_LIBRARY_LANGUAGES:
        library_language = DEFAULT_LIBRARY_LANGUAGE
    online_auto_match = _normalized_online_auto_match(
        payload.get("online_auto_match_threshold") if isinstance(payload, dict) else None
    )
    appearance = _normalized_appearance(
        payload.get("appearance") if isinstance(payload, dict) else None,
        theme,
    )
    export_page_cleanup = _normalized_export_page_cleanup(
        payload.get("export_page_cleanup") if isinstance(payload, dict) else None
    )
    return {
        "theme": theme,
        "appearance": appearance,
        "library_view": library_view,
        "calibration_view": calibration_view,
        "scan_directories": scan_directories,
        "pdf_open_mode": pdf_open_mode,
        "pdf_parse_mode": pdf_parse_mode,
        "document_export_mode": document_export_mode,
        "export_page_cleanup": export_page_cleanup,
        "auto_update": auto_update,
        "citation_styles": citation_styles,
        "citation_style": citation_style,
        "lib_default_language": library_language,
        "online_auto_match_threshold": online_auto_match,
    }


def _normalized_export_page_cleanup(value: Any) -> dict[str, Any]:
    """Coerce the format-neutral export cleanup block, filling safe defaults.

    Retained for older clients; unknown or malformed values fall back to the
    defaults (printed marker + cleanup on).
    """

    result = dict(DEFAULT_EXPORT_PAGE_CLEANUP)
    if not isinstance(value, dict):
        return result
    mode = value.get("page_marker_mode")
    if mode in VALID_PAGE_MARKER_MODES:
        result["page_marker_mode"] = str(mode)
    for key in (
        "remove_visible_page_numbers",
        "remove_running_headers",
        "remove_running_footers",
    ):
        flag = value.get(key)
        if isinstance(flag, bool):
            result[key] = flag
    return result


def _normalized_scan_directories(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip().strip('"')
        if not text:
            continue
        normalized = str(Path(text))
        if normalized not in result:
            result.append(normalized)
    return result[:20]


def _normalized_citation_styles(value: Any) -> list[str]:
    if not isinstance(value, list):
        return list(DEFAULT_CITATION_STYLES)
    selected = {str(item or "").strip().lower() for item in value}
    normalized = [style for style in VALID_CITATION_STYLES if style in selected]
    return normalized or list(DEFAULT_CITATION_STYLES)


def save_preferences(
    updates: Mapping[str, Any], path: Path | None = None
) -> dict[str, Any]:
    # ThreadingHTTPServer can receive theme/PDF/update-setting writes at the
    # same time. Serialize the whole read-modify-replace transaction so one
    # request cannot overwrite another request's newer fields or reuse its
    # temporary file.
    with _PREFERENCES_LOCK:
        return _save_preferences_locked(updates, path)


def _save_preferences_locked(
    updates: Mapping[str, Any], path: Path | None = None
) -> dict[str, Any]:
    preferences_path = path or resolve_preferences_path()
    current = read_preferences(preferences_path)
    if "theme" in updates:
        theme = updates["theme"]
        if theme not in VALID_THEMES:
            raise ValueError("不支持的主题")
        current["theme"] = str(theme)
    if "library_view" in updates:
        library_view = updates["library_view"]
        if library_view not in VALID_LIBRARY_VIEWS:
            raise ValueError("不支持的文献库显示方式")
        current["library_view"] = str(library_view)
    if "calibration_view" in updates:
        calibration_view = updates["calibration_view"]
        if calibration_view not in VALID_CALIBRATION_VIEWS:
            raise ValueError("不支持的页码校准显示方式")
        current["calibration_view"] = str(calibration_view)
    if "scan_directories" in updates:
        directories = updates["scan_directories"]
        if not isinstance(directories, list):
            raise ValueError("文献目录必须是路径列表")
        current["scan_directories"] = _normalized_scan_directories(directories)
    if "pdf_open_mode" in updates:
        pdf_open_mode = updates["pdf_open_mode"]
        if pdf_open_mode not in VALID_PDF_OPEN_MODES:
            raise ValueError("不支持的 PDF 打开方式")
        current["pdf_open_mode"] = str(pdf_open_mode)
    if "pdf_parse_mode" in updates:
        pdf_parse_mode = updates["pdf_parse_mode"]
        if pdf_parse_mode not in VALID_PDF_PARSE_MODES:
            raise ValueError("不支持的 PDF 解析方式")
        current["pdf_parse_mode"] = str(pdf_parse_mode)
    if "document_export_mode" in updates:
        document_export_mode = updates["document_export_mode"]
        if document_export_mode not in VALID_DOCUMENT_EXPORT_MODES:
            raise ValueError("不支持的文档包导出方式")
        current["document_export_mode"] = str(document_export_mode)
    if "export_page_cleanup" in updates:
        cleanup = updates["export_page_cleanup"]
        if not isinstance(cleanup, Mapping):
            raise ValueError("导出页面清理设置必须是对象")
        if "page_marker_mode" in cleanup and (
            cleanup["page_marker_mode"] not in VALID_PAGE_MARKER_MODES
        ):
            raise ValueError("不支持的页码锚点方式")
        for key in (
            "remove_visible_page_numbers",
            "remove_running_headers",
            "remove_running_footers",
        ):
            if key in cleanup and not isinstance(cleanup[key], bool):
                raise ValueError("导出页面清理开关必须为布尔值")
        # Merge onto the current block so a partial update only touches the keys
        # it actually provides, keeping every other flag as it was on disk.
        merged = dict(current.get("export_page_cleanup") or DEFAULT_EXPORT_PAGE_CLEANUP)
        if "page_marker_mode" in cleanup:
            merged["page_marker_mode"] = str(cleanup["page_marker_mode"])
        for key in (
            "remove_visible_page_numbers",
            "remove_running_headers",
            "remove_running_footers",
        ):
            if key in cleanup:
                merged[key] = bool(cleanup[key])
        current["export_page_cleanup"] = _normalized_export_page_cleanup(merged)
    if "auto_update" in updates:
        auto_update = updates["auto_update"]
        if not isinstance(auto_update, bool):
            raise ValueError("自动更新设置必须为布尔值")
        current["auto_update"] = auto_update
    if "citation_styles" in updates:
        citation_styles = updates["citation_styles"]
        if not isinstance(citation_styles, list):
            raise ValueError("引文格式必须是列表")
        requested = [str(item or "").strip().lower() for item in citation_styles]
        if any(style not in VALID_CITATION_STYLES for style in requested):
            raise ValueError("包含不支持的引文格式")
        normalized = [
            style for style in VALID_CITATION_STYLES if style in set(requested)
        ]
        if not normalized:
            raise ValueError("至少选择一种引文格式")
        current["citation_styles"] = normalized
        if current.get("citation_style") not in normalized:
            current["citation_style"] = normalized[0]
    if "citation_style" in updates:
        citation_style = str(updates["citation_style"] or "").strip().lower()
        if citation_style not in VALID_CITATION_STYLES:
            raise ValueError("不支持的当前引文格式")
        if citation_style not in current.get("citation_styles", []):
            raise ValueError("当前引文格式尚未启用")
        current["citation_style"] = citation_style
    if "lib_default_language" in updates:
        language = updates["lib_default_language"]
        if language not in VALID_LIBRARY_LANGUAGES:
            raise ValueError("不支持的文献默认语言")
        current["lib_default_language"] = str(language)
    if "online_auto_match_threshold" in updates:
        current["online_auto_match_threshold"] = _normalized_online_auto_match(
            updates["online_auto_match_threshold"]
        )
    if "appearance" in updates:
        appearance = _normalized_appearance(updates["appearance"], current["theme"])
        current["appearance"] = appearance
        # 客户端未显式带 legacy theme 时，从活动选择归约出内置回退，
        # 保证首帧与原生标题栏跟随外观设置（system 模式无从得知系统色，
        # 退到浅色选择，前端载入后会立即校正）。
        if "theme" not in updates:
            active_mode = "dark" if appearance["mode"] == "dark" else "light"
            active_selection = appearance[active_mode]
            current["theme"] = _builtin_fallback_theme(
                active_selection, appearance["custom_themes"], active_mode
            )

    preferences_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = preferences_path.with_suffix(preferences_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(preferences_path)
    return current

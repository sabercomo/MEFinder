"""Persistent desktop preferences shared by the web UI and pywebview shell."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping


DEFAULT_THEME = "frost-blue"
DEFAULT_LIBRARY_VIEW = "list"
DEFAULT_CALIBRATION_VIEW = "grid"
DEFAULT_PDF_OPEN_MODE = "native"
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
VALID_DOCUMENT_EXPORT_MODES = frozenset({"data_only", "with_pdf"})
# 文献默认语言与联网自动匹配阈值：此前只存 localStorage，换机/迁移/导入备份后
# 会静默复位。纳入 preferences.json 后随数据一起备份迁移（C-01）。
DEFAULT_LIBRARY_LANGUAGE = "chinese"
VALID_LIBRARY_LANGUAGES = frozenset({"chinese", "foreign"})
DEFAULT_ONLINE_AUTO_MATCH = 0.90
ONLINE_AUTO_MATCH_MIN = 0.80
ONLINE_AUTO_MATCH_MAX = 1.00


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
    return {
        "theme": theme,
        "library_view": library_view,
        "calibration_view": calibration_view,
        "scan_directories": scan_directories,
        "pdf_open_mode": pdf_open_mode,
        "document_export_mode": document_export_mode,
        "auto_update": auto_update,
        "citation_styles": citation_styles,
        "citation_style": citation_style,
        "lib_default_language": library_language,
        "online_auto_match_threshold": online_auto_match,
    }


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
    if "document_export_mode" in updates:
        document_export_mode = updates["document_export_mode"]
        if document_export_mode not in VALID_DOCUMENT_EXPORT_MODES:
            raise ValueError("不支持的文档包导出方式")
        current["document_export_mode"] = str(document_export_mode)
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

    preferences_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = preferences_path.with_suffix(preferences_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(preferences_path)
    return current

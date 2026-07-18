"""Persistent desktop preferences shared by the web UI and pywebview shell."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


DEFAULT_THEME = "frost-blue"
DEFAULT_LIBRARY_VIEW = "list"
DEFAULT_CALIBRATION_VIEW = "grid"
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


def resolve_preferences_path(root: Path | None = None) -> Path:
    configured = os.environ.get("ME_FINDER_PREFERENCES")
    if configured:
        return Path(configured).expanduser().resolve()
    base = (root or Path.cwd()).resolve()
    return base / "config" / "preferences.json"


def read_preferences(path: Path | None = None) -> dict[str, str]:
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
    return {
        "theme": theme,
        "library_view": library_view,
        "calibration_view": calibration_view,
    }


def save_preferences(
    updates: Mapping[str, Any], path: Path | None = None
) -> dict[str, str]:
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

    preferences_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = preferences_path.with_suffix(preferences_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(preferences_path)
    return current

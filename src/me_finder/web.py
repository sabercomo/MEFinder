"""Local web interface — iOS-style SPA shell."""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .database import DEFAULT_DATABASE_PATH
from .auto_page_mapping import has_manual_mapping
from .bibliographic_metadata import (
    METADATA_FIELDS,
    canonical_metadata,
    detect_pdf_bibliographic_metadata,
    manual_metadata,
    metadata_missing_fields,
    update_metadata_in_database,
)
from .mineru_api import MinerUError, mineru_config_summary, resolve_mineru_config_path, save_mineru_config
from .preferences import read_preferences, resolve_preferences_path, save_preferences
from .calibration_library import build_calibration_library
from .document_deletion import DocumentDeletionService
from .pdf_extractors import extract_pdf_source
from .pdf_import_service import (
    detect_imported_pdf,
    parse_pdf_with_mineru,
    rebuild_local_index,
    register_pdf,
    save_import_config,
)
from .runtime_page_mapping import apply_mapping_to_database, normalize_auto_segments
from .search import SearchEngine


def find_adobe_pdf_app() -> Optional[Path]:
    """Find an installed Adobe Acrobat/Reader executable on Windows."""

    candidate_paths = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if not base:
            continue
        candidate_paths.extend(
            [
                Path(base) / "Adobe" / "Acrobat DC" / "Acrobat" / "Acrobat.exe",
                Path(base) / "Adobe" / "Acrobat Reader DC" / "Reader" / "AcroRd32.exe",
                Path(base) / "Adobe" / "Acrobat" / "Acrobat.exe",
                Path(base) / "Adobe" / "Acrobat Reader" / "Reader" / "AcroRd32.exe",
            ]
        )
    registry_paths = _adobe_paths_from_registry()
    for path in registry_paths + candidate_paths:
        if path and Path(path).exists():
            return Path(path)
    return None


def _adobe_paths_from_registry() -> List[Path]:
    paths: List[Path] = []
    try:
        import winreg  # type: ignore
    except Exception:
        return paths
    registry_keys = [
        (winreg.HKEY_CLASSES_ROOT, r"AcroExch.Document.DC\shell\Open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"AcroExch.Document\shell\Open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"Applications\Acrobat.exe\shell\open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"Applications\AcroRd32.exe\shell\open\command"),
    ]
    for hive, key_name in registry_keys:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                command = str(winreg.QueryValueEx(key, "")[0])
        except OSError:
            continue
        match = re.search(r'"([^"]+\.exe)"', command, flags=re.IGNORECASE)
        if not match:
            match = re.search(r"([A-Za-z]:\\[^\s]+\.exe)", command, flags=re.IGNORECASE)
        if match:
            paths.append(Path(match.group(1)))
    return paths


HTML = r"""<!doctype html>
<html lang="zh-CN" data-theme="frost-blue">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>文献原句定位器</title>
<style>
/* ═══════════════════════════════════════════════════════════════
   Design System — iOS / iPadOS desktop style
   ═══════════════════════════════════════════════════════════════ */
:root, html[data-theme="frost-blue"], .theme-preview[data-preview-theme="frost-blue"] {
  --app-bg: #F5F8FC;
  --sidebar-bg: #F1F5FA;
  --surface-primary: #FFFFFF;
  --surface-secondary: #F8FAFD;
  --surface-elevated: #FFFFFF;
  --surface-hover: #F1F5FA;
  --surface-selected: #EAF3FF;
  --text-primary: #172033;
  --text-secondary: #667085;
  --text-tertiary: #98A2B3;
  --text-disabled: #B8C0CC;
  --border-subtle: #EDF1F6;
  --border-default: #DDE5EF;
  --border-strong: #C8D4E2;
  --accent: #1677FF;
  --accent-hover: #0F68E8;
  --accent-soft: #EAF3FF;
  --accent-contrast: #FFFFFF;
  --input-bg: #FFFFFF;
  --menu-bg: #FFFFFF;
  --dialog-bg: #FFFFFF;
  --tooltip-bg: #172033;
  --tooltip-text: #FFFFFF;
  --shadow-card: 0 2px 8px rgba(23,32,51,0.035);
  --shadow-card-hover: 0 5px 14px rgba(23,32,51,0.07);
  --shadow-popover: 0 12px 32px rgba(23,32,51,0.14);
  --focus-ring: 0 0 0 3px rgba(22,119,255,0.14);
  --scrollbar-track: transparent;
  --scrollbar-thumb: rgba(102,112,133,0.28);
  --scrollbar-thumb-hover: rgba(102,112,133,0.44);
  --skeleton-base: #EDF1F6;
  --skeleton-highlight: #F8FAFD;
  --dialog-backdrop: rgba(23,32,51,0.34);
  --match-block-bg: #FFF8E6;
  --match-block-border: #F2C66D;
  --match-block-accent: #D99000;
  --match-block-flash-bg: #FFEFC4;
  --match-inline-bg: #FFE7A8;
  --match-inline-border: #E9B644;
  --match-inline-text: #30240A;
  --match-focus-ring: rgba(217,144,0,0.22);

  /* Compatibility aliases used by existing components. */
  --bg: var(--app-bg);
  --surface: var(--surface-primary);
  --divider: var(--border-default);
  --accent-light: var(--accent-soft);
  --error: var(--danger);
  --sidebar-active: var(--surface-selected);
  --sidebar-width: 220px;
  --card-radius: 16px;
  --widget-radius: 10px;
  --input-radius: 12px;
  --input-height: 56px;
  --transition-fast: 160ms ease;
  --transition-normal: 220ms ease;
  color-scheme: light;
}

html[data-theme="sage-ivory"], .theme-preview[data-preview-theme="sage-ivory"] {
  --app-bg: #F7F7F1;
  --sidebar-bg: #F0F2E8;
  --surface-primary: #FFFDF8;
  --surface-secondary: #F5F5ED;
  --surface-elevated: #FFFFFF;
  --surface-hover: #F0F2E9;
  --surface-selected: #E8EEDF;
  --text-primary: #25291F;
  --text-secondary: #6E7464;
  --text-tertiary: #969C8D;
  --text-disabled: #B5B9AD;
  --border-subtle: #ECEDE4;
  --border-default: #DEE1D4;
  --border-strong: #C8CEBC;
  --accent: #637A50;
  --accent-hover: #536944;
  --accent-soft: #E8EEDF;
  --accent-contrast: #FFFFFF;
  --input-bg: #FFFDF8;
  --menu-bg: #FFFFFF;
  --dialog-bg: #FFFFFF;
  --tooltip-bg: #25291F;
  --tooltip-text: #FFFFFF;
  --shadow-card: 0 2px 8px rgba(37,41,31,0.045);
  --shadow-card-hover: 0 5px 14px rgba(37,41,31,0.08);
  --shadow-popover: 0 12px 32px rgba(37,41,31,0.15);
  --focus-ring: 0 0 0 3px rgba(99,122,80,0.16);
  --scrollbar-track: transparent;
  --scrollbar-thumb: rgba(110,116,100,0.28);
  --scrollbar-thumb-hover: rgba(110,116,100,0.44);
  --skeleton-base: #ECEDE4;
  --skeleton-highlight: #FFFDF8;
  --dialog-backdrop: rgba(37,41,31,0.32);
  --match-block-bg: #F5F0FF;
  --match-block-border: #BDA7EA;
  --match-block-accent: #7656B8;
  --match-block-flash-bg: #EADFFF;
  --match-inline-bg: #E5D8FF;
  --match-inline-border: #A889E2;
  --match-inline-text: #2B1D43;
  --match-focus-ring: rgba(118,86,184,0.20);
  color-scheme: light;
}

html[data-theme="warm-sand"], .theme-preview[data-preview-theme="warm-sand"] {
  --app-bg: #FBF7F1;
  --sidebar-bg: #F6EFE5;
  --surface-primary: #FFFCF8;
  --surface-secondary: #F9F2E9;
  --surface-elevated: #FFFFFF;
  --surface-hover: #F9EFE5;
  --surface-selected: #F7E3D5;
  --text-primary: #34251E;
  --text-secondary: #7C695E;
  --text-tertiary: #A49185;
  --text-disabled: #C0B3AA;
  --border-subtle: #F0E7DE;
  --border-default: #E7D9CC;
  --border-strong: #D6C2B1;
  --accent: #B85C2B;
  --accent-hover: #9F4D24;
  --accent-soft: #F7E3D5;
  --accent-contrast: #FFFFFF;
  --input-bg: #FFFCF8;
  --menu-bg: #FFFFFF;
  --dialog-bg: #FFFFFF;
  --tooltip-bg: #34251E;
  --tooltip-text: #FFFFFF;
  --shadow-card: 0 2px 8px rgba(52,37,30,0.045);
  --shadow-card-hover: 0 5px 14px rgba(52,37,30,0.08);
  --shadow-popover: 0 12px 32px rgba(52,37,30,0.15);
  --focus-ring: 0 0 0 3px rgba(184,92,43,0.15);
  --scrollbar-track: transparent;
  --scrollbar-thumb: rgba(124,105,94,0.28);
  --scrollbar-thumb-hover: rgba(124,105,94,0.44);
  --skeleton-base: #F0E7DE;
  --skeleton-highlight: #FFFCF8;
  --dialog-backdrop: rgba(52,37,30,0.32);
  --match-block-bg: #EFF6FF;
  --match-block-border: #8ABAF2;
  --match-block-accent: #2563B8;
  --match-block-flash-bg: #DDEBFF;
  --match-inline-bg: #DCEBFF;
  --match-inline-border: #6EA5E8;
  --match-inline-text: #102B4D;
  --match-focus-ring: rgba(37,99,184,0.20);
  color-scheme: light;
}

html[data-theme="rose-mist"], .theme-preview[data-preview-theme="rose-mist"] {
  --app-bg: #FDF6F8;
  --sidebar-bg: #FAF0F3;
  --surface-primary: #FFFFFF;
  --surface-secondary: #FEF9FA;
  --surface-elevated: #FFFFFF;
  --surface-hover: #F9F0F3;
  --surface-selected: #F5E0E8;
  --text-primary: #2C2528;
  --text-secondary: #71666A;
  --text-tertiary: #9A8E93;
  --text-disabled: #BDB3B7;
  --border-subtle: #F2E8EC;
  --border-default: #EBDCE2;
  --border-strong: #DCCAD2;
  --accent: #C9446A;
  --accent-hover: #B03A5C;
  --accent-soft: #FBE4EC;
  --accent-contrast: #FFFFFF;
  --input-bg: #FFFFFF;
  --menu-bg: #FFFFFF;
  --dialog-bg: #FFFFFF;
  --tooltip-bg: #2C2528;
  --tooltip-text: #FFFFFF;
  --shadow-card: 0 2px 8px rgba(44,37,40,0.035);
  --shadow-card-hover: 0 5px 14px rgba(44,37,40,0.07);
  --shadow-popover: 0 12px 32px rgba(44,37,40,0.13);
  --focus-ring: 0 0 0 3px rgba(201,68,106,0.16);
  --scrollbar-track: transparent;
  --scrollbar-thumb: rgba(113,102,106,0.25);
  --scrollbar-thumb-hover: rgba(113,102,106,0.40);
  --skeleton-base: #F0E6EA;
  --skeleton-highlight: #FFFFFF;
  --dialog-backdrop: rgba(44,37,40,0.30);
  --match-block-bg: #FFF8E6;
  --match-block-border: #E7C56D;
  --match-block-accent: #B8860B;
  --match-block-flash-bg: #FFEFC4;
  --match-inline-bg: #FFE8B5;
  --match-inline-border: #D4A844;
  --match-inline-text: #3D2B08;
  --match-focus-ring: rgba(184,134,11,0.20);
  color-scheme: light;
}

html[data-theme="lavender-purple"], .theme-preview[data-preview-theme="lavender-purple"] {
  --app-bg: #F9F7FD;
  --sidebar-bg: #F3F0F9;
  --surface-primary: #FFFFFF;
  --surface-secondary: #FBF9FE;
  --surface-elevated: #FFFFFF;
  --surface-hover: #F4F1F9;
  --surface-selected: #E8E2F4;
  --text-primary: #282532;
  --text-secondary: #6E697A;
  --text-tertiary: #9994A4;
  --text-disabled: #BBB7C3;
  --border-subtle: #EDEAF3;
  --border-default: #DED8EB;
  --border-strong: #CFC6DE;
  --accent: #7B5EC7;
  --accent-hover: #6A4FB5;
  --accent-soft: #EBE5F7;
  --accent-contrast: #FFFFFF;
  --input-bg: #FFFFFF;
  --menu-bg: #FFFFFF;
  --dialog-bg: #FFFFFF;
  --tooltip-bg: #282532;
  --tooltip-text: #FFFFFF;
  --shadow-card: 0 2px 8px rgba(40,37,50,0.035);
  --shadow-card-hover: 0 5px 14px rgba(40,37,50,0.07);
  --shadow-popover: 0 12px 32px rgba(40,37,50,0.13);
  --focus-ring: 0 0 0 3px rgba(123,94,199,0.16);
  --scrollbar-track: transparent;
  --scrollbar-thumb: rgba(110,105,122,0.25);
  --scrollbar-thumb-hover: rgba(110,105,122,0.40);
  --skeleton-base: #EAE6F1;
  --skeleton-highlight: #FFFFFF;
  --dialog-backdrop: rgba(40,37,50,0.30);
  --match-block-bg: #FFF7E6;
  --match-block-border: #E7B65C;
  --match-block-accent: #B86C08;
  --match-block-flash-bg: #FFEBC4;
  --match-inline-bg: #FFE4A8;
  --match-inline-border: #DB9F35;
  --match-inline-text: #3D2705;
  --match-focus-ring: rgba(184,108,8,0.20);
  color-scheme: light;
}

:root,
html[data-theme="frost-blue"], .theme-preview[data-preview-theme="frost-blue"],
html[data-theme="sage-ivory"], .theme-preview[data-preview-theme="sage-ivory"],
html[data-theme="warm-sand"], .theme-preview[data-preview-theme="warm-sand"],
html[data-theme="rose-mist"], .theme-preview[data-preview-theme="rose-mist"],
html[data-theme="lavender-purple"], .theme-preview[data-preview-theme="lavender-purple"] {
  --info: #2563EB;
  --info-soft: #EAF1FF;
  --info-border: #BDD0FA;
  --info-icon: #2563EB;
  --success: #168A46;
  --success-soft: #E8F7EE;
  --success-border: #B9E7C9;
  --success-icon: #168A46;
  --neutral: #667085;
  --neutral-soft: #F2F4F7;
  --neutral-border: #D9DEE7;
  --neutral-icon: #667085;
  --warning: #C96B12;
  --warning-soft: #FFF2E2;
  --warning-border: #F3CF9F;
  --warning-icon: #C96B12;
  --danger: #D62C3A;
  --danger-soft: #FDE9EB;
  --danger-border: #F4B9C0;
  --danger-icon: #D62C3A;
  --danger-contrast: #FFFFFF;
}

html[data-theme="midnight"], .theme-preview[data-preview-theme="midnight"] {
  --app-bg: #08111D;
  --sidebar-bg: #091522;
  --surface-primary: #111C29;
  --surface-secondary: #0D1723;
  --surface-elevated: #162332;
  --surface-hover: #172536;
  --surface-selected: #102B4C;
  --text-primary: #EEF4FB;
  --text-secondary: #A8B4C4;
  --text-tertiary: #748397;
  --text-disabled: #566476;
  --border-subtle: #1E2A38;
  --border-default: #2A394A;
  --border-strong: #3A4B5E;
  --accent: #2485FF;
  --accent-hover: #4397FF;
  --accent-soft: rgba(36,133,255,0.16);
  --accent-contrast: #FFFFFF;
  --success: #4ADE80;
  --success-soft: rgba(34,197,94,0.14);
  --success-border: rgba(74,222,128,0.38);
  --success-icon: #4ADE80;
  --neutral: #A8B4C4;
  --neutral-soft: rgba(148,163,184,0.10);
  --neutral-border: rgba(148,163,184,0.24);
  --neutral-icon: #A8B4C4;
  --warning: #FBBF24;
  --warning-soft: rgba(245,158,11,0.15);
  --warning-border: rgba(251,191,36,0.40);
  --warning-icon: #FBBF24;
  --danger: #FF6673;
  --danger-soft: rgba(255,83,99,0.15);
  --danger-border: rgba(255,102,115,0.42);
  --danger-icon: #FF6673;
  --danger-contrast: #FFFFFF;
  --info: #60A5FA;
  --info-soft: rgba(59,130,246,0.16);
  --info-border: rgba(96,165,250,0.40);
  --info-icon: #60A5FA;
  --input-bg: #0D1723;
  --menu-bg: #162332;
  --dialog-bg: #111C29;
  --tooltip-bg: #EEF4FB;
  --tooltip-text: #08111D;
  --shadow-card: 0 2px 10px rgba(0,0,0,0.18);
  --shadow-card-hover: 0 6px 18px rgba(0,0,0,0.28);
  --shadow-popover: 0 16px 38px rgba(0,0,0,0.42);
  --focus-ring: 0 0 0 3px rgba(36,133,255,0.22);
  --scrollbar-track: transparent;
  --scrollbar-thumb: rgba(168,180,196,0.24);
  --scrollbar-thumb-hover: rgba(168,180,196,0.38);
  --skeleton-base: #172536;
  --skeleton-highlight: #223246;
  --dialog-backdrop: rgba(0,0,0,0.58);
  --match-block-bg: rgba(93,63,10,0.42);
  --match-block-border: rgba(251,191,36,0.62);
  --match-block-accent: #FBBF24;
  --match-block-flash-bg: rgba(122,82,12,0.62);
  --match-inline-bg: rgba(251,191,36,0.24);
  --match-inline-border: rgba(253,210,76,0.72);
  --match-inline-text: #FFF8DF;
  --match-focus-ring: rgba(251,191,36,0.22);
  color-scheme: dark;
}

/* ── Reset ─────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; }
body {
  font-family: "Segoe UI Variable", "Microsoft YaHei UI", "Microsoft YaHei", -apple-system, sans-serif;
  font-size: 15px;
  line-height: 1.5;
  color: var(--text-primary);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
}
button { font: inherit; cursor: pointer; border: none; background: none; }
input, textarea, select { font: inherit; }
a { color: var(--accent); text-decoration: none; }

/* ── App Shell ─────────────────────────────────────────────── */
.app-shell {
  display: flex;
  height: 100vh;
  width: 100vw;
}

/* ── Sidebar ───────────────────────────────────────────────── */
.sidebar {
  width: var(--sidebar-width);
  min-width: var(--sidebar-width);
  height: 100vh;
  background: var(--sidebar-bg);
  border-right: 1px solid var(--divider);
  display: flex;
  flex-direction: column;
  padding: 16px 12px 12px;
  user-select: none;
  -webkit-app-region: drag;
}
.sidebar-brand {
  padding: 8px 10px 18px;
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.01em;
  -webkit-app-region: no-drag;
}
.sidebar-nav { flex: 1; display: flex; flex-direction: column; gap: 1px; }
.sidebar-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 12px;
  height: 40px;
  border-radius: var(--widget-radius);
  font-size: 14px;
  font-weight: 500;
  color: var(--text-secondary);
  transition: background var(--transition-fast), color var(--transition-fast);
  position: relative;
  -webkit-app-region: no-drag;
}
.sidebar-item:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.sidebar-item.active {
  background: var(--sidebar-active);
  color: var(--text-primary);
  font-weight: 600;
}
.sidebar-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 2px;
  background: var(--accent);
}
.sidebar-icon {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
  opacity: 0.7;
}
.sidebar-item.active .sidebar-icon { opacity: 1; }
.sidebar-footer {
  padding: 12px 10px 4px;
  font-size: 12px;
  color: var(--text-tertiary);
  border-top: 1px solid var(--divider);
  margin-top: 8px;
  -webkit-app-region: no-drag;
}

/* ── Main Area ─────────────────────────────────────────────── */
.main-area {
  flex: 1;
  height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

/* ── Page Container ────────────────────────────────────────── */
.page { display: none; flex-direction: column; height: 100%; overflow: hidden; }
.page.active { display: flex; }

/* ── Page Header ───────────────────────────────────────────── */
.page-header {
  padding: 28px 32px 0;
  flex-shrink: 0;
}
.page-header-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.page-title {
  font-size: 22px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.02em;
}
.page-subtitle {
  font-size: 14px;
  color: var(--text-secondary);
  margin-top: 4px;
}
.page-header-meta {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-shrink: 0;
}

/* ── Status badge ──────────────────────────────────────────── */
.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 500;
  background: var(--success-soft);
  color: var(--success);
}
.status-badge .dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--success);
}

/* ── Search box ────────────────────────────────────────────── */
.search-box-wrap {
  margin-top: 20px;
  position: relative;
}
.search-box {
  width: 100%;
  height: var(--input-height);
  padding: 0 52px 0 44px;
  border: 1.5px solid var(--divider);
  border-radius: var(--input-radius);
  background: var(--input-bg);
  font-size: 16px;
  color: var(--text-primary);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
  outline: none;
}
.search-box::placeholder { color: var(--text-tertiary); }
.search-box:focus {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.search-icon {
  position: absolute;
  left: 16px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-tertiary);
  font-size: 16px;
  pointer-events: none;
}
.search-submit {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  height: 38px;
  padding: 0 16px;
  border-radius: 8px;
  background: var(--accent);
  color: var(--accent-contrast);
  font-size: 14px;
  font-weight: 600;
  transition: opacity var(--transition-fast);
}
.search-submit:hover { opacity: 0.85; }

/* ── Segmented Control ─────────────────────────────────────── */
.segmented-control {
  display: inline-flex;
  background: var(--surface-secondary);
  border-radius: 9px;
  padding: 3px;
  margin-top: 16px;
  gap: 2px;
}
.seg-btn {
  padding: 6px 18px;
  border-radius: 7px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-secondary);
  transition: all var(--transition-fast);
  white-space: nowrap;
}
.seg-btn.active {
  background: var(--surface);
  color: var(--text-primary);
  box-shadow: var(--shadow-card);
  font-weight: 600;
}
.seg-btn:hover:not(.active) {
  color: var(--text-primary);
}

/* ── Search controls ───────────────────────────────────────── */
.search-controls-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding-bottom: 16px;
}
.search-controls-row .segmented-control { margin-top: 16px; flex: 0 0 auto; }
.search-filter-controls {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 9px;
  min-width: 0;
  margin-top: 16px;
}
.search-control-group { display: inline-flex; align-items: center; gap: 7px; min-width: 0; }
.search-control-label { color: var(--text-tertiary); font-size: 12px; white-space: nowrap; }
.source-type-control {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 36px;
  padding: 3px;
  border: 1px solid var(--border-default);
  border-radius: 10px;
  background: var(--surface-secondary);
}
.source-type-btn {
  height: 28px;
  min-width: 44px;
  padding: 0 10px;
  border-radius: 7px;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 550;
  transition: color var(--transition-fast), background var(--transition-fast), box-shadow var(--transition-fast);
}
.source-type-btn:hover { color: var(--text-primary); background: var(--surface-hover); }
.source-type-btn.active { color: var(--accent); background: var(--surface-primary); box-shadow: var(--shadow-card); }
.app-select { position: relative; min-width: 0; }
.app-select-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  height: 36px;
  min-width: 88px;
  padding: 0 10px 0 12px;
  border: 1px solid var(--border-default);
  border-radius: 10px;
  color: var(--text-primary);
  background: var(--input-bg);
  font-size: 12px;
  text-align: left;
  transition: border-color var(--transition-fast), background var(--transition-fast), box-shadow var(--transition-fast);
}
.app-select-trigger:hover { border-color: var(--border-strong); background: var(--surface-hover); }
.app-select-trigger:focus-visible { border-color: var(--accent); box-shadow: var(--focus-ring); outline: none; }
.app-select-trigger svg { width: 14px; height: 14px; flex: 0 0 auto; color: var(--text-tertiary); transition: transform var(--transition-fast); }
.app-select.is-open .app-select-trigger { border-color: var(--accent); box-shadow: var(--focus-ring); }
.app-select.is-open .app-select-trigger svg { transform: rotate(180deg); }
.document-select .app-select-trigger { width: clamp(170px, 18vw, 250px); }
.app-select-value { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.app-select-menu {
  display: none;
  position: absolute;
  z-index: 70;
  top: calc(100% + 7px);
  right: 0;
  width: 180px;
  padding: 6px;
  border: 1px solid var(--border-default);
  border-radius: 12px;
  background: var(--menu-bg);
  box-shadow: var(--shadow-popover);
}
.app-select.is-open .app-select-menu { display: block; }
.app-select-menu.document-menu { width: min(360px, calc(100vw - 48px)); padding: 8px; }
.app-select-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  width: 100%;
  min-height: 36px;
  padding: 7px 9px;
  border-radius: 8px;
  color: var(--text-secondary);
  font-size: 12px;
  text-align: left;
}
.app-select-option:hover { color: var(--text-primary); background: var(--surface-hover); }
.app-select-option.is-selected { color: var(--accent); background: var(--accent-soft); font-weight: 600; }
.app-select-option svg { width: 14px; height: 14px; flex: 0 0 auto; }
.document-menu-search-wrap { position: relative; margin-bottom: 6px; }
.document-menu-search-wrap > svg { position: absolute; left: 10px; top: 50%; width: 13px; height: 13px; transform: translateY(-50%); color: var(--text-tertiary); pointer-events: none; }
.document-menu-search {
  width: 100%;
  height: 34px;
  padding: 0 10px 0 31px;
  border: 1px solid var(--border-default);
  border-radius: 8px;
  background: var(--input-bg);
  color: var(--text-primary);
  font-size: 12px;
  outline: none;
}
.document-menu-search:focus { border-color: var(--accent); box-shadow: var(--focus-ring); }
.document-options { max-height: 270px; overflow-y: auto; }
.document-option-main { min-width: 0; }
.document-option-title { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: inherit; }
.document-option-meta { display: block; margin-top: 2px; color: var(--text-tertiary); font-size: 10px; }
.document-options-empty { padding: 18px 10px; color: var(--text-tertiary); font-size: 12px; text-align: center; }

.view-switch {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--border-default);
  border-radius: 10px;
  background: var(--surface-secondary);
}
.view-switch-btn {
  width: 32px;
  height: 30px;
  display: grid;
  place-items: center;
  border-radius: 7px;
  color: var(--text-tertiary);
  transition: color var(--transition-fast), background var(--transition-fast), box-shadow var(--transition-fast);
}
.view-switch-btn:hover { color: var(--text-primary); background: var(--surface-hover); }
.view-switch-btn.active { color: var(--accent); background: var(--surface-primary); box-shadow: var(--shadow-card); }
.view-switch-btn:focus-visible { outline: none; box-shadow: var(--focus-ring); }
.view-switch-btn svg { width: 16px; height: 16px; }

@media (max-width: 1180px) {
  .search-controls-row { align-items: flex-start; flex-direction: column; gap: 0; }
  .search-filter-controls { width: 100%; justify-content: flex-start; flex-wrap: wrap; margin-top: 10px; }
}
@media (max-width: 760px) {
  .search-control-label { display: none; }
  .document-select .app-select-trigger { width: min(220px, 42vw); }
}

/* ── Dual-pane results ─────────────────────────────────────── */
.results-area {
  flex: 1;
  display: flex;
  overflow: hidden;
  border-top: 1px solid var(--divider);
}
.results-list-pane {
  width: 420px;
  min-width: 320px;
  border-right: 1px solid var(--divider);
  overflow-y: auto;
  background: var(--surface);
}
.results-detail-pane {
  flex: 1;
  overflow-y: auto;
  background: var(--bg);
  padding: 28px 32px;
}

/* ── Status line ───────────────────────────────────────────── */
.results-status {
  padding: 10px 16px;
  font-size: 13px;
  color: var(--text-secondary);
  border-bottom: 1px solid var(--divider);
  background: var(--surface);
  flex-shrink: 0;
}

/* ── Result rows ───────────────────────────────────────────── */
.result-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--divider);
  cursor: pointer;
  transition: background var(--transition-fast);
  position: relative;
}
.result-row:hover { background: var(--surface-hover); }
.result-row.selected {
  background: var(--accent-light);
}
.result-row.selected::before {
  content: '';
  position: absolute;
  left: 0; top: 8px; bottom: 8px;
  width: 3px;
  border-radius: 2px;
  background: var(--accent);
}
.result-row-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.result-score {
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-light);
  padding: 1px 7px;
  border-radius: 6px;
  flex-shrink: 0;
}
.result-match-type {
  font-size: 11px;
  color: var(--text-tertiary);
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--surface-secondary);
  flex-shrink: 0;
}
.result-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.result-meta {
  font-size: 12px;
  color: var(--text-secondary);
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.result-meta span { white-space: nowrap; }
.result-snippet {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.6;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.result-snippet mark {
  background: var(--match-inline-bg);
  color: var(--match-inline-text);
  border: 1px solid var(--match-inline-border);
  padding: 0 3px;
  border-radius: 4px;
  -webkit-box-decoration-break: clone;
  box-decoration-break: clone;
}

/* ── Detail panel ──────────────────────────────────────────── */
.detail-card {
  background: var(--surface);
  border-radius: var(--card-radius);
  border: 1px solid var(--divider);
  overflow: hidden;
}
.detail-header {
  padding: 24px 28px 20px;
  border-bottom: 1px solid var(--divider);
}
.detail-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
  line-height: 1.4;
}
.detail-author {
  font-size: 14px;
  color: var(--text-secondary);
  margin-top: 4px;
}
.detail-pills {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.detail-pill {
  font-size: 12px;
  font-weight: 500;
  padding: 3px 10px;
  border-radius: 6px;
  background: var(--surface-secondary);
  color: var(--text-secondary);
}
.detail-pill.accent {
  background: var(--accent-light);
  color: var(--accent);
}
.detail-body {
  padding: 24px 28px;
}
.detail-context {
  font-size: 14px;
  color: var(--text-secondary);
  line-height: 1.85;
  padding: 12px 16px;
  border-left: 3px solid var(--border-default);
  border-radius: 10px;
  background: var(--surface-secondary);
  margin: 10px 0;
  white-space: pre-wrap;
}
.detail-hit {
  font-size: 15px;
  line-height: 1.95;
  color: var(--text-primary);
  white-space: pre-wrap;
  padding: 18px 20px 18px 22px;
  margin: 16px 0;
  border-radius: 12px;
  border: 1px solid var(--match-block-border);
  border-left: 3px solid var(--match-block-accent);
  background: var(--match-block-bg);
  transition: background var(--transition-normal), border-color var(--transition-normal), box-shadow var(--transition-normal);
}
.detail-hit mark {
  background: var(--match-inline-bg);
  color: var(--match-inline-text);
  border: 1px solid var(--match-inline-border);
  padding: 1px 4px;
  border-radius: 5px;
  -webkit-box-decoration-break: clone;
  box-decoration-break: clone;
}
.detail-hit.is-locating {
  animation: match-locate-pulse 620ms ease-out 1;
}
@keyframes match-locate-pulse {
  0% {
    background: var(--match-block-flash-bg);
    border-color: var(--match-block-accent);
    box-shadow: 0 0 0 4px var(--match-focus-ring);
  }
  100% {
    background: var(--match-block-bg);
    border-color: var(--match-block-border);
    box-shadow: 0 0 0 0 transparent;
  }
}

/* ── Page detail (expandable) ──────────────────────────────── */
.page-detail-toggle {
  font-size: 13px;
  color: var(--accent);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 0;
  margin-top: 4px;
}
.page-detail-toggle:hover { text-decoration: underline; }
.page-detail-body {
  display: none;
  margin-top: 8px;
  padding: 12px 16px;
  background: var(--surface-secondary);
  border-radius: var(--widget-radius);
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.7;
}
.page-detail-body.open { display: block; }
.page-detail-row {
  display: flex;
  justify-content: space-between;
  padding: 3px 0;
}
.page-detail-label { color: var(--text-tertiary); }

/* ── Action buttons ────────────────────────────────────────── */
.detail-actions {
  padding: 16px 28px 24px;
  border-top: 1px solid var(--divider);
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.action-btn {
  height: 36px;
  padding: 0 16px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  border: 1px solid var(--divider);
  background: var(--surface);
  color: var(--text-primary);
  transition: all var(--transition-fast);
}
.action-btn:hover { background: var(--surface-hover); }
.action-btn.primary {
  background: var(--accent);
  color: var(--accent-contrast);
  border-color: var(--accent);
}
.action-btn.primary:hover { opacity: 0.85; }
.citation-copy-group {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.citation-style-control .app-select-trigger { min-width: 126px; height: 36px; border-radius: 8px; font-size: 13px; }
.citation-style-control .app-select-menu { top: auto; bottom: calc(100% + 7px); right: auto; left: 0; width: 156px; }

/* ── Empty / placeholder states ────────────────────────────── */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-tertiary);
  text-align: center;
  padding: 40px;
  gap: 8px;
}
.empty-state-icon { font-size: 40px; opacity: 0.4; }
.empty-state-text { font-size: 15px; }
.empty-state-hint { font-size: 13px; }

/* ── Placeholder pages ─────────────────────────────────────── */
.placeholder-page {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  padding: 28px 32px;
}

/* ── Settings ───────────────────────────────────────────────── */
.settings-page-content {
  flex: 1;
  overflow-y: auto;
  padding: 28px 32px;
}
.settings-card {
  max-width: 760px;
  background: var(--surface);
  border: 1px solid var(--divider);
  border-radius: var(--card-radius);
  padding: 24px 28px 26px;
  box-shadow: var(--shadow-card);
}
.settings-card.appearance-card { max-width: 1180px; container-type: inline-size; }
.settings-card + .settings-card { margin-top: 16px; }
.settings-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}
.settings-card-title { font-size: 16px; font-weight: 650; color: var(--text-primary); }
.settings-card-subtitle { margin-top: 5px; font-size: 13px; line-height: 1.6; color: var(--text-secondary); }
.settings-status {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--surface-secondary);
  border-radius: 999px;
  padding: 5px 10px;
}
.settings-status.ready { color: var(--success); background: var(--success-soft); }
.settings-status.warning { color: var(--warning); background: var(--warning-soft); }
.settings-form { display: grid; gap: 16px; }
.settings-field { display: grid; gap: 7px; }
.settings-field label { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.settings-field small { font-size: 12px; color: var(--text-tertiary); line-height: 1.5; }
.settings-input-row { display: flex; gap: 8px; align-items: center; }
.settings-input {
  width: 100%;
  height: 40px;
  box-sizing: border-box;
  padding: 0 11px;
  border: 1px solid var(--divider);
  border-radius: 8px;
  background: var(--input-bg);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
}
.settings-input:focus { border-color: var(--accent); box-shadow: var(--focus-ring); }
.settings-input::placeholder { color: var(--text-tertiary); }
.settings-input[type="date"] { max-width: 220px; }
.settings-toggle { flex-shrink: 0; height: 40px; padding: 0 12px; }
.settings-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 4px; }
.settings-hint { font-size: 12px; color: var(--text-tertiary); }
.settings-notice {
  margin-top: 18px;
  padding: 12px 14px;
  border-radius: var(--widget-radius);
  background: var(--info-soft);
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.65;
}
.settings-expiry-detail {
  margin: -4px 0 16px;
  padding: 10px 12px;
  border-radius: var(--widget-radius);
  background: var(--surface-secondary);
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.5;
}
.settings-expiry-detail.ready { color: var(--success); background: var(--success-soft); }
.settings-expiry-detail.warning { color: var(--warning); background: var(--warning-soft); }
.settings-expiry-detail.error { color: var(--danger); background: var(--danger-soft); }

.theme-options {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 18px;
}
.theme-option {
  position: relative;
  min-width: 0;
  padding: 14px;
  border: 2px solid var(--border-default);
  border-radius: 12px;
  background: var(--surface-secondary);
  color: var(--text-primary);
  text-align: left;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  height: 100%;
  transition: transform 180ms ease, border-color 180ms ease, background 180ms ease, box-shadow 180ms ease;
}
.theme-option:hover { transform: translateY(-1px); border-color: var(--border-strong); background: var(--surface-hover); box-shadow: var(--shadow-card); }
.theme-option:active { transform: translateY(0); }
.theme-option:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--focus-ring); }
.theme-option.selected { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft), var(--shadow-card); }
.theme-option-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
.theme-option-identity { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
.theme-option-name { font-size: 14px; font-weight: 650; }
.theme-option-tone { padding: 2px 6px; border-radius: 5px; background: var(--surface-primary); border: 1px solid var(--border-subtle); color: var(--text-tertiary); font-size: 10px; line-height: 1.35; }
.theme-option-check {
  width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; flex: 0 0 auto;
  background: var(--accent); color: var(--accent-contrast); opacity: 0; transform: scale(.82);
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}
.theme-option.selected .theme-option-check { opacity: 1; transform: scale(1); }
.theme-option-description { display: block; min-height: 35px; margin-top: 9px; color: var(--text-secondary); font-size: 12px; line-height: 1.45; }
.theme-preview {
  height: 124px;
  display: grid;
  grid-template-columns: 26% minmax(0, 74%);
  overflow: hidden;
  border: 1px solid var(--border-default);
  border-radius: 11px;
  background: var(--app-bg);
  color: var(--text-primary);
  box-shadow: inset 0 0 0 1px var(--border-subtle);
  pointer-events: none;
}
.theme-mini-sidebar {
  min-width: 0;
  padding: 8px 6px;
  border-right: 1px solid var(--border-default);
  background: var(--sidebar-bg);
}
.theme-mini-brand { height: 18px; display: flex; align-items: center; gap: 5px; margin: 0 3px 8px; }
.theme-mini-brand-mark { width: 11px; height: 11px; border-radius: 3px; border: 1px solid var(--border-strong); background: var(--surface-primary); }
.theme-mini-brand-line { width: 40%; height: 4px; border-radius: 3px; background: var(--text-primary); opacity: .8; }
.theme-mini-nav { display: grid; gap: 4px; }
.theme-mini-nav-item {
  position: relative;
  height: 17px;
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 0 5px;
  border-radius: 4px;
  color: var(--text-tertiary);
}
.theme-mini-nav-item.is-selected { background: var(--accent-soft); color: var(--accent); }
.theme-mini-nav-item.is-selected::before { content: ''; position: absolute; left: 0; top: 3px; bottom: 3px; width: 2px; border-radius: 2px; background: var(--accent); }
.theme-mini-nav-icon { width: 7px; height: 7px; border: 1px solid currentColor; border-radius: 2px; flex: 0 0 auto; }
.theme-mini-nav-line { height: 3px; width: 48%; border-radius: 3px; background: currentColor; opacity: .78; }
.theme-mini-main { min-width: 0; padding: 8px; background: var(--app-bg); }
.theme-mini-header { height: 20px; display: flex; align-items: flex-start; justify-content: space-between; gap: 5px; }
.theme-mini-heading { min-width: 0; display: grid; gap: 3px; flex: 1; }
.theme-mini-title-line { width: 42%; height: 4px; border-radius: 3px; background: var(--text-primary); }
.theme-mini-subtitle-line { width: 29%; height: 3px; border-radius: 3px; background: var(--text-secondary); opacity: .7; }
.theme-mini-header-status { height: 10px; display: inline-flex; align-items: center; gap: 3px; padding: 0 4px; border-radius: 5px; background: var(--success-soft); color: var(--success); flex: 0 0 auto; }
.theme-mini-header-status i { width: 3px; height: 3px; border-radius: 50%; background: currentColor; }
.theme-mini-header-status b { width: 12px; height: 2px; border-radius: 2px; background: currentColor; opacity: .8; }
.theme-mini-search {
  height: 20px;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 3px 0 5px;
  margin-bottom: 6px;
  border: 1px solid var(--border-default);
  border-radius: 5px;
  background: var(--input-bg);
  color: var(--text-tertiary);
}
.theme-mini-search svg { width: 9px; height: 9px; flex: 0 0 auto; }
.theme-mini-search-line { width: 42%; height: 3px; border-radius: 3px; background: var(--text-tertiary); opacity: .55; }
.theme-mini-search-action { width: 20px; height: 12px; margin-left: auto; border-radius: 3px; background: var(--accent); }
.theme-mini-cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; }
.theme-mini-doc-card {
  min-width: 0;
  height: 61px;
  padding: 5px;
  border: 1px solid var(--border-default);
  border-radius: 6px;
  background: var(--surface-primary);
  box-shadow: var(--shadow-card);
}
.theme-mini-card-top { display: flex; align-items: center; justify-content: space-between; gap: 3px; margin-bottom: 6px; }
.theme-mini-source { width: 18px; height: 7px; border-radius: 3px; background: var(--surface-secondary); border: 1px solid var(--border-subtle); }
.theme-mini-state { width: 19px; height: 8px; border-radius: 4px; border: 1px solid currentColor; flex: 0 0 auto; position: relative; }
.theme-mini-state::before { content: ''; position: absolute; left: 3px; top: 2px; width: 2px; height: 2px; border-radius: 50%; background: currentColor; }
.theme-mini-state::after { content: ''; position: absolute; left: 7px; right: 3px; top: 2px; height: 2px; border-radius: 2px; background: currentColor; opacity: .72; }
.theme-mini-state.is-success { color: var(--success); background: var(--success-soft); border-color: var(--success-border); }
.theme-mini-state.is-danger { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-border); }
.theme-mini-doc-title { display: block; width: 78%; height: 4px; border-radius: 3px; background: var(--text-primary); opacity: .8; }
.theme-mini-doc-title.is-short { width: 54%; margin-top: 4px; opacity: .62; }
.theme-mini-doc-meta { display: block; width: 43%; height: 3px; margin-top: 8px; border-radius: 3px; background: var(--text-tertiary); opacity: .55; }
.theme-mini-match { display: block; width: 70%; height: 7px; margin-top: 6px; border-left: 2px solid var(--match-block-accent); border-radius: 2px; background: var(--match-inline-bg); box-shadow: inset 0 0 0 1px var(--match-inline-border); }
@container (min-width: 640px) { .theme-options { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@container (min-width: 960px) { .theme-options { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 430px) { .theme-mini-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } .theme-mini-doc-card:nth-child(3) { display: none; } }

/* ── Library list ──────────────────────────────────────────── */
.library-list-container {
  background: var(--surface);
  border-radius: var(--card-radius);
  border: 1px solid var(--divider);
  overflow: hidden;
  margin-top: 16px;
}
.library-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--divider);
  cursor: pointer;
  transition: background var(--transition-fast);
  font-size: 14px;
}
.library-row:last-child { border-bottom: none; }
.library-row:hover { background: var(--surface-hover); }
.type-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 5px;
  flex-shrink: 0;
}
.type-badge.word { background: var(--info-soft); color: var(--info); }
.type-badge.pdf { background: var(--warning-soft); color: var(--warning); }
.type-badge.mineru { background: var(--success-soft); color: var(--success); }

/* ── Library page layout ──────────────────────────────────── */
.library-toolbar { padding: 0 32px 16px; }
.library-search-wrap { position: relative; margin-top: 16px; }
.library-search {
  width: 100%;
  height: 42px;
  padding: 0 14px 0 38px;
  border: 1.5px solid var(--divider);
  border-radius: var(--widget-radius);
  background: var(--input-bg);
  font-size: 14px;
  color: var(--text-primary);
  outline: none;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}
.library-search::placeholder { color: var(--text-tertiary); }
.library-search:focus {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.library-search-icon {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-tertiary);
  pointer-events: none;
}
.library-controls-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 14px;
}
.library-controls-row .segmented-control { margin-top: 14px; }
.library-controls-row .view-switch { margin-top: 14px; flex: 0 0 auto; }
.library-toolbar-right {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 14px;
  flex-wrap: wrap;
}
.library-controls-row .library-toolbar-right .view-switch { margin-top: 0; }
.library-sort-controls { display: flex; align-items: center; gap: 8px; }
.library-sort-label { font-size: 12px; color: var(--text-tertiary); }
.library-sort-controls .app-select-trigger { height: 38px; color: var(--text-secondary); font-size: 13px; }
.library-sort-field-select .app-select-trigger { min-width: 142px; }
.library-sort-direction-select .app-select-trigger { min-width: 88px; }
.library-sort-controls .app-select-menu { width: 178px; }
.library-sort-direction-select .app-select-menu { width: 112px; }
.library-body {
  flex: 1;
  display: flex;
  overflow: hidden;
  border-top: 1px solid var(--divider);
  position: relative;
}
.library-list-scroll {
  flex: 1;
  overflow-y: auto;
  background: var(--surface);
}
.library-row { position: relative; }
.library-row.selected { background: var(--accent-light); }
.library-row.selected::before {
  content: '';
  position: absolute;
  left: 0; top: 6px; bottom: 6px;
  width: 3px;
  border-radius: 2px;
  background: var(--accent);
}
.library-row-title {
  flex: 1;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.library-row-info {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-tertiary);
}
.library-row-info .works-count {
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--surface-secondary);
  padding: 1px 8px;
  border-radius: 5px;
}
.library-list-container.library-view-list {
  margin: 0;
  border: 0;
  border-radius: 0;
}
.library-list-container.library-view-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin: 0;
  padding: 18px 24px 28px;
  overflow: visible;
  border: 0;
  border-radius: 0;
  background: var(--app-bg);
}
.library-card {
  position: relative;
  min-width: 0;
  min-height: 218px;
  display: flex;
  flex-direction: column;
  padding: 17px;
  border: 1px solid var(--border-default);
  border-radius: 14px;
  background: var(--surface-primary);
  box-shadow: var(--shadow-card);
  cursor: pointer;
  transition: transform var(--transition-fast), border-color var(--transition-fast), box-shadow var(--transition-fast), background var(--transition-fast);
}
.library-card:hover { transform: translateY(-1px); border-color: var(--border-strong); box-shadow: var(--shadow-card-hover); }
.library-card.selected { border-color: var(--accent); background: var(--surface-selected); box-shadow: var(--focus-ring); }
.library-card-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.library-card-badges { display: flex; align-items: center; gap: 6px; min-width: 0; }
.library-card-status { overflow: hidden; color: var(--text-tertiary); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.library-card-title { margin-top: 16px; min-height: 47px; color: var(--text-primary); font-size: 16px; font-weight: 650; line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.library-card-author { min-height: 20px; margin-top: 4px; overflow: hidden; color: var(--text-secondary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.library-card-meta { margin-top: 15px; color: var(--text-tertiary); font-size: 12px; }
.library-card-mapping { min-height: 18px; margin-top: 6px; overflow: hidden; color: var(--text-secondary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.library-card-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: auto; padding-top: 15px; }
.library-card-action { color: var(--accent); font-size: 12px; font-weight: 600; }
.library-card-date { color: var(--text-tertiary); font-size: 11px; white-space: nowrap; }
.library-view-grid > .empty-state { grid-column: 1 / -1; }
@media (max-width: 1220px) { .library-list-container.library-view-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 780px) { .library-list-container.library-view-grid { grid-template-columns: 1fr; padding: 14px; } }
@media (max-width: 900px) {
  .library-controls-row { align-items: flex-start; flex-wrap: wrap; }
  .library-toolbar-right { width: 100%; justify-content: space-between; }
}

/* ── Library drawer ───────────────────────────────────────── */
.library-drawer {
  width: 0;
  min-width: 0;
  overflow: hidden;
  background: var(--bg);
  border-left: 1px solid var(--divider);
  transition: width var(--transition-normal), min-width var(--transition-normal);
  flex-shrink: 0;
}
.library-drawer.open {
  width: 420px;
  min-width: 420px;
}
.library-drawer-inner {
  width: 420px;
  height: 100%;
  overflow-y: auto;
  padding: 24px;
}
.drawer-close {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 20px;
  padding: 4px 0;
}
.drawer-close:hover { color: var(--accent); }
.drawer-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
  line-height: 1.4;
}
.drawer-subtitle {
  font-size: 14px;
  color: var(--text-secondary);
  margin-top: 4px;
}
.drawer-info {
  margin-top: 20px;
  background: var(--surface);
  border-radius: var(--widget-radius);
  border: 1px solid var(--divider);
  overflow: hidden;
}
.drawer-info-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  font-size: 13px;
  border-bottom: 1px solid var(--divider);
}
.drawer-info-row:last-child { border-bottom: none; }
.drawer-info-label { color: var(--text-secondary); }
.drawer-info-value { color: var(--text-primary); font-weight: 500; }
.drawer-section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 24px 0 10px;
}
.drawer-works-list {
  background: var(--surface);
  border-radius: var(--widget-radius);
  border: 1px solid var(--divider);
  overflow: hidden;
  max-height: 300px;
  overflow-y: auto;
}
.drawer-work-item {
  padding: 8px 14px;
  font-size: 13px;
  border-bottom: 1px solid var(--divider);
  line-height: 1.5;
}
.drawer-work-item:last-child { border-bottom: none; }
.drawer-work-title { color: var(--text-primary); font-weight: 500; }
.drawer-work-meta {
  font-size: 12px;
  color: var(--text-tertiary);
  margin-top: 2px;
}
.drawer-actions {
  margin-top: 20px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.drawer-actions .action-btn { flex: 0 0 auto; white-space: nowrap; }

/* ── Import page ──────────────────────────────────────────── */
.import-content {
  flex: 1;
  overflow-y: auto;
  padding: 0 32px 32px;
}
.drop-zone {
  margin-top: 20px;
  border: 2px dashed var(--divider);
  border-radius: var(--card-radius);
  background: var(--surface);
  padding: 56px 40px;
  text-align: center;
  cursor: pointer;
  transition: border-color var(--transition-fast), background var(--transition-fast);
}
.drop-zone:hover, .drop-zone.dragover {
  border-color: var(--accent);
  background: var(--accent-light);
}
.drop-zone-icon {
  font-size: 48px;
  opacity: 0.25;
  margin-bottom: 16px;
}
.drop-zone-text {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 6px;
}
.drop-zone-hint {
  font-size: 13px;
  color: var(--text-tertiary);
}
.drop-zone-formats {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin-top: 16px;
}
.format-tag {
  font-size: 12px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 6px;
  background: var(--surface-secondary);
  color: var(--text-secondary);
}
.import-queue {
  margin-top: 24px;
}
.import-queue-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 10px;
}
.import-item {
  background: var(--surface);
  border: 1px solid var(--divider);
  border-radius: var(--widget-radius);
  padding: 14px 16px;
  margin-bottom: 8px;
}
.import-item-header {
  display: flex;
  align-items: center;
  gap: 10px;
}
.import-item-name {
  flex: 1;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.import-item-size {
  font-size: 12px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}
.import-route-badge {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
  border: 1px solid transparent;
}
.import-route-badge.native {
  color: var(--success);
  background: var(--success-soft);
  border-color: var(--success-border);
}
.import-route-badge.mineru {
  color: var(--warning);
  background: var(--warning-soft);
  border-color: var(--warning-border);
}
.import-item-remove {
  font-size: 18px;
  color: var(--text-tertiary);
  padding: 0 4px;
  line-height: 1;
}
.import-item-remove:hover { color: var(--error); }
.import-steps {
  margin-top: 10px;
  display: flex;
  gap: 4px;
}
.import-step {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}
.import-step-bar {
  width: 100%;
  height: 3px;
  border-radius: 2px;
  background: var(--border-default);
}
.import-step-bar.done { background: var(--success); }
.import-step-bar.active { background: var(--accent); animation: pulse-bar 1.2s ease infinite; }
.import-step-bar.error { background: var(--error); }
.import-step-label {
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
}
.import-step.done .import-step-label { color: var(--success); }
.import-step.active .import-step-label { color: var(--accent); font-weight: 600; }
.import-step.error .import-step-label { color: var(--error); }
@keyframes pulse-bar {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.import-item-status {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.import-item-status.error { color: var(--error); }
.import-item-status.done { color: var(--success); }

/* ── Calibration page ─────────────────────────────────────── */
#page-calibration .page-header {
  padding: 22px 32px 14px;
}
#page-calibration .page-title {
  font-size: 29px;
  line-height: 1.18;
  letter-spacing: 0;
}
#page-calibration .page-subtitle {
  margin-top: 6px;
  font-size: 14px;
}
.calibration-content {
  flex: 1;
  overflow-y: auto;
  padding: 0 32px 32px;
}
.calibration-header-stats {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.status-stat {
  height: 32px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: var(--surface-secondary);
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  line-height: 1;
  overflow: visible;
  transition: transform 180ms ease, box-shadow 180ms ease, border-color 180ms ease;
}
.status-stat:hover { transform: translateY(-1px); }
.status-stat:active { transform: scale(0.98); }
.status-stat.active, .status-stat:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--surface-primary), 0 0 0 3px currentColor; }
.status-stat__icon { width: 16px; height: 16px; flex: 0 0 auto; display: inline-grid; place-items: center; opacity: 1; visibility: visible; }
.status-stat__icon svg { width: 16px; height: 16px; display: block; flex: 0 0 auto; overflow: visible; }
.status-stat__label, .status-stat__count { display: inline-block; color: inherit; }
.status-stat__count { font-variant-numeric: tabular-nums; font-weight: 650; }
.status-stat--info, .status-stat--info:hover, .status-stat--info.active { color: var(--info); background: var(--info-soft); border-color: var(--info-border); }
.status-stat--info .status-stat__icon { color: var(--info-icon); opacity: 1; }
.status-stat--success, .status-stat--success:hover, .status-stat--success.active { color: var(--success); background: var(--success-soft); border-color: var(--success-border); }
.status-stat--success .status-stat__icon { color: var(--success-icon); opacity: 1; }
.status-stat--neutral, .status-stat--neutral:hover, .status-stat--neutral.active { color: var(--neutral); background: var(--neutral-soft); border-color: var(--neutral-border); }
.status-stat--neutral .status-stat__icon { color: var(--neutral-icon); opacity: 1; }
.status-stat--warning, .status-stat--warning:hover, .status-stat--warning.active { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-border); }
.status-stat--warning .status-stat__icon { color: var(--warning-icon); opacity: 1; }
.status-stat--danger, .status-stat--danger:hover, .status-stat--danger.active { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-border); }
.status-stat--danger .status-stat__icon { color: var(--danger-icon); opacity: 1; }
.calibration-body {
  flex: 1;
  display: flex;
  min-height: 0;
  overflow: hidden;
  border-top: 1px solid var(--border-subtle);
}
.calibration-body.detail-open {
  display: grid;
  grid-template-columns: minmax(320px, 36fr) minmax(0, 64fr);
}
.cal-library-pane {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 14px 32px 32px;
}
.calibration-body.detail-open .cal-library-pane {
  padding: 14px 18px 28px;
}
.cal-toolbar { display: grid; gap: 16px; }
.cal-toolbar-top {
  display: flex;
  align-items: center;
  gap: 11px;
}
.cal-search-wrap { position: relative; flex: 1; min-width: 180px; }
.cal-search {
  width: 100%;
  height: 44px;
  padding: 0 14px 0 40px;
  border: 1px solid var(--divider);
  border-radius: 12px;
  background: var(--input-bg);
  color: var(--text-primary);
  font-size: 14px;
  outline: none;
  transition: border-color 180ms ease, box-shadow 180ms ease;
}
.cal-search:focus {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.cal-search-icon { position: absolute; left: 14px; top: 50%; transform: translateY(-50%); color: var(--text-tertiary); display: grid; place-items: center; }
.cal-refresh-btn {
  width: 44px;
  height: 44px;
  min-width: 44px;
  padding: 0;
  display: grid;
  place-items: center;
  border-radius: 12px;
  border: 1px solid var(--divider);
  background: var(--input-bg);
  color: var(--text-primary);
  transition: background 180ms ease, border-color 180ms ease, transform 120ms ease, opacity 180ms ease;
}
.cal-refresh-btn:hover:not(:disabled) { background: var(--surface-hover); border-color: var(--border-strong); }
.cal-refresh-btn:active:not(:disabled) { transform: scale(0.96); }
.cal-refresh-btn:disabled { cursor: default; opacity: 0.62; }
.cal-refresh-btn svg { width: 18px; height: 18px; display: block; transform-origin: 50% 50%; }
.cal-refresh-btn.refreshing svg { animation: cal-refresh-spin 760ms linear infinite; }
@keyframes cal-refresh-spin { to { transform: rotate(360deg); } }
.cal-filter-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.cal-status-tabs { display: flex; gap: 7px; flex-wrap: wrap; }
.cal-status-tab {
  height: 38px;
  padding: 0 15px;
  border-radius: 10px;
  color: var(--text-secondary);
  font-size: 13px;
  background: var(--surface-secondary);
  border: 1px solid transparent;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  flex: 0 0 auto;
  white-space: nowrap;
  line-height: 1;
  transition: background 180ms ease, color 180ms ease, border-color 180ms ease;
}
.cal-status-tab:hover { background: var(--surface-hover); color: var(--text-primary); }
.cal-status-tab.active { font-weight: 600; box-shadow: 0 0 0 2px var(--surface-primary), 0 0 0 3px currentColor; }
.cal-status-tab.status-stat--info { color: var(--info); background: var(--info-soft); border-color: var(--info-border); }
.cal-status-tab.status-stat--success { color: var(--success); background: var(--success-soft); border-color: var(--success-border); }
.cal-status-tab.status-stat--neutral { color: var(--neutral); background: var(--neutral-soft); border-color: var(--neutral-border); }
.cal-status-tab.status-stat--warning { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-border); }
.cal-status-tab.status-stat--danger { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-border); }
.cal-status-tab .status-stat__icon, .cal-status-tab .status-stat__icon svg { width: 15px; height: 15px; }
.cal-status-tab .status-stat__icon { display: inline-grid; place-items: center; flex: 0 0 15px; }
.cal-status-tab__label { display: inline-block; color: inherit; }
.cal-toolbar-right { display: flex; align-items: center; justify-content: flex-end; gap: 10px; }
.cal-sort-controls { display: flex; align-items: center; gap: 8px; }
.cal-sort-label { font-size: 12px; color: var(--text-tertiary); }
.cal-sort-controls .app-select-trigger { height: 38px; color: var(--text-secondary); font-size: 13px; }
.cal-sort-field-select .app-select-trigger { min-width: 142px; }
.cal-sort-direction-select .app-select-trigger { min-width: 88px; }
.cal-sort-controls .app-select-menu { width: 178px; }
.cal-sort-direction-select .app-select-menu { width: 112px; }
.cal-card-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 15px;
  margin-top: 16px;
}
.cal-card-grid.is-list { grid-template-columns: 1fr; gap: 8px; }
.calibration-body.detail-open .cal-card-grid { grid-template-columns: minmax(0, 1fr); gap: 12px; }
.cal-doc-card {
  position: relative;
  min-height: 226px;
  padding: 18px 19px;
  border: 1px solid var(--divider);
  border-radius: 16px;
  background: var(--surface);
  box-shadow: var(--shadow-card);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  transition: transform 180ms ease, border-color 180ms ease, box-shadow 180ms ease, opacity 180ms ease;
}
.cal-doc-card:hover { transform: translateY(-1.5px); border-color: var(--border-strong); box-shadow: var(--shadow-card-hover); }
.cal-doc-card.selected {
  border-color: var(--accent);
  background: var(--surface-selected);
  box-shadow: var(--focus-ring);
}
.cal-doc-card.selected::before {
  content: '';
  position: absolute;
  left: -1px;
  top: 14px;
  bottom: 14px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--accent);
}
.calibration-body.detail-open .cal-doc-card { min-height: 210px; padding: 16px 17px; }
.calibration-body.detail-open .cal-card-title { margin-top: 11px; }
.calibration-body.detail-open .cal-card-meta { margin-top: 10px; }
.cal-card-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.cal-card-badges { display: flex; align-items: center; gap: 5px; min-width: 0; }
.cal-doc-card .type-badge { padding: 3px 8px; border-radius: 7px; background: var(--surface-secondary); color: var(--text-secondary); font-size: 11px; font-weight: 600; }
.cal-doc-card .type-badge.pdf, .cal-doc-card .type-badge.mineru { background: var(--surface-secondary); color: var(--text-secondary); }
.status-chip, .cal-status-badge { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 650; padding: 3px 8px; border: 1px solid transparent; border-radius: 7px; white-space: nowrap; transition: opacity 180ms ease, background 180ms ease, color 180ms ease; }
.status-chip__icon { width: 12px; height: 12px; display: inline-grid; place-items: center; flex: 0 0 auto; opacity: 1; }
.status-chip__icon svg { width: 12px; height: 12px; display: block; overflow: visible; }
.status-chip__icon.is-spinning svg { animation: cal-refresh-spin 720ms linear infinite; }
.status-chip--success, .cal-status-badge.calibrated { color: var(--success); background: var(--success-soft); border-color: var(--success-border); }
.status-chip--neutral, .cal-status-badge.pending { color: var(--neutral); background: var(--neutral-soft); border-color: var(--neutral-border); }
.status-chip--warning, .cal-status-badge.review { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-border); }
.status-chip--danger, .cal-status-badge.failed { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-border); }
.status-chip--info, .cal-status-badge.mapping { color: var(--info); background: var(--info-soft); border-color: var(--info-border); }
.status-chip--success .status-chip__icon { color: var(--success-icon); }
.status-chip--neutral .status-chip__icon { color: var(--neutral-icon); }
.status-chip--warning .status-chip__icon { color: var(--warning-icon); }
.status-chip--danger .status-chip__icon { color: var(--danger-icon); }
.status-chip--info .status-chip__icon { color: var(--info-icon); }
.cal-more-btn { width: 30px; height: 30px; display: grid; place-items: center; color: var(--text-secondary); border-radius: 8px; font-size: 18px; line-height: 1; }
.cal-more-btn:hover { background: var(--surface-hover); }
.cal-more-menu { position: absolute; z-index: 8; right: 14px; top: 46px; width: 180px; padding: 6px; border: 1px solid var(--divider); border-radius: 12px; background: var(--menu-bg); box-shadow: var(--shadow-popover); }
.cal-more-menu button { width: 100%; height: 38px; padding: 0 10px; border-radius: 8px; text-align: left; font-size: 13px; color: var(--text-primary); }
.cal-more-menu button:hover { background: var(--surface-hover); }
.cal-more-menu .menu-divider { height: 1px; margin: 5px 4px; background: var(--divider); }
.cal-more-menu button.danger { color: var(--error); }
.cal-more-menu button.danger:hover { background: var(--danger-soft); }
.cal-card-title { margin-top: 14px; font-size: 17px; line-height: 1.38; font-weight: 650; color: var(--text-primary); letter-spacing: 0; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.cal-card-author { margin-top: 4px; min-height: 20px; font-size: 13px; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cal-card-meta { margin-top: 13px; font-size: 12px; line-height: 1.55; color: var(--text-tertiary); }
.cal-card-mapping { margin-top: 5px; min-height: 20px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-secondary); font-size: 13px; }
.cal-card-mapping.failed { min-height: 0; padding: 6px 8px; color: var(--danger); background: var(--danger-soft); border: 1px solid var(--danger-border); border-radius: 7px; }
.cal-card-mapping.review { min-height: 0; padding: 6px 8px; color: var(--warning); background: var(--warning-soft); border: 1px solid var(--warning-border); border-radius: 7px; }
.cal-card-segments { min-height: 17px; color: var(--text-tertiary); font-size: 12px; }
.cal-card-method { min-height: 18px; margin-top: 1px; color: var(--success); font-size: 12px; }
.cal-card-method.failed { color: var(--error); }
.cal-card-method.review { color: var(--warning); }
.cal-card-footer { margin-top: auto; padding-top: 12px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.cal-card-action { height: 30px; padding: 0 7px; margin-left: -7px; border-radius: 7px; font-size: 12px; color: var(--text-secondary); font-weight: 600; }
.cal-card-action:hover { background: var(--surface-hover); color: var(--text-primary); }
.cal-card-action.needs-action { color: var(--accent); }
.cal-card-action.needs-action:hover { background: var(--accent-soft); }
.cal-card-date { font-size: 11px; color: var(--text-tertiary); white-space: nowrap; }
.cal-doc-row {
  position: relative;
  min-width: 0;
  min-height: 76px;
  display: grid;
  grid-template-columns: minmax(230px, 1.35fr) minmax(130px, .65fr) minmax(220px, 1fr) auto;
  align-items: center;
  gap: 16px;
  padding: 13px 16px;
  border: 1px solid var(--border-default);
  border-radius: 12px;
  background: var(--surface-primary);
  box-shadow: var(--shadow-card);
  cursor: pointer;
  transition: border-color var(--transition-fast), background var(--transition-fast), box-shadow var(--transition-fast), opacity var(--transition-fast);
}
.cal-doc-row:hover { border-color: var(--border-strong); background: var(--surface-hover); box-shadow: var(--shadow-card-hover); }
.cal-doc-row.selected { border-color: var(--accent); background: var(--surface-selected); box-shadow: var(--focus-ring); }
.cal-row-identity { min-width: 0; }
.cal-row-badges { display: flex; align-items: center; gap: 5px; margin-bottom: 7px; }
.cal-row-title { overflow: hidden; color: var(--text-primary); font-size: 14px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.cal-row-author { margin-top: 2px; overflow: hidden; color: var(--text-secondary); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.cal-row-file { color: var(--text-tertiary); font-size: 12px; line-height: 1.55; }
.cal-row-mapping { min-width: 0; }
.cal-row-mapping-main { overflow: hidden; color: var(--text-secondary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.cal-row-mapping-main.failed { color: var(--danger); }
.cal-row-mapping-main.review { color: var(--warning); }
.cal-row-mapping-sub { margin-top: 4px; overflow: hidden; color: var(--text-tertiary); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.cal-row-actions { display: flex; align-items: center; justify-content: flex-end; gap: 5px; }
.cal-doc-row .cal-more-btn { flex: 0 0 auto; }
.cal-doc-row .cal-more-menu { top: 54px; right: 12px; }
.cal-row-date { color: var(--text-tertiary); font-size: 10px; white-space: nowrap; }
.cal-doc-row .cal-card-action { margin-left: 0; }
.calibration-body.detail-open .cal-doc-row { grid-template-columns: minmax(190px, 1fr) minmax(155px, .8fr) auto; }
.calibration-body.detail-open .cal-row-file { display: none; }
@media (max-width: 1220px) {
  .cal-doc-row { grid-template-columns: minmax(220px, 1.25fr) minmax(210px, 1fr) auto; }
  .cal-row-file { display: none; }
}
@media (max-width: 820px) {
  .cal-toolbar-right { width: 100%; justify-content: space-between; }
  .cal-doc-row, .calibration-body.detail-open .cal-doc-row { grid-template-columns: minmax(0, 1fr) auto; }
  .cal-row-mapping { grid-column: 1 / -1; grid-row: 2; }
}
.cal-skeleton { cursor: default; overflow: hidden; }
.cal-skeleton:hover { transform: none; }
.skeleton-line { height: 11px; border-radius: 5px; margin-top: 12px; background: linear-gradient(90deg, var(--skeleton-base), var(--skeleton-highlight), var(--skeleton-base)); background-size: 220% 100%; animation: skeleton-move 1.3s linear infinite; }
@keyframes skeleton-move { to { background-position: -220% 0; } }
.cal-grid-empty { grid-column: 1 / -1; min-height: 260px; display: grid; place-items: center; text-align: center; color: var(--text-secondary); }
.cal-empty-title { font-size: 15px; font-weight: 600; color: var(--text-primary); }
.cal-empty-note { margin-top: 6px; font-size: 12px; color: var(--text-tertiary); }
.cal-detail-drawer { height: 100%; overflow: hidden; }
.calibration-body.detail-open .cal-detail-drawer.open { width: auto; min-width: 0; }
.cal-detail-drawer .library-drawer-inner {
  width: 100%;
  padding: 24px 28px 32px;
  overflow-y: auto;
  overflow-x: hidden;
}
.cal-detail-actions { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 16px; }
.cal-detail-status { margin-top: 10px; display: flex; gap: 6px; flex-wrap: wrap; }
.cal-danger-zone { margin-top: 28px; padding-top: 16px; border-top: 1px solid var(--divider); }
.danger-outline { color: var(--danger); border-color: var(--danger); }
.danger-outline:hover { background: var(--danger-soft); }
.cal-doc-info {
  margin-top: 16px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.cal-section {
  margin-top: 24px;
}
.cal-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.cal-section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.auto-detect-panel {
  margin-top: 14px;
  padding: 16px;
  border: 1px solid var(--divider);
  border-radius: 8px;
  background: var(--surface);
}
.auto-detect-title { font-size: 14px; font-weight: 600; margin-bottom: 8px; }
.auto-detect-note { font-size: 12px; color: var(--text-secondary); line-height: 1.6; }
.auto-detect-warning { color: var(--warning); margin: 8px 0; }
.auto-segment-list { display: grid; gap: 8px; margin-top: 12px; }
.auto-segment-row { padding: 10px 12px; border: 1px solid var(--divider); border-radius: 6px; }
.auto-segment-main { font-size: 13px; font-weight: 600; }
.auto-segment-evidence { margin-top: 4px; font-size: 12px; color: var(--text-secondary); line-height: 1.5; }
.auto-detect-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.bibliographic-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
.bibliographic-field { min-width: 0; }
.bibliographic-field.full { grid-column: 1 / -1; }
.bibliographic-field label { display: block; font-size: 11px; color: var(--text-secondary); margin-bottom: 4px; }
.bibliographic-field input { width: 100%; height: 34px; padding: 0 9px; border: 1px solid var(--divider); border-radius: 6px; background: var(--input-bg); color: var(--text-primary); }
.bibliographic-meta { margin-top: 8px; font-size: 12px; color: var(--text-secondary); }
.bibliographic-missing {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 100%;
  margin-top: 7px;
  padding: 4px 7px;
  border: 1px solid var(--warning-border);
  border-radius: 7px;
  background: var(--warning-soft);
  color: var(--warning);
  font-size: 11px;
  line-height: 1.35;
}
.bibliographic-missing svg { width: 13px; height: 13px; flex: 0 0 auto; }
.bibliographic-field.is-missing label { color: var(--warning); font-weight: 600; }
.bibliographic-field.is-missing input { border-color: var(--warning-border); background: color-mix(in srgb, var(--warning-soft) 55%, var(--input-bg)); }
.library-row-missing { color: var(--warning); }
.library-card .bibliographic-missing { align-self: flex-start; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cal-card-bib-missing, .cal-row-bib-missing { margin-top: 5px; color: var(--warning); font-size: 11px; line-height: 1.35; }
.segment-table-wrap {
  background: var(--surface);
  border: 1px solid var(--divider);
  border-radius: var(--widget-radius);
  overflow: hidden;
}
.segment-table-wrap:has(.segment-style-select.is-open) { overflow: visible; }
.segment-table {
  width: 100%;
  table-layout: fixed;
  border-collapse: collapse;
  font-size: 13px;
}
.segment-col-pdf { width: 14%; }
.segment-col-citation { width: 16%; }
.segment-col-style { width: 20%; }
.segment-col-note { width: auto; }
.segment-col-action { width: 44px; }
.segment-table th {
  text-align: left;
  padding: 10px 8px;
  font-weight: 600;
  color: var(--text-secondary);
  background: var(--surface-secondary);
  border-bottom: 1px solid var(--divider);
  white-space: nowrap;
}
.segment-table td {
  min-width: 0;
  padding: 8px;
  border-bottom: 1px solid var(--divider);
  vertical-align: middle;
}
.segment-table tr:last-child td { border-bottom: none; }
.seg-input {
  width: 100%;
  min-width: 60px;
  height: 32px;
  padding: 0 8px;
  border: 1px solid var(--divider);
  border-radius: 6px;
  background: var(--input-bg);
  font-size: 13px;
  color: var(--text-primary);
  outline: none;
}
.seg-input:focus {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.seg-input.narrow { max-width: 88px; }
.segment-table .seg-input.narrow { max-width: none; min-width: 0; }
.segment-style-select { width: 100%; }
.segment-style-select .app-select-trigger { width: 100%; min-width: 0; height: 32px; padding: 0 7px; border-radius: 6px; font-size: 12px; }
.segment-style-select .app-select-menu { right: auto; left: 0; width: 178px; }
.seg-remove {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  color: var(--text-tertiary);
  border-radius: 7px;
}
.seg-remove svg { width: 16px; height: 16px; display: block; }
.seg-remove:hover { color: var(--error); background: var(--danger-soft); }
.cal-preview {
  margin-top: 24px;
  background: var(--surface);
  border: 1px solid var(--divider);
  border-radius: var(--widget-radius);
  padding: 16px;
}
.cal-preview-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.cal-preview-label {
  font-size: 13px;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.cal-preview-result {
  font-size: 15px;
  font-weight: 600;
  color: var(--accent);
  min-height: 22px;
}
.cal-actions {
  margin-top: 20px;
  display: flex;
  gap: 8px;
  align-items: center;
}
.cal-save-hint {
  font-size: 12px;
  color: var(--text-tertiary);
  margin-left: 8px;
}
.cal-empty {
  text-align: center;
  padding: 40px 20px;
  color: var(--text-tertiary);
  font-size: 14px;
}
.remove-modal-backdrop { position: fixed; inset: 0; z-index: 300; display: none; place-items: center; padding: 20px; background: var(--dialog-backdrop); backdrop-filter: blur(2px); }
.remove-modal-backdrop.open { display: grid; }
.remove-modal { width: min(470px, 100%); padding: 22px; border-radius: 14px; background: var(--dialog-bg); border: 1px solid var(--border-default); box-shadow: var(--shadow-popover); }
.remove-modal-title { font-size: 17px; font-weight: 700; color: var(--text-primary); }
.remove-modal-copy { margin-top: 9px; font-size: 13px; line-height: 1.65; color: var(--text-secondary); }
.remove-options { margin-top: 16px; border-top: 1px solid var(--divider); border-bottom: 1px solid var(--divider); padding: 8px 0; }
.remove-option { display: flex; align-items: flex-start; gap: 9px; padding: 8px 0; font-size: 13px; color: var(--text-primary); }
.remove-option input { margin-top: 2px; }
.remove-option small { display: block; color: var(--text-tertiary); line-height: 1.45; margin-top: 2px; }
.remove-modal-warning { display: none; margin-top: 12px; padding: 9px 11px; border-radius: 7px; background: var(--danger-soft); color: var(--danger); font-size: 12px; line-height: 1.5; }
.remove-modal-warning.show { display: block; }
.remove-modal-actions { margin-top: 18px; display: flex; justify-content: flex-end; gap: 8px; }
@media (max-width: 1180px) { .cal-card-grid { grid-template-columns: repeat(2, minmax(0,1fr)); } }
@media (max-width: 1120px) {
  .calibration-body.detail-open { display: block; position: relative; }
  .calibration-body.detail-open .cal-library-pane { display: none; }
  .calibration-body.detail-open .cal-detail-drawer.open { width: 100%; min-width: 0; height: 100%; border-left: 0; }
  .calibration-body.detail-open .cal-detail-drawer .library-drawer-inner { padding-left: 24px; padding-right: 24px; }
}
@media (max-width: 980px) { #page-calibration .page-header-row { flex-wrap: wrap; } .calibration-header-stats { justify-content: flex-start; } .cal-library-pane { padding-left: 24px; padding-right: 24px; } }
@media (max-width: 820px) { .cal-card-grid { grid-template-columns: 1fr; } .cal-filter-row { align-items: flex-start; } .cal-sort-controls { width: 100%; justify-content: flex-end; } }

/* ── Scrollbar ─────────────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: var(--scrollbar-track); }
::-webkit-scrollbar-thumb { background: var(--scrollbar-thumb); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-thumb-hover); }

/* ── Toast notification ────────────────────────────────────── */
.toast {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%) translateY(60px);
  padding: 10px 20px;
  border-radius: 10px;
  background: var(--tooltip-bg);
  color: var(--tooltip-text);
  font-size: 13px;
  font-weight: 500;
  opacity: 0;
  transition: all 200ms ease;
  pointer-events: none;
  z-index: 100;
}
.toast.show {
  transform: translateX(-50%) translateY(0);
  opacity: 1;
}
</style>
</head>
<body>
<div class="app-shell">

  <!-- ═══ Sidebar ═══ -->
  <aside class="sidebar">
    <div class="sidebar-brand">文献原句定位器</div>
    <nav class="sidebar-nav">
      <button class="sidebar-item active" data-page="search" onclick="navigateTo('search')">
        <span class="sidebar-icon">
          <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="8.5" cy="8.5" r="6"/><line x1="13" y1="13" x2="18" y2="18"/></svg>
        </span>
        <span>文献检索</span>
      </button>
      <button class="sidebar-item" data-page="library" onclick="navigateTo('library')">
        <span class="sidebar-icon">
          <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="16" height="14" rx="2"/><line x1="7" y1="3" x2="7" y2="17"/></svg>
        </span>
        <span>文献库</span>
      </button>
      <button class="sidebar-item" data-page="calibration" onclick="navigateTo('calibration')">
        <span class="sidebar-icon">
          <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h12M4 8h8M4 12h10M4 16h6"/></svg>
        </span>
        <span>页码校准</span>
      </button>
      <button class="sidebar-item" data-page="import" onclick="navigateTo('import')">
        <span class="sidebar-icon">
          <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3v10M6 9l4 4 4-4"/><path d="M3 14v2a1 1 0 001 1h12a1 1 0 001-1v-2"/></svg>
        </span>
        <span>文献导入</span>
      </button>
      <button class="sidebar-item" data-page="settings" onclick="navigateTo('settings')">
        <span class="sidebar-icon">
          <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="3"/><path d="M10 1v3M10 16v3M1 10h3M16 10h3M3.5 3.5l2 2M14.5 14.5l2 2M3.5 16.5l2-2M14.5 5.5l2-2"/></svg>
        </span>
        <span>设置</span>
      </button>
    </nav>
    <div class="sidebar-footer">ME_Finder v0.1.0</div>
  </aside>

  <!-- ═══ Main ═══ -->
  <div class="main-area">

    <!-- ── Search Page ── -->
    <div id="page-search" class="page active">
      <div class="page-header">
        <div class="page-header-row">
          <div>
            <div class="page-title">文献检索</div>
            <div class="page-subtitle">在本地文献库中定位原句、文献与准确页码</div>
          </div>
          <div class="page-header-meta">
            <div class="status-badge" id="index-status">
              <span class="dot"></span>
              <span id="index-count">加载中…</span>
            </div>
          </div>
        </div>
        <div class="search-box-wrap">
          <span class="search-icon">
            <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="8.5" cy="8.5" r="6"/><line x1="13" y1="13" x2="18" y2="18"/></svg>
          </span>
          <input type="text" id="query" class="search-box" placeholder="输入中文引文…" autocomplete="off">
          <button class="search-submit" id="search-btn" onclick="runSearch()">搜索</button>
        </div>
        <div class="search-controls-row">
          <div class="segmented-control" id="mode-control">
            <button class="seg-btn active" data-mode="auto" onclick="setMode(this)">综合检索</button>
            <button class="seg-btn" data-mode="exact" onclick="setMode(this)">精确匹配</button>
            <button class="seg-btn" data-mode="fuzzy" onclick="setMode(this)">模糊检索</button>
          </div>
          <div class="search-filter-controls">
            <div class="search-control-group">
              <span class="search-control-label">来源</span>
              <div class="source-type-control" id="source-type-control" role="group" aria-label="来源类型">
                <button class="source-type-btn active" type="button" data-source="all" onclick="setSearchSourceType('all')">全部</button>
                <button class="source-type-btn" type="button" data-source="word" onclick="setSearchSourceType('word')">Word</button>
                <button class="source-type-btn" type="button" data-source="pdf" onclick="setSearchSourceType('pdf')">PDF</button>
              </div>
            </div>
            <div class="search-control-group">
              <span class="search-control-label">文献范围</span>
              <div class="app-select document-select" id="document-select">
                <button class="app-select-trigger" id="document-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleSearchSelect(event,'document-select')">
                  <span class="app-select-value" id="document-select-label">全部文献</span>
                  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>
                </button>
                <div class="app-select-menu document-menu" role="listbox">
                  <div class="document-menu-search-wrap">
                    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.7"/><path d="m13 13 4 4"/></svg>
                    <input class="document-menu-search" id="document-filter-query" type="search" placeholder="搜索书名或文件名…" autocomplete="off" oninput="renderSearchDocumentOptions()">
                  </div>
                  <div class="document-options" id="document-options"></div>
                </div>
              </div>
            </div>
            <div class="search-control-group">
              <span class="search-control-label">返回数量</span>
              <div class="app-select" id="limit-select">
                <button class="app-select-trigger" id="limit-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleSearchSelect(event,'limit-select')">
                  <span class="app-select-value" id="limit-select-label">10 条</span>
                  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>
                </button>
                <div class="app-select-menu" role="listbox" id="limit-options">
                  <button class="app-select-option is-selected" type="button" data-value="10" onclick="setSearchLimit(event,10)"><span>10 条</span></button>
                  <button class="app-select-option" type="button" data-value="20" onclick="setSearchLimit(event,20)"><span>20 条</span></button>
                  <button class="app-select-option" type="button" data-value="50" onclick="setSearchLimit(event,50)"><span>50 条</span></button>
                  <button class="app-select-option" type="button" data-value="100" onclick="setSearchLimit(event,100)"><span>100 条</span></button>
                  <button class="app-select-option" type="button" data-value="200" onclick="setSearchLimit(event,200)"><span>200 条</span></button>
                  <button class="app-select-option" type="button" data-value="all" onclick="setSearchLimit(event,'all')"><span>全部</span></button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="results-area">
        <div class="results-list-pane">
          <div class="results-status" id="results-status" style="display:none"></div>
          <div id="results-list">
            <div class="empty-state">
              <div class="empty-state-icon">
                <svg width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.35"><circle cx="20" cy="20" r="14"/><line x1="30" y1="30" x2="42" y2="42"/></svg>
              </div>
              <div class="empty-state-text">输入引文开始检索</div>
              <div class="empty-state-hint">支持精确匹配和模糊检索</div>
            </div>
          </div>
        </div>
        <div class="results-detail-pane">
          <div id="detail-panel">
            <div class="empty-state">
              <div class="empty-state-icon">
                <svg width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.35"><rect x="8" y="6" width="32" height="36" rx="3"/><line x1="16" y1="16" x2="32" y2="16"/><line x1="16" y1="22" x2="32" y2="22"/><line x1="16" y1="28" x2="28" y2="28"/></svg>
              </div>
              <div class="empty-state-text">选择一条结果查看详情</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ── Library Page ── -->
    <div id="page-library" class="page">
      <div class="page-header">
        <div class="page-header-row">
          <div>
            <div class="page-title">文献库</div>
            <div class="page-subtitle">管理已导入的文献及其索引状态</div>
          </div>
          <div class="page-header-meta">
            <div class="status-badge" id="library-status">
              <span class="dot"></span>
              <span id="library-total">加载中…</span>
            </div>
          </div>
        </div>
        <div class="library-search-wrap">
          <span class="library-search-icon">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="8.5" cy="8.5" r="6"/><line x1="13" y1="13" x2="18" y2="18"/></svg>
          </span>
          <input type="text" id="lib-search" class="library-search" placeholder="搜索文献…" autocomplete="off" oninput="filterLibrary()">
        </div>
        <div class="library-controls-row">
          <div class="segmented-control" id="lib-type-control">
            <button class="seg-btn active" data-type="all" onclick="setLibFilter(this)">全部</button>
            <button class="seg-btn" data-type="word" onclick="setLibFilter(this)">Word</button>
            <button class="seg-btn" data-type="pdf" onclick="setLibFilter(this)">PDF</button>
          </div>
          <div class="library-toolbar-right">
            <div class="view-switch" role="group" aria-label="文献库显示方式">
              <button class="view-switch-btn active" id="library-view-list" type="button" title="列表显示" aria-label="列表显示" aria-pressed="true" onclick="setLibraryView('list')"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M6 5h11M6 10h11M6 15h11"/><circle cx="3" cy="5" r=".7" fill="currentColor" stroke="none"/><circle cx="3" cy="10" r=".7" fill="currentColor" stroke="none"/><circle cx="3" cy="15" r=".7" fill="currentColor" stroke="none"/></svg></button>
              <button class="view-switch-btn" id="library-view-grid" type="button" title="卡片显示" aria-label="卡片显示" aria-pressed="false" onclick="setLibraryView('grid')"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="2.5" y="2.5" width="6" height="6" rx="1"/><rect x="11.5" y="2.5" width="6" height="6" rx="1"/><rect x="2.5" y="11.5" width="6" height="6" rx="1"/><rect x="11.5" y="11.5" width="6" height="6" rx="1"/></svg></button>
            </div>
            <div class="library-sort-controls"><span class="library-sort-label">排序</span>
              <div class="app-select library-sort-field-select" id="library-sort-field-select">
                <button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,'library-sort-field-select')"><span class="app-select-value" id="library-sort-field-label">导入时间</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>
                <div class="app-select-menu" role="listbox"><button class="app-select-option is-selected" type="button" data-value="imported_at" onclick="setLibrarySortOption(event,'field','imported_at')">导入时间</button><button class="app-select-option" type="button" data-value="title" onclick="setLibrarySortOption(event,'field','title')">书名</button><button class="app-select-option" type="button" data-value="author" onclick="setLibrarySortOption(event,'field','author')">作者</button><button class="app-select-option" type="button" data-value="modified_at" onclick="setLibrarySortOption(event,'field','modified_at')">最近修改时间</button><button class="app-select-option" type="button" data-value="source_type" onclick="setLibrarySortOption(event,'field','source_type')">来源类型</button></div>
              </div>
              <div class="app-select library-sort-direction-select" id="library-sort-direction-select">
                <button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,'library-sort-direction-select')"><span class="app-select-value" id="library-sort-direction-label">降序</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>
                <div class="app-select-menu" role="listbox"><button class="app-select-option is-selected" type="button" data-value="desc" onclick="setLibrarySortOption(event,'direction','desc')">降序</button><button class="app-select-option" type="button" data-value="asc" onclick="setLibrarySortOption(event,'direction','asc')">升序</button></div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="library-body">
        <div class="library-list-scroll">
          <div id="library-list" class="library-list-container library-view-list"></div>
        </div>
        <div class="library-drawer" id="library-drawer">
          <div class="library-drawer-inner" id="library-drawer-content"></div>
        </div>
      </div>
    </div>

    <!-- ── Import Page ── -->
    <div id="page-import" class="page">
      <div class="page-header">
        <div class="page-header-row">
          <div>
            <div class="page-title">文献导入</div>
            <div class="page-subtitle">导入 PDF 或 DOCX 文献到本地索引</div>
          </div>
        </div>
      </div>
      <div class="import-content">
        <div class="drop-zone" id="drop-zone" onclick="document.getElementById('file-input').click()">
          <div class="drop-zone-icon">
            <svg width="52" height="52" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M24 8v20M16 20l8-8 8 8"/><path d="M8 32v6a2 2 0 002 2h28a2 2 0 002-2v-6"/></svg>
          </div>
          <div class="drop-zone-text">拖放文件到此处，或点击选择</div>
          <div class="drop-zone-hint">支持单个或多个文件同时导入</div>
          <div class="drop-zone-formats">
            <span class="format-tag">PDF</span>
            <span class="format-tag">DOCX</span>
          </div>
        </div>
        <input type="file" id="file-input" accept=".pdf,.docx" multiple style="display:none" onchange="handleFileSelect(this.files)">
        <div class="import-queue" id="import-queue" style="display:none">
          <div class="import-queue-title">导入队列</div>
          <div id="import-items"></div>
        </div>
      </div>
    </div>

    <!-- ── Calibration Page ── -->
    <div id="page-calibration" class="page">
      <div class="page-header">
        <div class="page-header-row">
          <div>
            <div class="page-title">页码校准</div>
            <div class="page-subtitle">管理 PDF 文献的引用页码映射</div>
          </div>
          <div class="calibration-header-stats" id="calibration-stats">
            <button class="status-stat status-stat--info active" type="button" onclick="applyCalStatusFilter('all')"><span class="status-stat__icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h9l3 3v15H6z"/><path d="M15 3v4h4"/></svg></span><span class="status-stat__label">PDF 总数</span><span class="status-stat__count">—</span></button>
            <button class="status-stat status-stat--success" type="button" onclick="applyCalStatusFilter('calibrated')"><span class="status-stat__icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/></svg></span><span class="status-stat__label">已校准</span><span class="status-stat__count">—</span></button>
            <button class="status-stat status-stat--neutral" type="button" onclick="applyCalStatusFilter('pending')"><span class="status-stat__icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></span><span class="status-stat__label">待校准</span><span class="status-stat__count">—</span></button>
            <button class="status-stat status-stat--warning" type="button" onclick="applyCalStatusFilter('review')"><span class="status-stat__icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/></svg></span><span class="status-stat__label">待确认</span><span class="status-stat__count">—</span></button>
            <button class="status-stat status-stat--danger" type="button" onclick="applyCalStatusFilter('failed')"><span class="status-stat__icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/></svg></span><span class="status-stat__label">检测失败</span><span class="status-stat__count">—</span></button>
          </div>
        </div>
      </div>
      <div class="calibration-body">
        <section class="cal-library-pane" id="cal-library-pane">
          <div class="cal-toolbar">
            <div class="cal-toolbar-top">
              <div class="cal-search-wrap">
                <span class="cal-search-icon"><svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="8.5" cy="8.5" r="6"/><line x1="13" y1="13" x2="18" y2="18"/></svg></span>
                <input id="cal-search" class="cal-search" type="search" placeholder="搜索书名、作者、译者、出版社或文件名…" autocomplete="off" oninput="renderCalibrationCards()">
              </div>
              <button class="cal-refresh-btn" id="cal-refresh-btn" type="button" onclick="refreshCalibrationLibrary()" title="刷新文献列表" aria-label="刷新文献列表"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 0 0-15.5-6.2L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 15.5 6.2L21 16"/><path d="M16 16h5v5"/></svg></button>
            </div>
            <div class="cal-filter-row">
              <div class="cal-status-tabs" id="cal-status-tabs">
                <button class="cal-status-tab status-stat--info active" data-status="all" onclick="setCalStatusFilter(this)"><span class="status-stat__icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h9l3 3v15H6z"/><path d="M15 3v4h4"/></svg></span><span class="cal-status-tab__label">全部</span></button>
                <button class="cal-status-tab status-stat--success" data-status="calibrated" onclick="setCalStatusFilter(this)"><span class="status-stat__icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/></svg></span><span class="cal-status-tab__label">已校准</span></button>
                <button class="cal-status-tab status-stat--neutral" data-status="pending" onclick="setCalStatusFilter(this)"><span class="status-stat__icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></span><span class="cal-status-tab__label">待校准</span></button>
                <button class="cal-status-tab status-stat--warning" data-status="review" onclick="setCalStatusFilter(this)"><span class="status-stat__icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/></svg></span><span class="cal-status-tab__label">待确认</span></button>
                <button class="cal-status-tab status-stat--danger" data-status="failed" onclick="setCalStatusFilter(this)"><span class="status-stat__icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/></svg></span><span class="cal-status-tab__label">检测失败</span></button>
              </div>
              <div class="cal-toolbar-right">
                <div class="view-switch" role="group" aria-label="页码校准显示方式">
                  <button class="view-switch-btn" id="cal-view-list" type="button" title="列表显示" aria-label="列表显示" aria-pressed="false" onclick="setCalibrationView('list')"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M6 5h11M6 10h11M6 15h11"/><circle cx="3" cy="5" r=".7" fill="currentColor" stroke="none"/><circle cx="3" cy="10" r=".7" fill="currentColor" stroke="none"/><circle cx="3" cy="15" r=".7" fill="currentColor" stroke="none"/></svg></button>
                  <button class="view-switch-btn active" id="cal-view-grid" type="button" title="卡片显示" aria-label="卡片显示" aria-pressed="true" onclick="setCalibrationView('grid')"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="2.5" y="2.5" width="6" height="6" rx="1"/><rect x="11.5" y="2.5" width="6" height="6" rx="1"/><rect x="2.5" y="11.5" width="6" height="6" rx="1"/><rect x="11.5" y="11.5" width="6" height="6" rx="1"/></svg></button>
                </div>
                <div class="cal-sort-controls"><span class="cal-sort-label">排序</span>
                  <div class="app-select cal-sort-field-select" id="cal-sort-field-select">
                    <button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,'cal-sort-field-select')"><span class="app-select-value" id="cal-sort-field-label">导入时间</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>
                    <div class="app-select-menu" role="listbox"><button class="app-select-option is-selected" type="button" data-value="imported_at" onclick="setCalSortOption(event,'field','imported_at')">导入时间</button><button class="app-select-option" type="button" data-value="title" onclick="setCalSortOption(event,'field','title')">书名</button><button class="app-select-option" type="button" data-value="author" onclick="setCalSortOption(event,'field','author')">作者</button><button class="app-select-option" type="button" data-value="modified_at" onclick="setCalSortOption(event,'field','modified_at')">最近修改时间</button><button class="app-select-option" type="button" data-value="status" onclick="setCalSortOption(event,'field','status')">校准状态</button></div>
                  </div>
                  <div class="app-select cal-sort-direction-select" id="cal-sort-direction-select">
                    <button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,'cal-sort-direction-select')"><span class="app-select-value" id="cal-sort-direction-label">降序</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>
                    <div class="app-select-menu" role="listbox"><button class="app-select-option is-selected" type="button" data-value="desc" onclick="setCalSortOption(event,'direction','desc')">降序</button><button class="app-select-option" type="button" data-value="asc" onclick="setCalSortOption(event,'direction','asc')">升序</button></div>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <div id="cal-card-grid" class="cal-card-grid"></div>
        </section>
        <aside class="library-drawer cal-detail-drawer" id="cal-detail-drawer">
          <div class="library-drawer-inner">
            <button class="drawer-close" onclick="closeCalibrationDetail()">← 返回文献列表</button>
            <div id="cal-detail-heading"></div>
            <div id="cal-doc-info" class="cal-doc-info" style="display:none"></div>
            <div class="cal-detail-actions" id="cal-detail-actions"></div>
            <div id="cal-editor" style="display:none">
          <div class="cal-section">
            <div class="cal-section-head">
              <span class="cal-section-title">自动检测</span>
            </div>
            <div id="cal-auto-preview" class="auto-detect-panel" style="display:none"></div>
          </div>
          <div class="cal-section">
            <div class="cal-section-head">
              <span class="cal-section-title">页码映射分段</span>
              <button class="action-btn" onclick="addCalSegment()">+ 添加分段</button>
            </div>
            <div class="segment-table-wrap">
              <table class="segment-table">
                <colgroup>
                  <col class="segment-col-pdf">
                  <col class="segment-col-pdf">
                  <col class="segment-col-citation">
                  <col class="segment-col-style">
                  <col class="segment-col-note">
                  <col class="segment-col-action">
                </colgroup>
                <thead>
                  <tr>
                    <th>PDF 起始页</th>
                    <th>PDF 结束页</th>
                    <th>引用起始页</th>
                    <th>编号样式</th>
                    <th>范围说明</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody id="cal-segments-body"></tbody>
              </table>
            </div>
            <div id="cal-no-segments" class="cal-empty" style="display:none">暂无映射分段，点击上方按钮添加</div>
          </div>
          <div class="cal-preview">
            <div class="cal-preview-row">
              <span class="cal-preview-label">预览：PDF 第</span>
              <input type="number" class="seg-input narrow" id="cal-preview-input" min="1" value="1" oninput="updateCalPreview()">
              <span class="cal-preview-label">页 →</span>
              <span class="cal-preview-result" id="cal-preview-result">—</span>
            </div>
          </div>
          <div class="cal-actions">
            <button class="action-btn primary" onclick="saveCalibration()">保存校准配置</button>
            <button class="action-btn" onclick="loadCalibrationDoc()">放弃修改</button>
            <span class="cal-save-hint">保存后自动重建索引</span>
          </div>
          <div class="cal-danger-zone"><button class="action-btn danger-outline" onclick="openRemoveDocumentModal()">从文献库移除</button></div>
            </div>
          </div>
        </aside>
      </div>
    </div>

    <!-- ── Settings Page ── -->
    <div id="page-settings" class="page">
      <div class="page-header">
        <div class="page-header-row">
          <div>
            <div class="page-title">设置</div>
            <div class="page-subtitle">应用配置与索引管理</div>
          </div>
        </div>
      </div>
      <div class="settings-page-content">
        <section class="settings-card appearance-card">
          <div class="settings-card-head">
            <div>
              <div class="settings-card-title">外观</div>
              <div class="settings-card-subtitle">选择适合当前阅读环境的应用主题，切换会立即生效。</div>
            </div>
          </div>
          <div id="theme-options" class="theme-options" role="radiogroup" aria-label="应用主题"></div>
        </section>
        <section class="settings-card">
          <div class="settings-card-head">
            <div>
              <div class="settings-card-title">MinerU API</div>
              <div class="settings-card-subtitle">用于扫描版、乱码或复杂排版 PDF 的精准解析。</div>
            </div>
            <span id="mineru-config-status" class="settings-status">读取中…</span>
          </div>
          <div id="mineru-expiry-detail" class="settings-expiry-detail">API 到期时间：读取中…</div>
          <div class="settings-form">
            <div class="settings-field">
              <label for="mineru-token">Bearer Token</label>
              <div class="settings-input-row">
                <input id="mineru-token" class="settings-input" type="password" autocomplete="off" spellcheck="false" placeholder="输入 MinerU API Token">
                <button id="mineru-token-toggle" class="action-btn settings-toggle" type="button" onclick="toggleMineruSecret('mineru-token','mineru-token-toggle')">显示</button>
              </div>
              <small>在 MinerU 的 API 管理页面创建或复制。留空表示保留当前已保存的 Token。</small>
            </div>
            <div class="settings-field">
              <label for="mineru-access-key-id">Access Key ID（可选）</label>
              <input id="mineru-access-key-id" class="settings-input" type="text" autocomplete="off" spellcheck="false" placeholder="如果 API 页面提供了 Access Key ID，再填写">
            </div>
            <div class="settings-field">
              <label for="mineru-secret-access-key">Secret Access Key（可选）</label>
              <div class="settings-input-row">
                <input id="mineru-secret-access-key" class="settings-input" type="password" autocomplete="off" spellcheck="false" placeholder="如果 API 页面提供了 Secret Access Key，再填写">
                <button id="mineru-secret-toggle" class="action-btn settings-toggle" type="button" onclick="toggleMineruSecret('mineru-secret-access-key','mineru-secret-toggle')">显示</button>
              </div>
            </div>
            <div class="settings-field">
              <label for="mineru-api-base">API 地址</label>
              <input id="mineru-api-base" class="settings-input" type="url" value="https://mineru.net" spellcheck="false">
            </div>
            <div class="settings-field">
              <label for="mineru-expires-at">Token 到期日期（可选）</label>
              <input id="mineru-expires-at" class="settings-input" type="date">
              <small>只用于本地提醒，不会自动停用 API。</small>
            </div>
            <div class="settings-actions">
              <button class="action-btn primary" type="button" onclick="saveMineruConfig()">保存 API 配置</button>
              <button class="action-btn" type="button" onclick="loadMineruConfig()">重新读取</button>
              <span id="mineru-save-hint" class="settings-hint">密钥只保存在本机。</span>
            </div>
          </div>
          <div class="settings-notice">保存后，命令行 MinerU 处理和以后接入应用内导入功能都会使用这份本机配置。当前页面不会显示已经保存的密钥。</div>
        </section>
      </div>
    </div>

  </div><!-- /main-area -->
</div><!-- /app-shell -->

<div id="toast" class="toast"></div>
<div id="remove-document-modal" class="remove-modal-backdrop" onclick="removeModalBackdropClick(event)">
  <div class="remove-modal" role="dialog" aria-modal="true" aria-labelledby="remove-modal-title">
    <div class="remove-modal-title" id="remove-modal-title">从文献库移除？</div>
    <div class="remove-modal-copy" id="remove-modal-copy"></div>
    <div class="remove-options">
      <label class="remove-option"><input type="checkbox" id="remove-generated" checked><span>删除解析缓存和 MinerU 产物<small>再次导入时需要重新解析或 OCR；共享产物不会删除。</small></span></label>
      <label class="remove-option" id="remove-internal-option"><input type="checkbox" id="remove-internal-copy"><span>同时删除应用内保存的 PDF 副本<small>默认不勾选。外部原始文件永远不会被删除。</small></span></label>
    </div>
    <div class="remove-modal-warning" id="remove-modal-warning">删除应用内 PDF 副本后无法恢复。请再次确认此操作。</div>
    <div class="remove-modal-actions"><button class="action-btn" onclick="closeRemoveDocumentModal()">取消</button><button class="action-btn danger-outline" id="confirm-remove-btn" onclick="confirmRemoveDocument()">从文献库移除</button></div>
  </div>
</div>

<script>
/* ═══════════════════════════════════════════════════════════════
   App State
   ═══════════════════════════════════════════════════════════════ */
let currentPage = 'search';
let currentMode = 'auto';
let searchResults = [];
let selectedIndex = -1;
let citationStyle = localStorage.getItem('meFinderCitationStyle') || 'chinese';
let searchSourceType = 'all';
let searchLimit = 10;
let searchDocumentId = '';
let searchSourceFiles = [];
let searchVolumes = [];
let searchDocumentsLoaded = false;

let libSources = [];
let libVolumes = [];
let libWorks = [];
let libLoaded = false;
let libTypeFilter = 'all';
let libSelectedId = null;
let libViewMode = localStorage.getItem('meFinderLibraryView') === 'grid' ? 'grid' : 'list';
let libSortField = ['imported_at','title','author','modified_at','source_type'].indexOf(localStorage.getItem('meFinderLibrarySortField')) >= 0 ? localStorage.getItem('meFinderLibrarySortField') : 'imported_at';
let libSortDirection = localStorage.getItem('meFinderLibrarySortDirection') === 'asc' ? 'asc' : 'desc';

let calPdfsLoaded = false;
let calPdfList = [];
let calSegments = [];
let calSelectedDoc = null;
let calSelectedSourceId = null;
let calAutoResult = null;
let calStatusFilter = 'all';
let calSortField = 'imported_at';
let calSortDirection = 'desc';
let calTransientStatus = {};
let calOpenMenuId = null;
let calRefreshInProgress = false;
let calViewMode = localStorage.getItem('meFinderCalibrationView') === 'list' ? 'list' : 'grid';
let removeDocumentTarget = null;
let removeSecondStage = false;
let mineruConfigLoaded = false;
let preferencesLoaded = false;
let currentTheme = document.documentElement.dataset.theme || 'frost-blue';

/* ═══ Navigation ═══ */
function navigateTo(page) {
  currentPage = page;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.sidebar-item').forEach(a => a.classList.remove('active'));
  const target = document.getElementById('page-' + page);
  if (target) target.classList.add('active');
  const link = document.querySelector('.sidebar-item[data-page="' + page + '"]');
  if (link) link.classList.add('active');
  if (page === 'library' && !libLoaded) loadLibrary();
  if (page === 'calibration' && !calPdfsLoaded) loadCalPdfs();
  if (page === 'settings') {
    if (!preferencesLoaded) loadPreferences();
    if (!mineruConfigLoaded) loadMineruConfig();
  }
}

/* ═══ Mode segmented control ═══ */
function setMode(btn) {
  currentMode = btn.dataset.mode;
  document.querySelectorAll('#mode-control .seg-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

/* ═══ Search filters ═══ */
function rerunSearchAfterFilterChange() {
  var query = document.getElementById('query').value.trim();
  var status = document.getElementById('results-status');
  if (query && status && status.style.display !== 'none') runSearch();
}

function setSearchSourceType(sourceType) {
  searchSourceType = ['all','word','pdf'].indexOf(sourceType) >= 0 ? sourceType : 'all';
  document.querySelectorAll('#source-type-control .source-type-btn').forEach(function(button) {
    button.classList.toggle('active', button.dataset.source === searchSourceType);
  });
  if (searchDocumentId) {
    var selected = searchSourceFiles.find(function(item) { return item.source_file_id === searchDocumentId; });
    if (selected && searchSourceType !== 'all' && selected.source_type !== searchSourceType) searchDocumentId = '';
  }
  updateSearchDocumentLabel();
  renderSearchDocumentOptions();
  rerunSearchAfterFilterChange();
}

function closeAppSelects(exceptId) {
  document.querySelectorAll('.app-select').forEach(function(select) {
    if (select.id === exceptId) return;
    select.classList.remove('is-open');
    var trigger = select.querySelector('.app-select-trigger');
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
  });
}

function closeSearchSelects(exceptId) { closeAppSelects(exceptId); }

async function toggleAppSelect(event, selectId) {
  event.stopPropagation();
  var select = document.getElementById(selectId);
  if (!select) return;
  var shouldOpen = !select.classList.contains('is-open');
  closeAppSelects(selectId);
  select.classList.toggle('is-open', shouldOpen);
  var trigger = select.querySelector('.app-select-trigger');
  if (trigger) trigger.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
  if (shouldOpen && selectId === 'document-select') {
    await ensureSearchDocuments();
    renderSearchDocumentOptions();
    var input = document.getElementById('document-filter-query');
    if (input) { input.value = ''; requestAnimationFrame(function() { input.focus(); }); }
  }
}

async function toggleSearchSelect(event, selectId) { return toggleAppSelect(event, selectId); }

function setSearchLimit(event, limit) {
  event.stopPropagation();
  searchLimit = limit === 'all' ? 'all' : Math.max(1, Math.min(Number(limit) || 10, 200));
  document.getElementById('limit-select-label').textContent = searchLimit === 'all' ? '全部' : searchLimit + ' 条';
  document.querySelectorAll('#limit-options .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', String(option.dataset.value) === String(searchLimit));
  });
  closeAppSelects();
  rerunSearchAfterFilterChange();
}

async function ensureSearchDocuments(force) {
  if (searchDocumentsLoaded && !force) return;
  var options = document.getElementById('document-options');
  if (options) options.innerHTML = '<div class="document-options-empty">正在读取文献库…</div>';
  try {
    var response = await fetch('/api/sources');
    var data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '读取失败');
    searchSourceFiles = data.source_files || [];
    searchVolumes = data.volumes || [];
    searchDocumentsLoaded = true;
  } catch (error) {
    searchDocumentsLoaded = false;
    if (options) options.innerHTML = '<div class="document-options-empty">文献列表读取失败</div>';
  }
}

function searchDocumentView(source) {
  var volume = searchVolumes.find(function(item) { return item.source_file_id === source.source_file_id; });
  var bib = source.bibliographic_metadata || {};
  var title = bib.title || source.title || (volume && volume.display_title) || source.display_title || source.file_name || source.source_file_id;
  var author = bib.author || source.author || '';
  return {title:title, author:author, sourceType:source.source_type === 'pdf' ? 'PDF' : 'Word'};
}

function renderSearchDocumentOptions() {
  var options = document.getElementById('document-options');
  if (!options) return;
  if (!searchDocumentsLoaded) {
    options.innerHTML = '<div class="document-options-empty">打开菜单后读取文献列表</div>';
    return;
  }
  var queryInput = document.getElementById('document-filter-query');
  var query = String(queryInput ? queryInput.value : '').trim().toLowerCase().replace(/\s+/g, '');
  var sources = searchSourceFiles.filter(function(source) {
    if (searchSourceType !== 'all' && source.source_type !== searchSourceType) return false;
    var view = searchDocumentView(source);
    var haystack = [view.title, view.author, source.file_name].join('|').toLowerCase().replace(/\s+/g, '');
    return !query || haystack.indexOf(query) >= 0;
  }).sort(function(a, b) {
    return calPinyinCollator.compare(searchDocumentView(a).title, searchDocumentView(b).title);
  });
  var check = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 10 3 3 7-7"/></svg>';
  var allOption = '<button class="app-select-option' + (!searchDocumentId ? ' is-selected' : '') + '" type="button" data-value="" onclick="selectSearchDocument(event,this.dataset.value)"><span>全部文献</span>' + (!searchDocumentId ? check : '') + '</button>';
  if (!sources.length) {
    options.innerHTML = allOption + '<div class="document-options-empty">没有符合条件的文献</div>';
    return;
  }
  options.innerHTML = allOption + sources.map(function(source) {
    var view = searchDocumentView(source);
    var selected = source.source_file_id === searchDocumentId;
    return '<button class="app-select-option' + (selected ? ' is-selected' : '') + '" type="button" data-value="' + esc(source.source_file_id) + '" onclick="selectSearchDocument(event,this.dataset.value)"><span class="document-option-main"><span class="document-option-title">' + esc(view.title) + '</span><span class="document-option-meta">' + esc([view.sourceType, view.author].filter(Boolean).join(' · ')) + '</span></span>' + (selected ? check : '') + '</button>';
  }).join('');
}

function selectSearchDocument(event, sourceId) {
  event.stopPropagation();
  searchDocumentId = sourceId || '';
  updateSearchDocumentLabel();
  closeSearchSelects();
  rerunSearchAfterFilterChange();
}

function updateSearchDocumentLabel() {
  var label = document.getElementById('document-select-label');
  if (!label) return;
  var source = searchSourceFiles.find(function(item) { return item.source_file_id === searchDocumentId; });
  label.textContent = source ? searchDocumentView(source).title : '全部文献';
  label.title = source ? searchDocumentView(source).title : '';
}

/* ═══ Search ═══ */
async function runSearch() {
  const query = document.getElementById('query').value.trim();
  if (!query) return;
  const statusEl = document.getElementById('results-status');
  const listEl = document.getElementById('results-list');
  statusEl.style.display = 'block';
  statusEl.textContent = '检索中…';
  listEl.innerHTML = '';
  selectedIndex = -1;
  showEmptyDetail();

  try {
    const resp = await fetch('/api/search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query, mode: currentMode, limit: searchLimit, source_type: searchSourceType, source_file_id: searchDocumentId || null})
    });
    const data = await resp.json();
    searchResults = data.results || [];
    statusEl.textContent = '找到 ' + data.total + ' 条候选，显示 ' + searchResults.length + ' 条';

    if (searchResults.length === 0) {
      listEl.innerHTML = '<div class="empty-state" style="min-height:200px"><div class="empty-state-text">未找到匹配结果</div><div class="empty-state-hint">尝试更短的引文或切换为模糊检索</div></div>';
      return;
    }

    listEl.innerHTML = searchResults.map((item, i) => resultRowHTML(item, i)).join('');
    selectResult(0);
  } catch (err) {
    statusEl.textContent = '检索失败：' + err.message;
  }
}

function resultRowHTML(item, index) {
  const score = Math.round(item.match_score * 100);
  const typeLabel = matchTypeLabel(item.match_type);
  const title = esc(item.document_title || item.work_title || item.volume_display || '');
  const author = item.author_label ? esc(item.author_label) : '';
  const vol = item.volume_display ? esc(item.volume_display) : '';
  const page = esc(formatCitationPageLabel(item));
  const sourceIcon = item.source_type === 'pdf' ? 'PDF' : 'Word';
  const snippet = item.highlighted_html ? truncateHTML(item.highlighted_html, 100) : esc(truncate(item.paragraph_text || '', 100));
  return '<div class="result-row" data-index="' + index + '" onclick="selectResult(' + index + ')">'
    + '<div class="result-row-head">'
    + '<span class="result-score">' + score + '%</span>'
    + '<span class="result-match-type">' + typeLabel + '</span>'
    + '<span class="result-title">' + title + '</span>'
    + '</div>'
    + '<div class="result-meta">'
    + (author ? '<span>' + author + '</span>' : '')
    + (vol ? '<span>' + vol + '</span>' : '')
    + '<span>' + page + '</span>'
    + '<span>' + sourceIcon + '</span>'
    + '</div>'
    + '<div class="result-snippet">' + snippet + '</div>'
    + '</div>';
}

function selectResult(index) {
  if (index < 0 || index >= searchResults.length) return;
  selectedIndex = index;
  document.querySelectorAll('.result-row').forEach((row, i) => {
    row.classList.toggle('selected', i === index);
  });
  const item = searchResults[index];
  showDetail(item);

  const row = document.querySelector('.result-row[data-index="' + index + '"]');
  if (row) row.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

function showDetail(item) {
  const panel = document.getElementById('detail-panel');
  const title = esc(item.document_title || item.work_title || item.volume_display || '');
  const author = item.author_label ? esc(item.author_label) : '';
  const pageLabel = formatCitationPageLabel(item);
  const page = esc(pageLabel);
  const score = Math.round(item.match_score * 100);
  const typeLabel = matchTypeLabel(item.match_type);
  const sourceLabel = item.source_type === 'pdf' ? 'PDF' : 'Word';

  let contextBefore = '';
  if (item.context_before && item.context_before.length) {
    contextBefore = '<div class="detail-context">' + item.context_before.map(c => esc(c.text)).join('\n') + '</div>';
  }
  let contextAfter = '';
  if (item.context_after && item.context_after.length) {
    contextAfter = '<div class="detail-context">' + item.context_after.map(c => esc(c.text)).join('\n') + '</div>';
  }

  let pageDetail = '';
  if (item.source_type === 'pdf') {
    pageDetail = '<div class="page-detail-toggle" onclick="togglePageDetail(this)">页码详情 ▸</div>'
      + '<div class="page-detail-body">'
      + pdRow('引用页码', pageLabel)
      + pdRow('PDF 页码标签', item.pdf_page_start_label || '无')
      + pdRow('PDF 物理页', item.pdf_page_start_index != null ? 'PDF 第 ' + (item.pdf_page_start_index + 1) + ' 页' : '—')
      + pdRow('映射方式', mappingMethodLabel(item.page_mapping_method))
      + (item.mapping_confidence_level ? pdRow('映射置信度', mappingConfidenceLabel(item.mapping_confidence_level, item.page_mapping_confidence)) : '')
      + (item.page_scope ? pdRow('页码范围', pageScopeLabel(item.page_scope)) : '')
      + (item.mapping_evidence ? pdRow('映射依据', mappingEvidenceSummary(item.mapping_evidence)) : '')
      + (item.is_cross_page ? pdRow('跨页命中', '是') : '')
      + '</div>';
  }

  const citationStyleLabel = citationStyle === 'gb' ? 'GB/T 7714' : '中文脚注';
  const citationIncomplete = item.citation_formats && (
    item.citation_formats.chinese_status !== 'complete' || item.citation_formats.gb_status !== 'complete'
  );

  panel.innerHTML = '<div class="detail-card">'
    + '<div class="detail-header">'
    + '<div class="detail-title">' + title + '</div>'
    + (author ? '<div class="detail-author">' + author + '</div>' : '')
    + '<div class="detail-pills">'
    + '<span class="detail-pill">' + sourceLabel + '</span>'
    + (item.volume_display ? '<span class="detail-pill">' + esc(item.volume_display) + '</span>' : '')
    + '<span class="detail-pill">' + page + '</span>'
    + '<span class="detail-pill accent">' + score + '% ' + typeLabel + '</span>'
    + '</div>'
    + pageDetail
    + '</div>'
    + '<div class="detail-body">'
    + contextBefore
    + '<div class="detail-hit">' + (item.highlighted_html || esc(item.paragraph_text || '')) + '</div>'
    + contextAfter
    + '</div>'
    + '<div class="detail-actions">'
    + '<button class="action-btn" onclick="copySelectedOriginal()">复制原文</button>'
    + '<span class="citation-copy-group">'
    + '<span class="app-select citation-style-control" id="citation-style-control">'
    + '<button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,\'citation-style-control\')"><span class="app-select-value" id="citation-style-label">' + citationStyleLabel + '</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>'
    + '<span class="app-select-menu" role="listbox"><button class="app-select-option' + (citationStyle === 'chinese' ? ' is-selected' : '') + '" type="button" data-value="chinese" onclick="selectCitationStyle(event,\'chinese\')">中文脚注</button><button class="app-select-option' + (citationStyle === 'gb' ? ' is-selected' : '') + '" type="button" data-value="gb" onclick="selectCitationStyle(event,\'gb\')">GB/T 7714</button></span>'
    + '</span>'
    + '<button class="action-btn" onclick="copySelectedCitation()">复制出处</button>'
    + '</span>'
    + '<button class="action-btn" onclick="copySelectedOriginalAndCitation()">复制原文与出处</button>'
    + (citationIncomplete && item.source_type === 'pdf' ? '<button class="action-btn" onclick="openMetadataForSource(\'' + esc(item.source_file_id) + '\')">补全书目信息</button>' : '')
    + (item.source_file_id ? '<button class="action-btn primary" onclick="openSource(\'' + esc(item.source_file_id) + '\',' + (item.pdf_page_start_index != null ? item.pdf_page_start_index + 1 : 'null') + ')">打开原文</button>' : '')
    + '</div>'
    + '</div>';

  requestAnimationFrame(function() {
    const hit = panel.querySelector('.detail-hit');
    if (!hit) return;
    hit.classList.remove('is-locating');
    void hit.offsetWidth;
    hit.classList.add('is-locating');
    const detailPane = document.querySelector('.results-detail-pane');
    if (!detailPane) return;
    const paneRect = detailPane.getBoundingClientRect();
    const hitRect = hit.getBoundingClientRect();
    if (hitRect.top < paneRect.top + 16 || hitRect.bottom > paneRect.bottom - 16) {
      hit.scrollIntoView({block: 'center', behavior: 'smooth'});
    }
  });
}

function showEmptyDetail() {
  document.getElementById('detail-panel').innerHTML = '<div class="empty-state"><div class="empty-state-icon"><svg width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.35"><rect x="8" y="6" width="32" height="36" rx="3"/><line x1="16" y1="16" x2="32" y2="16"/><line x1="16" y1="22" x2="32" y2="22"/><line x1="16" y1="28" x2="28" y2="28"/></svg></div><div class="empty-state-text">选择一条结果查看详情</div></div>';
}

/* ═══ Page detail toggle ═══ */
function togglePageDetail(el) {
  const body = el.nextElementSibling;
  if (!body) return;
  const open = body.classList.toggle('open');
  el.textContent = open ? '页码详情 ▾' : '页码详情 ▸';
}

/* ═══ Keyboard shortcuts ═══ */
document.addEventListener('keydown', function(e) {
  if (currentPage !== 'search') return;
  if (e.target && e.target.id === 'document-filter-query') {
    if (e.key === 'Escape') closeSearchSelects();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    runSearch();
    e.preventDefault();
    return;
  }
  if (e.key === 'Enter' && !e.isComposing) {
    runSearch();
    e.preventDefault();
    return;
  }
  if (e.key === 'ArrowDown' && searchResults.length) {
    e.preventDefault();
    selectResult(Math.min(selectedIndex + 1, searchResults.length - 1));
  }
  if (e.key === 'ArrowUp' && searchResults.length) {
    e.preventDefault();
    selectResult(Math.max(selectedIndex - 1, 0));
  }
});

/* ═══ Helpers ═══ */
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function truncate(s, n) {
  s = String(s || '').replace(/\s+/g, ' ');
  return s.length > n ? s.slice(0, n) + '…' : s;
}
function truncateHTML(html, maxText) {
  const div = document.createElement('div');
  div.innerHTML = html;
  const text = div.textContent || '';
  if (text.length <= maxText) return html;
  return esc(text.slice(0, maxText)) + '…';
}
function matchTypeLabel(t) {
  const m = {exact:'精确',normalized_exact:'标准化',space_insensitive:'忽略空格',punctuation_insensitive:'忽略标点',ngram_fuzzy:'模糊'};
  return m[t] || t || '';
}
function mappingMethodLabel(m) {
  const labels = {manual_segment:'人工分段',manual:'人工',manual_override:'人工覆盖',fixed_offset:'固定偏移',manual_page:'逐页校准',pdf_page_label:'PDF标签',numeric_bookmark_sequence:'PDF数字书签',native_pdf_edge_sequence:'页边数字序列',ocr_sequence:'OCR序列',ocr_sequence_with_structure:'OCR序列+结构',combined_sequence:'多来源序列',uncalibrated:'未校准',mixed:'混合'};
  return labels[m] || m || '';
}
function mappingStatusLabel(status) {
  const labels = {manual_mapped:'人工映射',auto_mapped_high:'自动映射 · 高可信',auto_mapped_medium:'自动映射 · 待确认',needs_review:'待确认',unmapped:'未映射',auto_mapping_failed:'自动检测失败',source_missing:'原文件缺失'};
  return labels[status] || status || '未映射';
}
function mappingConfidenceLabel(level, score) {
  const labels = {high:'高', medium:'中', low:'低', mixed:'混合'};
  const pct = score != null ? '（' + Math.round(Number(score) * 100) + '%）' : '';
  return (labels[level] || level || '') + pct;
}
function pageScopeLabel(scope) {
  const labels = {body:'正文', preface:'序言', front_matter:'前置页', appendix:'附录', mixed:'混合'};
  return labels[scope] || scope || '';
}
function mappingEvidenceSummary(evidence) {
  if (!evidence) return '';
  if (typeof evidence === 'string') return evidence;
  const parts = [];
  if (evidence.observed_page_numbers != null) parts.push('识别页码 ' + evidence.observed_page_numbers + ' 个');
  if (evidence.sequence_consistency != null) parts.push('连续性 ' + Math.round(Number(evidence.sequence_consistency) * 100) + '%');
  if (evidence.inferred_offset != null) parts.push('offset ' + evidence.inferred_offset);
  if (evidence.structure_evidence) parts.push('结构：' + pageScopeLabel(evidence.structure_evidence));
  return parts.join('；') || JSON.stringify(evidence).slice(0, 120);
}
function autoMappingSegmentText(seg) {
  if (!seg) return '';
  const pdfStart = Number(seg.pdf_page_start) + 1;
  const pdfEnd = Number(seg.pdf_page_end) + 1;
  const citation = formatCitationPageLabel({
    source_type: 'pdf',
    citation_page_label_start: seg.citation_page_label_start,
    citation_page_label_end: seg.citation_page_label_end,
    citation_page_start: seg.citation_page_start,
    citation_page_end: seg.citation_page_end
  });
  return pageScopeLabel(seg.page_scope) + ' PDF ' + pdfStart + '–' + pdfEnd + ' → ' + citation + ' ' + mappingConfidenceLabel(seg.confidence_level, seg.mapping_confidence);
}

function firstPageValue(values) {
  for (var i = 0; i < values.length; i++) {
    if (values[i] !== undefined && values[i] !== null && String(values[i]).trim() !== '') {
      return String(values[i]).trim();
    }
  }
  return '';
}

function isUncalibratedPageLabel(value) {
  return /(?:页码尚未校准|引用页码尚未校准|页码未验证|未校准)/.test(String(value || ''));
}

function formatChinesePageRange(start, end) {
  start = String(start || '').trim();
  end = String(end || '').trim();
  if (!start || isUncalibratedPageLabel(start)) return '页码尚未校准';
  if (!end || end === start || isUncalibratedPageLabel(end)) end = '';

  var startMatch = start.match(/^(.*?第)([^页]+)页$/);
  var endMatch = end.match(/^(.*?第)([^页]+)页$/);
  if (startMatch && !end) return start;
  if (startMatch && endMatch && startMatch[1] === endMatch[1]) {
    return startMatch[1] + startMatch[2] + '—' + endMatch[2] + '页';
  }
  if (startMatch && end && end.indexOf('页') < 0) {
    return startMatch[1] + startMatch[2] + '—' + end + '页';
  }
  if (start.indexOf('页') >= 0) return end ? start + '—' + end : start;
  if (endMatch && endMatch[1] === '第') return '第' + start + '—' + endMatch[2] + '页';
  return '第' + start + (end ? '—' + end : '') + '页';
}

function formatCitationPageLabel(item) {
  item = item || {};
  var sourceType = String(item.source_type || '').toLowerCase();
  var start;
  var end;
  if (sourceType === 'pdf') {
    start = firstPageValue([
      item.citation_page_label,
      item.citation_page_label_start,
      item.citation_page,
      item.citation_page_start
    ]);
    end = firstPageValue([item.citation_page_label_end, item.citation_page_end]);
  } else {
    start = firstPageValue([
      item.citation_page_label,
      item.citation_page_label_start,
      item.original_page_start,
      item.page
    ]);
    end = firstPageValue([
      item.citation_page_label_end,
      item.original_page_end,
      item.end_page
    ]);
  }
  return formatChinesePageRange(start, end);
}
function pdRow(label, value) {
  return '<div class="page-detail-row"><span class="page-detail-label">' + esc(label) + '</span><span>' + esc(String(value)) + '</span></div>';
}

function selectedResult() {
  if (selectedIndex < 0 || selectedIndex >= searchResults.length) return null;
  return searchResults[selectedIndex];
}

function setCitationStyle(style) {
  citationStyle = style === 'gb' ? 'gb' : 'chinese';
  localStorage.setItem('meFinderCitationStyle', citationStyle);
}

function selectCitationStyle(event, style) {
  event.stopPropagation();
  setCitationStyle(style);
  var label = document.getElementById('citation-style-label');
  if (label) label.textContent = citationStyle === 'gb' ? 'GB/T 7714' : '中文脚注';
  document.querySelectorAll('#citation-style-control .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', option.dataset.value === citationStyle);
  });
  closeAppSelects();
}

function citationForItem(item) {
  const formats = item && item.citation_formats ? item.citation_formats : {};
  return formats[citationStyle] || formats.chinese || formats.gb || item.copy_text || '';
}

function citationIsComplete(item) {
  const formats = item && item.citation_formats ? item.citation_formats : {};
  return formats[citationStyle + '_status'] === 'complete';
}

function showCitationMetadataError(item) {
  const formats = item && item.citation_formats ? item.citation_formats : {};
  const missing = formats[citationStyle + '_missing_fields'] || [];
  const labels = {author:'作者',title:'书名',translator:'译者',publisher:'出版社',publish_place:'出版地',publish_year:'出版年份',citation_page:'引用页码'};
  showToast('无法复制：缺少' + missing.map(function(x){return labels[x] || x;}).join('、'));
}

function copySelectedOriginal() {
  const item = selectedResult();
  if (!item) return;
  copyText(item.paragraph_text || '');
}

function copySelectedCitation() {
  const item = selectedResult();
  if (!item) return;
  if (!citationIsComplete(item)) { showCitationMetadataError(item); return; }
  copyText(citationForItem(item));
}

function copySelectedOriginalAndCitation() {
  const item = selectedResult();
  if (!item) return;
  if (!citationIsComplete(item)) { showCitationMetadataError(item); return; }
  const original = item.paragraph_text || '';
  const citation = citationForItem(item);
  copyText(original + (citation ? '\n\n' + citation : ''));
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => showToast('已复制')).catch(() => showToast('复制失败'));
}

async function openSource(sourceId, page) {
  if (!sourceId) return;
  try {
    const resp = await fetch('/api/open-source', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId, page: page})
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '打开失败');
    if (data.page && data.page_jump) showToast('已打开原文并跳转到 PDF 第 ' + data.page + ' 页');
    else if (data.page) showToast('已用系统默认阅读器打开，请手动翻到 PDF 第 ' + data.page + ' 页');
    else showToast('已打开原文');
  } catch(e) {
    showToast(e.message || '打开失败');
  }
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1800);
}

/* ═══ Library ═══ */
async function loadLibrary() {
  try {
    const resp = await fetch('/api/sources');
    const data = await resp.json();
    libSources = data.source_files || [];
    libVolumes = data.volumes || [];
    libWorks = data.works || [];
    libLoaded = true;
    document.getElementById('library-total').textContent = libSources.length + ' 部文献';
    syncLibraryViewButtons();
    syncLibrarySortControls();
    renderLibraryList();
  } catch(e) {
    document.getElementById('library-total').textContent = '加载失败';
  }
}

function setLibFilter(btn) {
  libTypeFilter = btn.dataset.type;
  document.querySelectorAll('#lib-type-control .seg-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  closeLibDrawer();
  renderLibraryList();
}

function filterLibrary() {
  renderLibraryList();
}

function setLibraryView(mode) {
  libViewMode = mode === 'grid' ? 'grid' : 'list';
  localStorage.setItem('meFinderLibraryView', libViewMode);
  persistDisplayPreference('library_view', libViewMode);
  syncLibraryViewButtons();
  renderLibraryList();
}

function syncLibraryViewButtons() {
  ['list','grid'].forEach(function(mode) {
    var button = document.getElementById('library-view-' + mode);
    if (!button) return;
    var active = libViewMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function setLibrarySortOption(event, control, value) {
  event.stopPropagation();
  if (control === 'direction') {
    libSortDirection = value === 'asc' ? 'asc' : 'desc';
    localStorage.setItem('meFinderLibrarySortDirection', libSortDirection);
  } else {
    libSortField = ['imported_at','title','author','modified_at','source_type'].indexOf(value) >= 0 ? value : 'imported_at';
    localStorage.setItem('meFinderLibrarySortField', libSortField);
  }
  syncLibrarySortControls();
  closeAppSelects();
  renderLibraryList();
}

function syncLibrarySortControls() {
  var labels = {imported_at:'导入时间',title:'书名',author:'作者',modified_at:'最近修改时间',source_type:'来源类型',desc:'降序',asc:'升序'};
  var fieldLabel = document.getElementById('library-sort-field-label');
  var directionLabel = document.getElementById('library-sort-direction-label');
  if (fieldLabel) fieldLabel.textContent = labels[libSortField] || labels.imported_at;
  if (directionLabel) directionLabel.textContent = labels[libSortDirection] || labels.desc;
  document.querySelectorAll('#library-sort-field-select .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', option.dataset.value === libSortField);
  });
  document.querySelectorAll('#library-sort-direction-select .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', option.dataset.value === libSortDirection);
  });
}

function librarySortProjection(source) {
  var volume = libVolumes.find(function(item) { return item.source_file_id === source.source_file_id; });
  var worksForSource = libWorks.filter(function(work) { return volume && work.volume_id === volume.volume_id; });
  var bib = sourceBibliographicMetadata(source);
  return {
    title: (volume && volume.display_title) || source.display_title || source.title || source.file_name || source.source_file_id,
    author: bib.author || (worksForSource[0] && worksForSource[0].author_label) || '',
    imported_at: source.imported_at || source.last_modified || '',
    modified_at: source.last_modified || source.imported_at || '',
    source_type: source.source_type === 'word' ? 'Word' : 'PDF'
  };
}

function compareLibraryDates(a, b) {
  var av = Date.parse(a || '') || 0;
  var bv = Date.parse(b || '') || 0;
  if (!av && !bv) return 0;
  if (!av) return 1;
  if (!bv) return -1;
  return libSortDirection === 'desc' ? bv - av : av - bv;
}

function getFilteredSources() {
  let sources = libSources.slice();
  if (libTypeFilter !== 'all') {
    sources = sources.filter(s => s.source_type === libTypeFilter);
  }
  const q = (document.getElementById('lib-search').value || '').trim().toLowerCase();
  if (q) {
    sources = sources.filter(s => {
      const vol = libVolumes.find(v => v.source_file_id === s.source_file_id);
      const title = (vol ? vol.display_title : s.file_name || '').toLowerCase();
      const fname = (s.file_name || '').toLowerCase();
      return title.includes(q) || fname.includes(q);
    });
  }
  sources.sort(function(a, b) {
    var left = librarySortProjection(a);
    var right = librarySortProjection(b);
    var result;
    if (libSortField === 'imported_at' || libSortField === 'modified_at') {
      result = compareLibraryDates(left[libSortField], right[libSortField]);
    } else {
      result = calibrationSortText(left[libSortField], right[libSortField], libSortDirection);
    }
    return result || calibrationSortText(left.title, right.title, 'asc');
  });
  return sources;
}

function renderLibraryList() {
  const sources = getFilteredSources();
  const listEl = document.getElementById('library-list');
  listEl.className = 'library-list-container library-view-' + libViewMode;
  const allCount = libSources.length;
  const wordCount = libSources.filter(s => s.source_type === 'word').length;
  const pdfCount = libSources.filter(s => s.source_type === 'pdf').length;
  document.querySelectorAll('#lib-type-control .seg-btn').forEach(function(btn) {
    var t = btn.dataset.type;
    var c = t === 'all' ? allCount : t === 'word' ? wordCount : pdfCount;
    var label = t === 'all' ? '全部' : t === 'word' ? 'Word' : 'PDF';
    btn.textContent = label + ' (' + c + ')';
  });

  if (sources.length === 0) {
    listEl.innerHTML = '<div class="empty-state" style="min-height:200px"><div class="empty-state-text">未找到匹配文献</div></div>';
    return;
  }
  listEl.innerHTML = sources.map(function(src) {
    var vol = libVolumes.find(function(v) { return v.source_file_id === src.source_file_id; });
    var title = vol ? vol.display_title : (src.file_name || src.source_file_id);
    var worksForSrc = libWorks.filter(function(w) { return vol && w.volume_id === vol.volume_id; });
    var bib = sourceBibliographicMetadata(src);
    var author = bib.author || (worksForSrc[0] && worksForSrc[0].author_label) || '作者信息待完善';
    var missingMetadataText = src.source_type === 'pdf' ? bibliographicMissingText(bib) : '';
    var size = formatFileSize(src.size_bytes);
    var isSelected = src.source_file_id === libSelectedId;
    var typeCls = src.source_type === 'pdf' ? 'pdf' : 'word';
    var typeLabel = src.source_type === 'pdf' ? 'PDF' : 'Word';
    if (src.source_type === 'pdf' && src.pdf_profile && src.pdf_profile.detected_pdf_type === 'mineru_structured') {
      typeCls = 'mineru';
      typeLabel = 'MinerU';
    }
    var mappingStatus = src.source_type === 'pdf' && src.pdf_profile
      ? mappingStatusLabel(src.pdf_profile.mapping_status)
      : '';
    if (libViewMode === 'grid') {
      var imported = formatCalDate(src.imported_at || src.last_modified);
      var secondary = src.source_type === 'word' ? ((vol && vol.corpus_title) || '') : mappingStatus;
      return '<article class="library-card library-entry' + (isSelected ? ' selected' : '') + '" data-id="' + esc(src.source_file_id) + '" onclick="selectLibDoc(\'' + esc(src.source_file_id) + '\')">'
        + '<div class="library-card-top"><div class="library-card-badges"><span class="type-badge ' + typeCls + '">' + typeLabel + '</span>' + (secondary ? '<span class="library-card-status">' + esc(secondary) + '</span>' : '') + '</div></div>'
        + '<div class="library-card-title">' + esc(title) + '</div><div class="library-card-author">' + esc(author) + '</div>'
        + (missingMetadataText ? bibliographicMissingBadge(bib) : '')
        + '<div class="library-card-meta">' + esc((worksForSrc.length || 1) + ' 篇 · ' + size) + '</div>'
        + '<div class="library-card-mapping">' + esc(src.source_type === 'pdf' ? (mappingStatus || '页码状态待检查') : ((vol && vol.version_info) || 'Word 文献')) + '</div>'
        + '<div class="library-card-footer"><span class="library-card-action">查看详情</span><span class="library-card-date">' + esc(imported === '未知' ? '日期未知' : imported + ' 导入') + '</span></div></article>';
    }
    return '<div class="library-row library-entry' + (isSelected ? ' selected' : '') + '" data-id="' + esc(src.source_file_id) + '" onclick="selectLibDoc(\'' + esc(src.source_file_id) + '\')">'
      + '<span class="type-badge ' + typeCls + '">' + typeLabel + '</span>'
      + '<span class="library-row-title">' + esc(title) + '</span>'
      + '<span class="library-row-info">'
      + (worksForSrc.length > 0 ? '<span class="works-count">' + worksForSrc.length + ' 篇</span>' : '')
      + (mappingStatus ? '<span>' + esc(mappingStatus) + '</span>' : '')
      + (missingMetadataText ? '<span class="library-row-missing" title="' + esc(missingMetadataText) + '">' + esc(missingMetadataText) + '</span>' : '')
      + '<span>' + size + '</span>'
      + '</span>'
      + '</div>';
  }).join('');
}

function selectLibDoc(sourceId) {
  libSelectedId = sourceId;
  document.querySelectorAll('#library-list .library-entry').forEach(function(row) {
    row.classList.toggle('selected', row.dataset.id === sourceId);
  });
  var src = libSources.find(function(s) { return s.source_file_id === sourceId; });
  if (!src) return;
  var vol = libVolumes.find(function(v) { return v.source_file_id === sourceId; });
  var works = vol ? libWorks.filter(function(w) { return w.volume_id === vol.volume_id; }) : [];
  var title = vol ? vol.display_title : (src.file_name || sourceId);
  var corpusTitle = vol ? (vol.corpus_title || '') : '';

  var info = '';
  info += drawerInfoRow('文件类型', src.source_type === 'pdf' ? 'PDF 文档' : 'Word 文档');
  info += drawerInfoRow('文件名', src.file_name);
  info += drawerInfoRow('大小', formatFileSize(src.size_bytes));
  if (src.source_type === 'pdf' && src.pdf_profile) {
    info += drawerInfoRow('PDF 页数', src.pdf_profile.pdf_page_count + ' 页');
    info += drawerInfoRow('PDF 类型', pdfTypeLabel(src.pdf_profile.detected_pdf_type));
    info += drawerInfoRow('页码状态', mappingStatusLabel(src.pdf_profile.mapping_status));
    if (src.pdf_profile.auto_page_mapping) {
      var autoMap = src.pdf_profile.auto_page_mapping;
      var autoText = autoMap.method === 'manual_override'
        ? '保留人工映射'
        : '应用 ' + (autoMap.applied_segment_count || 0) + ' 个自动段，候选 ' + (autoMap.candidate_count || 0) + ' 个';
      info += drawerInfoRow('自动页码映射', autoText);
      if (autoMap.applied_segments && autoMap.applied_segments.length) {
        info += drawerInfoRow('自动映射区间', autoMap.applied_segments.map(autoMappingSegmentText).join('；'));
      }
      if (autoMap.exception_pages && autoMap.exception_pages.length) {
        info += drawerInfoRow('异常页面', autoMap.exception_pages.length + ' 页');
      }
    }
  }
  if (src.last_modified) {
    info += drawerInfoRow('修改日期', src.last_modified.split('T')[0]);
  }
  if (vol && vol.version_info) {
    info += drawerInfoRow('版本', vol.version_info);
  }

  var worksHTML = '';
  if (works.length > 0) {
    worksHTML = '<div class="drawer-section-title">收录文献 (' + works.length + ')</div>'
      + '<div class="drawer-works-list">'
      + works.map(function(w) {
        var meta = [];
        if (w.author_label) meta.push(w.author_label);
        if (w.date_label) meta.push(w.date_label);
        if (w.toc_page_start) meta.push('p.' + w.toc_page_start + (w.toc_page_end ? '–' + w.toc_page_end : ''));
        return '<div class="drawer-work-item">'
          + '<div class="drawer-work-title">' + esc(w.title) + '</div>'
          + (meta.length ? '<div class="drawer-work-meta">' + esc(meta.join(' · ')) + '</div>' : '')
          + '</div>';
      }).join('')
      + '</div>';
  }

  var bibliographicHTML = '';
  if (src.source_type === 'pdf') {
    bibliographicHTML = bibliographicEditorHTML(src);
  }

  var autoActions = '';
  if (src.source_type === 'pdf') {
    autoActions += '<button class="action-btn primary" onclick="openCalibrationAndDetect(\'' + esc(src.source_file_id) + '\')">自动检测页码</button>';
  }
  if (src.source_type === 'pdf' && src.pdf_profile && src.pdf_profile.auto_page_mapping) {
    var autoMapForActions = src.pdf_profile.auto_page_mapping;
    if (autoMapForActions.applied_segments && autoMapForActions.applied_segments.length) {
      autoActions += '<button class="action-btn" onclick="acceptAutoMapping(\'' + esc(src.source_file_id) + '\')">接受自动映射</button>';
    }
    if (autoMapForActions.exception_pages && autoMapForActions.exception_pages.length) {
      autoActions += '<button class="action-btn" onclick="showAutoMappingExceptions(\'' + esc(src.source_file_id) + '\')">检查异常</button>';
    }
    autoActions += '<button class="action-btn" onclick="openCalibrationForSource(\'' + esc(src.source_file_id) + '\')">编辑区间</button>';
  }

  var el = document.getElementById('library-drawer-content');
  el.innerHTML = '<button class="drawer-close" onclick="closeLibDrawer()">← 关闭</button>'
    + '<div class="drawer-title">' + esc(title) + '</div>'
    + (corpusTitle ? '<div class="drawer-subtitle">' + esc(corpusTitle) + '</div>' : '')
    + '<div class="detail-pills" style="margin-top:12px">'
    + '<span class="detail-pill">' + (src.source_type === 'pdf' ? 'PDF' : 'Word') + '</span>'
    + (vol && vol.primary_structure ? '<span class="detail-pill">' + structureLabel(vol.primary_structure) + '</span>' : '')
    + '</div>'
    + '<div class="drawer-info">' + info + '</div>'
    + bibliographicHTML
    + worksHTML
    + '<div class="drawer-actions">'
    + autoActions
    + (src.source_file_id ? '<button class="action-btn primary" onclick="openSource(\'' + esc(src.source_file_id) + '\', null)">打开原文</button>' : '')
    + '</div>';
  document.getElementById('library-drawer').classList.add('open');
}

function sourceBibliographicMetadata(src) {
  var nested = src && src.bibliographic_metadata ? src.bibliographic_metadata : {};
  var meta = Object.assign({}, nested);
  ['title','author','country','translator','publisher','publish_place','publish_year','isbn','document_type','metadata_status','metadata_source','metadata_confidence','metadata_evidence','metadata_conflicts','metadata_missing_fields'].forEach(function(key) {
    if (src && src[key] != null && src[key] !== '') meta[key] = src[key];
  });
  return meta;
}

const bibliographicFieldLabels = {author:'作者',title:'书名',translator:'译者',publisher:'出版社',publish_place:'出版地',publish_year:'出版年份'};

function bibliographicMissingFields(meta) {
  meta = meta || {};
  var listed = Array.isArray(meta.metadata_missing_fields) ? meta.metadata_missing_fields.slice() : null;
  var required = listed || ['author','title','publisher','publish_place','publish_year'];
  if (!listed && meta.document_type === 'translated_book') required.splice(2, 0, 'translator');
  return required.filter(function(field, index, values) {
    if (field === 'isbn' || !bibliographicFieldLabels[field] || values.indexOf(field) !== index) return false;
    if (listed) return true;
    return !String(meta[field] == null ? '' : meta[field]).trim();
  });
}

function bibliographicMissingText(meta) {
  var fields = bibliographicMissingFields(meta);
  return fields.length ? '缺少：' + fields.map(function(field) { return bibliographicFieldLabels[field]; }).join('、') : '';
}

function bibliographicMissingBadge(meta) {
  var text = bibliographicMissingText(meta);
  if (!text) return '';
  return '<span class="bibliographic-missing" title="ISBN 不计入引文必需字段"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/></svg><span>' + esc(text) + '</span></span>';
}

function bibliographicEditorHTML(src) {
  var meta = sourceBibliographicMetadata(src);
  var missing = bibliographicMissingFields(meta);
  function field(id, metadataField, label, value, full) {
    var isMissing = missing.indexOf(metadataField) >= 0;
    return '<div class="bibliographic-field' + (full ? ' full' : '') + (isMissing ? ' is-missing' : '') + '"><label for="bib-' + id + '">' + label + (isMissing ? ' · 缺少' : '') + '</label><input id="bib-' + id + '" value="' + esc(value || '') + '"></div>';
  }
  return '<div class="drawer-section-title">书目信息</div>'
    + bibliographicMissingBadge(meta)
    + '<div class="bibliographic-grid">'
    + field('author','author','作者',meta.author,false) + field('country','country','国别',meta.country,false)
    + field('title','title','书名',meta.title,false) + field('translator','translator','译者',meta.translator,false)
    + field('publish-place','publish_place','出版地',meta.publish_place,false)
    + field('publisher','publisher','出版社',meta.publisher,false) + field('publish-year','publish_year','出版年份',meta.publish_year,false)
    + field('isbn','isbn','ISBN',meta.isbn,true) + '</div>'
    + '<div class="bibliographic-meta">状态：' + esc(metadataStatusLabel(meta.metadata_status)) + ' · 来源：' + esc(metadataSourceLabel(meta.metadata_source)) + '</div>'
    + '<div class="auto-detect-actions">'
    + '<button class="action-btn" onclick="detectBibliographicMetadata(\'' + esc(src.source_file_id) + '\',false)">自动识别书目信息</button>'
    + (meta.metadata_source === 'manual' ? '<button class="action-btn" onclick="detectBibliographicMetadata(\'' + esc(src.source_file_id) + '\',true)">重新识别并覆盖表单</button>' : '')
    + '<button class="action-btn primary" onclick="saveBibliographicMetadata(\'' + esc(src.source_file_id) + '\')">保存</button>'
    + '<button class="action-btn" onclick="showBibliographicEvidence(\'' + esc(src.source_file_id) + '\')">查看识别依据</button>'
    + '</div>';
}

function metadataStatusLabel(status) {
  return ({complete:'完整',partial:'部分缺失',missing:'缺失',needs_review:'待确认',recognition_failed:'识别失败'})[status] || status || '未识别';
}

function metadataSourceLabel(source) {
  return ({manual:'人工维护',auto:'自动识别',automatic_recognition:'自动识别',pdf_metadata:'PDF 元数据'})[source] || source || '未知';
}

function collectBibliographicForm() {
  function value(id) { var el = document.getElementById('bib-' + id); return el ? el.value.trim() : ''; }
  return {author:value('author'),country:value('country'),title:value('title'),translator:value('translator'),publish_place:value('publish-place'),publisher:value('publisher'),publish_year:value('publish-year'),isbn:value('isbn')};
}

async function detectBibliographicMetadata(sourceId, force) {
  if (force && !confirm('确认用自动识别结果覆盖当前表单中的人工书目信息吗？')) return;
  try {
    showToast('正在识别封面、书名页、CIP 与版权页…');
    var resp = await fetch('/api/bibliographic-metadata/detect', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:sourceId,force:!!force})});
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '识别失败');
    var src = libSources.find(function(item){return item.source_file_id === sourceId;});
    if (src) {
      src.bibliographic_metadata = data.metadata;
      Object.keys(data.metadata).forEach(function(key){src[key]=data.metadata[key];});
      selectLibDoc(sourceId);
    }
    showToast(data.metadata.metadata_source === 'manual' && !force ? '人工元数据已保护，未覆盖' : '识别结果已载入，请检查后保存');
  } catch(e) { showToast('识别失败：' + e.message); }
}

async function saveBibliographicMetadata(sourceId) {
  try {
    var resp = await fetch('/api/bibliographic-metadata/save', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:sourceId,metadata:collectBibliographicForm()})});
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '保存失败');
    showToast('书目信息已保存并立即生效');
    libLoaded = false;
    await loadLibrary();
    selectLibDoc(sourceId);
  } catch(e) { showToast('保存失败：' + e.message); }
}

function showBibliographicEvidence(sourceId) {
  var src = libSources.find(function(item){return item.source_file_id === sourceId;});
  var evidence = sourceBibliographicMetadata(src).metadata_evidence || {};
  var labels = {title:'书名',author:'作者',country:'国别',translator:'译者',publisher:'出版社',publish_place:'出版地',publish_year:'出版年份',isbn:'ISBN'};
  var lines = Object.keys(evidence).map(function(field) {
    var item = evidence[field] || {};
    return (labels[field] || field) + '：' + (item.evidence_text || '无文本依据') + (item.source_page ? '（PDF 第 ' + item.source_page + ' 页）' : '') + (item.source === 'inferred_from_publisher' ? '（由出版社推断）' : '');
  });
  alert(lines.length ? lines.join('\n') : '暂无自动识别依据。');
}

async function openMetadataForSource(sourceId) {
  navigateTo('library');
  if (!libLoaded) await loadLibrary();
  selectLibDoc(sourceId);
}

function closeLibDrawer() {
  libSelectedId = null;
  document.getElementById('library-drawer').classList.remove('open');
  document.querySelectorAll('#library-list .library-entry').forEach(function(row) { row.classList.remove('selected'); });
}

async function acceptAutoMapping(sourceId) {
  if (!sourceId) return;
  try {
    showToast('正在接受自动映射…');
    var resp = await fetch('/api/auto-page-mapping/accept', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '接受失败');
    showToast('自动映射已接受为人工映射');
    libLoaded = false;
    calPdfsLoaded = false;
    await loadMeta();
    await loadLibrary();
    selectLibDoc(sourceId);
  } catch(e) {
    showToast('接受失败：' + e.message);
  }
}

function showAutoMappingExceptions(sourceId) {
  var src = libSources.find(function(s) { return s.source_file_id === sourceId; });
  var autoMap = src && src.pdf_profile ? src.pdf_profile.auto_page_mapping : null;
  var pages = autoMap && autoMap.exception_pages ? autoMap.exception_pages : [];
  if (!pages.length) {
    showToast('没有异常页面');
    return;
  }
  alert('异常页面（PDF 物理页）：\\n' + pages.slice(0, 80).map(function(p) { return Number(p) + 1; }).join(', ') + (pages.length > 80 ? '\\n…' : ''));
}

async function openCalibrationForSource(sourceId) {
  navigateTo('calibration');
  if (!calPdfsLoaded) await loadCalPdfs();
  await selectCalibrationDoc(sourceId);
}

async function openCalibrationAndDetect(sourceId) {
  await openCalibrationForSource(sourceId);
  await runAutoDetection(sourceId);
}

function drawerInfoRow(label, value) {
  return '<div class="drawer-info-row"><span class="drawer-info-label">' + esc(label) + '</span><span class="drawer-info-value">' + esc(String(value || '—')) + '</span></div>';
}

function pdfTypeLabel(type) {
  var labels = {native_text:'原生文本',scanned:'扫描版',broken_text:'文本损坏',complex_layout:'复杂排版',mineru_structured:'MinerU 结构化'};
  return labels[type] || type || '未知';
}

function structureLabel(s) {
  var labels = {article_collection:'文集',monograph:'专著',whole_pdf:'整本',pdf_document:'PDF 文献',manuscript_selection:'手稿选编',mixed:'混合',letters:'书信集'};
  return labels[s] || s || '';
}

function formatFileSize(bytes) {
  if (!bytes) return '—';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

/* ═══ Calibration ═══ */
async function loadCalPdfs(options) {
  options = options || {};
  var grid = document.getElementById('cal-card-grid');
  syncCalibrationViewButtons();
  if (grid) grid.classList.toggle('is-list', calViewMode === 'list');
  if (grid && options.showSkeleton !== false) grid.innerHTML = calibrationSkeletonHTML();
  try {
    var resp = await fetch('/api/calibration-library');
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '加载失败');
    calPdfList = data.items || [];
    calPdfsLoaded = true;
    renderCalibrationStats();
    renderCalibrationCards();
    if (calSelectedSourceId && calPdfList.some(function(item) { return item.source_file_id === calSelectedSourceId; })) {
      await loadCalibrationDoc(calSelectedSourceId);
    } else if (calSelectedSourceId) {
      closeCalibrationDetail();
    }
  } catch(e) {
    calPdfsLoaded = false;
    if (grid) grid.innerHTML = calibrationEmptyHTML('加载失败', e.message, false);
  }
}

async function refreshCalibrationSource(sourceId) {
  var resp = await fetch('/api/calibration-library');
  var data = await resp.json();
  if (!resp.ok || data.error) throw new Error(data.error || '刷新文献状态失败');
  var refreshed = (data.items || []).find(function(item) { return item.source_file_id === sourceId; });
  if (!refreshed) throw new Error('刷新后未找到当前文献');
  var index = calPdfList.findIndex(function(item) { return item.source_file_id === sourceId; });
  if (index >= 0) calPdfList[index] = refreshed;
  else calPdfList.push(refreshed);
  calPdfsLoaded = true;
  updateCalibrationCard(sourceId);
  if (calSelectedSourceId === sourceId) await loadCalibrationDoc(sourceId);
}

async function refreshCalibrationLibrary() {
  if (calRefreshInProgress) return;
  calRefreshInProgress = true;
  var button = document.getElementById('cal-refresh-btn');
  var pane = document.getElementById('cal-library-pane');
  var scrollTop = pane ? pane.scrollTop : 0;
  if (button) { button.disabled = true; button.classList.add('refreshing'); }
  try {
    calPdfsLoaded = false;
    await loadCalPdfs({showSkeleton:false});
    if (pane) requestAnimationFrame(function() { pane.scrollTop = scrollTop; });
  } finally {
    calRefreshInProgress = false;
    if (button) { button.disabled = false; button.classList.remove('refreshing'); }
  }
}

function calibrationSkeletonHTML() {
  if (calViewMode === 'list') {
    return [1,2,3,4,5,6].map(function() {
      return '<div class="cal-doc-row cal-skeleton"><div><div class="skeleton-line" style="width:34%"></div><div class="skeleton-line" style="width:78%;height:14px"></div></div><div class="skeleton-line" style="width:65%"></div><div class="skeleton-line" style="width:70%"></div><div class="skeleton-line" style="width:56px"></div></div>';
    }).join('');
  }
  return [1,2,3,4,5,6].map(function() {
    return '<div class="cal-doc-card cal-skeleton"><div class="skeleton-line" style="width:35%"></div><div class="skeleton-line" style="width:82%;margin-top:28px;height:15px"></div><div class="skeleton-line" style="width:48%"></div><div class="skeleton-line" style="width:70%;margin-top:26px"></div></div>';
  }).join('');
}

function renderCalibrationStats() {
  var current = {total:calPdfList.length,calibrated:0,pending:0,review:0,failed:0,mapping:0};
  calPdfList.forEach(function(item) {
    var status = calTransientStatus[item.source_file_id] || item.status;
    var group = calibrationStatusGroup(status);
    if (current[group] != null) current[group] += 1;
  });
  document.getElementById('calibration-stats').innerHTML = calibrationStatButton('all','PDF 总数',current.total,'info','document')
    + calibrationStatButton('calibrated','已校准',current.calibrated,'success','check')
    + calibrationStatButton('pending','待校准',current.pending,'neutral','clock')
    + calibrationStatButton('review','待确认',current.review,'warning','notice')
    + calibrationStatButton('failed','检测失败',current.failed,'danger','danger');
}

function statusStatIcon(icon) {
  var paths = {
    document:'<path d="M6 3h9l3 3v15H6z"/><path d="M15 3v4h4"/>',
    check:'<circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    notice:'<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/>',
    danger:'<path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/>'
  };
  return '<span class="status-stat__icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (paths[icon] || paths.notice) + '</svg></span>';
}

function statusChipIcon(group) {
  var icons = {
    calibrated:'<circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/>',
    pending:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    review:'<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/>',
    failed:'<path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/>',
    mapping:'<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/>'
  };
  var spinning = group === 'mapping' ? ' is-spinning' : '';
  return '<span class="status-chip__icon' + spinning + '" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (icons[group] || icons.pending) + '</svg></span>';
}

function calibrationStatButton(status, label, value, variant, icon) {
  return '<button type="button" data-status="' + status + '" class="status-stat status-stat--' + variant + (calStatusFilter === status ? ' active' : '') + '" onclick="applyCalStatusFilter(\'' + status + '\')">'
    + statusStatIcon(icon)
    + '<span class="status-stat__label">' + label + '</span>'
    + '<span class="status-stat__count">' + value + '</span></button>';
}

function setCalStatusFilter(button) {
  applyCalStatusFilter(button.dataset.status || 'all');
}

function applyCalStatusFilter(status) {
  calStatusFilter = status || 'all';
  document.querySelectorAll('#cal-status-tabs .cal-status-tab').forEach(function(item) { item.classList.toggle('active', item.dataset.status === calStatusFilter); });
  renderCalibrationStats();
  renderCalibrationCards();
}

function setCalSortOption(event, control, value) {
  event.stopPropagation();
  var labels = {imported_at:'导入时间',title:'书名',author:'作者',modified_at:'最近修改时间',status:'校准状态',desc:'降序',asc:'升序'};
  var selectId;
  if (control === 'direction') {
    calSortDirection = value === 'asc' ? 'asc' : 'desc';
    selectId = 'cal-sort-direction-select';
    document.getElementById('cal-sort-direction-label').textContent = labels[calSortDirection];
  } else {
    calSortField = ['imported_at','title','author','modified_at','status'].indexOf(value) >= 0 ? value : 'imported_at';
    selectId = 'cal-sort-field-select';
    document.getElementById('cal-sort-field-label').textContent = labels[calSortField];
  }
  document.querySelectorAll('#' + selectId + ' .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', option.dataset.value === (control === 'direction' ? calSortDirection : calSortField));
  });
  closeAppSelects();
  renderCalibrationCards();
}

function setCalibrationView(mode) {
  calViewMode = mode === 'list' ? 'list' : 'grid';
  localStorage.setItem('meFinderCalibrationView', calViewMode);
  persistDisplayPreference('calibration_view', calViewMode);
  syncCalibrationViewButtons();
  renderCalibrationCards();
}

function syncCalibrationViewButtons() {
  ['list','grid'].forEach(function(mode) {
    var button = document.getElementById('cal-view-' + mode);
    if (!button) return;
    var active = calViewMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

const calPinyinCollator = new Intl.Collator('zh-CN-u-co-pinyin', {sensitivity:'base', numeric:true});
const calLatinCollator = new Intl.Collator('en', {sensitivity:'base', numeric:true});

function calibrationSortText(a, b, direction) {
  a = String(a || '').trim(); b = String(b || '').trim();
  if (!a && !b) return 0;
  if (!a) return 1;
  if (!b) return -1;
  var ag = /^[\u3400-\u9fff]/.test(a) ? 0 : (/^[A-Za-z]/.test(a) ? 1 : 2);
  var bg = /^[\u3400-\u9fff]/.test(b) ? 0 : (/^[A-Za-z]/.test(b) ? 1 : 2);
  if (ag !== bg) return ag - bg;
  var value = (ag === 0 ? calPinyinCollator : calLatinCollator).compare(a, b);
  return direction === 'desc' ? -value : value;
}

function calibrationStatusGroup(status) {
  if (status === 'manual_mapped' || status === 'auto_mapped_high') return 'calibrated';
  if (status === 'needs_review') return 'review';
  if (status === 'auto_mapping_failed' || status === 'source_missing') return 'failed';
  if (status === 'mapping') return 'mapping';
  return 'pending';
}

function statusSemanticVariant(group) {
  var variants = {calibrated:'success',pending:'neutral',review:'warning',failed:'danger',mapping:'info'};
  return variants[group] || 'neutral';
}

function calibrationStatusLabel(status) {
  var labels = {manual_mapped:'已校准',auto_mapped_high:'已校准',needs_review:'待确认',unmapped:'待校准',auto_mapping_failed:'检测失败',mapping:'正在检测',source_missing:'原文件缺失'};
  return labels[status] || '待校准';
}

function renderCalibrationCards() {
  var grid = document.getElementById('cal-card-grid');
  if (!grid) return;
  grid.classList.toggle('is-list', calViewMode === 'list');
  syncCalibrationViewButtons();
  var query = String(document.getElementById('cal-search').value || '').toLowerCase().replace(/\s+/g, '');
  var items = calPdfList.filter(function(item) {
    var status = calTransientStatus[item.source_file_id] || item.status;
    var matchesStatus = calStatusFilter === 'all' || calibrationStatusGroup(status) === calStatusFilter;
    var haystack = [item.title,item.author,item.translator,item.publisher,item.file_name].map(function(value) { return String(value || '').toLowerCase().replace(/\s+/g,''); }).join('|');
    return matchesStatus && (!query || haystack.indexOf(query) >= 0);
  });
  items.sort(function(a,b) {
    if (calSortField === 'title' || calSortField === 'author') return calibrationSortText(a[calSortField], b[calSortField], calSortDirection);
    if (calSortField === 'status') {
      var order = {manual_mapped:0,auto_mapped_high:1,unmapped:2,needs_review:3,auto_mapping_failed:4,source_missing:5,mapping:6};
      var av = order[calTransientStatus[a.source_file_id] || a.status] == null ? 99 : order[calTransientStatus[a.source_file_id] || a.status];
      var bv = order[calTransientStatus[b.source_file_id] || b.status] == null ? 99 : order[calTransientStatus[b.source_file_id] || b.status];
      return calSortDirection === 'desc' ? bv-av : av-bv;
    }
    var av = Date.parse(a[calSortField] || '') || 0;
    var bv = Date.parse(b[calSortField] || '') || 0;
    return calSortDirection === 'desc' ? bv-av : av-bv;
  });
  if (!calPdfList.length) {
    grid.innerHTML = calibrationEmptyHTML('尚未导入 PDF 文献','导入 PDF 后，可以在这里自动检测和校准页码。',true);
    return;
  }
  if (!items.length) {
    grid.innerHTML = calibrationEmptyHTML('没有符合当前条件的文献','可以清除搜索和状态筛选。',false);
    return;
  }
  grid.innerHTML = items.map(calViewMode === 'list' ? calibrationListRowHTML : calibrationCardHTML).join('');
}

function updateCalibrationCard(sourceId) {
  renderCalibrationStats();
  if (calStatusFilter !== 'all' || calSortField === 'status') {
    renderCalibrationCards();
    return;
  }
  var item = calPdfList.find(function(value) { return value.source_file_id === sourceId; });
  var current = document.querySelector('#cal-card-grid .cal-document-entry[data-id="' + CSS.escape(sourceId) + '"]');
  if (!item || !current) {
    renderCalibrationCards();
    return;
  }
  var template = document.createElement('template');
  template.innerHTML = (calViewMode === 'list' ? calibrationListRowHTML(item) : calibrationCardHTML(item)).trim();
  var replacement = template.content.firstElementChild;
  replacement.style.opacity = '0.45';
  current.replaceWith(replacement);
  requestAnimationFrame(function() { replacement.style.opacity = '1'; });
}

function calibrationEmptyHTML(title, note, importAction) {
  return '<div class="cal-grid-empty"><div><div class="cal-empty-title">' + esc(title) + '</div><div class="cal-empty-note">' + esc(note) + '</div><button class="action-btn' + (importAction ? ' primary' : '') + '" style="margin-top:14px" onclick="' + (importAction ? "navigateTo('import')" : 'clearCalibrationFilters()') + '">' + (importAction ? '导入 PDF' : '清除筛选') + '</button></div></div>';
}

function clearCalibrationFilters() {
  document.getElementById('cal-search').value = '';
  applyCalStatusFilter('all');
}

function calibrationCardHTML(item) {
  var status = calTransientStatus[item.source_file_id] || item.status;
  var group = calibrationStatusGroup(status);
  var action = group === 'calibrated' ? '查看映射' : (group === 'review' ? '检查结果' : (group === 'failed' ? '重新检测' : '自动检测页码'));
  var pageMeta = item.page_count ? item.page_count + ' 页' : '页数未知';
  var sizeMeta = formatFileSize(item.size_bytes);
  var imported = formatCalDate(item.imported_at);
  var mapping = item.mapping_summary || (group === 'failed' ? '未找到可靠页码序列' : (group === 'review' ? '已检测到映射，等待确认' : '尚未建立引用页码映射'));
  var segmentText = item.mapping_segment_count ? '共 ' + item.mapping_segment_count + ' 个映射区间' : '';
  var methodText = calibrationMappingMethodText(item, status);
  var metadataMissing = bibliographicMissingText(item);
  var selected = calSelectedSourceId === item.source_file_id ? ' selected' : '';
  return '<article class="cal-doc-card cal-document-entry' + selected + '" data-id="' + esc(item.source_file_id) + '" onclick="selectCalibrationDoc(\'' + esc(item.source_file_id) + '\')">'
    + '<div class="cal-card-top"><div class="cal-card-badges"><span class="type-badge ' + (item.parser_label === 'MinerU' ? 'mineru' : 'pdf') + '">' + esc(item.parser_label) + '</span><span class="cal-status-badge status-chip status-chip--' + statusSemanticVariant(group) + ' ' + group + '">' + statusChipIcon(group) + esc(calibrationStatusLabel(status)) + '</span></div>'
    + '<button class="cal-more-btn" title="更多操作" aria-label="更多操作" onclick="toggleCalibrationMore(event,\'' + esc(item.source_file_id) + '\')">⋯</button></div>'
    + (calOpenMenuId === item.source_file_id ? calibrationMoreMenuHTML(item.source_file_id) : '')
    + '<div class="cal-card-title">' + esc(item.title) + '</div><div class="cal-card-author">' + esc(item.author || '作者信息待完善') + '</div>'
    + (metadataMissing ? '<div class="cal-card-bib-missing">' + esc(metadataMissing) + '</div>' : '')
    + '<div class="cal-card-meta"><div>' + esc(pageMeta + ' · ' + sizeMeta) + '</div></div>'
    + '<div class="cal-card-mapping' + (group === 'failed' ? ' failed' : (group === 'review' ? ' review' : '')) + '">' + esc(mapping) + '</div><div class="cal-card-segments">' + esc(segmentText) + '</div>'
    + '<div class="cal-card-method' + (group === 'failed' ? ' failed' : (group === 'review' ? ' review' : '')) + '">' + esc(methodText) + '</div>'
    + '<div class="cal-card-footer"><button class="cal-card-action' + (group === 'calibrated' ? '' : ' needs-action') + '" onclick="calibrationPrimaryAction(event,\'' + esc(item.source_file_id) + '\')">' + action + '</button><span class="cal-card-date">' + esc(imported) + ' 导入</span></div></article>';
}

function calibrationListRowHTML(item) {
  var status = calTransientStatus[item.source_file_id] || item.status;
  var group = calibrationStatusGroup(status);
  var action = group === 'calibrated' ? '查看映射' : (group === 'review' ? '检查结果' : (group === 'failed' ? '重新检测' : '自动检测'));
  var pageMeta = item.page_count ? item.page_count + ' 页' : '页数未知';
  var mapping = item.mapping_summary || (group === 'failed' ? '未找到可靠页码序列' : (group === 'review' ? '已检测到映射，等待确认' : '尚未建立引用页码映射'));
  var method = calibrationMappingMethodText(item, status);
  var segments = item.mapping_segment_count ? item.mapping_segment_count + ' 个映射区间' : '';
  var metadataMissing = bibliographicMissingText(item);
  var selected = calSelectedSourceId === item.source_file_id ? ' selected' : '';
  return '<article class="cal-doc-row cal-document-entry' + selected + '" data-id="' + esc(item.source_file_id) + '" onclick="selectCalibrationDoc(\'' + esc(item.source_file_id) + '\')">'
    + '<div class="cal-row-identity"><div class="cal-row-badges"><span class="type-badge ' + (item.parser_label === 'MinerU' ? 'mineru' : 'pdf') + '">' + esc(item.parser_label) + '</span><span class="cal-status-badge status-chip status-chip--' + statusSemanticVariant(group) + ' ' + group + '">' + statusChipIcon(group) + esc(calibrationStatusLabel(status)) + '</span></div><div class="cal-row-title">' + esc(item.title) + '</div><div class="cal-row-author">' + esc(item.author || '作者信息待完善') + '</div>' + (metadataMissing ? '<div class="cal-row-bib-missing">' + esc(metadataMissing) + '</div>' : '') + '</div>'
    + '<div class="cal-row-file"><div>' + esc(pageMeta + ' · ' + formatFileSize(item.size_bytes)) + '</div><div class="cal-row-date">' + esc(formatCalDate(item.imported_at)) + ' 导入</div></div>'
    + '<div class="cal-row-mapping"><div class="cal-row-mapping-main' + (group === 'failed' ? ' failed' : (group === 'review' ? ' review' : '')) + '">' + esc(mapping) + '</div><div class="cal-row-mapping-sub">' + esc([segments, method].filter(Boolean).join(' · ')) + '</div></div>'
    + '<div class="cal-row-actions"><button class="cal-card-action' + (group === 'calibrated' ? '' : ' needs-action') + '" onclick="calibrationPrimaryAction(event,\'' + esc(item.source_file_id) + '\')">' + action + '</button><button class="cal-more-btn" title="更多操作" aria-label="更多操作" onclick="toggleCalibrationMore(event,\'' + esc(item.source_file_id) + '\')">⋯</button></div>'
    + (calOpenMenuId === item.source_file_id ? calibrationMoreMenuHTML(item.source_file_id) : '') + '</article>';
}

function calibrationMappingMethodText(item, status) {
  if (status === 'manual_mapped') return '人工映射';
  if (status === 'auto_mapped_high') return '自动映射 · 高可信';
  if (status === 'needs_review') return '自动检测 · 等待确认';
  if (status === 'mapping') return '正在分析页码序列';
  if (status === 'auto_mapping_failed' || status === 'source_missing') return '';
  return '';
}

function calibrationMoreMenuHTML(sourceId) {
  return '<div class="cal-more-menu" onclick="event.stopPropagation()">'
    + '<button onclick="calibrationMenuAction(event,\'open\',\'' + esc(sourceId) + '\')">打开原文</button>'
    + '<button onclick="calibrationMenuAction(event,\'view\',\'' + esc(sourceId) + '\')">查看映射</button>'
    + '<button onclick="calibrationMenuAction(event,\'detect\',\'' + esc(sourceId) + '\')">自动检测页码</button>'
    + '<button onclick="calibrationMenuAction(event,\'metadata\',\'' + esc(sourceId) + '\')">编辑书目信息</button>'
    + '<div class="menu-divider"></div><button class="danger" onclick="calibrationMenuAction(event,\'remove\',\'' + esc(sourceId) + '\')">从文献库移除</button></div>';
}

async function calibrationMenuAction(event, action, sourceId) {
  event.stopPropagation();
  calOpenMenuId = null;
  if (action === 'open') return openSource(sourceId, 1);
  if (action === 'view') return selectCalibrationDoc(sourceId);
  if (action === 'detect') return openCalibrationAndDetect(sourceId);
  if (action === 'metadata') return openMetadataForSource(sourceId);
  if (action === 'remove') return openRemoveDocumentModal(sourceId);
}

async function calibrationPrimaryAction(event, sourceId) {
  event.stopPropagation();
  var item = calPdfList.find(function(value) { return value.source_file_id === sourceId; });
  await selectCalibrationDoc(sourceId);
  var group = calibrationStatusGroup(calTransientStatus[sourceId] || (item ? item.status : 'unmapped'));
  if (group === 'pending' || group === 'failed') await runAutoDetection(sourceId);
}

function formatCalDate(value) {
  if (!value) return '未知';
  var date = new Date(value);
  if (isNaN(date.getTime())) return '未知';
  return date.getFullYear() + '-' + String(date.getMonth()+1).padStart(2,'0') + '-' + String(date.getDate()).padStart(2,'0');
}

function toggleCalibrationMore(event, sourceId) {
  event.stopPropagation();
  calOpenMenuId = calOpenMenuId === sourceId ? null : sourceId;
  renderCalibrationCards();
}

async function selectCalibrationDoc(sourceId) {
  calOpenMenuId = null;
  calSelectedSourceId = sourceId;
  renderCalibrationCards();
  document.getElementById('cal-detail-drawer').classList.add('open');
  document.querySelector('#page-calibration .calibration-body').classList.add('detail-open');
  await loadCalibrationDoc(sourceId);
}

function closeCalibrationDetail() {
  calSelectedSourceId = null;
  calSelectedDoc = null;
  document.getElementById('cal-detail-drawer').classList.remove('open');
  document.querySelector('#page-calibration .calibration-body').classList.remove('detail-open');
  renderCalibrationCards();
}

async function loadCalibrationDoc(sourceId) {
  sourceId = sourceId || calSelectedSourceId;
  var editor = document.getElementById('cal-editor');
  var infoEl = document.getElementById('cal-doc-info');
  if (!sourceId) {
    editor.style.display = 'none';
    infoEl.style.display = 'none';
    calSelectedDoc = null;
    calSegments = [];
    calAutoResult = null;
    document.getElementById('cal-auto-preview').style.display = 'none';
    return;
  }
  try {
    var resp = await fetch('/api/calibration?source_id=' + encodeURIComponent(sourceId));
    calSelectedDoc = await resp.json();
    if (calSelectedDoc.error) {
      showToast('文献未找到');
      return;
    }
    var mapping = calSelectedDoc.page_mapping || {};
    calSegments = (mapping.segments || []).map(function(s) { return Object.assign({}, s); });
    var pdf = calPdfList.find(function(p) { return p.source_file_id === sourceId; });
    var pages = pdf ? pdf.page_count || '?' : '?';
    var pdfType = pdf ? pdf.parser_label || 'PDF' : '—';
    var detailStatusGroup = calibrationStatusGroup(pdf ? pdf.status : 'unmapped');
    document.getElementById('cal-detail-heading').innerHTML = '<div class="drawer-title">' + esc(pdf ? pdf.title : 'PDF 文献') + '</div><div class="drawer-subtitle">' + esc(pdf && pdf.author ? pdf.author : '作者信息未完善') + '</div><div class="cal-detail-status"><span class="cal-status-badge status-chip status-chip--' + statusSemanticVariant(detailStatusGroup) + ' ' + detailStatusGroup + '">' + statusChipIcon(detailStatusGroup) + esc(calibrationStatusLabel(pdf ? pdf.status : 'unmapped')) + '</span></div>';
    infoEl.style.display = 'flex';
    infoEl.innerHTML = '<span class="detail-pill">PDF ' + pages + ' 页</span>'
      + '<span class="detail-pill">' + esc(pdfType) + '</span>'
      + (mapping.validated_by ? '<span class="detail-pill accent">已验证</span>' : '<span class="detail-pill">未验证</span>');
    document.getElementById('cal-detail-actions').innerHTML = '<button class="action-btn primary" id="cal-auto-detect-btn" onclick="runAutoDetection()">自动检测页码</button><button class="action-btn" onclick="scrollToManualMapping()">手动设置</button><button class="action-btn" onclick="showCalibrationEvidence()">查看识别依据</button><button class="action-btn" onclick="openSource(\'' + esc(sourceId) + '\',1)">打开原文</button>';
    editor.style.display = 'block';
    calAutoResult = null;
    document.getElementById('cal-auto-preview').style.display = 'none';
    renderCalSegments();
    updateCalPreview();
  } catch(e) {
    showToast('加载校准数据失败');
  }
}

async function runAutoDetection(sourceId) {
  sourceId = sourceId || calSelectedSourceId;
  if (!sourceId) {
    showToast('请先选择一本文献');
    return;
  }
  calTransientStatus[sourceId] = 'mapping';
  updateCalibrationCard(sourceId);
  var panel = document.getElementById('cal-auto-preview');
  var button = document.getElementById('cal-auto-detect-btn');
  panel.style.display = 'block';
  panel.innerHTML = '<div class="auto-detect-title">正在检测页码…</div><div class="auto-detect-note">正在读取 PDF 标签、数字书签、现有 MinerU 结果和页面边缘文本。</div>';
  if (button) button.disabled = true;
  try {
    var resp = await fetch('/api/auto-page-mapping/detect', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({source_id:sourceId, dry_run:true})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '检测失败');
    calAutoResult = data.result;
    renderAutoDetectionResult(calAutoResult);
    var current = calPdfList.find(function(item) { return item.source_file_id === sourceId; });
    var segments = (calAutoResult.selected_segments || []).filter(function(item) { return item && item.confidence_level !== 'low'; });
    calTransientStatus[sourceId] = current && current.status === 'manual_mapped' ? 'manual_mapped' : (segments.length ? 'needs_review' : 'auto_mapping_failed');
  } catch(e) {
    calAutoResult = null;
    calTransientStatus[sourceId] = 'auto_mapping_failed';
    panel.innerHTML = '<div class="auto-detect-title">自动检测失败</div><div class="auto-detect-note">' + esc(e.message) + '</div>';
  } finally {
    if (button) button.disabled = false;
    updateCalibrationCard(sourceId);
  }
}

function renderAutoDetectionResult(result) {
  var panel = document.getElementById('cal-auto-preview');
  var segments = (result.selected_segments || []).filter(function(s) { return s && s.confidence_level !== 'low'; });
  var html = '<div class="auto-detect-title">检测完成</div>';
  if (result.manual_mapping_present) {
    html += '<div class="auto-detect-note auto-detect-warning">当前文献已有人工页码映射。以下结果仅为预览，不会自动覆盖。</div>';
  }
  if (!segments.length) {
    html += '<div class="auto-detect-note">未能自动识别可靠页码区间。</div>';
    html += '<div class="auto-detect-note">' + autoFailureReasons(result.failure_reasons || []) + '</div>';
    html += '<div class="auto-detect-actions"><button class="action-btn" onclick="cancelAutoDetection()">关闭</button></div>';
    panel.innerHTML = html;
    return;
  }
  html += '<div class="auto-detect-note">识别到 ' + segments.length + ' 个页码区间，当前仍是预览状态。</div>';
  html += '<div class="auto-segment-list">' + segments.map(function(seg, index) {
    var evidence = seg.mapping_evidence || {};
    return '<div class="auto-segment-row"><div class="auto-segment-main">' + (index + 1) + '. ' + esc(autoMappingSegmentText(seg)) + '</div>'
      + '<div class="auto-segment-evidence">依据：' + esc(mappingMethodLabel(seg.method))
      + (evidence.inferred_offset != null ? '；稳定 offset = ' + evidence.inferred_offset : '')
      + (evidence.observed_page_numbers != null ? '；观察到 ' + evidence.observed_page_numbers + ' 个候选' : '')
      + (evidence.sequence_consistency != null ? '；序列一致性 ' + Math.round(Number(evidence.sequence_consistency) * 100) + '%' : '')
      + '</div></div>';
  }).join('') + '</div>';
  html += '<details style="margin-top:10px"><summary class="auto-detect-note">查看检测依据</summary><div class="auto-detect-note" style="margin-top:6px">'
    + 'PDF 标签 ' + Number((result.evidence_counts || {}).pdf_page_labels || 0) + ' 个；数字书签 ' + Number((result.evidence_counts || {}).numeric_bookmarks || 0)
    + ' 个；MinerU 候选 ' + Number((result.evidence_counts || {}).mineru_candidates || 0) + ' 个；页边候选 ' + Number((result.evidence_counts || {}).native_edge_candidates || 0) + ' 个。</div></details>';
  html += '<div class="auto-detect-actions">'
    + '<button class="action-btn primary" onclick="applyAutoDetection()">' + (result.manual_mapping_present ? '用自动结果替换人工映射' : '应用自动映射') + '</button>'
    + '<button class="action-btn" onclick="editAutoDetectionResult()">编辑后应用</button>'
    + '<button class="action-btn" onclick="cancelAutoDetection()">取消</button></div>';
  panel.innerHTML = html;
}

function autoFailureReasons(reasons) {
  var labels = {no_page_labels:'没有 PDF Page Labels',no_bookmarks:'没有数字书签',no_mineru_candidates:'现有 MinerU 结果没有可靠页码候选',no_edge_candidates:'页边区域未发现页码候选',sequence_not_found:'未找到稳定递增页码序列',source_missing:'原始 PDF 文件不存在'};
  return reasons.map(function(reason) { return '• ' + (labels[reason] || reason); }).join('<br>');
}

async function applyAutoDetection() {
  if (!calAutoResult) return;
  var sourceId = calSelectedSourceId;
  var segments = (calAutoResult.selected_segments || []).filter(function(s) { return s && s.confidence_level !== 'low'; });
  if (!segments.length) return;
  var replaceManual = false;
  if (calAutoResult.manual_mapping_present) {
    replaceManual = confirm('当前文献已有人工映射。确认用本次自动检测结果替换吗？');
    if (!replaceManual) return;
  }
  try {
    showToast('正在应用自动映射…');
    var resp = await fetch('/api/auto-page-mapping/apply', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source_id:sourceId,segments:segments,auto_mapping:calAutoResult,replace_manual:replaceManual})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '应用失败');
    showToast('自动页码映射已生效');
    delete calTransientStatus[sourceId];
    libLoaded = false;
    await loadMeta();
    await refreshCalibrationSource(sourceId);
  } catch(e) {
    showToast('应用失败：' + e.message);
  }
}

function editAutoDetectionResult() {
  if (!calAutoResult) return;
  calSegments = (calAutoResult.selected_segments || []).filter(function(s) { return s && s.confidence_level !== 'low'; }).map(function(s) {
    return Object.assign({}, s, {confidence:s.mapping_confidence || s.confidence});
  });
  renderCalSegments();
  updateCalPreview();
  document.getElementById('cal-auto-preview').style.display = 'none';
  showToast('自动结果已载入手动编辑区');
}

function cancelAutoDetection() {
  calAutoResult = null;
  document.getElementById('cal-auto-preview').style.display = 'none';
}

function segmentNumberStyleLabel(style) {
  return ({arabic:'阿拉伯数字',roman_lower:'罗马数字（小写）',roman_upper:'罗马数字（大写）',none:'无编号'})[style] || '阿拉伯数字';
}

function segmentNumberStyleControl(style, index) {
  var values = ['arabic','roman_lower','roman_upper','none'];
  return '<div class="app-select segment-style-select" id="segment-style-select-' + index + '">'
    + '<button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,\'segment-style-select-' + index + '\')"><span class="app-select-value">' + segmentNumberStyleLabel(style) + '</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>'
    + '<div class="app-select-menu" role="listbox">' + values.map(function(value) {
      return '<button class="app-select-option' + (style === value ? ' is-selected' : '') + '" type="button" data-value="' + value + '" onclick="setSegmentNumberStyle(event,' + index + ',\'' + value + '\')">' + segmentNumberStyleLabel(value) + '</button>';
    }).join('') + '</div></div>';
}

function setSegmentNumberStyle(event, index, value) {
  event.stopPropagation();
  updateCalSeg(index, 'number_style', value);
  closeAppSelects();
  renderCalSegments();
}

function renderCalSegments() {
  var body = document.getElementById('cal-segments-body');
  var noSeg = document.getElementById('cal-no-segments');
  if (calSegments.length === 0) {
    body.innerHTML = '';
    noSeg.style.display = 'block';
    document.querySelector('.segment-table-wrap').style.display = 'none';
    return;
  }
  noSeg.style.display = 'none';
  document.querySelector('.segment-table-wrap').style.display = 'block';
  body.innerHTML = calSegments.map(function(seg, i) {
    var citStart = seg.citation_page_start != null ? seg.citation_page_start : '';
    if (seg.citation === null && !citStart) citStart = '';
    var style = seg.number_style || 'arabic';
    var label = seg.label || seg.evidence || '';
    return '<tr>'
      + '<td><input class="seg-input narrow" type="number" min="1" value="' + (seg.pdf_page_start != null ? seg.pdf_page_start + 1 : '') + '" onchange="updateCalSeg(' + i + ',\'pdf_page_start\',this.value)"></td>'
      + '<td><input class="seg-input narrow" type="number" min="1" value="' + (seg.pdf_page_end != null ? seg.pdf_page_end + 1 : '') + '" onchange="updateCalSeg(' + i + ',\'pdf_page_end\',this.value)"></td>'
      + '<td><input class="seg-input narrow" type="text" value="' + esc(String(citStart)) + '" placeholder="留空=不映射" onchange="updateCalSeg(' + i + ',\'citation_page_start\',this.value)"></td>'
      + '<td>' + segmentNumberStyleControl(style, i) + '</td>'
      + '<td><input class="seg-input" type="text" value="' + esc(label) + '" placeholder="序言、正文或附录" onchange="updateCalSeg(' + i + ',\'label\',this.value)"></td>'
      + '<td><button class="seg-remove" onclick="removeCalSegment(' + i + ')" title="删除分段" aria-label="删除分段"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="m7 7 1 13h8l1-13"/><path d="M10 11v5M14 11v5"/></svg></button></td>'
      + '</tr>';
  }).join('');
}

function updateCalSeg(index, field, value) {
  var seg = calSegments[index];
  if (!seg) return;
  if (field === 'pdf_page_start' || field === 'pdf_page_end') {
    seg[field] = value === '' ? null : Math.max(0, parseInt(value, 10) - 1);
  } else if (field === 'citation_page_start') {
    if (value === '') {
      seg.citation_page_start = undefined;
      seg.citation = null;
    } else {
      seg.citation_page_start = value;
      delete seg.citation;
    }
  } else if (field === 'number_style' && value === 'none') {
    seg.number_style = 'none';
    seg.citation = null;
    delete seg.citation_page_start;
  } else {
    seg[field] = value;
  }
  if (!seg.method) seg.method = 'manual_segment';
  if (seg.confidence == null) seg.confidence = 0.9;
  updateCalPreview();
}

function addCalSegment() {
  var lastEnd = 0;
  if (calSegments.length > 0) {
    var last = calSegments[calSegments.length - 1];
    lastEnd = (last.pdf_page_end != null ? last.pdf_page_end : 0) + 1;
  }
  calSegments.push({
    pdf_page_start: lastEnd,
    pdf_page_end: lastEnd + 49,
    citation_page_start: '1',
    number_style: 'arabic',
    method: 'manual_segment',
    confidence: 0.9,
    label: ''
  });
  renderCalSegments();
  updateCalPreview();
}

function removeCalSegment(index) {
  calSegments.splice(index, 1);
  renderCalSegments();
  updateCalPreview();
}

function updateCalPreview() {
  var input = document.getElementById('cal-preview-input');
  var result = document.getElementById('cal-preview-result');
  var pageIndex = parseInt(input.value, 10) - 1;
  if (isNaN(pageIndex) || pageIndex < 0) {
    result.textContent = '—';
    return;
  }
  var mapped = null;
  var method = 'uncalibrated';
  for (var i = 0; i < calSegments.length; i++) {
    var seg = calSegments[i];
    var start = seg.pdf_page_start != null ? seg.pdf_page_start : -1;
    var end = seg.pdf_page_end != null ? seg.pdf_page_end : start;
    if (pageIndex >= start && pageIndex <= end) {
      if (seg.citation === null && !seg.citation_page_start) {
        method = seg.method || 'uncalibrated';
        mapped = null;
        break;
      }
      if (seg.citation_page_start != null && seg.citation_page_start !== '') {
        var offset = pageIndex - start;
        var style = seg.number_style || 'arabic';
        var citNum;
        try { citNum = parseInt(seg.citation_page_start, 10) + offset; } catch(e) { citNum = offset + 1; }
        if (style === 'roman_lower' || style === 'roman_upper') {
          mapped = intToRoman(citNum, style === 'roman_upper');
        } else {
          mapped = String(citNum);
        }
        method = seg.method || 'manual_segment';
        break;
      }
    }
  }
  if (mapped) {
    result.textContent = '引用' + formatCitationPageLabel({source_type:'pdf', citation_page_start:mapped}) + '（' + mappingMethodLabel(method) + '）';
    result.style.color = 'var(--accent)';
  } else {
    result.textContent = '未校准';
    result.style.color = 'var(--text-tertiary)';
  }
}

function intToRoman(num, upper) {
  if (num <= 0) return String(num);
  var vals = [[1000,'m'],[900,'cm'],[500,'d'],[400,'cd'],[100,'c'],[90,'xc'],[50,'l'],[40,'xl'],[10,'x'],[9,'ix'],[5,'v'],[4,'iv'],[1,'i']];
  var out = '';
  for (var i = 0; i < vals.length; i++) {
    while (num >= vals[i][0]) { out += vals[i][1]; num -= vals[i][0]; }
  }
  return upper ? out.toUpperCase() : out;
}

async function saveCalibration() {
  var sourceId = calSelectedSourceId;
  if (!sourceId) return;
  var hint = document.querySelector('.cal-save-hint');
  var cleanSegs = calSegments.map(function(seg) {
    var clean = {};
    if (seg.pdf_page_start != null) clean.pdf_page_start = seg.pdf_page_start;
    if (seg.pdf_page_end != null) clean.pdf_page_end = seg.pdf_page_end;
    if (seg.citation_page_start != null && seg.citation_page_start !== '') {
      clean.citation_page_start = seg.citation_page_start;
    } else {
      clean.citation = null;
    }
    if (seg.number_style) clean.number_style = seg.number_style;
    if (seg.method) clean.method = seg.method;
    if (seg.confidence != null) clean.confidence = seg.confidence;
    if (seg.label) clean.label = seg.label;
    if (seg.evidence) clean.evidence = seg.evidence;
    return clean;
  });
  try {
    hint.textContent = '正在保存并重建索引，请稍候…';
    var resp = await fetch('/api/calibration', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId, segments: cleanSegs})
    });
    var data = await resp.json();
    if (data.ok) {
      hint.textContent = '校准已生效';
      showToast('校准已保存，索引已更新');
      await loadMeta();
      delete calTransientStatus[sourceId];
      libLoaded = false;
      await refreshCalibrationSource(sourceId);
    } else {
      hint.textContent = '保存失败';
      showToast('保存失败：' + (data.error || '未知错误'));
    }
  } catch(e) {
    hint.textContent = '保存失败';
    showToast('保存失败：' + e.message);
  }
}

function scrollToManualMapping() {
  var table = document.querySelector('#cal-detail-drawer .segment-table-wrap');
  if (table) table.scrollIntoView({behavior:'smooth', block:'center'});
}

function showCalibrationEvidence() {
  var item = calPdfList.find(function(value) { return value.source_file_id === calSelectedSourceId; });
  if (!item) return;
  var panel = document.getElementById('cal-auto-preview');
  var evidence = item.mapping_evidence || [];
  var failures = item.failure_reasons || [];
  var html = '<div class="auto-detect-title">自动映射依据</div>';
  if (item.mapping_summary) html += '<div class="auto-detect-note">当前映射：' + esc(item.mapping_summary) + '</div>';
  html += '<div class="auto-detect-note">映射方式：' + esc(mappingMethodLabel(item.mapping_method)) + '</div>';
  if (item.mapping_confidence) html += '<div class="auto-detect-note">置信度：' + Math.round(Number(item.mapping_confidence) * 100) + '%</div>';
  if (evidence.length) html += '<div class="auto-detect-note" style="margin-top:8px">已保存 ' + evidence.length + ' 组序列、位置或结构证据。</div>';
  if (failures.length) html += '<div class="auto-detect-note" style="margin-top:8px">未使用的证据：<br>' + autoFailureReasons(failures) + '</div>';
  if (!item.mapping_summary && !evidence.length && !failures.length) html += '<div class="auto-detect-note">当前没有可显示的自动识别依据。</div>';
  panel.innerHTML = html;
  panel.style.display = 'block';
  panel.scrollIntoView({behavior:'smooth', block:'center'});
}

function openRemoveDocumentModal(sourceId) {
  if (sourceId && typeof sourceId === 'string') calSelectedSourceId = sourceId;
  var item = calPdfList.find(function(value) { return value.source_file_id === calSelectedSourceId; });
  if (!item) return;
  calOpenMenuId = null;
  removeDocumentTarget = item;
  removeSecondStage = false;
  document.getElementById('remove-modal-title').textContent = '从文献库移除《' + item.title + '》？';
  document.getElementById('remove-modal-copy').textContent = '移除后，该文献将从文献库和搜索结果中消失。默认清理索引、页码映射和元数据，但保留 PDF 文件。';
  document.getElementById('remove-generated').checked = true;
  document.getElementById('remove-internal-copy').checked = false;
  document.getElementById('remove-internal-option').style.display = item.can_delete_internal_copy ? 'flex' : 'none';
  document.getElementById('remove-modal-warning').classList.remove('show');
  document.getElementById('confirm-remove-btn').textContent = '从文献库移除';
  document.getElementById('confirm-remove-btn').disabled = false;
  document.getElementById('remove-document-modal').classList.add('open');
  renderCalibrationCards();
}

function closeRemoveDocumentModal() {
  document.getElementById('remove-document-modal').classList.remove('open');
  removeDocumentTarget = null;
  removeSecondStage = false;
}

function removeModalBackdropClick(event) {
  if (event.target.id === 'remove-document-modal') closeRemoveDocumentModal();
}

async function confirmRemoveDocument() {
  if (!removeDocumentTarget) return;
  var deleteInternal = document.getElementById('remove-internal-copy').checked && removeDocumentTarget.can_delete_internal_copy;
  if (deleteInternal && !removeSecondStage) {
    removeSecondStage = true;
    document.getElementById('remove-modal-warning').classList.add('show');
    document.getElementById('confirm-remove-btn').textContent = '确认移除并删除副本';
    return;
  }
  var button = document.getElementById('confirm-remove-btn');
  button.disabled = true;
  button.textContent = '正在移除…';
  var sourceId = removeDocumentTarget.source_file_id;
  try {
    var resp = await fetch('/api/documents/remove', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:sourceId,delete_generated_artifacts:document.getElementById('remove-generated').checked,delete_internal_copy:deleteInternal})});
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '移除失败');
    calPdfList = calPdfList.filter(function(item) { return item.source_file_id !== sourceId; });
    delete calTransientStatus[sourceId];
    closeRemoveDocumentModal();
    closeCalibrationDetail();
    renderCalibrationStats();
    renderCalibrationCards();
    libLoaded = false;
    searchDocumentsLoaded = false;
    if (searchDocumentId === sourceId) searchDocumentId = '';
    await ensureSearchDocuments(true);
    updateSearchDocumentLabel();
    calPdfsLoaded = true;
    await loadMeta();
    window.dispatchEvent(new CustomEvent('library_changed', {detail:{source_id:sourceId}}));
    var query = document.getElementById('query').value.trim();
    if (query && searchResults.some(function(item) { return item.source_file_id === sourceId; })) await runSearch();
    showToast(deleteInternal ? '文献及应用内 PDF 副本已移除' : '文献已移除，PDF 文件已保留');
  } catch(e) {
    button.disabled = false;
    button.textContent = deleteInternal ? '确认移除并删除副本' : '从文献库移除';
    showToast('移除失败：' + e.message);
  }
}

/* ═══ Appearance settings ═══ */
const THEME_OPTIONS = [
  {id:'frost-blue', name:'清霜蓝', tone:'浅色', description:'清爽理性，适合日间使用。'},
  {id:'sage-ivory', name:'鼠尾草', tone:'浅色', description:'低刺激、安静，适合长时间阅读。'},
  {id:'warm-sand', name:'暖砂金', tone:'浅色', description:'温暖柔和，带轻微纸张气质。'},
  {id:'rose-mist', name:'蔷薇雾', tone:'浅色', description:'清柔克制，带淡粉强调。'},
  {id:'lavender-purple', name:'暮云紫', tone:'浅色', description:'优雅现代，使用柔和薰衣草紫。'},
  {id:'midnight', name:'深海夜', tone:'深色', description:'低亮度深色主题，适合夜间使用。'}
];
const THEME_IDS = new Set(THEME_OPTIONS.map(function(theme) { return theme.id; }));

function themePreviewMarkup(themeId) {
  return '<span class="theme-preview" data-preview-theme="' + themeId + '" aria-hidden="true">'
    + '<span class="theme-mini-sidebar">'
    + '<span class="theme-mini-brand"><span class="theme-mini-brand-mark"></span><span class="theme-mini-brand-line"></span></span>'
    + '<span class="theme-mini-nav">'
    + '<span class="theme-mini-nav-item"><span class="theme-mini-nav-icon"></span><span class="theme-mini-nav-line"></span></span>'
    + '<span class="theme-mini-nav-item is-selected"><span class="theme-mini-nav-icon"></span><span class="theme-mini-nav-line"></span></span>'
    + '<span class="theme-mini-nav-item"><span class="theme-mini-nav-icon"></span><span class="theme-mini-nav-line"></span></span>'
    + '</span></span>'
    + '<span class="theme-mini-main">'
    + '<span class="theme-mini-header"><span class="theme-mini-heading"><i class="theme-mini-title-line"></i><i class="theme-mini-subtitle-line"></i></span><span class="theme-mini-header-status"><i></i><b></b></span></span>'
    + '<span class="theme-mini-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><span class="theme-mini-search-line"></span><span class="theme-mini-search-action"></span></span>'
    + '<span class="theme-mini-cards">'
    + '<span class="theme-mini-doc-card"><span class="theme-mini-card-top"><i class="theme-mini-source"></i><i class="theme-mini-state is-success"></i></span><i class="theme-mini-doc-title"></i><i class="theme-mini-doc-title is-short"></i><i class="theme-mini-doc-meta"></i></span>'
    + '<span class="theme-mini-doc-card"><span class="theme-mini-card-top"><i class="theme-mini-source"></i><i class="theme-mini-state is-danger"></i></span><i class="theme-mini-doc-title"></i><i class="theme-mini-doc-title is-short"></i><i class="theme-mini-match"></i></span>'
    + '<span class="theme-mini-doc-card"><span class="theme-mini-card-top"><i class="theme-mini-source"></i><i class="theme-mini-state is-success"></i></span><i class="theme-mini-doc-title"></i><i class="theme-mini-doc-title is-short"></i><i class="theme-mini-doc-meta"></i></span>'
    + '</span></span></span>';
}

function themeOptionMarkup(theme) {
  return '<button class="theme-option" type="button" data-theme-choice="' + theme.id + '" role="radio" aria-checked="false" onclick="setTheme(\'' + theme.id + '\')">'
    + '<span class="theme-option-head"><span class="theme-option-identity"><span class="theme-option-name">' + theme.name + '</span><span class="theme-option-tone">' + theme.tone + '</span></span>'
    + '<span class="theme-option-check" aria-hidden="true"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 4 4L19 6"/></svg></span></span>'
    + themePreviewMarkup(theme.id)
    + '<span class="theme-option-description">' + theme.description + '</span></button>';
}

function renderThemeOptions() {
  var container = document.getElementById('theme-options');
  if (!container) return;
  container.innerHTML = THEME_OPTIONS.map(themeOptionMarkup).join('');
  renderThemeSelection();
}

function renderThemeSelection() {
  document.querySelectorAll('.theme-option').forEach(function(option) {
    var selected = option.dataset.themeChoice === currentTheme;
    option.classList.toggle('selected', selected);
    option.setAttribute('aria-checked', selected ? 'true' : 'false');
  });
}

function applyTheme(theme) {
  if (!THEME_IDS.has(theme)) return;
  currentTheme = theme;
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('meFinderTheme', theme); } catch (_) {}
  renderThemeSelection();
}

async function loadPreferences() {
  renderThemeSelection();
  try {
    var resp = await fetch('/api/preferences');
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
    applyTheme(data.theme || 'frost-blue');
    if (data.library_view === 'list' || data.library_view === 'grid') libViewMode = data.library_view;
    if (data.calibration_view === 'list' || data.calibration_view === 'grid') calViewMode = data.calibration_view;
    syncLibraryViewButtons();
    syncCalibrationViewButtons();
    if (libLoaded) renderLibraryList();
    if (calPdfsLoaded) renderCalibrationCards();
    preferencesLoaded = true;
  } catch (e) {
    showToast('读取外观设置失败：' + e.message);
  }
}

function persistDisplayPreference(key, value) {
  var payload = {};
  payload[key] = value;
  fetch('/api/preferences', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  }).catch(function() {});
}

async function setTheme(theme) {
  if (!THEME_IDS.has(theme)) return;
  var previousTheme = currentTheme;
  applyTheme(theme);
  try {
    var resp = await fetch('/api/preferences', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({theme: theme})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    preferencesLoaded = true;
    applyTheme(data.theme);
    var selected = THEME_OPTIONS.find(function(option) { return option.id === theme; });
    showToast('已切换到' + (selected ? selected.name : '所选主题'));
  } catch (e) {
    applyTheme(previousTheme);
    showToast('主题保存失败：' + e.message);
  }
}

renderThemeOptions();

/* ═══ MinerU API settings ═══ */
async function loadMineruConfig() {
  var status = document.getElementById('mineru-config-status');
  var expiryDetail = document.getElementById('mineru-expiry-detail');
  if (!status) return;
  status.className = 'settings-status';
  status.textContent = '读取中…';
  if (expiryDetail) {
    expiryDetail.className = 'settings-expiry-detail';
    expiryDetail.textContent = 'API 到期时间：读取中…';
  }
  try {
    var resp = await fetch('/api/mineru-config');
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
    document.getElementById('mineru-api-base').value = data.api_base || 'https://mineru.net';
    document.getElementById('mineru-expires-at').value = data.expires_at || '';
    document.getElementById('mineru-token').value = '';
    document.getElementById('mineru-access-key-id').value = '';
    document.getElementById('mineru-secret-access-key').value = '';
    if (data.configured) {
      status.className = 'settings-status ready';
      var parts = ['已配置'];
      if (data.expires_at) parts.push('到期 ' + data.expires_at);
      status.textContent = parts.join(' · ');
    } else {
      status.className = 'settings-status warning';
      status.textContent = '尚未配置 Token';
    }
    updateMineruExpiryDetail(data);
    mineruConfigLoaded = true;
  } catch (e) {
    status.className = 'settings-status warning';
    status.textContent = '读取失败';
    if (expiryDetail) {
      expiryDetail.className = 'settings-expiry-detail warning';
      expiryDetail.textContent = 'API 到期时间：读取失败';
    }
    showToast('读取 MinerU 配置失败：' + e.message);
  }
}

function updateMineruExpiryDetail(data) {
  var el = document.getElementById('mineru-expiry-detail');
  if (!el) return;
  var configured = !!(data && data.configured);
  var status = data ? data.expiry_status : 'unset';
  var label = data && data.expiry_label ? data.expiry_label : '';
  el.className = 'settings-expiry-detail';
  if (!configured) {
    el.classList.add('warning');
    el.textContent = 'API 到期时间：尚未配置 Token';
    return;
  }
  if (status === 'expired' || status === 'invalid') {
    el.classList.add('error');
  } else if (status === 'expires_today' || status === 'unset') {
    el.classList.add('warning');
  } else {
    el.classList.add('ready');
  }
  el.textContent = 'API 到期时间：' + (label || '未设置到期时间');
}

function toggleMineruSecret(inputId, buttonId) {
  var input = document.getElementById(inputId);
  var button = document.getElementById(buttonId);
  if (!input || !button) return;
  var visible = input.type === 'text';
  input.type = visible ? 'password' : 'text';
  button.textContent = visible ? '显示' : '隐藏';
}

async function saveMineruConfig() {
  var hint = document.getElementById('mineru-save-hint');
  var payload = {
    token: document.getElementById('mineru-token').value.trim(),
    access_key_id: document.getElementById('mineru-access-key-id').value.trim(),
    secret_access_key: document.getElementById('mineru-secret-access-key').value.trim(),
    api_base: document.getElementById('mineru-api-base').value.trim(),
    expires_at: document.getElementById('mineru-expires-at').value
  };
  hint.textContent = '正在保存…';
  try {
    var resp = await fetch('/api/mineru-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    hint.textContent = '已保存到本机';
    showToast('MinerU API 配置已保存');
    mineruConfigLoaded = false;
    await loadMineruConfig();
  } catch (e) {
    hint.textContent = '保存失败';
    showToast('保存 MinerU 配置失败：' + e.message);
  }
}

/* ═══ Import ═══ */
let importQueue = [];

function initDropZone() {
  var zone = document.getElementById('drop-zone');
  if (!zone) return;
  zone.addEventListener('dragover', function(e) {
    e.preventDefault();
    zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', function() {
    zone.classList.remove('dragover');
  });
  zone.addEventListener('drop', function(e) {
    e.preventDefault();
    zone.classList.remove('dragover');
    handleFileSelect(e.dataTransfer.files);
  });
}

function handleFileSelect(files) {
  if (!files || files.length === 0) return;
  var validExts = ['.pdf', '.docx'];
  for (var i = 0; i < files.length; i++) {
    var file = files[i];
    var ext = file.name.toLowerCase().slice(file.name.lastIndexOf('.'));
    if (validExts.indexOf(ext) === -1) {
      showToast('不支持的格式: ' + file.name);
      continue;
    }
    var id = 'import-' + Date.now() + '-' + i;
    importQueue.push({
      id: id,
      file: file,
      name: file.name,
      size: file.size,
      type: ext === '.pdf' ? 'pdf' : 'docx',
      status: 'queued',
      step: 0,
      message: '等待处理'
    });
  }
  document.getElementById('file-input').value = '';
  renderImportQueue();
  importQueue.filter(function(q) { return q.status === 'queued'; }).forEach(function(q) {
    uploadImport(q.id);
  });
}

function importStepsFor(q) {
  if (q.type !== 'pdf') return ['读取文件', '文本入库', '建立索引'];
  if (q.route === 'mineru') return ['读取文件', '类型检测', 'MinerU 解析', '文本入库', '建立索引'];
  return ['读取文件', '类型检测', '本地解析', '建立索引'];
}

function importRouteBadge(q) {
  if (q.type !== 'pdf' || !q.detectedType) return '';
  var mineru = q.route === 'mineru';
  return '<span class="import-route-badge ' + (mineru ? 'mineru' : 'native') + '">'
    + esc(pdfTypeLabel(q.detectedType)) + (mineru ? ' · 提交 MinerU' : ' · 本地解析')
    + '</span>';
}

function renderImportQueue() {
  var queueEl = document.getElementById('import-queue');
  var itemsEl = document.getElementById('import-items');
  if (importQueue.length === 0) {
    queueEl.style.display = 'none';
    return;
  }
  queueEl.style.display = 'block';
  itemsEl.innerHTML = importQueue.map(function(q) {
    var typeCls = q.type === 'pdf' ? 'pdf' : 'word';
    var steps = importStepsFor(q);
    var stepsHTML = steps.map(function(label, i) {
      var cls = '';
      if (q.status === 'error' && i === q.step) cls = 'error';
      else if (i < q.step) cls = 'done';
      else if (i === q.step && q.status === 'processing') cls = 'active';
      return '<div class="import-step ' + cls + '">'
        + '<div class="import-step-bar ' + cls + '"></div>'
        + '<span class="import-step-label">' + label + '</span>'
        + '</div>';
    }).join('');
    var statusCls = q.status === 'error' ? ' error' : q.status === 'done' ? ' done' : '';
    return '<div class="import-item" data-id="' + q.id + '">'
      + '<div class="import-item-header">'
      + '<span class="type-badge ' + typeCls + '">' + (q.type === 'pdf' ? 'PDF' : 'DOCX') + '</span>'
      + '<span class="import-item-name">' + esc(q.name) + '</span>'
      + importRouteBadge(q)
      + '<span class="import-item-size">' + formatFileSize(q.size) + '</span>'
      + '<button class="import-item-remove" onclick="removeImport(\'' + q.id + '\')" title="移除">&times;</button>'
      + '</div>'
      + '<div class="import-steps">' + stepsHTML + '</div>'
      + '<div class="import-item-status' + statusCls + '">' + esc(q.message) + '</div>'
      + '</div>';
  }).join('');
}

function removeImport(id) {
  importQueue = importQueue.filter(function(q) { return q.id !== id; });
  renderImportQueue();
}

async function uploadImport(id) {
  var q = importQueue.find(function(q) { return q.id === id; });
  if (!q) return;
  q.status = 'processing';
  q.step = 0;
  q.message = '正在读取文件…';
  renderImportQueue();
  try {
    var resp = await fetch('/api/import', {
      method: 'POST',
      headers: {
        'Content-Type': q.file.type || (q.type === 'pdf' ? 'application/pdf' : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        'X-File-Name': encodeURIComponent(q.name)
      },
      body: q.file
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '导入失败');
    q.jobId = data.job_id;
    if (q.type === 'pdf' && data.detected_pdf_type) {
      q.detectedType = data.detected_pdf_type;
      q.route = data.detected_pdf_type === 'native_text' ? 'native' : 'mineru';
      q.step = 2;
      q.message = '检测结果：' + pdfTypeLabel(data.detected_pdf_type)
        + (q.route === 'mineru' ? '，将提交 MinerU 解析' : '，本地解析，无需 MinerU');
    } else {
      q.step = 1;
      q.message = '文件已保存，正在建立索引…';
    }
    renderImportQueue();
    if (q.jobId) pollImportJob(q.id);
  } catch (e) {
    q.status = 'error';
    q.message = e.message || '导入失败';
    renderImportQueue();
  }
}

function pollImportJob(id) {
  var q = importQueue.find(function(item) { return item.id === id; });
  if (!q || !q.jobId) return;
  fetch('/api/import-status?job_id=' + encodeURIComponent(q.jobId))
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      if (data.error) throw new Error(data.error);
      if (data.phase === 'mineru_submitting' || data.phase === 'mineru_processing') q.route = 'mineru';
      else if (data.phase === 'text_parsing' && q.type === 'pdf') q.route = 'native';
      var steps = importStepsFor(q);
      if (data.phase === 'mineru_submitting' || data.phase === 'mineru_processing') q.step = steps.indexOf('MinerU 解析');
      else if (data.phase === 'text_parsing') q.step = q.type === 'pdf' ? steps.indexOf('本地解析') : steps.indexOf('文本入库');
      else if (data.phase === 'rebuilding_index' || data.phase === 'metadata_recognition') q.step = steps.indexOf('建立索引');
      else if (data.status === 'completed') q.step = steps.length;
      q.message = data.message || q.message;
      if (data.status === 'completed') {
        q.status = 'done';
        q.message = data.message || '导入完成，已自动更新索引';
        libLoaded = false;
        calPdfsLoaded = false;
        searchDocumentsLoaded = false;
        ensureSearchDocuments(true).then(updateSearchDocumentLabel);
      } else if (data.status === 'failed') {
        q.status = 'error';
        q.message = data.message || '导入失败';
      }
      renderImportQueue();
      if (q.status === 'processing') setTimeout(function() { pollImportJob(id); }, 2500);
    })
    .catch(function(err) {
      q.status = 'error';
      q.message = err.message || '读取导入状态失败';
      renderImportQueue();
    });
}

/* ═══ Load index metadata ═══ */
async function loadMeta() {
  try {
    const resp = await fetch('/api/index-meta');
    const meta = await resp.json();
    document.getElementById('index-count').textContent =
      '索引 ' + (meta.eligible_paragraph_count || 0).toLocaleString() + ' 条';
  } catch (e) {
    document.getElementById('index-count').textContent = '索引状态未知';
  }
}

/* ═══ Init ═══ */
document.addEventListener('click', function(event) {
  if (!event.target.closest('.app-select')) closeAppSelects();
  if (calOpenMenuId && !event.target.closest('.cal-more-menu') && !event.target.closest('.cal-more-btn')) {
    calOpenMenuId = null;
    renderCalibrationCards();
  }
});
loadMeta();
loadPreferences();
syncLibraryViewButtons();
syncCalibrationViewButtons();
renderSearchDocumentOptions();
ensureSearchDocuments().then(function() { renderSearchDocumentOptions(); updateSearchDocumentLabel(); });
initDropZone();
const requestedInitialPage = new URLSearchParams(window.location.search).get('page');
if (['search','library','import','calibration','settings'].indexOf(requestedInitialPage) >= 0) navigateTo(requestedInitialPage);
if (currentPage === 'search') document.getElementById('query').focus();
</script>
</body>
</html>
"""


def render_html(theme: str) -> str:
    """Inject the persisted theme before the browser paints the first frame."""

    marker = '<html lang="zh-CN" data-theme="frost-blue">'
    return HTML.replace(marker, f'<html lang="zh-CN" data-theme="{theme}">', 1)


def make_handler(index_path: Path):
    engine = SearchEngine(index_path)
    root = Path(".").resolve()
    runtime = {
        "engine": engine,
        "source_files": {
            str(item.get("source_file_id")): item
            for item in engine.index.get("source_files", [])
            if item.get("source_file_id")
        },
        "index_metadata": engine.index.get("metadata", {}),
        "rebuilding": False,
    }
    runtime_lock = threading.RLock()
    rebuild_lock = threading.Lock()
    metadata_lock = threading.Lock()
    import_jobs: Dict[str, Dict[str, object]] = {}
    import_jobs_lock = threading.RLock()
    calibration_active_sources: set[str] = set()

    def update_import_job(job_id: str, **updates: object) -> None:
        with import_jobs_lock:
            job = import_jobs.get(job_id)
            if job is not None:
                job.update(updates)

    def progress_import_job(job_id: str, update: Dict[str, object]) -> None:
        phase = str(update.get("phase") or "")
        message = "正在处理…"
        if phase == "mineru_processing":
            message = f"MinerU 解析中：{update.get('completed', 0)}/{update.get('total', 0)} 个分段"
        elif phase == "rebuilding_index":
            message = "正在重建本地 SQLite 索引…"
        update_import_job(job_id, phase=phase, message=message, progress=update)

    def reload_runtime_index() -> None:
        with runtime_lock:
            new_engine = SearchEngine(index_path)
            old_engine = runtime["engine"]
            runtime["engine"] = new_engine
            runtime["source_files"] = {
                str(item.get("source_file_id")): item
                for item in new_engine.index.get("source_files", [])
                if item.get("source_file_id")
            }
            runtime["index_metadata"] = new_engine.index.get("metadata", {})
            if hasattr(old_engine, "close"):
                old_engine.close()

    def latest_pdf_import_runs() -> Dict[str, Dict[str, object]]:
        connection = sqlite3.connect(str(index_path))
        try:
            rows = connection.execute("SELECT source_file_id, payload_json FROM pdf_import_runs ORDER BY row_id").fetchall()
        finally:
            connection.close()
        result: Dict[str, Dict[str, object]] = {}
        for source_id, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            result[str(source_id)] = payload
        return result

    def calibration_library_data() -> Dict[str, object]:
        config_path = root / "config" / "pdf_imports.json"
        config = json.loads(config_path.read_text("utf-8")) if config_path.exists() else {"documents": []}
        with runtime_lock:
            current_engine = runtime["engine"]
            sources = list(current_engine.index.get("source_files", []))
            volumes = list(current_engine.index.get("volumes", []))
            active = set(calibration_active_sources)
        with import_jobs_lock:
            for job in import_jobs.values():
                if job.get("status") == "processing" and job.get("source_file_id"):
                    active.add(str(job["source_file_id"]))
        return build_calibration_library(
            root,
            sources,
            volumes,
            config.get("documents", []),
            latest_runs=latest_pdf_import_runs(),
            active_source_ids=active,
        )

    def rebuild_runtime_index(job_id: str) -> None:
        with rebuild_lock:
            update_import_job(job_id, phase="rebuilding_index", message="正在重建本地 SQLite 索引…")
            with runtime_lock:
                runtime["rebuilding"] = True
                old_engine = runtime["engine"]
                if hasattr(old_engine, "close"):
                    old_engine.close()
            try:
                rebuild_local_index(root, lambda update: progress_import_job(job_id, update))
                with runtime_lock:
                    runtime["engine"] = SearchEngine(index_path)
                    runtime["source_files"] = {
                        str(item.get("source_file_id")): item
                        for item in runtime["engine"].index.get("source_files", [])
                        if item.get("source_file_id")
                    }
                    runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                    runtime["rebuilding"] = False
            except Exception:
                with runtime_lock:
                    runtime["engine"] = SearchEngine(index_path)
                    runtime["source_files"] = {
                        str(item.get("source_file_id")): item
                        for item in runtime["engine"].index.get("source_files", [])
                        if item.get("source_file_id")
                    }
                    runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                    runtime["rebuilding"] = False
                raise

    def run_import_job(job_id: str, target: Path, source_file_id: str, profile: Dict[str, object], is_pdf: bool) -> None:
        try:
            if is_pdf and str(profile.get("detected_pdf_type")) != "native_text":
                update_import_job(job_id, phase="mineru_submitting", message="文本层不可靠，正在自动提交 MinerU…")
                parse_pdf_with_mineru(
                    root,
                    target,
                    source_file_id,
                    on_progress=lambda update: progress_import_job(job_id, update),
                )
            else:
                update_import_job(job_id, phase="text_parsing", message="原生文本，跳过 MinerU，正在建立索引…")
            rebuild_runtime_index(job_id)
            metadata_note = ""
            if is_pdf:
                update_import_job(job_id, phase="metadata_recognition", message="索引已建立，正在自动识别书目信息…")
                try:
                    metadata = detect_bibliographic_metadata(source_file_id)
                    metadata = persist_bibliographic_metadata(source_file_id, metadata)
                    missing = list(metadata.get("metadata_missing_fields") or metadata_missing_fields(metadata))
                    labels = {
                        "author": "作者",
                        "title": "书名",
                        "translator": "译者",
                        "publisher": "出版社",
                        "publish_place": "出版地",
                        "publish_year": "出版年份",
                    }
                    missing_labels = [labels[field] for field in missing if field in labels]
                    metadata_note = "；书目信息已自动填入"
                    if missing_labels:
                        metadata_note += "，缺少" + "、".join(missing_labels)
                    update_import_job(job_id, bibliographic_metadata=metadata, bibliographic_missing_fields=missing)
                except Exception as metadata_exc:
                    metadata_note = "；书目信息自动识别未完成，可在文献库中重试"
                    update_import_job(job_id, bibliographic_error=str(metadata_exc))
            update_import_job(job_id, status="completed", phase="completed", message="导入完成，已自动更新索引" + metadata_note)
        except Exception as exc:
            update_import_job(job_id, status="failed", phase="failed", message=str(exc))

    def start_import_job(target: Path, profile: Dict[str, object], source_file_id: str, is_pdf: bool) -> str:
        job_id = f"import-{uuid.uuid4().hex[:12]}"
        with import_jobs_lock:
            import_jobs[job_id] = {
                "job_id": job_id,
                "status": "processing",
                "phase": "stored",
                "message": "文件已保存，准备处理…",
                "file_name": target.name,
                "source_file_id": source_file_id,
                "detected_pdf_type": profile.get("detected_pdf_type") if is_pdf else None,
            }
        threading.Thread(
            target=run_import_job,
            args=(job_id, target, source_file_id, profile, is_pdf),
            daemon=True,
        ).start()
        return job_id

    def store_upload(filename: str, length: int, is_pdf: bool, reader) -> Path:
        if length <= 0 or length > 600 * 1024 * 1024:
            raise MinerUError("文件为空或超过 600 MB 限制。")
        safe_name = Path(filename).name
        if not safe_name or safe_name in {".", ".."}:
            raise MinerUError("无法识别文件名。")
        suffix = Path(safe_name).suffix.lower()
        expected = ".pdf" if is_pdf else ".docx"
        if suffix != expected:
            raise MinerUError(f"导入文件必须是 {expected}。")
        directory = root / "corpus" / ("raw_pdf" if is_pdf else "raw_docx")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / safe_name
        if target.exists():
            target = directory / f"{Path(safe_name).stem} (imported-{uuid.uuid4().hex[:8]}){suffix}"
        temp_path = directory / f".{target.name}.{uuid.uuid4().hex}.uploading"
        remaining = length
        with temp_path.open("wb") as stream:
            while remaining > 0:
                chunk = reader.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise MinerUError("上传数据不完整。")
                stream.write(chunk)
                remaining -= len(chunk)
        temp_path.replace(target)
        return target

    def accept_auto_page_mapping(source_id: str) -> int:
        config_path = root / "config" / "pdf_imports.json"
        if not config_path.exists():
            raise MinerUError("PDF 导入配置不存在。")
        with runtime_lock:
            source = runtime["source_files"].get(source_id)
        if not source:
            raise MinerUError("文献未找到。")
        auto_mapping = ((source.get("pdf_profile") or {}).get("auto_page_mapping") or {})
        applied = [segment for segment in auto_mapping.get("applied_segments", []) if isinstance(segment, dict)]
        if not applied:
            raise MinerUError("没有可接受的高置信度自动映射段。")
        config = json.loads(config_path.read_text("utf-8"))
        document = next((doc for doc in config.get("documents", []) if doc.get("source_file_id") == source_id), None)
        if not document:
            raise MinerUError("PDF 配置中找不到该文献。")
        manual_segments = []
        for segment in applied:
            clean = {
                "pdf_page_start": int(segment["pdf_page_start"]),
                "pdf_page_end": int(segment["pdf_page_end"]),
                "citation_page_start": str(segment["citation_page_start"]),
                "number_style": str(segment.get("number_style") or "arabic"),
                "method": "manual_segment",
                "confidence": float(segment.get("mapping_confidence") or 0.95),
                "label": "已接受自动页码映射",
                "evidence": segment.get("mapping_evidence"),
            }
            manual_segments.append(clean)
        document.setdefault("page_mapping", {})
        document["page_mapping"]["segments"] = manual_segments
        document["page_mapping"]["validated_by"] = "auto_mapping_accepted"
        document["page_mapping"]["mapping_status"] = "manual_mapped"
        document["page_mapping"]["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_import_config(config_path, config)
        return len(manual_segments)

    def source_path_from_id(source_id: str) -> Path:
        with runtime_lock:
            record = runtime["source_files"].get(source_id)
        if not record:
            raise MinerUError("文献未找到。")
        relative_path = str(record.get("relative_path") or "")
        target = (root / relative_path).resolve()
        if target != root and root not in target.parents:
            raise MinerUError("拒绝打开应用目录外的文件。")
        if target.suffix.lower() not in {".pdf", ".doc", ".docx"} or not target.exists():
            raise MinerUError("原始文件不存在。")
        return target

    def detect_auto_page_mapping(source_id: str) -> Dict[str, object]:
        config_path = root / "config" / "pdf_imports.json"
        if not config_path.exists():
            raise MinerUError("PDF 导入配置不存在。")
        config = json.loads(config_path.read_text("utf-8"))
        document = next((doc for doc in config.get("documents", []) if doc.get("source_file_id") == source_id), None)
        if not document:
            raise MinerUError("PDF 配置中找不到该文献。")
        try:
            path = source_path_from_id(source_id)
        except MinerUError as exc:
            if "不存在" in str(exc):
                return {
                    "source_id": source_id,
                    "mapping_status": "source_missing",
                    "failure_reasons": ["source_missing"],
                    "selected_segments": [],
                    "applied_segments": [],
                    "manual_mapping_present": has_manual_mapping(document),
                    "dry_run": True,
                }
            raise
        if path.suffix.lower() != ".pdf":
            raise MinerUError("自动页码检测只支持 PDF。")
        manual_present = has_manual_mapping(document)
        detection_config = copy.deepcopy(document)
        detection_config.setdefault("page_mapping", {})
        detection_config["page_mapping"]["segments"] = []
        detection_config["page_mapping"]["validated_by"] = None
        extracted = extract_pdf_source(path, root, detection_config, parsed_dir=None)
        sources = extracted.get("source_files", [])
        if not sources:
            raise MinerUError("无法读取文献页码证据。")
        profile = sources[0].get("pdf_profile") or {}
        result = dict(profile.get("auto_page_mapping") or {})
        result["manual_mapping_present"] = manual_present
        result["dry_run"] = True
        result["source_id"] = source_id
        result["source_file"] = path.name
        result["current_mapping"] = document.get("page_mapping") or {}
        return result

    def apply_live_auto_mapping(
        source_id: str,
        segments: List[Dict[str, object]],
        auto_mapping: Dict[str, object],
        replace_manual: bool,
    ) -> Dict[str, int]:
        config_path = root / "config" / "pdf_imports.json"
        config = json.loads(config_path.read_text("utf-8"))
        document = next((doc for doc in config.get("documents", []) if doc.get("source_file_id") == source_id), None)
        if not document:
            raise MinerUError("PDF 配置中找不到该文献。")
        manual_present = has_manual_mapping(document)
        if manual_present and not replace_manual:
            raise MinerUError("当前文献已有人工页码映射，必须明确确认后才能替换。")
        cleaned = normalize_auto_segments(segments)
        if not cleaned:
            raise MinerUError("没有可应用的自动页码区间。")
        original_config = copy.deepcopy(config)
        document.setdefault("page_mapping", {})
        document["page_mapping"]["segments"] = cleaned
        document["page_mapping"]["validated_by"] = "auto_mapping_ui"
        document["page_mapping"]["mapping_origin"] = "auto"
        document["page_mapping"]["updated_at"] = datetime.now(timezone.utc).isoformat()
        confidence_levels = {str(item.get("confidence_level") or "") for item in cleaned}
        mapping_status = "auto_mapped_high" if confidence_levels == {"high"} else "auto_mapped_medium"
        document["page_mapping"]["mapping_status"] = mapping_status
        save_import_config(config_path, config)
        with runtime_lock:
            runtime["rebuilding"] = True
            old_engine = runtime["engine"]
            if hasattr(old_engine, "close"):
                old_engine.close()
        try:
            updated = apply_mapping_to_database(
                index_path,
                source_id,
                cleaned,
                auto_mapping=auto_mapping,
                mapping_status=mapping_status,
            )
            reload_runtime_index()
            with runtime_lock:
                runtime["rebuilding"] = False
            return updated
        except Exception:
            save_import_config(config_path, original_config)
            with runtime_lock:
                runtime["engine"] = SearchEngine(index_path)
                runtime["source_files"] = {
                    str(item.get("source_file_id")): item
                    for item in runtime["engine"].index.get("source_files", [])
                    if item.get("source_file_id")
                }
                runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                runtime["rebuilding"] = False
            raise

    def open_source_file(source_id: str, page: object = None) -> Dict[str, object]:
        target = source_path_from_id(source_id)
        suffix = target.suffix.lower()
        if suffix == ".pdf":
            try:
                page_number = int(page) if page not in (None, "") else None
            except (TypeError, ValueError):
                page_number = None
            if page_number is not None and page_number <= 0:
                page_number = None
            # Adobe is only required for the page-jump feature (its /A page=N
            # switch); every other case goes through the system default viewer
            # so machines without Adobe (WPS, Edge, ...) still work.
            if page_number:
                adobe = find_adobe_pdf_app()
                if adobe is not None:
                    args = [str(adobe), "/A", f"page={page_number}", str(target)]
                    subprocess.Popen(args, close_fds=True)
                    return {
                        "ok": True,
                        "app": str(adobe),
                        "page_jump": True,
                        "file": target.name,
                        "page": page_number,
                    }
            os.startfile(str(target))  # type: ignore[attr-defined]
            return {
                "ok": True,
                "app": "system_default",
                "page_jump": False,
                "file": target.name,
                "page": page_number,
            }
        os.startfile(str(target))  # type: ignore[attr-defined]
        return {"ok": True, "app": "system_default", "page_jump": False, "file": target.name}

    def configured_document(source_id: str) -> Tuple[Path, Dict[str, object], Dict[str, object]]:
        config_path = root / "config" / "pdf_imports.json"
        if not config_path.exists():
            raise MinerUError("PDF 导入配置不存在。")
        config = json.loads(config_path.read_text("utf-8"))
        document = next((doc for doc in config.get("documents", []) if doc.get("source_file_id") == source_id), None)
        if not document:
            raise MinerUError("PDF 配置中找不到该文献。")
        return config_path, config, document

    def front_matter_pages(source_id: str, limit: int = 20) -> List[Dict[str, object]]:
        connection = sqlite3.connect(str(index_path))
        try:
            rows = connection.execute(
                "SELECT payload_json FROM pdf_pages WHERE source_file_id = ? AND pdf_page_index < ? ORDER BY pdf_page_index",
                (source_id, limit),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            connection.close()

    def detect_bibliographic_metadata(source_id: str, force: bool = False) -> Dict[str, object]:
        _, _, document = configured_document(source_id)
        path = source_path_from_id(source_id)
        return detect_pdf_bibliographic_metadata(
            path,
            front_matter_pages(source_id),
            document,
            force=force,
        )

    def persist_bibliographic_metadata(source_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        with metadata_lock:
            config_path, config, document = configured_document(source_id)
            original_config = copy.deepcopy(config)
            metadata = canonical_metadata(payload)
            if not metadata.get("metadata_missing_fields"):
                metadata["metadata_missing_fields"] = metadata_missing_fields(metadata)
            for field in METADATA_FIELDS:
                document[field] = metadata.get(field)
            for field in (
                "document_type",
                "metadata_status",
                "metadata_source",
                "metadata_confidence",
                "metadata_evidence",
                "metadata_conflicts",
                "metadata_missing_fields",
            ):
                document[field] = metadata.get(field)
            document["publication_year"] = metadata.get("publish_year")
            document["bibliographic_metadata"] = metadata
            save_import_config(config_path, config)
            with runtime_lock:
                runtime["rebuilding"] = True
                old_engine = runtime["engine"]
                if hasattr(old_engine, "close"):
                    old_engine.close()
            try:
                update_metadata_in_database(index_path, source_id, metadata)
                reload_runtime_index()
                with runtime_lock:
                    runtime["rebuilding"] = False
                return metadata
            except Exception:
                save_import_config(config_path, original_config)
                with runtime_lock:
                    runtime["engine"] = SearchEngine(index_path)
                    runtime["source_files"] = {
                        str(item.get("source_file_id")): item
                        for item in runtime["engine"].index.get("source_files", [])
                        if item.get("source_file_id")
                    }
                    runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                    runtime["rebuilding"] = False
                raise

    def save_bibliographic_metadata(source_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        _, _, document = configured_document(source_id)
        metadata = manual_metadata(payload, document)
        return persist_bibliographic_metadata(source_id, metadata)

    class Handler(BaseHTTPRequestHandler):
        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            content_length: int | None = None,
            send_body: bool = True,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body) if content_length is None else content_length))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _send_json(self, data: object, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                preferences_path = resolve_preferences_path(root)
                theme = read_preferences(preferences_path)["theme"]
                body = render_html(theme).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/index-meta":
                with runtime_lock:
                    self._send_json(runtime["index_metadata"])
                return
            if parsed.path == "/api/mineru-config":
                config_path = resolve_mineru_config_path(root)
                try:
                    self._send_json(mineru_config_summary(config_path))
                except (MinerUError, OSError, json.JSONDecodeError):
                    self._send_json({"error": "本机 MinerU 配置文件无法读取。"}, status=500)
                return
            if parsed.path == "/api/preferences":
                preferences_path = resolve_preferences_path(root)
                self._send_json(read_preferences(preferences_path))
                return
            if parsed.path == "/api/sources":
                with runtime_lock:
                    current_engine = runtime["engine"]
                    self._send_json({
                        "source_files": current_engine.index.get("source_files", []),
                        "volumes": current_engine.index.get("volumes", []),
                        "works": current_engine.index.get("works", []),
                    })
                return
            if parsed.path == "/api/calibration-library":
                try:
                    self._send_json(calibration_library_data())
                except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                    self._send_json({"error": f"页码校准文献加载失败：{exc}"}, status=500)
                return
            if parsed.path == "/api/import-status":
                params = parse_qs(parsed.query)
                job_id = (params.get("job_id") or [None])[0]
                with import_jobs_lock:
                    job = dict(import_jobs.get(str(job_id), {})) if job_id else {}
                self._send_json(job or {"error": "导入任务不存在。"}, status=200 if job else 404)
                return
            if parsed.path == "/api/calibration":
                config_path = root / "config" / "pdf_imports.json"
                if not config_path.exists():
                    self._send_json({"documents": []})
                    return
                config = json.loads(config_path.read_text("utf-8"))
                params = parse_qs(parsed.query)
                sid = (params.get("source_id") or [None])[0]
                if sid:
                    doc = next((d for d in config.get("documents", []) if d.get("source_file_id") == sid), None)
                    self._send_json(doc or {"error": "not found"})
                else:
                    self._send_json(config)
                return
            if parsed.path == "/api/bibliographic-metadata":
                params = parse_qs(parsed.query)
                sid = (params.get("source_id") or [None])[0]
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    _, _, document = configured_document(str(sid))
                    self._send_json({"ok": True, "metadata": canonical_metadata(document)})
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=404)
                return
            if parsed.path.startswith("/source/"):
                self._send_source(parsed.path)
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/source/"):
                self._send_source(parsed.path, send_body=False)
                return
            if parsed.path in {"/", "/index.html"}:
                preferences_path = resolve_preferences_path(root)
                theme = read_preferences(preferences_path)["theme"]
                content_length = len(render_html(theme).encode("utf-8"))
                self._send(200, b"", "text/html; charset=utf-8", content_length=content_length, send_body=False)
                return
            self._send(404, b"", "text/plain; charset=utf-8", send_body=False)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/import":
                filename = unquote(self.headers.get("X-File-Name", ""))
                suffix = Path(filename).suffix.lower()
                if suffix not in {".pdf", ".docx"}:
                    self._send_json({"error": "只支持 PDF 或 DOCX 文件。"}, status=400)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    target = store_upload(filename, length, suffix == ".pdf", self.rfile)
                    is_pdf = suffix == ".pdf"
                    if is_pdf:
                        profile = detect_imported_pdf(target)
                        document = register_pdf(root, target)
                        source_file_id = str(document["source_file_id"])
                    else:
                        profile = {"detected_pdf_type": "docx"}
                        source_file_id = f"docx-import-{uuid.uuid4().hex[:16]}"
                    job_id = start_import_job(target, profile, source_file_id, is_pdf)
                    self._send_json({
                        "ok": True,
                        "job_id": job_id,
                        "file_name": target.name,
                        "source_file_id": source_file_id,
                        "detected_pdf_type": profile.get("detected_pdf_type") if is_pdf else None,
                    })
                except (MinerUError, OSError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception:
                    self._send_json({"error": "导入失败，请查看 desktop.log。"}, status=500)
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json({"error": "请求格式无效。"}, status=400)
                return
            if parsed.path == "/api/search":
                requested_limit = payload.get("limit", 10)
                search_limit: int | str
                if str(requested_limit).strip().lower() in {"all", "0"}:
                    search_limit = "all"
                else:
                    try:
                        search_limit = int(requested_limit)
                    except (TypeError, ValueError):
                        search_limit = 10
                with runtime_lock:
                    if runtime["rebuilding"]:
                        self._send_json({"error": "索引正在重建，请稍候再搜索。"}, status=503)
                        return
                    result = runtime["engine"].search(
                        payload.get("query", ""),
                        payload.get("mode", "auto"),
                        search_limit,
                        payload.get("source_type", "all"),
                        payload.get("source_file_id"),
                    )
                self._send_json(result)
                return
            if parsed.path == "/api/preferences":
                preferences_path = resolve_preferences_path(root)
                try:
                    preferences = save_preferences(payload, preferences_path)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except OSError:
                    self._send_json({"error": "外观设置无法保存，请检查配置目录是否可写。"}, status=500)
                    return
                self._send_json({"ok": True, **preferences})
                return
            if parsed.path == "/api/open-source":
                sid = str(payload.get("source_id") or "")
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    result = open_source_file(sid, payload.get("page"))
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"打开原文失败：{exc}"}, status=500)
                    return
                self._send_json(result)
                return
            if parsed.path == "/api/mineru-config":
                config_path = resolve_mineru_config_path(root)
                try:
                    summary = save_mineru_config(payload, config_path)
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except (OSError, json.JSONDecodeError):
                    self._send_json({"error": "本机配置文件无法保存，请检查应用目录是否可写。"}, status=500)
                    return
                self._send_json({"ok": True, **summary})
                return
            if parsed.path == "/api/calibration":
                sid = payload.get("source_id")
                segments = payload.get("segments", [])
                config_path = root / "config" / "pdf_imports.json"
                if not config_path.exists() or not sid:
                    self._send_json({"error": "invalid request"})
                    return
                config = json.loads(config_path.read_text("utf-8"))
                doc = next((d for d in config.get("documents", []) if d.get("source_file_id") == sid), None)
                if not doc:
                    self._send_json({"error": "document not found"})
                    return
                if "page_mapping" not in doc:
                    doc["page_mapping"] = {}
                doc["page_mapping"]["segments"] = segments
                doc["page_mapping"]["validated_by"] = "manual_ui"
                doc["page_mapping"]["mapping_origin"] = "manual"
                doc["page_mapping"]["mapping_status"] = "manual_mapped"
                doc["page_mapping"]["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_import_config(config_path, config)
                job_id = f"calibration-{uuid.uuid4().hex[:12]}"
                with import_jobs_lock:
                    import_jobs[job_id] = {
                        "job_id": job_id,
                        "status": "processing",
                        "phase": "rebuilding_index",
                        "message": "正在应用页码校准并重建索引…",
                    }
                try:
                    rebuild_runtime_index(job_id)
                    update_import_job(job_id, status="completed", phase="completed", message="页码校准已生效")
                except Exception as exc:
                    update_import_job(job_id, status="failed", phase="failed", message=str(exc))
                    self._send_json({"error": f"校准已保存，但索引重建失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True, "rebuilt": True})
                return
            if parsed.path == "/api/auto-page-mapping/detect":
                sid = str(payload.get("source_id") or "")
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    with runtime_lock:
                        calibration_active_sources.add(sid)
                    result = detect_auto_page_mapping(sid)
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"自动检测失败：{exc}"}, status=500)
                    return
                finally:
                    with runtime_lock:
                        calibration_active_sources.discard(sid)
                self._send_json({"ok": True, "result": result})
                return
            if parsed.path == "/api/documents/remove":
                sid = str(payload.get("source_id") or "")
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                with runtime_lock:
                    runtime["rebuilding"] = True
                    old_engine = runtime["engine"]
                    if hasattr(old_engine, "close"):
                        old_engine.close()
                try:
                    result = DocumentDeletionService(root, index_path).remove(
                        sid,
                        delete_generated_artifacts=bool(payload.get("delete_generated_artifacts", True)),
                        delete_internal_copy=bool(payload.get("delete_internal_copy", False)),
                    )
                    with runtime_lock:
                        runtime["engine"] = SearchEngine(index_path)
                        runtime["source_files"] = {
                            str(item.get("source_file_id")): item
                            for item in runtime["engine"].index.get("source_files", [])
                            if item.get("source_file_id")
                        }
                        runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                        runtime["rebuilding"] = False
                except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                    with runtime_lock:
                        runtime["engine"] = SearchEngine(index_path)
                        runtime["source_files"] = {
                            str(item.get("source_file_id")): item
                            for item in runtime["engine"].index.get("source_files", [])
                            if item.get("source_file_id")
                        }
                        runtime["index_metadata"] = runtime["engine"].index.get("metadata", {})
                        runtime["rebuilding"] = False
                    self._send_json({"error": str(exc)}, status=400)
                    return
                self._send_json({"ok": True, "result": result, "event": "library_changed"})
                return
            if parsed.path == "/api/bibliographic-metadata/detect":
                sid = str(payload.get("source_id") or "")
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    metadata = detect_bibliographic_metadata(sid, force=bool(payload.get("force")))
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"书目信息识别失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True, "metadata": metadata})
                return
            if parsed.path == "/api/bibliographic-metadata/save":
                sid = str(payload.get("source_id") or "")
                metadata_payload = payload.get("metadata") or {}
                if not sid or not isinstance(metadata_payload, dict):
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    metadata = save_bibliographic_metadata(sid, metadata_payload)
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"书目信息保存失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True, "metadata": metadata})
                return
            if parsed.path == "/api/auto-page-mapping/apply":
                sid = str(payload.get("source_id") or "")
                segments = payload.get("segments") or []
                auto_mapping = payload.get("auto_mapping") or {}
                if not sid or not isinstance(segments, list) or not isinstance(auto_mapping, dict):
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                try:
                    updated = apply_live_auto_mapping(
                        sid,
                        segments,
                        auto_mapping,
                        bool(payload.get("replace_manual")),
                    )
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=409)
                    return
                except Exception as exc:
                    self._send_json({"error": f"应用自动映射失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True, "updated": updated})
                return
            if parsed.path == "/api/auto-page-mapping/accept":
                sid = str(payload.get("source_id") or "")
                if not sid:
                    self._send_json({"error": "invalid request"}, status=400)
                    return
                job_id = f"auto-map-{uuid.uuid4().hex[:12]}"
                try:
                    segment_count = accept_auto_page_mapping(sid)
                    with import_jobs_lock:
                        import_jobs[job_id] = {
                            "job_id": job_id,
                            "status": "processing",
                            "phase": "rebuilding_index",
                            "message": "正在接受自动页码映射并重建索引…",
                        }
                    rebuild_runtime_index(job_id)
                    update_import_job(job_id, status="completed", phase="completed", message="自动页码映射已接受")
                except MinerUError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    update_import_job(job_id, status="failed", phase="failed", message=str(exc))
                    self._send_json({"error": f"自动映射已保存失败或索引重建失败：{exc}"}, status=500)
                    return
                self._send_json({"ok": True, "segment_count": segment_count, "rebuilt": True})
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def _send_source(self, request_path: str, send_body: bool = True) -> None:
            source_id = unquote(request_path[len("/source/") :])
            with runtime_lock:
                record = runtime["source_files"].get(source_id)
            if not record:
                self._send(404, b"Unknown source", "text/plain; charset=utf-8")
                return
            relative_path = str(record.get("relative_path") or "")
            target = (root / relative_path).resolve()
            if target != root and root not in target.parents:
                self._send(403, b"Forbidden", "text/plain; charset=utf-8")
                return
            if target.suffix.lower() not in {".pdf", ".doc", ".docx"} or not target.exists():
                self._send(404, b"Source not found", "text/plain; charset=utf-8")
                return
            content_type = {
                ".pdf": "application/pdf",
                ".doc": "application/msword",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }.get(target.suffix.lower(), "application/octet-stream")
            body = target.read_bytes() if send_body else b""
            self._send(200, body, content_type, content_length=target.stat().st_size, send_body=send_body)

        def log_message(self, format: str, *args) -> None:
            return

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, index_path: Path = DEFAULT_DATABASE_PATH) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(index_path))
    print(f"ME Finder running at http://{host}:{port}/")
    server.serve_forever()

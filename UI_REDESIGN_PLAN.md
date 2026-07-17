# UI Redesign Plan — ME_Finder

## Overview

Redesign the existing single-page HTML frontend into a multi-page, iOS/iPadOS-style
local literature search application.  The backend (`search.py`, `indexer.py`,
`pdf_extractors.py`, `extractors.py`, etc.) is **not modified**.  The new UI is
still a self-contained HTML/CSS/JS app served by the existing Python
`http.server`-based backend with JSON API.

---

## Architecture

```
Python backend (unchanged)
  ├── GET  /                → new SPA HTML
  ├── POST /api/search      → existing search engine
  ├── GET  /api/index-meta  → NEW: return index metadata + source list
  ├── GET  /api/sources     → NEW: return source_files + volumes + works
  ├── GET  /api/calibration → NEW: return pdf_page_mappings for a document
  ├── POST /api/calibration → NEW: save updated page_mapping segments
  ├── GET  /source/:id      → existing: serve raw PDF/DOCX
  └── POST /api/import      → FUTURE: accept uploaded file
```

New API endpoints return data already present in the local index database; they do **not**
introduce new databases or search algorithms.

---

## Design System

### Colors

| Token              | Value     |
|--------------------|-----------|
| `--bg`             | `#F5F5F7` |
| `--surface`        | `#FFFFFF` |
| `--text-primary`   | `#1D1D1F` |
| `--text-secondary` | `#6E6E73` |
| `--accent`         | `#007AFF` |
| `--accent-light`   | `#007AFF1A` |
| `--success`        | `#34C759` |
| `--warning`        | `#FF9500` |
| `--error`          | `#FF3B30` |
| `--divider`        | `#E5E5EA` |
| `--sidebar-bg`     | `#F2F2F7` |
| `--sidebar-active` | `#E8E8ED` |

### Typography

```css
font-family: "Segoe UI Variable", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
```

| Use case        | Size  | Weight | Line-height |
|-----------------|-------|--------|-------------|
| Page title      | 22px  | 700    | 1.3         |
| Section title   | 17px  | 600    | 1.4         |
| Body / results  | 15px  | 400    | 1.8–1.9     |
| Caption / meta  | 13px  | 400    | 1.5         |
| Small label     | 12px  | 500    | 1.4         |

### Spacing & Radii

- Card radius: 16px
- Widget radius: 10px
- Input height: 56px
- Sidebar width: 220px
- Transition: `160ms ease` (interactions), `220ms ease` (panels)

---

## Page Structure

### Global Shell

```
┌──────────┬────────────────────────────────┐
│ Sidebar  │  Main content area             │
│ 220px    │  (scrolls independently)       │
│          │                                │
│ ● 检索   │                                │
│   文献库  │                                │
│   导入   │                                │
│   校准   │                                │
│   设置   │                                │
│          │                                │
│          │                                │
│ ver 0.1  │                                │
└──────────┴────────────────────────────────┘
```

Sidebar items: icon + label, 44px row height, 10px radius selected bg.
Active item: `--sidebar-active` background + `--accent` left 3px bar.

### Page: 文献检索 (Search)

```
┌─────────────────────────────────────────────┐
│ 文献检索                     索引: 25,848 条 │
│ 在本地文献库中定位原句…                       │
├─────────────────────────────────────────────┤
│ ┌─────────────────────────────────────────┐ │
│ │ 🔍  输入中文引文…                        │ │
│ └─────────────────────────────────────────┘ │
│ [综合检索] [精确匹配] [模糊检索]              │
│ ○来源类型  ○卷次  ○作者  ○年份               │
├────────────────────┬────────────────────────┤
│ Results list       │ Selected detail        │
│ ┌────────────────┐ │ Title / Author         │
│ │▎result 1 (sel) │ │ Meta pills             │
│ ├────────────────┤ │ Matched text (mark)    │
│ │ result 2       │ │ Context before/after   │
│ ├────────────────┤ │                        │
│ │ result 3       │ │ [Copy] [Open PDF]      │
│ └────────────────┘ │                        │
└────────────────────┴────────────────────────┘
```

- Left list: unified container, thin dividers, no independent cards.
- Selected row: `--accent-light` bg + left 3px `--accent` bar.
- Right detail: full matched paragraph with `<mark>`, context paragraphs.
- PDF page detail: collapsed by default, expandable.

### Page: 文献库 (Library)

- Top: search + segment filter (全部 / Word / PDF)
- List: title, author, type badge, year, index/calibration status
- Click row → right drawer slides in with full detail

### Page: 文献导入 (Import)

- Large drop zone (dashed border, icon)
- Accepted: .pdf, .docx
- After drop: step-by-step status (file read → type detect → parse → index)
- Future: wired to backend import API

### Page: 页码校准 (Calibration)

- Document selector
- Segment table: PDF range → citation range, method, confidence
- Add / edit / delete segments
- Live preview: "PDF 第 183 页 → 引用第 155 页"
- Save button → POST /api/calibration

### Page: 设置 (Settings)

- Index path display
- Rebuild index button
- Version info

---

## Implementation Phases

| Phase | Scope | Validates |
|-------|-------|-----------|
| 1 | Shell + sidebar + CSS variables + routing scaffold | Layout renders, sidebar nav works |
| 2 | Search page full rebuild | All existing search tests pass, search works in browser |
| 3 | Library page | Source list loads, filters work |
| 4 | Import page | Drop zone renders (backend wiring deferred) |
| 5 | Calibration page | Segment editor renders, save round-trips |

After all 5 phases: desktop shell via pywebview.

---

## Constraints

- All code lives in a single HTML string inside `web.py` (same as current).
- No external CDN dependencies. No build tooling. Pure HTML/CSS/JS.
- Backend search algorithm, index format, and data extraction are frozen.
- New API endpoints only expose data already in the local SQLite index.

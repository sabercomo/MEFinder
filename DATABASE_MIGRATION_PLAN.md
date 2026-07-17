# 数据库 / 索引迁移计划

日期：2026-07-07

## 当前状态

当前项目没有关系型数据库。所谓“数据库 schema”实际是 `data/index.json` 的 JSON 索引结构。

当前顶层结构：

```json
{
  "metadata": {},
  "source_files": [],
  "volumes": [],
  "works": [],
  "toc_entries": [],
  "paragraphs": [],
  "page_anchors": [],
  "audit_issues": []
}
```

当前索引规模：

- 源文件：10
- 段落：45233
- 可检索段落：22909
- 索引文件大小：约 127 MB

PDF 批量导入后 JSON 会继续变大。第一阶段可以继续沿用 JSON，降低改动风险；第二阶段应评估迁移到 SQLite。

## 迁移目标

目标不是重写系统，而是在现有索引结构中加入 PDF 能力：

```text
Word importer -> same index
PDF importer  -> same index
SearchEngine  -> same API
Web UI        -> minimal additions
```

需要做到：

- Word 记录继续可用。
- 旧测试继续通过。
- PDF 记录能进入统一检索。
- 搜索结果能按来源类型筛选。
- PDF 页码字段不污染 Word 页码字段。

## 索引版本

在 `metadata` 中新增：

```json
{
  "schema_version": 2,
  "supported_source_types": ["word", "pdf"],
  "corpus_dirs": {
    "word": "corpus/raw_docx",
    "pdf": "corpus/raw_pdf"
  }
}
```

兼容规则：

- 没有 `schema_version` 的旧索引视为版本 1。
- 版本 1 的 Word 段落默认 `source_type = "word"`。
- 版本 2 搜索代码应兼容版本 1。

## source_files 迁移

当前字段：

- `source_file_id`
- `relative_path`
- `volume_number`
- `file_format`
- `container_format`
- `file_name`
- `size_bytes`
- `sha256`
- `last_modified`

新增字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_type` | enum | `word` 或 `pdf` |
| `source_file_id` | string | Word 保持 `source-01`；PDF 用 `pdf-0001` |
| `collection_id` | string/null | 文献集合 ID |
| `display_title` | string | PDF 可用文件名或元数据标题 |
| `open_source_url` | string | Web 打开源文件的 URL |
| `pdf_profile` | object/null | PDF 分类和解析特征 |

PDF 示例：

```json
{
  "source_file_id": "pdf-0001",
  "source_type": "pdf",
  "relative_path": "corpus/raw_pdf/Critique of Forms of Life (...).pdf",
  "file_format": "pdf",
  "container_format": "pdf",
  "file_name": "Critique of Forms of Life (...).pdf",
  "display_title": "Critique of Forms of Life",
  "size_bytes": 25021420,
  "sha256": "...",
  "open_source_url": "/source/pdf-0001",
  "pdf_profile": {
    "detected_pdf_type": "native_text",
    "has_page_labels": true,
    "text_source": "native_text"
  }
}
```

## volumes / collections 迁移

当前 `volumes` 偏向《马克思恩格斯文集》卷次。

PDF 文献不一定有“卷次”。建议保留 `volumes` 表兼容 Word，同时新增 `collections` 或扩展为通用 `documents`。

低风险方案：

- 保留 `volumes` 给 Word。
- 新增 `collections`。
- PDF 的 `volume_id` 可为空或使用 `PDF-0001`，但搜索结果要能处理。

建议新增：

```json
"collections": [
  {
    "collection_id": "MEWJ",
    "display_title": "马克思恩格斯文集",
    "source_type": "word"
  },
  {
    "collection_id": "PDF",
    "display_title": "PDF 文献",
    "source_type": "pdf"
  }
]
```

如果暂时不加 `collections`，则 PDF `volume_display` 应回退为 PDF 文献标题。

## works 迁移

当前 `works` 记录 Word 文献标题。

PDF 可以把整本书视为一个 `work`，后续再解析目录章节。

新增字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_type` | enum | `word` / `pdf` |
| `source_file_id` | string | 关联源文件 |
| `document_title` | string | PDF 书名 |
| `publisher` | string/null | 出版信息 |
| `publication_year` | string/null | 年份 |
| `isbn` | string/null | ISBN |
| `pdf_outline_ref` | string/null | PDF 目录/书签来源 |

PDF 示例：

```json
{
  "work_id": "PDF-0001-W0001",
  "source_type": "pdf",
  "source_file_id": "pdf-0001",
  "volume_id": null,
  "title": "Critique of Forms of Life",
  "author_label": "Rahel Jaeggi",
  "title_source": "pdf_metadata_or_filename",
  "boundary_source": "whole_pdf",
  "confidence": 0.6
}
```

## paragraphs 迁移

当前 `paragraphs` 是搜索核心，PDF 必须输出到这里。

新增通用字段：

| 字段 | 类型 | 默认 |
|---|---|---|
| `source_type` | enum | Word 旧记录默认为 `word` |
| `source_file_id` | string | 已有 |
| `document_title` | string/null | PDF 标题 |
| `source_order` | integer | 源文件内顺序 |
| `open_source_url` | string/null | 打开源文件 |

新增 PDF 字段：

| 字段 | 类型 |
|---|---|
| `pdf_page_start_index` | integer/null |
| `pdf_page_end_index` | integer/null |
| `pdf_page_start_label` | string/null |
| `pdf_page_end_label` | string/null |
| `printed_page_start` | string/null |
| `printed_page_end` | string/null |
| `citation_page_start` | string/null |
| `citation_page_end` | string/null |
| `page_mapping_method` | string |
| `page_mapping_confidence` | number |
| `is_cross_page` | boolean |
| `text_source` | string |
| `mineru_block_ids` | array/null |
| `bbox_refs` | array/null |

兼容：

- Word 旧字段 `original_page_start`、`original_page_end` 保留。
- PDF 已校准时可同步填入 `original_page_start`、`original_page_end`。
- PDF 未校准时 `original_page_start` 必须为空。

## 新增 pdf_pages

新增顶层表：

```json
"pdf_pages": []
```

字段见 `PDF_PAGE_MODEL.md`。

用途：

- 页面级文本审计。
- 页面级页码映射。
- 打开 PDF 时定位物理页。
- 支持跨页窗口生成。

## 新增 pdf_page_mappings

新增顶层表：

```json
"pdf_page_mappings": []
```

示例：

```json
{
  "mapping_id": "MAP-PDF-0001",
  "source_file_id": "pdf-0001",
  "method": "manual_segment",
  "segments": [
    {
      "pdf_page_start": 0,
      "pdf_page_end": 10,
      "citation": null
    },
    {
      "pdf_page_start": 11,
      "pdf_page_end": 20,
      "citation_page_start": "i",
      "number_style": "roman_lower"
    },
    {
      "pdf_page_start": 21,
      "pdf_page_end": 420,
      "citation_page_start": "1",
      "number_style": "arabic"
    }
  ],
  "confidence": 0.95,
  "validated_by": "manual_sample"
}
```

## 新增 pdf_import_runs

用于可复核导入过程：

```json
"pdf_import_runs": [
  {
    "run_id": "PDF-RUN-20260707-001",
    "source_file_id": "pdf-0001",
    "parser": "pymupdf",
    "parser_version": "...",
    "method": "native_text",
    "started_at": "...",
    "finished_at": "...",
    "status": "success",
    "notes": []
  }
]
```

## SearchEngine 迁移

### 1. 读取兼容

加载 paragraph 时：

```python
source_type = paragraph.get("source_type", "word")
```

### 2. source_type 筛选

`search()` 增加参数：

```python
def search(self, query, mode="auto", limit=10, source_type="all"):
```

过滤：

- `all`：不过滤。
- `word`：只检索 `source_type == "word"`。
- `pdf`：只检索 `source_type == "pdf"`。

### 3. 结果格式

新增：

- `source_type`
- `source_file_id`
- `document_title`
- `open_source_url`
- PDF 页码字段
- `is_cross_page`

Word 结果保持原样。

## Web API 迁移

### POST /api/search

请求新增：

```json
{
  "source_type": "all"
}
```

旧请求不带 `source_type` 时默认 `all`。

### GET /source/<source_file_id>

新增只读路由。

安全要求：

- 只允许索引中的源文件。
- 路径必须 resolve 到项目根目录内。
- 不允许任意路径参数。

## 前端迁移

最小改动：

1. 控件区增加来源选择：

```html
<select id="sourceType">
  <option value="all">全部</option>
  <option value="word">Word</option>
  <option value="pdf">PDF</option>
</select>
```

2. 请求体增加：

```javascript
source_type: $("sourceType").value
```

3. 卡片中如果有 `open_source_url`，显示“打开原始 PDF”。

4. 页码显示继续使用 `item.page` 和 `item.page_note`。

## CLI 迁移

当前：

```text
build-index --corpus corpus/raw_docx
```

建议扩展：

```text
build-index --word-corpus corpus/raw_docx --pdf-corpus corpus/raw_pdf --include-pdf
```

或新增：

```text
import-pdf --pdf corpus/raw_pdf/xxx.pdf --index data/pdf_staging/xxx.json
build-index --include-pdf
```

本轮不建议默认导入全部 PDF。默认仍只导入 Word，除非显式传入 `--include-pdf`。

## 测试迁移

保留现有 `tests/known_quotes.json`，继续验证 Word 不被破坏。

新增：

```text
tests/known_pdf_quotes.json
tests/test_pdf_import.py
tests/test_pdf_search.py
tests/test_page_mapping.py
```

测试覆盖：

- PDF 原生文本页级抽取。
- MinerU `content_list.json` 的 `page_idx` 保留。
- 未校准页码显示。
- 固定 offset 映射。
- 分段映射。
- 跨页搜索。
- `source_type` 筛选。
- 打开原始 PDF URL 只允许索引文件。

## 迁移步骤

### Step 1：schema_version = 2

- 为 Word 记录补默认 `source_type = "word"`。
- 搜索层兼容旧索引。
- 测试 Word 全部通过。

### Step 2：PDF staging

- 不写入主索引。
- 单个 PDF 生成 `data/pdf_staging/<source_file_id>.json`。
- 审查 `pdf_pages`、页码字段和文本质量。

### Step 3：合并少量 PDF

- 只合并 P1-P3。
- 建立 `source_files`、`works`、`pdf_pages`、`paragraphs`。
- 保持 `page_mapping_method = "uncalibrated"`，除非已人工校准。

### Step 4：Web 最小扩展

- source_type 筛选。
- PDF 页码提示。
- 打开 PDF。

### Step 5：完整链路验证

对每个验证 PDF 至少准备 2 条引文：

```text
输入一句话
  -> 搜索命中 PDF
  -> 显示具体文献
  -> 显示可靠页码或明确未校准
  -> 打开原始 PDF
```

### Step 6：考虑 SQLite

当 PDF 导入后 `data/index.json` 超过可接受体积或启动加载变慢，再迁移 SQLite。

## SQLite 备选方案

如果进入批量 PDF 阶段，建议使用 SQLite：

```text
data/me_finder.sqlite
```

核心表：

- `source_files`
- `works`
- `paragraphs`
- `pdf_pages`
- `page_mappings`
- `search_terms` 或 FTS5 表
- `audit_issues`

但这属于后续性能阶段，不应阻塞 PDF MVP。

## 回滚策略

- 保留当前 Word-only 索引构建路径。
- `--include-pdf` 关闭时输出与当前行为一致。
- PDF 导入失败只写入 `audit_issues`，不影响 Word 记录。
- Web source_type 默认 `all`，旧前端请求仍可用。

## 本轮不做

- 不批量导入全部 PDF。
- 不统一转 Markdown。
- 不重写前端。
- 不引入在线服务。
- 不把 PDF 物理页序当作引用页码。

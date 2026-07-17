# PDF 扩展方案

日期：2026-07-07

## 当前项目确认

### 技术栈

当前项目是一个纯本地 Python 标准库 MVP：

- 后端：Python 3 标准库。
- Web 服务：`http.server.ThreadingHTTPServer`。
- 前端：`src/me_finder/web.py` 中内嵌的单页 HTML/JavaScript。
- 索引存储：`data/index.json`，不是关系型数据库。
- 搜索：`src/me_finder/search.py` 中的确定性本地检索。
- Word 导入：`src/me_finder/extractors.py`。
- CLI：`py -3 -m src.me_finder ...`。
- 测试：`unittest` + `tests/known_quotes.json`。

当前环境未发现本地 PDF 解析命令或 Python PDF 包：

- 未发现 `pdftotext`、`pdfinfo`、`mutool` 等命令。
- 未安装 `pypdf`、`PyPDF2`、`fitz`/PyMuPDF、`pdfminer`、`pdfplumber`。

因此 PDF 导入需要新增一个本地解析依赖，或读取已经由 MinerU 产生的结构化文件。

### 当前数据流

```text
corpus/raw_docx/
  -> indexer.build_index()
  -> extractors.extract_source()
  -> extract_docx() / extract_doc()
  -> source_files / volumes / works / toc_entries / paragraphs / page_anchors / audit_issues
  -> data/index.json
  -> SearchEngine
  -> /api/search
  -> Web 结果卡片
```

当前 `SearchEngine` 只依赖 `paragraphs` 中的统一字段：

- `text_raw`
- `normalized_text`
- `compact_text`
- `plain_text`
- `eligible_for_search`
- `volume_id`
- `volume_number`
- `work_id`
- `work_title`
- `page_display`
- `page_source_type`
- `page_confidence`
- `original_file_name`

这说明 PDF 支持不需要重写搜索引擎。只要 PDF 导入器产出相同的 `paragraphs` 结构，并补充 PDF 页码字段，现有搜索流程可以继续使用。

### 当前 Web 入口

当前 Web 页面在 `src/me_finder/web.py` 中：

- `GET /` 返回内嵌 HTML。
- `POST /api/search` 接收 `query`、`mode`、`limit`。
- 前端直接渲染 API 返回的 `results`。

PDF 扩展应保持这个架构，仅做最小必要增加：

- 请求中增加 `source_type` 筛选。
- 结果卡片增加 PDF 页码提示和“打开原始 PDF”链接。
- 后端增加只读文件打开路由，例如 `GET /source/<source_file_id>`。

## 当前 PDF 目录与验证候选

目录：`corpus/raw_pdf/`

当前有 10 个 PDF。低层特征扫描显示这些文件类型混合：有疑似原生文本 PDF、扫描 PDF、带 OCR 文本层的扫描 PDF、复杂布局 PDF。

本轮不批量导入。建议先选择 3 个候选验证完整链路：

| 候选 | 文件 | 选择原因 | 预期分类 |
|---|---|---|---|
| P1 | `Axel Honneth Reconceiving Social Philosophy ...pdf` | 文件小，检测到 PageLabels，图像对象少，适合作为英文原生文本或轻量 PDF 验证 | A 原生文本 PDF |
| P2 | `Critique of Forms of Life ...pdf` | 有 PageLabels、ToUnicode 映射、页对象较完整，适合验证页标签与原生文本抽取 | A/C 原生文本或文本层需校验 |
| P3 | `伦理学简史 ...pdf` | 图像对象几乎等于页数，无 ToUnicode，适合验证扫描 PDF 与 OCR/MinerU 路径 | B 扫描 PDF |

备选：

- `法哲学原理：或自然法和国家学纲要 ...pdf`：体积很大，图像对象约等于页数且有 ToUnicode，适合第二批验证“扫描 + OCR 文本层/复杂中文排版”。
- `批判理论（Critical theory）...pdf`：中文文件名、图像对象多、无 ToUnicode，适合验证 MinerU/OCR。

## 目标架构

```text
DOCX / DOC
  -> 现有 Word 导入器
  -> 统一文献数据结构

PDF
  -> PDF 类型检测
  -> 原生文本解析 或 MinerU 结构化解析 或 OCR
  -> 页面级文本块
  -> 页码映射
  -> 跨页窗口
  -> 统一文献数据结构

统一文献数据结构
  -> data/index.json
  -> SearchEngine
  -> /api/search
  -> 当前 Web 搜索界面
```

## PDF 导入器设计

新增模块建议：

```text
src/me_finder/pdf/
  __init__.py
  detector.py
  native.py
  mineru.py
  page_mapping.py
  assembler.py
```

也可以先用较少文件实现 MVP：

```text
src/me_finder/pdf_extractors.py
src/me_finder/pdf_page_mapping.py
```

### 1. PDF 类型检测

导入前先生成 `pdf_profile`：

| 字段 | 说明 |
|---|---|
| `pdf_page_count` | 页数 |
| `has_page_labels` | 是否有 PDF Page Labels |
| `image_object_count` | 图像对象数量 |
| `to_unicode_map_count` | ToUnicode 映射数量 |
| `text_extractable_page_ratio` | 可提取文本页比例 |
| `avg_text_chars_per_page` | 平均每页文本字符数 |
| `garbled_text_ratio` | 乱码比例 |
| `layout_complexity_hint` | 多栏、表格、脚注、图片密度等提示 |
| `detected_pdf_type` | `native_text`、`scanned`、`garbled_text_layer`、`complex_layout` |

分类规则：

- A 原生文本 PDF：多数页能稳定提取文本，乱码率低。
- B 扫描 PDF：图像对象接近页数，文本极少或无文本层。
- C 文本层乱码 PDF：可提取文本但乱码率高，缺少可用 ToUnicode 或字符异常。
- D 复杂布局 PDF：有文本层但多栏、表格、脚注、图片/公式密集，简单按页抽文本会破坏阅读顺序。

### 2. 原生文本 PDF

优先按页直接提取文本：

```text
PDF page
  -> page_text_raw
  -> blocks / lines
  -> paragraphs
  -> sentences
  -> normalized_text
```

推荐本地依赖优先级：

1. PyMuPDF：页级文本、块坐标、打开 PDF 位置较方便。
2. pdfminer.six：文本抽取和布局参数可控。
3. pypdf：适合 Page Labels、基本文本，不适合复杂布局。

第一版可以选择 PyMuPDF 或 pdfminer.six 之一，不需要同时引入多个解析器。

### 3. MinerU 结构化输出

不要统一先转 Markdown。

如果存在 MinerU 输出，优先解析：

- `content_list.json`
- `content_list_v2.json`
- `middle.json`

必须保留：

- `page_idx`
- 块类型
- 坐标
- 文本
- 阅读顺序
- 原始结构化块 ID

Markdown 只能作为调试和阅读输出，不作为唯一索引来源。

推荐目录约定：

```text
corpus/raw_pdf/
  xxx.pdf

corpus/processed_pdf/mineru/
  xxx/
    content_list.json
    middle.json
    markdown.md
```

PDF 导入器先查找同名 MinerU 目录；若存在结构化文件，则走 MinerU 结构化解析；若不存在，再根据分类尝试原生文本解析。

### 4. OCR 路径

扫描 PDF 或乱码文本层 PDF 走 OCR/MinerU。

本阶段不建议直接把 OCR 图片流程内嵌进 Web 服务。应作为离线导入步骤：

```text
py -3 -m src.me_finder import-pdf --pdf corpus/raw_pdf/xxx.pdf --method mineru
py -3 -m src.me_finder build-index --include-pdf
```

Web 搜索只读取已建立的本地索引。

## PDF 页面级数据结构

PDF 导入器输出两层文本：

1. `pdf_pages`：每个物理页一条记录，保留页码模型。
2. `paragraphs`：检索单元，可来自单页，也可来自跨页窗口。

页面记录示例：

```json
{
  "pdf_page_id": "PDF-0001-PAGE-000021",
  "source_file_id": "pdf-0001",
  "pdf_page_index": 21,
  "pdf_page_label": "1",
  "printed_page": "1",
  "citation_page": "1",
  "page_mapping_method": "manual_segment",
  "page_mapping_confidence": 0.95,
  "text_raw": "...",
  "blocks": [...]
}
```

段落记录示例：

```json
{
  "paragraph_id": "PDF-0001-P000321",
  "source_type": "pdf",
  "source_file_id": "pdf-0001",
  "work_id": "PDF-0001-W0001",
  "text_raw": "...",
  "normalized_text": "...",
  "pdf_page_start_index": 38,
  "pdf_page_end_index": 39,
  "citation_page_start": "38",
  "citation_page_end": "39",
  "page_display": "38-39",
  "page_source_type": "manual_segment",
  "page_confidence": 0.95
}
```

## 跨页搜索

PDF 不能只按页独立检索，否则一句话跨页会断裂。

建议生成三类检索单元：

1. 页内段落：正常段落。
2. 页尾-下一页页首窗口：例如每页末尾 300 字 + 下一页开头 300 字。
3. 连续文本块：按页面顺序拼接全文，记录每个字符到页码的映射。

第一版推荐实现“跨页窗口”：

```text
page N tail + page N+1 head
  -> paragraph_id = PDF-0001-CROSS-000038-000039
  -> is_cross_page = true
  -> pdf_page_start_index = 38
  -> pdf_page_end_index = 39
```

搜索结果应能显示：

- 单页命中：`引用页码：38`
- 跨页命中：`引用页码：38-39`
- 未校准：`PDF 第 38-39 页，引用页码尚未校准`

## Web 最小扩展

不重写前端，只增加小控件和少量字段。

### 搜索请求

当前：

```json
{ "query": "...", "mode": "auto", "limit": 10 }
```

扩展：

```json
{ "query": "...", "mode": "auto", "limit": 10, "source_type": "all" }
```

`source_type` 可选：

- `all`
- `word`
- `pdf`

### 搜索结果

新增字段：

- `source_type`
- `source_file_id`
- `open_source_url`
- `pdf_page_start_index`
- `pdf_page_end_index`
- `citation_page_start`
- `citation_page_end`
- `page_mapping_method`
- `page_mapping_confidence`
- `is_cross_page`

### Web 页面

新增：

- 来源类型筛选：全部 / Word / PDF。
- PDF 结果展示：“PDF 第 X 页”与“引用页码尚未校准”。
- “打开原始 PDF”链接。

不做：

- 不改造成桌面应用。
- 不重做页面视觉设计。
- 不把 PDF 阅读器嵌入第一版。

## 打开原始 PDF

新增只读路由：

```text
GET /source/<source_file_id>
```

实现原则：

- 只能打开 `source_files.relative_path` 指向的项目内文件。
- 只允许 `.pdf`、`.doc`、`.docx` 等已索引源文件。
- 路径必须经过 `resolve()`，确认位于工作区内，避免任意文件访问。
- PDF 响应头使用 `application/pdf`。

如果后续要定位到页面，可以在 URL 上追加 fragment：

```text
/source/pdf-0001#page=38
```

浏览器是否跳转到指定页由内置 PDF 查看器决定；系统仍应在结果卡片中明确展示页码。

## 实施阶段

### 阶段 1：结构和文档

本轮完成：

- 当前项目分析。
- PDF 扩展设计。
- 页码模型。
- 数据迁移计划。
- 选择少量 PDF 验证候选。

### 阶段 2：PDF 验证导入 MVP

只处理 1-3 个 PDF：

1. 安装或接入一个本地 PDF 文本解析器。
2. 实现 `extract_pdf()`.
3. 产出 `pdf_pages` 和 PDF `paragraphs`.
4. 实现未校准页码显示。
5. 实现 `source_type` 筛选。
6. 实现打开原始 PDF。
7. 为验证 PDF 准备少量 known quotes。

### 阶段 3：页码校准

对验证 PDF 建立 `page_mappings.json`：

```json
{
  "source_file_id": "pdf-0001",
  "segments": [
    { "pdf_page_start": 0, "pdf_page_end": 10, "citation": null },
    { "pdf_page_start": 11, "pdf_page_end": 20, "citation_start": "i", "style": "roman" },
    { "pdf_page_start": 21, "pdf_page_end": 420, "citation_start": "1", "style": "arabic" }
  ]
}
```

### 阶段 4：扩大 PDF 文献库

只有当完整链路通过后，才批量处理 `corpus/raw_pdf/`。

验收链路：

```text
输入一句话
  -> 搜索
  -> 返回具体文献
  -> 返回可靠页码或明确“未校准”
  -> 打开原始 PDF
```

## 风险与约束

- 当前 PDF 解析依赖缺失，需要新增本地依赖或要求 MinerU 输出目录。
- 页码必须校准，不能把物理页序冒充引用页码。
- 扫描 PDF 的 OCR 质量会直接影响搜索召回。
- 复杂布局 PDF 需要保留块坐标和阅读顺序，否则跨栏文本会错乱。
- 现有 `data/index.json` 已达约 127 MB，PDF 批量导入后体积会显著增大；后续可能需要从 JSON 迁移到 SQLite，但本轮先设计兼容迁移。

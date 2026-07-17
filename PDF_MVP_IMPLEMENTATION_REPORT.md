# PDF 检索 MVP 实施报告

日期：2026-07-07

## 2026-07-10 增量更新

本次在原 MVP 上完成了数据库迁移、PDF 语料扩展和桌面打包更新。以下内容以本节为准，下面的旧记录保留作历史过程参考。

### 当前索引状态

- Word 来源：10 个；
- PDF 来源：10 个，已全部登记到 `config/pdf_imports.json` 和 SQLite 数据库；
- 总来源：20 个；
- 总段落：50,363；
- 可检索段落：28,025；
- 主数据库：`data/index.sqlite3`，约 349 MB；
- `data/index.json` 保留为可读导出和迁移回退副本，应用默认不加载它。

### PDF 状态

- 可直接搜索：`Critique of Forms of Life`、`Reconceiving Social Philosophy`、`劳动的主权者`、`自由的权利`、`法哲学原理`、`食人资本主义`、`批判理论`；
- 已登记但需要 MinerU/OCR：`伦理学简史`、`消费社会`、`追寻美德：道德理论研究`；
- 新增可搜索原生文本 PDF：`法哲学原理`、`食人资本主义`；
- 新增 PDF 页码目前均显示“引用页码尚未校准”，没有把 PDF 物理页序冒充原书页码。

### 数据库迁移

新增 `src/me_finder/database.py`，SQLite 表保存来源、卷次、文献、段落、页码、审计记录和检索字段。搜索服务支持 SQLite 多线程只读连接，默认搜索、Web 应用和桌面应用都使用 `data/index.sqlite3`；显式传入旧 `data/index.json` 时，在 SQLite 存在的情况下也会自动切换到数据库后端。

### 桌面包

新版完整包位于 `dist/MEFinder/`，包含 SQLite 数据库、20 个原始文献文件、新图标以及已使用的 MinerU 结构化结果，不在发布目录中携带私有密钥。桌面版 API 配置保存在 `%LOCALAPPDATA%\MEFinder\mineru_api.local.json`，程序升级不会清除。桌面包已实际启动并验证 Word 搜索、新 PDF 搜索、索引元数据、原始 PDF 路由和 MinerU 配置接口。

搜索框现在直接按回车即可检索，`Ctrl+Enter` 仍保留。应用内 PDF 导入已从演示队列改为真实处理流程：本地类型检测后，原生文本 PDF 直接入库；扫描、乱码或复杂布局 PDF 自动按不超过 200 页分段提交 MinerU，下载结构化页面结果，再重建 SQLite 索引。

页码校准界面统一采用从 1 开始的 PDF 文件页数，保存后自动重建 SQLite 索引。《批判理论》存在两处重复扫描：PDF 第 141/142 页均为引用第 94 页，PDF 第 214/215 页均为引用第 166 页，因此采用三段映射：PDF 48-141 → 引用 1-94，PDF 142-214 → 引用 94-166，PDF 215-325 → 引用 166-276。实测 PDF 第 195 页返回引用第 147 页。

### 当前限制

当前目录只有 10 本 PDF，因此“剩余”实际为 6 本而不是 7 本。6 本中 2 本是原生文本，已完成搜索接入；《批判理论》已用两个 MinerU 分段处理并接入 653 条正文记录；其余 3 本损坏文本层 PDF 仍待解析。后续从应用导入这类文件时会自动执行类型检测、MinerU 分段、结果下载和索引重建，但已登记的 3 本旧文件尚未自动补跑。

## 完成概况

本轮已在现有本地 Web 架构上接入 PDF 检索 MVP，没有重写前端，没有改造成桌面应用，也没有破坏现有 Word 检索。

当前统一索引 `data/index.json` 已包含：

- Word 源文件：10 个；
- PDF 源文件：1 个；
- PDF 页级记录：406 页；
- PDF 搜索段落/跨页窗口：777 条；
- 总可检索记录：23686 条。

已在写入主索引前备份旧索引：

```text
data/backups/index-20260707101034.json
```

## 新增模块

### `src/me_finder/pdf_extractors.py`

新增 PDF MVP 导入能力：

- PDF 类型检测；
- PyMuPDF 优先抽取；
- PyMuPDF 缺失时的简易原生文本 PDF 退回解析；
- PDF Page Label 读取；
- 页面级文本记录；
- 页面文本块记录；
- 跨页搜索窗口；
- 扫描、乱码、复杂布局 PDF 的 MinerU/OCR 待处理标记；
- `corpus/parsed/pdf/` 解析快照输出。

### `src/me_finder/pdf_page_mapping.py`

新增 PDF 页码模型：

- `pdf_page_index` 到 `citation_page` 的分段映射；
- 未校准状态；
- 罗马页码辅助；
- 跨页显示；
- 禁止把 PDF 物理页序直接当引用页码。

## 修改模块

### `src/me_finder/indexer.py`

新增：

- `--include-pdf` 启用 PDF 导入；
- `pdf_pages`；
- `pdf_page_mappings`；
- `pdf_import_runs`；
- `schema_version = 2`；
- Word 记录默认补 `source_type = "word"`；
- PDF 导入前自动备份已有主索引。

默认 `build-index` 仍保持 Word-only。

### `src/me_finder/search.py`

新增：

- `source_type` 筛选：`all` / `word` / `pdf`；
- PDF 结果字段；
- PDF 页码提示；
- PDF 打开源文件链接；
- 跨页结果字段；
- 排序中优先显示已校准、非跨页命中。

### `src/me_finder/web.py`

最小修改：

- 来源筛选控件；
- PDF Page Label / 引用页码 / 未校准提示展示；
- “打开原始 PDF”链接；
- 安全只读源文件路由：`/source/<source_file_id>`；
- HEAD 支持，便于验证源文件路由。

### `src/me_finder/__main__.py`

新增命令参数：

```text
build-index --include-pdf --pdf-corpus --pdf-config --parsed-pdf-dir --pdf-limit
search --source-type
```

## 新增配置

### `config/pdf_imports.json`

本轮只启用 1 个 PDF：

```text
Critique of Forms of Life (Jaeggi, RahelCronin, Ciaran(Translation)) (Z-Library).pdf
```

页码映射：

- PDF 物理页 `0-20`：前置页，引用页码未校准；
- PDF 物理页 `21-405`：正文页，`21 -> 引用页码 1`，之后连续递增；
- 映射方法：`manual_segment`；
- 置信度：`0.95`。

## 数据字段变化

顶层新增：

- `pdf_pages`
- `pdf_page_mappings`
- `pdf_import_runs`

`metadata` 新增：

- `schema_version`
- `supported_source_types`
- `corpus_dirs`
- `include_pdf`

`source_files` 新增或补齐：

- `source_type`
- `document_id`
- `display_title`
- `open_source_url`
- `pdf_profile`

`paragraphs` 新增 PDF 字段：

- `source_type`
- `document_title`
- `pdf_page_start_index`
- `pdf_page_end_index`
- `pdf_page_start_label`
- `pdf_page_end_label`
- `printed_page_start`
- `printed_page_end`
- `citation_page_start`
- `citation_page_end`
- `page_mapping_method`
- `page_mapping_confidence`
- `is_cross_page`
- `text_source`
- `open_source_url`

## 测试 PDF

### 已接入搜索

1. `Critique of Forms of Life ...pdf`

检测结果：

- 类型：`native_text`
- 页数：406；
- 有 Page Label；
- 可搜索；
- 正文页有人工分段引用页码映射；
- 已验证页内命中和跨页命中。

### 类型检测但未接入搜索

2. `伦理学简史 ...pdf`

检测结果：

- 类型：`scanned`；
- 图像对象接近页数；
- 当前不自动 OCR；
- 需要 MinerU/OCR。

3. `Axel Honneth Reconceiving Social Philosophy ...pdf`

检测结果：

- 类型：`complex_layout`；
- 当前环境未安装 PyMuPDF，内置简易解析器无法建立页序；
- 需要 PyMuPDF 或 MinerU 结构化解析。

## 已验证链路

### Word 搜索

查询：

```text
宗教是人民的鸦片。
```

结果：

- source_type：`word`；
- 文献：《黑格尔法哲学批判》导言；
- Word 旧结果正常。

### PDF 正文搜索

查询：

```text
We make and cannot escape making value judgments
```

结果：

- source_type：`pdf`；
- 文献：`Critique of Forms of Life`；
- 引用页码：`1`；
- 打开链接：`/source/pdf-critique-forms-life#page=22`。

### PDF 未校准页

查询：

```text
CRITIQUE OF FORMS OF LIFE
```

结果：

- 命中前置页；
- 显示：`PDF 第 2 页，引用页码尚未校准`；
- 未把 PDF 物理页序冒充引用页码。

### PDF 跨页搜索

查询：

```text
eating bananas or wearing red cowboy boots These things as they say
```

结果：

- 跨页命中：`true`；
- 起始页：引用页码 `1`；
- 结束页：引用页码 `2`；
- 显示：`引用页码：1-2`。

### 打开原始 PDF

验证：

```text
HEAD /source/pdf-critique-forms-life
```

结果：

- HTTP 200；
- `Content-Type: application/pdf`；
- 只允许打开索引中的源文件。

## 自动测试

新增：

- `tests/known_pdf_quotes.json`
- `tests/test_pdf_support.py`

覆盖：

- PDF 类型检测；
- 扫描 PDF 标记；
- 复杂布局 PDF 标记；
- PDF 页码分段映射；
- PDF 精确匹配；
- 未校准页码提示；
- PDF source_type 筛选；
- 跨页搜索；
- 打开 PDF 链接字段。

完整测试结果：

```text
Ran 8 tests in 16.814s
OK
```

## 当前限制

- 当前 Python 环境未安装 PyMuPDF；本轮实际抽取 `Critique of Forms of Life` 使用内置简易解析器跑通链路。
- 内置简易解析器只用于 MVP 验证，不适合作为复杂 PDF 的长期解析方案。
- 扫描 PDF、乱码文本层 PDF、复杂对象流 PDF 暂不自动 OCR，也不批量跑 MinerU。
- PDF 文献标题目前主要来自 `config/pdf_imports.json`，尚未解析 PDF 目录或章节边界。
- PDF 页码映射依赖人工配置；未配置页面必须显示“引用页码尚未校准”。
- `data/index.json` 仍是 JSON 文件，PDF 扩大后可能需要迁移 SQLite。

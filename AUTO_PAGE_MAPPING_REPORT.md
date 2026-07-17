# 自动页码映射实现报告

生成日期：2026-07-10

## 本轮完成内容

本轮在现有 Windows 桌面应用、DOCX/PDF 导入、MinerU OCR、统一搜索和手工页码校准机制上，新增了 PDF 自动页码映射 MVP。实现重点是：不调用 LLM，不逐页猜测，而是从 MinerU 结构化结果中提取页码候选，并用跨页连续序列和稳定 offset 推断 citation page mapping。

新增模块：

- `src/me_finder/auto_page_mapping.py`
  - 读取 MinerU 结构化输出中的页码候选；
  - 支持 `content_list_v2.json`、`model.json`、`middle.json`、`content_list.json`；
  - 优先识别 `page_number`，同时读取 header/footer/discarded/页面边缘短数字；
  - 规范化阿拉伯数字、罗马数字、`— 12 —`、`[12]`、`第12页`、全角数字，以及页码上下文中的 `I/l/|/O` 混淆；
  - 基于稳定 offset 和跨页递增序列拟合页码段；
  - 支持多个自动 segment；
  - 输出 `high / medium / low` 置信度和可解释 evidence；
  - 只自动应用 high 置信度段，medium/low 只作为建议。

修改模块：

- `src/me_finder/pdf_extractors.py`
  - MinerU PDF 导入后自动提取页码候选；
  - 没有人工页码映射时，自动应用高置信度 OCR 序列；
  - 已有人工页码映射时，标记 `manual_override`，自动映射不覆盖；
  - 段落记录增加 `page_scope`、`citation_page_number_*`、`citation_page_label_*`、`mapping_method`、`mapping_confidence_level`、`mapping_evidence`、`segment_id`。

- `src/me_finder/search.py`
  - 搜索结果继续返回原有页码字段；
  - 额外返回自动映射字段和 evidence，供 UI 复核。

- `src/me_finder/web.py`
  - PDF 搜索详情页显示 OCR 映射方式、置信度、页码范围和映射依据摘要；
  - 文献库详情显示自动页码映射摘要、自动区间、异常页面数量；
  - 增加“接受自动映射”“检查异常”“编辑区间”入口；
  - 新增 `/api/auto-page-mapping/accept`，可将高置信度自动段写入 `config/pdf_imports.json`，作为人工确认后的 `manual_segment`，并重建索引。

- `desktop.py`
  - 打包版应用的可变数据改为放在 `%LOCALAPPDATA%\MEFinder\runtime`；
  - 首次启动会从 exe 包复制内置 `data`、`config`、`corpus`；
  - 后续导入的 PDF、MinerU 结果、索引和配置不再只依赖 `dist\MEFinder`，避免重打包覆盖用户导入数据。

## 数据模型

现有 `citation_page` 未删除。自动映射会在 PDF page 和 paragraph payload 中增加或复用以下字段：

- `page_scope`
- `citation_page_number`
- `citation_page_label`
- `mapping_method`
- `mapping_confidence`
- `mapping_confidence_level`
- `mapping_evidence`
- `segment_id`

当前支持的 mapping method：

- `manual_segment`
- `manual`
- `manual_override`
- `pdf_page_label`
- `ocr_sequence`
- `ocr_sequence_with_structure`
- `uncalibrated`
- `mixed`

人工映射永远优先。只要 `config/pdf_imports.json` 中已有 `page_mapping.segments`，自动映射不会覆盖它。

## 实际验证

已重建索引：

- `data/index.sqlite3`
- `data/index.json`
- `dist/MEFinder/data/index.sqlite3`

索引规模：

- source files：20
- paragraphs：50363
- searchable paragraphs：28025

现有 MinerU 扫描 PDF 验证：

### 《自由的权利》

配置：`pdf-honneth-freedom-rights`

结果：

- 识别页码候选：1659 个；
- 自动应用高置信度段：1 个；
- 自动段：PDF 物理页 index 9-563，对应引用页码 1-555；
- 方法：`ocr_sequence_with_structure`；
- 置信度：high，0.99；
- 搜索“如果他的著作能够通过翻译”返回《自由的权利》，页码显示“引用页码：1”；
- 搜索“经济自由化和民主更新”返回《自由的权利》，页码显示“引用页码：3”；
- 搜索“正义论作为社会分析”返回《自由的权利》，页码显示“引用页码：9”。

### 《批判理论》

配置：`pdf-critical-theory`

结果：

- MinerU 结构化页码可被识别；
- 由于已有人工分段映射，自动映射标记为 `manual_override`；
- 当前人工映射继续生效；
- 测试确认物理页 195 附近命中仍返回引用页码 147；
- 自动规则没有覆盖你之前修正过的重复扫描页偏移。

## 测试

新增测试：

- `tests/test_auto_page_mapping.py`

覆盖：

1. 正文从 1 开始；
2. 序言与正文分别从 1 开始；
3. Roman front matter + Arabic body；
4. 章节首页缺页码；
5. OCR 漏识别约一半页码；
6. 个别页码识别错误；
7. 左右页页码位置交替；
8. 页码底部居中；
9. 扫描中缺页；
10. PDF 中插入无编号图片页；
11. 目录之后并非立即进入正文；
12. 全书只有少量页码可识别。

全量测试结果：

```text
py -3 -B -m unittest discover
Ran 35 tests
OK
```

同时确认：

- 旧 Word 搜索测试继续通过；
- 旧 PDF 搜索测试继续通过；
- 引用格式测试继续通过；
- MinerU API 配置测试继续通过。

## 关于《24/7 晚期资本主义与睡眠的终结》

后续在桌面应用实际运行目录中找到了该书：

`%LOCALAPPDATA%\MEFinder\runtime\corpus\raw_pdf\247晚期资本主义与睡眠的终结 ... .pdf`

2026-07-11 重建运行索引后再次核验：运行库包含 21 个来源文件，该书的 `source_file_id` 为 `pdf-import-ff4e05c4116c2d07`，文献和索引均未丢失。

已采取的防止复发措施：

- 打包版应用现在使用 `%LOCALAPPDATA%\MEFinder\runtime` 保存可变数据；
- 后续重打包不会直接覆盖这个运行时目录；
- 索引升级使用 `%LOCALAPPDATA%` 中的实际语料重建，不再用打包目录中的较旧索引覆盖用户导入数据。

## 数字书签页码映射

新增 PDF outline/bookmark 页码证据源：

- 使用 PyMuPDF `Document.get_toc()` 读取书签；
- 明确把书签目标页从 1-based 转为内部 `pdf_page_index` 0-based；
- 支持 `1`、`003`、`第12页`、`P.12`、`P 12`、`Page 12`、`页12`；
- `第一章`、`第2章`、`第三编`、`附录一`不会被识别为页码；
- 连续或稀疏书签必须共同支持稳定 offset 才形成映射；
- 映射方法保存为 `numeric_bookmark_sequence`，证据保留原书签标题、目标物理页、outline level 和来源。

《自由的权利》实际验证结果：

- PDF 物理页数：565；
- 数字页码书签：555；
- 自动映射：内部 `pdf_page_index 9-563` 对应引用页 `1-555`；
- 物理第 10 页对应引用第 1 页；
- 映射置信度：0.99（high）；
- 前置无数字书签页和最后一页保持未校准。

## MinerU API 到期时间

当前本机 MinerU 配置位于：

`C:\Users\xfx\AppData\Local\MEFinder\mineru_api.local.json`

检查结果：

- Token 已配置；
- Access Key / Secret 已配置；
- API base：`https://mineru.net`；
- 已补充 `expires_at: 2026-10-09`；
- 应用设置页应显示：`2026-10-09（剩余 91 天）`。

不需要重新填 Token。以后 Token 轮换时，设置页空着旧 Token 字段也会保留原值；只填新 Token 或新到期日期即可。

## 已知限制

- 自动映射只自动应用 high 置信度段；medium/low 建议需要人工进入“编辑区间”确认。
- 当前 fallback OCR 尚未实现为独立裁剪顶部/底部区域的二次 OCR；如果 MinerU 结构化结果完全没有页码候选，系统会保持未校准或给出低置信度建议。
- 插入无编号图片页会破坏稳定 offset；第一版会保守处理为异常/分段建议，不会强行补页码。
- 序言页码标签目前只对 `preface` 生成“序言第 X 页”，更细分的“译序/导言/附录”等标签还可继续扩展。
- 自动映射的“重新检测”目前通过重建索引触发；尚未提供单独只重跑页码检测、不重建全文索引的轻量按钮。

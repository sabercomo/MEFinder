# 无标签 PDF 自动页码映射与 UI 实现报告

## 完成情况

本轮已实际实现：所有 PDF 进入统一自动页码检测；无 PDF Page Labels 的原生 PDF 会继续检查数字书签、MinerU 结构化结果和页面边缘文本；历史文献可在页码校准页 dry-run 预览并按用户确认应用；应用后单篇更新 SQLite 和搜索引擎，无需重启或重建整库。

## 修改文件

- `src/me_finder/auto_page_mapping.py`
  - 新增 PDF Page Label provider。
  - 新增 Native PDF 页边文本 provider。
  - 支持 `Page 12`、`页12` 等格式，过滤年份、ISBN、长数字和正文数字。
  - sequence fitter 增加 `pdf_page_label`、`native_pdf_edge_sequence` 方法判定。
- `src/me_finder/page_mapping_service.py`
  - 新增统一 `PageMappingService.infer()`。
  - 汇总 Page Labels、数字书签、MinerU、原生页边候选并调用同一 sequence fitter。
  - 保存 `mapping_status`、`failure_reasons`、`evidence_counts`。
- `src/me_finder/pdf_extractors.py`
  - 原生、MinerU 和无文本层 PDF 全部进入统一检测。
  - PyMuPDF 文本块增加规范化 bbox 和页面尺寸。
  - source profile 保存映射状态与失败原因。
- `src/me_finder/runtime_page_mapping.py`
  - 新增单篇 SQLite 事务更新。
  - 同步更新 PDF 页、跨页段落、搜索列、source profile 和 mapping payload。
  - 修改前自动备份数据库。
- `src/me_finder/web.py`
  - 新增 `/api/auto-page-mapping/detect` dry-run API。
  - 新增 `/api/auto-page-mapping/apply` 单篇应用 API。
  - 页码校准页新增“自动检测页码”和结果预览。
  - 文献库列表/详情显示页码状态，详情增加自动检测入口。
  - 人工映射替换必须显式确认。
- `tests/test_auto_page_mapping.py`
  - 增加无标签页边序列、候选过滤、失败原因和人工 dry-run 测试。
- `tests/test_runtime_page_mapping.py`
  - 增加单篇事务应用、搜索结果即时更新和 UI 控件测试。

## 原因分析

以前没有发现单独的 `if not page_labels: return`，但原生 PDF 分支只收集数字书签，没有收集页边文本；MinerU 候选只在 MinerU 分支执行。无 Page Labels、无数字书签的普通 PDF 因候选列表为空，实际效果等同于被静默跳过。

现在 PDF Page Labels 只是一种高优先级证据，不再是自动映射入口条件。

## 当前自动检测流程

1. 读取原始 PDF。
2. 收集显式 PDF Page Labels。
3. 收集数字书签/outlines。
4. 读取已有 MinerU `page_number`、header/footer、discarded block 等候选。
5. 对原生 PDF 只扫描顶部/底部 15% 的短数字文本块。
6. 所有候选进入同一个 offset/sequence fitter。
7. 输出区间、置信度、证据数量、稳定 offset 和失败原因。
8. high 自动结果可应用；medium 显示待确认；low 不进入默认应用集合。

失败状态会明确保存为 `auto_mapping_failed`，原因可能包括：

- `no_page_labels`
- `no_bookmarks`
- `no_mineru_candidates`
- `no_edge_candidates`
- `sequence_not_found`
- `source_missing`

## UI、dry-run 与 apply

页码校准页面和文献库详情均增加“自动检测页码”。检测请求使用 dry-run，不写配置、不覆盖当前映射。预览显示检测区间、引用页范围、置信度、方法、候选数、offset 和序列一致性。

已有人工映射时，预览明确警告。只有用户点击“用自动结果替换人工映射”并再次确认，后端才接受 `replace_manual=true`。

应用时先保存映射配置，再在事务中只更新该 PDF 的数据库记录，随后热重载 SearchEngine。文献库状态、校准页当前映射和搜索结果页码立即更新，不需要重启应用，也不需要耗时的全库重建。

## 实际语料验收

### A. 无标签原生 PDF

《劳动的主权者：劳动的规范理论》：

- PDF Page Labels：0
- 数字书签：0
- MinerU：无
- 原生页边候选：339
- 状态：`auto_mapped_high`
- 方法：`native_pdf_edge_sequence`
- dry-run API 正常返回高可信区间。

此外，《法哲学原理》和《食人资本主义》也通过页边序列得到高可信自动映射。

### B. 无标签 MinerU PDF

《批判理论》：

- PDF Page Labels：0
- MinerU 页码候选：915
- 方法：`ocr_sequence_with_structure`
- 原有人工映射存在，因此检测结果只作为预览，不默认覆盖。

### C. 数字书签 PDF

《自由的权利》：

- 数字书签候选：555
- MinerU 候选：1659
- 同时存在可用 Page Labels，因此按证据优先级最终采用 `pdf_page_label`。
- 数字书签证据仍被读取并用于交叉验证。

当前现有库中没有“完全无 Page Labels 但有数字书签”的 PDF；该组合通过自动测试验证，包含 1-based bookmark target 到 0-based `pdf_page_index` 的防偏一页测试。

### D. 已有人工映射 PDF

《批判理论》和 `Critique of Forms of Life` 均验证：dry-run 可检测，但 `manual_mapping_present=true`，默认不会覆盖；后端在未提供明确替换确认时返回冲突错误。

## 测试结果

- 自动映射、PDF 支持、数据库搜索、引用格式、MinerU 配置和 UI 相关测试共 45 项通过。
- JavaScript 通过 Node 语法检查。
- 真实 HTTP dry-run API 已对无标签原生 PDF、无标签 MinerU PDF 和数字书签 PDF 验证。
- SQLite 单篇应用测试确认搜索结果会立即返回新的 `citation_page`。

## 数据库变化

本轮没有破坏性 schema migration。状态和证据继续保存在现有 JSON payload 中，新增/复用字段包括：

- `mapping_status`
- `mapping_failure_reasons`
- `evidence_counts`
- `mapping_origin`
- `mapping_method`
- `mapping_confidence_level`
- `mapping_evidence`

## 已知限制

- 页边局部 OCR fallback 尚未接入本机 OCR 引擎；本轮实现的是原生文本层页边提取，并复用已有 MinerU OCR 结构化结果。
- 自动序列可能识别出多个重置页码区间，尤其是目录、序言和正文都独立编号时；medium/异常结果仍需人工确认。
- 当前库中没有“无标签 + 数字书签”的真实样本，只能以现有带标签书签 PDF和合成自动测试共同验收。
- 单篇热更新以 SQLite 为当前运行真源；旧的 `data/index.json` 不做同步重写，下一次完整重建会根据已保存配置重新生成两者。

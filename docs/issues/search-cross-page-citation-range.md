# 跨页段落命中只在一页时，引用页码仍报整段范围

2026-10-01 从 RapidFuzz 窗口评分（D7）的验收中分出，与 D7 无关，新旧版本行为一致。

## 事实

- 冻结快照 `short4_sub-001`（种子 20261002）命中跨页合并段落 `pdf-import-da35e868e834b36d-CROSS-000485-000486`。
  - 高亮完全正确；`page_match_spans` 只落在 PDF 页索引 485（入库引用页码 463）。
  - 但结果的 `citation_page_start/end` 是 463–464，`page` 与 `copy_text` 都写“引用页码：463–464”，也就是整段的范围。
- 已核对代码：`search_assembly.format_result` 只在双页扫描版式（`layout_mode == "spread"`）时经 `search_anchors.resolve_spread_hit` 按 `page_match_spans` 收窄引用页码；普通单页版式直接沿用段落本身的 `citation_page_*`，跨页合并段落因此报整段范围。
- D7 让高亮更精确后，重复结果会合并到单页段落，冻结快照上因此有 43 条从“整段范围”变为“原句所在单页”。但没有单页重复段落可合并时，问题依然存在。证据：`reports/fuzzy-search-benchmark-2026-09-30.md` 第十节。

## 推断与待定

- 推断：普通版式在高亮只落一页时，也按 `page_match_spans` 把显示和复制的引用页码收窄到该页（与 spread 版式的处理一致），可能更符合“引用可定位”的原则。但这会改变引用输出（`copy_text`、各引文格式），须先确认产品上是否希望这样，再用基准核对页码（预期值取 `pdf_pages`，见 `scripts/fuzzy_search_benchmark.py` 的 `_page_checks`）。
- 待核实：MCP `locate_quote` 等工具输出的页码字段是否同样如此，以及对照阅读、导出是否依赖整段范围。

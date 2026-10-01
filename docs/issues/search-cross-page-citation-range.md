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

## 2026-10-01：已修复（用户决定修）

- 事实：新增 `search_cross_page.py`。普通版式（`layout_mode` 不是 `spread` / `mixed`）的跨页段落，若 `page_match_spans` 全部落在段落的起始页或结束页，显示、复制和各引文格式的引用页码收窄到该页；原句本身跨两页时仍报两页范围。只改搜索结果里用于显示和引文的字段副本，段落记录与 `pdf_page_start/end_index` 不变。收窄取的是该侧本来就写入段落的单页字段（入库时各自取自对应页的 `pdf_pages` 记录），不推算页码。
- 事实：MCP `locate_quote` / `verify_quotes` 等的 `citation_page` 直接取搜索结果的 `citation_page_start/end`，随之收窄；对照阅读与导出读段落记录，不受影响。
- 测试：`tests.test_search_match_spans` 的 `test_single_layout_cross_hit_narrows_citation_to_hit_page`（命中左页报 463、右页报 464、跨两页报 463–464；去掉修复时失败）。
- 已知限制：两页页码来源不同（`page_source_type == "mixed"`）时，收窄后仍按“来源混合，需核验”显示，不改用单页的来源判定。
- 事实：冻结快照独立样本（种子 20261002）复测，D7 一侧能核对的引用页码全部正确，`short4_sub` 10/11 → 11/11（即本议题的例子）。证据：`reports/fuzzy-search-benchmark-2026-09-30.md` 第十一节。
- 已知代价（事实）：引用页码跟随高亮。高亮若在跨页处漏框（旧评分 `cross_page_sub2-014`，原句跨两页、高亮只框到前一页），现在只报一页；以前整段范围会碰巧包住。D7 下同一条高亮正确、报两页。

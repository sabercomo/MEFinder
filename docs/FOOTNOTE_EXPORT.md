# PDF 脚注导出契约

## 调用顺序与数据边界

正式入口为 `/api/document/export-markdown`、`/api/document/export-epub`，经 `ArchiveTransferController` 调用 `document_export_service`。

1. `ensure_document_headings` 从原 PDF 目录、已有 MinerU 缓存补全标题。补全只增加标题元数据，不改正文、解析器级别或页码映射。
2. `_text_export_pages` 读取导出副本；有 `layout.json` 时，将上游明确标注 `cross_page: true` 的版面块标为不可自动配注。此处不寻找另一页的对应注释，不拆分跨页段落。
3. `build_page_artifact_profile` 首先排除脚注候选和显式引用，再统计页边重复文本。解析器 `text_level` 不豁免重复检测；短章的页眉不会被全书页数稀释。
4. `prepare_export_structure` 统一判断可信标题，按明确版面证据修正标题位置，建立编号范围与父级输出边界，再清理页眉页脚及可见页码。注释与引用先于任何删除操作受到保护。
5. `normalize_document_export` 保守配对同页唯一 marker，按首次正文引用顺序编号，并产生 `NormalizedDocument(items, footnote_report)`。
6. Markdown/EPUB renderer 消费同一个中间结果；正式入口不在 renderer 中重复 normalization。旧的 `normalize_document_footnotes` 仍返回兼容的 item 列表。

导出中的清理、来源标记和脚注重排不写回数据库；原有标题补全会更新标题元数据。验收脚本只在隔离库执行正式入口，真实库始终以 SQLite `mode=ro` 读取。

## 标题及 numbering scope

`HeadingDecision` 包含 `level / kind / reason`。原始标题级别、补全级别均须经共享判断，目录和脚注不各自识别章节。

- 重复页边文字不能作为标题；原 PDF 书签定位，或目录在对应印刷页核实的标题，可以确认真实的那一次出现。
- `document_toc` 标签本身不是定位成功的证明；补全记录 `document_heading_printed_page` 和原目录标题 `document_heading_title`。目录中有目标页但该页找不到唯一标题时，不跑到其他页选择同名页眉。
- 部/篇为父级，不重置脚注；可信章标题建立独立 scope。节和普通子标题继承所属章。不能用标题级别 1 代替“章”。
- 父级标题带导出专用的 `_export_scope_end_before`，共享内容流在该标题及其页锚点之前输出已引用的注释，不等待下一章，也不回看或移动已输出的标题。父级与下一章之间的副标题、空白页不影响这一顺序；没有弹窗支持的 EPUB 阅读器也按此顺序显示 inline aside。
- 标题补全保留原始 block 数组。导出副本中，只有可信标题的 bbox 明确位于前置块上方且横向范围重叠时，才将标题前移到这些块之前；缺少坐标、上下重叠、不同栏位均不移动。不重排普通正文，保留 `_export_index` 供脚注 ID 和报告定位。
- 章前有候选时保留 preface scope；整篇没有可信章时使用 document scope。已确认的章即使没有匹配脚注，也保留 scope，`number_range` 为 `null`。
- 报告中的未信任标题单独列在 `heading_issues`，不混入引用/注释候选统计。

`document_heading_profile` 仍然只是补全的版本、状态和来源记录，不是章节树。

## 脚注及报告

`FootnoteReference` 保留正文字符范围、稳定 note/ref ID 和显示编号；`Footnote` 保留 scope（兼容字段 `chapter_id`）、原标记、来源物理页/印刷页/块及全部 backlink ID。同文注释不会合并。模型支持多引用同一注释；原始 OCR 重复 marker 本身不足以证明这一关系，仍不自动配对。

`footnote_report` 是 JSON 对象，至少包含：

- `candidate_ref_count / matched_ref_count / unresolved_ref_count`
- `candidate_note_count / matched_note_count / unresolved_note_count`
- `numbering_scope_count`、`scopes` 中每个范围的来源、父级、编号范围和 matched/unresolved 数量
- `match_reason`、`unresolved_reason`，各分 `ref` 和 `note` 统计
- `candidates` 中每个候选的状态、一个最终原因、原文、来源页/块及引用起止偏移
- `heading_issues`、`unstructured_pages`，分别定位标题歧义和原文/块不一致的整页降级

计数单位：ref 是识别出的显式行内 marker；note 是候选源块。每一类满足 `candidate = matched + unresolved`。同一候选可能有多项风险，但只有一个最终原因参与分布计数，因此不能与旧版日志事件次数直接相加比较。未对齐的原文整页保留并单独报告，不伪造块级引用数量。

原因包括 `SAME_PAGE_UNIQUE_MARKER`、`DUPLICATE_MARKER_ON_PAGE`、`NO_NOTE_BODY`、`NO_REFERENCE`、`POSSIBLE_CROSS_PAGE_CONTINUATION`、`UNCERTAIN_PAGE_FLOW`、`CROSS_PAGE_SOURCE_BLOCK` 等，没有人为 confidence 分数。明确跨页来源拒绝整个相关块的配对，会连带保留其中可能有效的本页引用；这是当前保守策略的代价。

`source_layout_available` 表示该页有可读取的布局缓存；`source_cross_page` 表示块有明确的跨页来源标记。没有该标记不等于原 PDF 语义已经核实；链接有效也不能替代原书抽查。

## 输出与限制

物理页供内部配对、稳定 ID 和报告追溯使用，不能从最终 `PageMarker` 反推；默认 EPUB/Markdown 只输出已知印刷页隐藏锚点。EPUB 按 scope 显示编号并提供全部前向/返回链接。Markdown 使用稳定且不冲突的脚注标识符，具体阅读器可能仍采用全篇显示编号。

本轮不做跨页续注推断、OCR 错号纠正、裸数字扩展匹配或重复原始引用的猜配。缓存缺失、标题样式模糊、未识别标记等情况仍需人工复核，报告不是“全书恢复率”的证明。

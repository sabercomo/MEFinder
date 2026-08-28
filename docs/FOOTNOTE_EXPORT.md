# PDF 脚注导出契约

## 调用顺序与数据边界

正式入口为 `/api/document/export-markdown`、`/api/document/export-epub`，经 `ArchiveTransferController` 调用 `document_export_service`。

1. `ensure_document_headings` 从原 PDF 目录、已有 MinerU 缓存补全标题。补全只增加标题元数据，不改正文、解析器级别或页码映射。
2. `_text_export_pages` 读取导出副本；`attach_export_layout` 从已有 `layout.json` 读取合并前的 native spans、合并后的 `cross_page` 标记及页内 block 顺序。没有缓存则不推测来源。
3. `normalize_document_export` 首先执行 `reconstruct_export_pages`：只拆有完整证据的两页普通文本块，检查实际片段流的内容守恒，再进入后续处理。未重建的跨页块仍受原有配注 veto 约束。
4. `build_page_artifact_profile` 首先排除脚注候选和显式引用，再统计页边重复文本。解析器 `text_level` 不豁免重复检测；短章的页眉不会被全书页数稀释。
5. `prepare_export_structure` 统一判断可信标题，按明确版面证据修正标题位置，建立编号范围与父级输出边界，再清理页眉页脚及可见页码。注释与引用先于任何删除操作受到保护。
6. 原有保守 matcher 配对同页唯一 marker，按首次正文引用顺序编号，并产生 `NormalizedDocument(items, footnote_report, reconstruction_report)`。
7. Markdown/EPUB renderer 消费同一个中间结果；正式入口不在 renderer 中重复 normalization。旧的 `normalize_document_footnotes` 仍返回兼容的 item 列表。

导出中的清理、来源标记和脚注重排不写回数据库；原有标题补全会更新标题元数据。验收脚本只在隔离库执行正式入口，真实库始终以 SQLite `mode=ro` 读取。

## 导出页面重建与内容守恒

当前原型只接受普通文本的两个连续来源页。切点必须由缓存中唯一的 `type + content + bbox` 原生 span 身份确定，且满足完整 native block、单调 span 顺序、原文精确对齐、目标 `lines_deleted` tombstone、目标 block 顺序可证等条件。不根据文字语义、marker 或相邻页位置猜切点；不移动普通正文块之间的顺序。

`SpanOrigin` 保存 native page/block/line/span、合并后 line/span 索引及 bbox；`SourceFragment` 保存原始 source block ID、页/块/item 索引、原 block 内字符区间、目标页 index/physical/printed、目标 bbox 与 span origins。字符区间是 Python Unicode 字符下标，左闭右开。原 block 中的 `pdf_page_index`、`block_index`、parser indices、page_char offsets 不变；逻辑归属从新 page 容器及 `target_*` 读取。`FootnoteText.source_fragment` 和候选报告同时保留 source/export 两套定位，引用 ID 使用原 block 字符偏移。

内容守恒检查使用独立采集的原合并块 span 快照，审核**实际插入后的片段流**，不只审核拆分计划：

- 每个原 `(merged_line_index, merged_span_index)` 恰好出现一次；分别统计 missing、duplicated、unexpected spans。
- 片段 span 顺序与源 span 顺序完全一致；拼接的规范化文本与源 span、源 block 文本相同。
- 片段文本拼接与原 block **逐字符一致，包括原空白**；每个片段对应自己的源 spans，字符区间连续覆盖原文。
- 失败即抛出包含原 page/block 索引的 `DocumentExportError`，在清理、匹配和渲染前中止，不静默发布不守恒的产物。

两种正式 HTTP 导出结果均提供 `reconstruction_report`，包括重建/保留数量、原因分布、每块的 fragments 和 `content_invariant`，以及聚合 `checked_block_count / checked_span_count / missing_span_count / duplicated_span_count / unexpected_span_count / content_order_invariant_failure_count`。

三页及以上合并、注释正文、标题、图片、显示公式、表格均保持原样；普通正文内 inline equation 作为完整 span 保留。缺少 span/bbox、来源冲突或原文不一致时也不拆分。未重建块不计入 checked spans，不能把“保持原样”报告成“成功拆分”。现有新 engine 发布目录若没有转存 layout 证据，本功能不会凭空恢复它；没有改 importer 或数据库 schema。

### 真实书验收（2026-08-28）

《自由的权利》在隔离库经正式 HTTP 入口导出：326 个 cross-page blocks 中重建 258 个，产生 516 个片段；内容守恒检查 943 个源 spans，missing/duplicated/unexpected/content-order failures 全部为 0。原有候选数 836 refs / 831 notes 不变，匹配由 107 对增至 556 对，未解决由 729/724 降至 280/275。新增 449 对，原 107 对没有撤回，detector 与 matcher 条件不变。

固定随机种子 `20260828` 从新增关系中分层抽查 30 条，覆盖前置 scope 及六章（4/4/4/4/4/4/6），其中 14 条 page-boundary refs；27 条来自两页重建块、3 条来自页面流恢复后的未拆块。对照原 PDF 正文 marker、上下文与页底注释，未发现误配。三页块没有新增配对正例，另核对 printed 12–14、50–52、196–198 的三个保留负例，没有猜配。抽样不代表全部449条已人工复核。

EPUB 556 noteref/footnote/backlink、0 断链、0 重复 ID；Markdown 556 refs/defs；两者556个连续印刷页锚点及7个编号 scope 保持。前言顺序、父级边界 flush、原库 payload 哈希通过复验。新增 invariant 后 Markdown 与上一轮验收文件逐字节一致。

自动回归包含普通跨页段落、同一句跨页、下一页 marker、多 spans/inline equation、三页保留、证据不足保留，以及注入 missing/duplicated/reordered/changed fragments 后的失败检测与导出中止。另覆盖 source indices、媒体块、标题、页锚点和非 cross-page 输出不变。

完整本地验收材料在 `output/page-reconstruction-final/`，基线及数据流诊断在 `output/page-reconstruction/`；不将原书、数据库或导出全文提交到仓库。既有标点页眉残留另见 [issue](issues/running-header-punctuation.md)。

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

原因包括 `SAME_PAGE_UNIQUE_MARKER`、`DUPLICATE_MARKER_ON_PAGE`、`NO_NOTE_BODY`、`NO_REFERENCE`、`POSSIBLE_CROSS_PAGE_CONTINUATION`、`UNCERTAIN_PAGE_FLOW`、`CROSS_PAGE_SOURCE_BLOCK` 等，没有人为 confidence 分数。仍未重建的明确跨页来源拒绝整个相关块的配对，会连带保留其中可能有效的本页引用；这是当前保守策略的代价。

`source_layout_available` 表示该页有可读取的布局缓存；`source_cross_page` 表示块有明确的跨页来源标记。没有该标记不等于原 PDF 语义已经核实；链接有效也不能替代原书抽查。

## 输出与限制

物理页供内部配对、稳定 ID 和报告追溯使用，不能从最终 `PageMarker` 反推；默认 EPUB/Markdown 只输出已知印刷页隐藏锚点。EPUB 按 scope 显示编号并提供全部前向/返回链接。Markdown 使用稳定且不冲突的脚注标识符，具体阅读器可能仍采用全篇显示编号。

本轮不做跨页续注推断、OCR 错号纠正、裸数字扩展匹配或重复原始引用的猜配。缓存缺失、标题样式模糊、未识别标记等情况仍需人工复核，报告不是“全书恢复率”的证明。

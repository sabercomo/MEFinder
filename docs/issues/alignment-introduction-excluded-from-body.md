# 导论/Introduction 被排除出对齐正文区域

## 现象（事实）

用户对「谁在害怕性别」中文 EPUB ↔ 英文 EPUB 生成对照后，在译本对照里对**导论**中的正文
段落定位，报「所选文字属于副文本区域，请通过人工修正指定对应段落。」。换 MiniLM 重新生成、
且对齐本身成功（v21 completed）后仍不行。

## 根因（事实，2026-09-09 用户真实索引取证）

- `locate` 路径按运行参数里的 `body_ranges[source_side]` 判定：所选 segment 的 order_index
  不在 `[start, end)` 内即判为副文本（`text_alignment.py` `_map_segments_through_run`）。
- 该书中文侧 `body_ranges.pivot = [539, 3918]`，而 order 539 正是**「第一章 全球局势」**；
  order 3 是**「导论 社会性别意识形态和对破坏的恐惧」**，orders 5–538 是导论正文。
- `alignment_body_bounds`（原 `semantic_alignment.py`）用 `min(chapter 标题位置)` 作为正文
  起点，只认编号章（`chapter:`）。导论/绪论/引言这类作者导言既非编号章、也未被
  `_document_heading_positions` 识别，于是整段导论被当作前置副文本排除。
- 英文侧同理：起点被定在 order 500，而 order 22 是「Introduction: Gender Ideology and the
  Fear of Destruction」。

结论：作者导论（书的开篇论证）被错误排除出正文区域，导致导论内所有跨版本定位失败。这不是
OCR/数据问题，也不是之前修的关闭卡死/取消误报问题，而是正文区域检测的结构性缺口。

## 修复

- 新增 `_INTRODUCTION_HEADING`（导论/導論/导言/導言/绪论/緒論/绪言/緒言/引言/introduction，
  允许同段带副标题），把**位于首个编号章之前的作者导论**纳入正文起点候选：
  `start = min(编号章位置 + 导论位置)`。
- 明确**不含** `导读`（编辑导读，属前置副文本，`test_frontmatter_and_spaced_afterword`
  仍判其为副文本），也不改 `前言/序言`（仍作 preface 排除，符合中文学术惯例：导论=正文、
  序/前言=前置）。
- 顺带把 `alignment_body_bounds` 及其区域判定逻辑拆到独立的 `alignment_regions.py`
  （与 `test_alignment_regions.py` 对应），使 `semantic_alignment.py` 回到行数预算内。

验证：`test_alignment_regions` 新增 `test_author_introduction_before_chapter_one_is_body`
与 `test_editor_reading_guide_stays_frontmatter`；对用户真实索引重算，中文侧
`body_bounds` 由 (539,3918) → (3,3918)、英文侧由 (500,4231) → (22,4231)。

## 待验收（推断）

- 需**重新生成对照**后新 `body_ranges` 才生效（存量运行仍是旧范围）。
- 仅对「首个编号章之前的导论」放宽；夹在章节中的「引言」小节因 `min` 取最早编号章位置而
  不受影响。更多书目的回归须在私有金标语料上复核，本机无法执行，用户侧以该书实测为准。

## 2026-09-09 — 后续实证与存量结果修复

事实：用户再次报告同一选句不可对照。真实库仍在读取今早的 v21 结果，中文 order 41 落在旧
正文范围之外；仅更新区域检测源码不能修复已存链接。另有 WKWebView 在 61 秒出现
`Load failed`，导致重新生成流程的界面先失败、后台后完成。实证见
`reports/gender-alignment-recovery-2026-09-09.md` 与
`docs/issues/alignment-webview-load-failed.md`。

后续处理：区域版本升到 2，旧自动范围误排除的正文提示“重新生成对照”；生成使用后台轮询，
重算后再验收真实选句。人工复核范围、置信度阈值和出版方页码规则不变。

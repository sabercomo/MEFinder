# 已知问题：书名页眉的标点变体未归并

状态：已确认既有 cleanup 问题，留待独立修复；不阻塞 export-time page reconstruction。本轮不修改 cleaner。

## 复现与证据

《自由的权利》印刷页228（PDF物理页237，全局page index236）的数组block1为独立 `自由的权利.`，位于导出印刷页229锚点之前。原记录role为header、没有text_level、bbox为`[436,66,590,86]`（1000坐标），parser item index201；layout中是页顶部native block0。

- 原始只读数据库已包含该文字；原PDF渲染也确认它是页眉，不是正文。
- 第一阶段107对脚注的基线Markdown第1709行已存在；页面重建后的556对Markdown第1871行仍存在。
- 重建前后该block除内部追溯用 `_export_index` 外完全一致，没有生成fragment，也未重新分配页面。
- `normalize_heading_text` 做NFKC和空白归一化，但保留句点。页顶部统计中 `自由的权利` 出现267次，被识别为running header；`自由的权利.` 只出现1次，未达到重复检测阈值。

因此这是标点变体未被现有重复文本分组识别，不是reconstruction新增或重新暴露的问题。不得据此在本次修改中增加通用去标点规则，以免影响正文或可信标题。

## 后续验收要求

单独设计并评估页边标点变体识别；覆盖已有书名、句点变体与真实正文/标题的区分，继续保护重复脚注。是否归并必须有页边位置与跨页重复证据，不能只凭去掉标点后字符串相同。

本地完整诊断：`output/page-reconstruction-final/running-header-issue.json`；PDF检查图：`output/page-reconstruction-final/pdf-review/physical-237.png`。

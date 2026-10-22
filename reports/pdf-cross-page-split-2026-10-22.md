# MinerU 跨页合并拆回实证

2026-10-22：MinerU 把跨页段落并进上一页的情况在库内约占 MinerU 页面的一半以上；导入时按 `layout.json` 拆回后，可读的 120 本 MinerU 书中 92.5% 的页面核准为“页内文字就是该页印的文字”。本报告只读用户文献库，未改写任何库数据。

## 背景

MEFinder 的设计是：一段文字的引用页码，就是它所在那一页书上印的页码；PDF 本身缺页时，页码照书上跳号，不做换算。读书笔记里反复出现的“印刷页＝PDF 页−N”“页码偏移”“OCR 质量导致页界错位”等说法，经核查有两个来源：

1. `describe_page_mapping` 以 `offset`（印刷页 − PDF 页号）描述页码区间，扫描缺页表现为 offset“换档”，被照抄成换算式。页码映射本身是对的：例如《人类世的马克思》PDF 缺印刷 pp.160—161，映射从 159 直接到 162。
2. MinerU 解析时把跨页段落合并进前一页的块，下一页开头几行于是挂在上一页页码下。这是解析器的段落合并，不是 OCR 识别质量问题。

## 事实一：合并的普遍程度

- 方法：遍历运行目录下全部 MinerU `layout.json`（`corpus/processed/mineru/results/*/layout.json` 与 `corpus/processed/parser_jobs/*/provider/*/layout.json`），统计 `para_blocks` 中含 `cross_page: true` span 的块。
- 结果：337 个 layout 文件、46,491 页，含跨页合并块 25,642 个，即 55.2% 的页面把下一页的开头并进了本页。
- 例证（《学术与政治》，`pdf-import-cd906ad6fb86a49b`）：“世界的除魅”句带注⑨，注⑨印在 p.168 页下，但旧索引把整句记在 p.167；印刷 p.166 一页在旧索引里只剩页眉“学术与政治”和页码“166”，正文全部并入 p.165 的 §11 段落（该段从 p.165 跨满 p.166，止于 p.167 页首）。

## 事实二：按原始 span 证据拆回

拆分沿用导出重建（`export_page_reconstruction`）的证据规则：合并块中每个 span 都须在合并前的 `preproc_blocks` 中唯一找到所属页，各页片段连续且非空，目标页对应块被标为 `lines_deleted`，目标页内块序可证，拆后逐 span 内容守恒；任何一条不满足即保持原样并计数，不推测。

| 范围 | 合并块 | 拆回 | 比例 |
|---|---:|---:|---:|
| 《学术与政治》，仅两页规则 | 217 | 185 | 85.3% |
| 《学术与政治》，放开为连续多页 | 217 | 215 | 99.1% |
| 《人类世的马克思》，仅两页规则 | 283 | 280 | 98.9% |
| 全库可读 MinerU 书（120 本，28,235 页），连续多页 | 14,894 | 13,484 | 90.5% |

全库口径（新导入流程只读重建页面记录，未写库）：

- 页面状态：`layout_checked` 26,108 页（92.5%），`parser_merged` 1,410 页（5.0%，仍有未能证明的合并），无 layout 证据 717 页（2.5%；其中 3 本整书无 layout，共 451 页，其余是个别分段缺 layout）。
- 未拆原因：`TARGET_BLOCK_ORDER_UNPROVEN` 921、`SOURCE_TEXT_MISMATCH` 301、`MULTIPLE_NATIVE_BLOCKS_ON_PAGE` 67、`NATIVE_SPAN_NOT_UNIQUE` 65、`NOT_CONSECUTIVE_EVIDENCED_PAGES` 38、`SOURCE_OR_TARGET_TEXT_UNALIGNED` 18。
- 不可读：配置中 146 本书的分段清单写的是 Windows 路径（`corpus\processed\…`、`D:\ME_Finder\…`）；统计时把反斜杠换成本机路径后可读其中 95 本，另 51 本清单指向本机不存在的 OneDrive 目录。180 本原生文字层 PDF（pymupdf）逐页取字，不存在此问题。

## 推断

- 推断：笔记中“库本电子文本常把一页正文并入前一页”“长段首尾引文与真实印刷页差 1”的现象，主要来自上述合并；按新流程重建后，这类页界错位在已核准页面上不应再出现。
- 推断：剩余 `parser_merged` 页的主要原因是目标页块序无法由 bbox 唯一对应证明，后续可单独研究，但不得为提高比例而放宽证据。

## 复现

只读脚本按以下步骤即可复现（未入库，属一次性统计）：对 `config/pdf_imports.json` 中每本有 MinerU 清单的书，`load_mineru_segments(config, root)` 后调用新版 `load_mineru_pdf_pages(..., root=root)`，统计 `reconstruct_export_pages` 报告与各页 `page_text_boundary`。全库一次约 80 秒（Apple Silicon，本机运行目录）。

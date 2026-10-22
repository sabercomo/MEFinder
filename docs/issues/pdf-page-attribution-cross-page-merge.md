# PDF 引用页码：页内文字须是该页印的文字

2026-10-22 立项并在 0.6.0 开发线修复（新导入生效；已入库书需重建后生效）。

## 设计原则（用户确认）

引用页码就是那一页书上印的页码（页脚／页眉上的数）。PDF 本身缺页时，页码照书上跳号（如 159 之后是 162），不按 PDF 页号换算，也不把跳号描述成“页码偏移”。

## 事实

- 页码映射本身符合该原则：自动映射按页码序列分段，扫描缺页处另起一段，不插值。例：《人类世的马克思》缺印刷 pp.160—161，映射 159 → 162。
- 读书笔记写出“印刷页＝PDF 页−N”换算式，来源是 `describe_page_mapping` 的 `offset` 字段与 MCP 说明“PDF 页与印刷页偏移”，以及读书笔记技能和 vault 规则要求在核对记录里写换算式。
- 真正的页码错位来自 MinerU 跨页段落合并：下一页开头几行被并进上一页的块，引用时报上一页页码。全库 MinerU 页面中 55.2% 存在这种合并。证据：`reports/pdf-cross-page-split-2026-10-22.md`。
- 合并前的原始 span 证据保存在 `layout.json`：旧云端结果在 `result_dir` 内；新 engine 发布目录只有 `content_list.json`，原始文件在 `corpus/processed/parser_jobs/<job>/provider/<slice>/`。
- 本地 MinerU（`mineru-local`）任务未保留 provider 原始目录，这类书无法按证据拆分。

## 修复（0.6.0 开发线）

- 导入：`pdf_page_split.split_parser_merged_pages` 在 `load_mineru_pdf_pages` 中按 `layout.json` 把合并块拆回各自印刷页（复用导出重建的证据规则，并放开为连续多页），给页面写 `page_text_boundary`（`layout_checked` / `parser_merged`），给拆出的块写 `page_split`。无证据时保持 MinerU 原样、不标记。
- engine 清单新增 `parser_slice_id`；旧清单经只读查询 `data/parser_jobs.sqlite3` 找回切片（`persistence/parser_job_reads.py`）。
- 跨页检索窗口只取正文块：左页取到最后一个正文块，右页从第一个正文块起，避免页眉、页下注、页码隔断跨页句。
- MCP：`describe_page_mapping` 去掉 `offset`，相邻区间印刷页不连续时给 `page_jump`（`missing` 为扫描缺页，`null` 为重新起算）；新增 `page_text` 统计核准页／未拆合并页／未核对页。服务器说明改为“引用页即该页印的页码，不换算”。
- 读书笔记技能与 vault `AGENTS.md`：删去写换算式的要求，改为照书上页码引用，仅对 `page_text` 报告未核准的页的页末引文核对页界。
- 测试：`tests.test_pdf_page_split`（拆回、导出不重复拆、无证据不动、证据不足标 `parser_merged`、经账本／切片号找 layout、跨页窗口跳过页眉注码）；`tests.test_export_page_reconstruction` 新增整页续接三页拆回与目标页不可用时保留；`tests.test_mcp_structure_tools` 改测 `page_jump` 与 `page_text`。

## 未完成与推断

- 未完成：已入库的书仍是旧页面记录，需从缓存重建索引后才按页拆准；重建会改动页面文字，已有译本对齐需重算（库内 174 个对齐运行，无人工修订）。重建前须先处理下一条。
- 事实：本机配置里 146 本旧书的分段清单是 Windows 路径，按原样无法在 macOS 上从缓存读取；把反斜杠换为本机路径后可读 95 本，另 51 本指向本机不存在的 OneDrive 目录。这是既有的跨平台问题，直接在本机重建索引可能影响这些书，须先确认重建流程对它们的处理。
- 推断：剩余未拆合并以 `TARGET_BLOCK_ORDER_UNPROVEN` 为主（目标页块序不能由 bbox 唯一证明），可另行研究；不得为提高比例放宽证据。

## 2026-10-22（续）：旧库刷新前的只读核查与更正

- 更正（事实）：上文“51 本指向本机不存在的 OneDrive 目录”有误。171 本 MinerU 书的解析缓存都在当前数据根 `corpus/processed/` 下；146 本旧书只是清单里记着旧数据根的绝对路径（95 本 Windows：`D:\ME_Finder\…`、`E:\OneDrive\MeFinder\…`；51 本旧 Mac OneDrive 目录）。把 `corpus/processed/` 之后的部分接到当前数据根即可全部读到。
- 事实（逐本只读预演，用新导入代码对 352 本逐本抽取并与现库比对）：路径改接后 352 本全部可抽取；页数无一变化；180 本原生文字层书文字与页码完全不变；164 本 MinerU 书页内文字变化；页码只在 1 本（科尔施《马克思主义和哲学》，旧版导入）变化——序言 26 页由“1、2…”变为现行规则的“序言第1页…”。17 组当前对齐中 10 组涉及文字变化的书。
- 事实：整库重建（`rebuild_local_index`）任一本失败即整体不发布，不会悄悄丢书；但会按新导入重写书目记录（抽样 30 本中 20 本显示书名改变、16 本丢失自动识别的字段来源证据）、清掉 6 个作品的阅读位置、丢失《马克思入门》一组人工核过的正文范围，并在重建事务内联重算全部对齐。推断（未实测）：打包应用主进程不含 fastembed / onnxruntime，内联重算对齐会失败并整体回滚。
- 决策（用户确认）：不整库重建，改为只从缓存局部刷新页内文字变化的 MinerU 书；书目记录、映射记录、导入历史原样写回；页码会变的书跳过（科尔施一本先跳过）；刷新前整库备份一次；之后重算受影响的 10 组对齐。
- 修复：`pdf_extractors.runtime_corpus_path` 把清单里的旧数据根路径改接到当前数据根；`pdf_page_split` 对版面记录覆盖的空白页同样标 `layout_checked`；新增 `pdf_page_refresh` 与命令 `python -m me_finder refresh-page-text --root <数据根> [--apply] [--skip …]`（读旧行在 `persistence/source_rows_reads.read_source_rows`；备份为 `backups/page-refresh-<时间>-index.sqlite3`，不触发 `index-*` 备份轮换）。测试：`tests.test_pdf_page_refresh`、`tests.test_pdf_page_split` 的路径改接与空白页用例。


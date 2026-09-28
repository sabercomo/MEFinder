2026-09-29：v0.5.8 迭代中，尚未发布；MCP 新增只读结构查询已在源码与测试中完成，发布包未构建。

# v0.5.8 迭代说明

## MCP：让 Agent 少试探的只读查询

工具数 13 → 15，全部新增能力只读，不新增写工具，不运行自动检测，不读源文件。

- 新增 `describe_page_mapping`：按连续区间说明一篇文献哪些位置有可引用页码、映射方法与印刷页码起止；PDF 一页对一页的区间给出偏移（印刷页码 = PDF 页号 + offset），区间之间偏移变化即书中换档，双开页等非一页对一页区间不给偏移。判定与检索、阅读结果共用 `page_display.resolve_citation_page`，未校准区间如实标 `uncalibrated`，不推算。
- 新增 `list_sections`：列出与阅读器目录同源的一、二级章节及位置范围；没有可识别标题时返回空列表，不猜章节边界。
- `read_document_window` 新增可选 `section_index`：只读该章节，窗口不越过章节终点，`section.next_start` 用于续读；不传时行为不变。
- `list_documents` 每篇文献新增 `work`：所属作品、是否基准版本，以及组内其他版本是否已与本篇对齐（即 `find_parallel_passages` 能否返回对照）。读取不会为旧库补建作品组表。
- `verify_quotes` 的每个命中新增 `section`（章节标题路径），用于发现“逐字命中但语境不对”的引用；`locate_quote` / `diff_quote` 输出不变。
- 代价：模型可见工具上下文 41,385 → 49,480 字节（+19.6%），见 `docs/mcp-v1-quality-report.md` 2026-09-29 补充。

待发布时处理：`__version__` 仍为 0.5.7；官网 `site/index.html` 仍写 13 个工具（描述的是已发布的 0.5.7），随 0.5.8 发布一起改。

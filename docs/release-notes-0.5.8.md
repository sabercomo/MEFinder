2026-09-29：v0.5.8 macOS arm64 本机验收包已构建（未发布、未上传）；MCP 只读结构查询与书目补全已完成，并用打包侧车在真实库副本上试用通过；Windows 包未构建。

# v0.5.8 迭代说明

## MCP：让 Agent 少试探的只读查询

工具数 13 → 15，全部新增能力只读，不新增写工具，不运行自动检测，不读源文件。

- 新增 `describe_page_mapping`：按连续区间说明一篇文献哪些位置有可引用页码、映射方法与印刷页码起止；PDF 一页对一页的区间给出偏移（印刷页码 = PDF 页号 + offset），区间之间偏移变化即书中换档，双开页等非一页对一页区间不给偏移。判定与检索、阅读结果共用 `page_display.resolve_citation_page`，未校准区间如实标 `uncalibrated`，不推算。
- 新增 `list_sections`：列出与阅读器目录同源的一、二级章节及位置范围；没有可识别标题时返回空列表，不猜章节边界。
- `read_document_window` 新增可选 `section_index`：只读该章节，窗口不越过章节终点，`section.next_start` 用于续读；不传时行为不变。
- `list_documents` 每篇文献新增 `work`：所属作品、是否基准版本，以及组内其他版本是否已与本篇对齐（即 `find_parallel_passages` 能否返回对照）。读取不会为旧库补建作品组表。
- `verify_quotes` 的每个命中新增 `section`（章节标题路径），用于发现“逐字命中但语境不对”的引用；`locate_quote` / `diff_quote` 输出不变。
- 代价：模型可见工具上下文 41,385 → 49,480 字节（+19.6%），见 `docs/mcp-v1-quality-report.md` 2026-09-29 补充。

## MCP：AI 书目补全（经用户确认）

工具数 15 → 17，新增两个带确认门槛的写工具。

- `propose_bibliographic_update`：AI 从原书版权页等读出题录后提议补全，每个字段必须附原书依据原文，可附页码。逐字段预览 fill / same / conflict：只填空字段，已有值即使不同也不覆盖；只记录待确认请求并返回一次性确认码，不改任何题录。
- `confirm_bibliographic_update`：用户明确同意后确认。MCP 进程不写导入配置（其锁只在桌面进程内有效，跨进程写会与导入/保存互相覆盖）；由桌面端后台每 5 秒取已确认请求，走人工保存同一流程写入配置与索引，并在保存锁内按最新题录重新判定只填空字段，用户同时编辑的值不会被覆盖。桌面端未运行时，下次启动写入。
- 写入结果（applied / failed、已填字段、冲突保留的原值）经 `read_bibliographic_metadata.update_requests` 可查。面板来源显示「人工维护」，字段证据记为 `mcp_agent` 并带依据页码与原文。
- 数据库 schema v8 → v9：新增 `bibliographic_update_requests` 表（迁移为纯加法；全量重建时随快照保留）。v9 库不能再被 0.5.7 及更早版本打开。
- 模型可见工具上下文 49,480 → 54,174 字节。

## 试用中发现并修复

- 书目补全写入时原以导入配置记录为“当前题录”：配置记录不全的旧文献，补一个字段会把配置里缺的书名、作者等连同写空。改为以索引中面板所显示的题录为准，配置有而索引缺的值也保留（先写复现测试 `test_sparse_config_record_never_blanks_values_shown_from_the_index`）。
- MCP 服务器版本号原取契约文件的旧值 0.5.1，改为直接报应用版本。

## 打包侧车试用（真实库只读快照的副本）

用 arm64 包内 `MEFinderMCP` 经 STDIO 客户端逐一调用：《法哲学原理》页码映射分 6 段（罗马页、序言页、正文各自偏移）；原句“凡是合乎理性的东西都是现实的”逐字命中于序言第 12 页（已校准），改错一字的版本判为疑似错引并给出原句；从书尾 CIP 页（PDF 第 484 页）提议补出版年 1961，出版社判 same、改动过的译者判 conflict；在副本上确认后由桌面运行时 0.3 秒内写入，配置与索引均更新、证据记录页码与原文。《马恩文集》第 1 卷按章节读取与核对命中的章节路径正常；该卷只识别出 6 个标题，最后一节覆盖其后全部著作，属目录数据限制。

## 构建产物（macOS arm64，本机验收包）

`build_macos.sh` 全量 `unittest` 2643 项通过、28 项环境跳过；逐文件 Node 语法、PyInstaller、ad-hoc 签名、MCP 侧车冒烟、ZIP/DMG 验签与 SHA-256 校验均通过；应用与 MCP 服务器版本为 0.5.8。未 notarize。

| 文件 | 字节数 | SHA-256 |
|---|---|---|
| `MEFinder-v0.5.8-macos-arm64.dmg` | 100237253 | `3e73f677cf8483ef32b9555d46abb909e5afbff945d0e07d2aa82f412547d880` |
| `MEFinder-v0.5.8-macos-arm64.zip` | 93604725 | `0b81fceef1431c2a1d67084de2704bcdf4e1695650c607f7eedb6a8d3ef45fbd` |

本版暂不提供 macOS x86_64 包。

待发布时处理：官网 `site/index.html` 仍写 13 个工具（描述的是已发布的 0.5.7），随 0.5.8 发布一起改。

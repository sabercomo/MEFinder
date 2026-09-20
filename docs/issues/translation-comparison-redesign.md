# 译本对照改版（作品—版本—统一阅读器）

2026-09-18：设计依据为 `DESIGN.md` §3「阅读与对照」「作品版本页」，交互原型为 `docs/design/translation-comparison-prototype.html`（示例数据）。本记录区分事实与推断。

## 动工前核实（事实）

- 仓库此前没有阅读位置存储（无 reading_position / last_read）；reader.js 仅在 localStorage 记忆每本书上次选择的对照目标。
- `alignment_manual_overrides.target_segment_ids_json` 本就是数组，一对多校正已有数据形状；但只有 MCP 写路径（提议 → 一次性 token 确认 → 可撤销），没有 HTTP 接口；空目标会被拒绝。
- 「暂不处理」「不是同一作品」均无存储；同名建议只是前端按标题聚类。
- 已卸载对齐组件时，生成请求已由 `TextAlignmentCoordinator.generate` 以 503 + `component_unavailable` 拒绝；targets / locate / 作品组读写不依赖计算栈，无需新增后端拦截。
- 对齐计算 worker 只发 `compute-start` 与 `result`，没有批次进度；后台任务状态需 job_id，页面刷新后无法发现正在运行的任务。
- `add_group_member` 移动文献时会删除该文献参与的全部 `alignment_runs`，原作品变空不删除。
- 数据库开启外键：删除作品会级联删除该作品全部对齐与人工校正，删除后无法通过重建作品恢复。

## 决定（2026-09-18 用户确认）

- schema v7 新增三张表，新增只读概况 / 链接窗口 / 候选接口与校正、暂缓、阅读位置、忽略建议、批量移动写接口。
- 删除作品的「撤销」：界面先隐藏，提示条消失后才真正调用删除；期间撤销则不发请求。
- 移入其他作品会丢失原对齐，确认提示中写明。

## 未完成与限制

- 「生成中 N%」：没有真实批次进度来源，界面只显示「生成中」。补进度需要改 worker 协议，而已安装的独立运行时自带 worker 代码，需要单独立项并考虑协议兼容。

## 实施后核实（2026-09-18）

事实：

- 在真实书库的 APFS 克隆副本上（`serve --index <副本>`，未触碰正式库，副本迁移到 v7）走通：译本对照页 8 部作品与版本清单；两版对照阅读（法哲学原理 Nisbet 英译 ↔ 德文原版）定位、点段落高亮对应段、「!」弹层候选（当前对应已预勾）、暂不处理后变灰且刷新后保留；左右版本下拉的 listbox 语义与方向键 / Escape；跳到页；返回后侧栏恢复；文献库多选 →「加入作品…」→ 提示条「前往译本对照」；未对齐版本对的右栏原位生成面板；管理版本抽屉搜索添加、删除作品撤销与超时提交；同名核对「不是同一作品」持久化；检索 →「查看结构化文本」返回标签为「检索结果」；模拟组件不可用时入口标「只读」、生成按钮禁用并写明原因。
- 浅色（晴蓝）与深色（午夜）主题、720px 窄窗口下无横向滚动；版本表按作品区宽度（容器查询）收列。
- 真实库里低置信 `rejected` 链接很多，且大量没有目标段（副文本）：最初把 rejected / unmatched 全部标「!」时，一个 60 页窗口出现 790 个。已收窄为「rejected 且有目标段、无人工校正」，同窗口正文区约 44 个；「N 处待检查」用同一口径。
- 真实库 17 个完成的对齐 run 使用 multilingual-e5-large（其中 2 个算法版本 20，已不可读）。预览进程的运行根是仓库目录，偏好为 MiniLM 且未下载模型，所以预览中所有对显示「需重新对齐 · 模型已更换」、生成按钮显示「需先在设置中下载对齐模型」，这与该运行根的真实状态一致。

未验证：

- 预览运行根没有已安装模型，没有在界面里实际跑一次生成对齐 / 取消的完整链路（接口与轮询逻辑沿用原有 `/api/text-alignments/start|status|cancel`）。
- 独立阅读窗口的「在新窗口打开 / 回到主窗口」需要 pywebview 桌面壳，只有 `ReaderWindows` 单元测试覆盖，未在冻结包里点过。
- Windows WebView2 未看。

推断：

- 「已匹配段落 N%」的分母包含被质量门拒绝的副文本段落，副文本多的书比例会偏低；这只影响数字大小，不影响「不代表准确」的定性。

## 2026-09-18 — 审核修复与阶段计划调整

事实（复现与验证见 `reports/translation-reader-review-2026-09-18.md`）：

- 整条链接包含多个源分段时，阅读器校正原先无法命中滚动产生的单字符选区。现在精确选区校正优先；否则允许 `reader_review` 校正作用于其包含的选区，优先更小的范围，同范围按确认时间选取。跨出校正范围不套用；MCP 提案仍保持精确选区语义，撤销及分段版本失效规则保留。
- 关闭或切换对照后，过期定位返回 `false` 不再触发「从开头打开」回退。
- 对齐任务成功后使当前作品的链接缓存失效并重新定位；基准分支重算也会刷新间接对照。旧轮询响应不能清除后来任务的状态。
- 右栏定位不再重置左栏到仍可见的搜索高亮；定位成功后清除先前定位失败的提示。跟随仍为左栏驱动右栏。

更新后的计划：

1. 验收基线：保留已有数据与性能证据，新增本次阅读行为回归。
2. 独立计算组件：主体沿用已有成果；Windows 发布冒烟失败按用户决定留给 Windows 本机验收。
3. 改为「译本对照工作区 + 统一双语阅读器」，文献库保留快捷加入作品，共用作品数据；独立窗口承载整个阅读器，不做两窗口联动。本次修复不等于正式冻结包桌面验收完成。
4. batch16 继续为可跳过的独立实验，默认 batch64 不改。
5. 业务重构随当前问题推进，不另开全仓拆分。
6. 阅读流程稳定后再推进独立后端生命周期与 macOS 原生宿主。

用户提出章节目录，明确先修审核问题：目录本轮未实现。已核实 `document_heading.py` 的入库标题层级与 `markdown_export_normalize.py` 的 `trusted_heading` 可作为后续复用依据；实现前需核对 PDF/EPUB 的标题到页码及字符锚点，不重新解析源文件。候选交互为一、二级目录定位左栏，再经对齐关系定位右栏；缺对应时明确提示，不按两书章节序号硬配。

## 2026-09-18 — 后续授权实现章节目录

用户随后明确要求实施。目录入口、只读数据接口、双栏章节跳转及回归已落地；复用规则、旧数据限制与浏览器证据见 `reader-chapter-navigation.md`。这属于第三阶段阅读功能补充，未恢复原双窗口联动方案。

## 2026-09-19 — 启动与结构化阅读延迟复审

事实：延迟 1.2 秒启动 overview 未消除其持索引状态锁的全库统计耗时，阅读器也在正文前调用全库统计。已拆轻量状态与按需逐对统计，正文优先，未改变索引锁或结果计算。诊断/完整响应一致性及限制见 [报告](../../reports/startup-reader-latency-2026-09-19.md)。GLM 的滑杆递归修复保留；“单线程 HTTP”说法更正为共享索引状态锁导致排队。

## 2026-09-19 — 作品页对齐状态常显与全部重新对齐

用户反馈两点：看不出「对照阅读」是否就是「对齐」；换模型后一键重新对齐的按钮不见了。

事实：旧版文献库的「重新对齐已有译本（N 组）」（`7adac7f` 引入）在 `646a325` 移除文献库作品组 UI 时一并删除，新作品页没有替代；对齐状态只在勾选两个版本后的底部栏和「管理版本」抽屉里出现。后端 `/api/translation-works/overview?include_statistics=0` 已为每对版本返回 `status` 与 `stale_reason`，无需新接口。

处理：先出可交互效果图确认方案（用户确认后实施），再改 `35-works.js` / `45-works.css`：逐对常显对齐状态的「对照」区取代勾选 + 底部栏；作品列表顶部「全部重新对齐」前端队列（沿用原 pivot/target、`force: true`、逐个等待任务结束、可停止）；设置切换模型后 `MEFinder.works.invalidate()`。DESIGN.md §3「作品版本页」同步改为逐对列出。守卫见 `tests/test_translation_works_frontend.py` 的 `test_every_pair_shows_its_alignment_state_without_picking`、`test_stale_alignments_can_be_realigned_in_one_batch`、`test_realign_queue_runs_pairs_in_order_and_stops_on_cancel`。

限制：队列只存在于当前页面内存，刷新后正在跑的那一组仍会被接回显示，但剩余组需要再点一次；批量重算的真实耗时未在本轮测量（本机开发环境未装对齐模型，浏览器验证用模拟任务接口）。


## 2026-09-20 — 第五阶段业务重构（一）：校正读取

按「业务重构随当前问题推进，不另开全仓拆分」推进第一项：人工校正的读取。

事实（改前，均已复现于单元测试）：

- 同一条 `alignment_manual_overrides` 记录有三套读取规则：`text_alignment._lookup_confirmed_override`（精确 + `reader_review` 包含回退 + 目标分段集过期判定）、`translation_works.alignment_link_window`（只有精确命中）、`translation_works._override_keys`（只有精确命中，且不校验目标分段集与目标段是否仍在）。
- 可观察后果：阅读器按人工校正定位成功的链接，逐段「!」与作品页「N 处待检查」可能仍当它没改；重新分段后已失效的校正仍被统计算作「已处理」。
- 「待检查」的判定另有两份实现：后端 `_direct_run_statistics` 与前端 `reader.js` 的 `linkNeedsReview`。

处理：

- `text_alignment.confirmed_overrides_for_pair()` 承担唯一读取（含过期判定），`override_for_selection()` 承担唯一选取规则（精确优先，`reader_review` 可作用于所含选区）；定位、链接窗口、逐对统计三处改调同一对函数。分层不变：核心模块不反向依赖 `alignment_overrides.py`。
- 「待检查」收敛为后端一个谓词 `_link_needs_review`（rejected + 两侧非空 + 两个方向都无有效校正），链接窗口每条返回 `needs_review`，前端只读不再自算。
- 行为变更一处：精确命中的校正若已过期，现在会回退到仍然有效的、范围更大的阅读器校正，而不是直接退回算法结果——只使用当前分段集内仍然成立的校正，不触碰「页码/定位不虚构」。

守卫：`tests/test_translation_works.py` 新增三例（校正后三处一致、从另一版改也算已处理、重新分段后三处同时判过期）；前端口径由 `tests/test_structured_reader_frontend.py` 钉死。契约见 `docs/contracts/v0.5.5-alignment-corrections.md`。

未做（下一步，用户已排序）：② 任务完成刷新（`35-works.js` 与 `reader.js` 两个独立轮询器）、③ 阅读会话状态（位置 / 对照目标散在五处）。

## 2026-09-20 — 第五阶段业务重构（二）：任务完成刷新

事实（改前）：

- 后端一次只有一个对齐任务（`text_alignment_controller` 单 `_job_id`），前端却有两个轮询器：`35-works.js` 的 `watchRunningJob` 与 `reader.js` 的 `pollComparisonAlignment`，各自分类结局、各自弹提示、各自决定刷新什么。
- 阅读器关闭时 `onReaderOpenChange` 会 `invalidate()` → `loadGroupsAndOverview()` → 读 `/current` → 作品页接管阅读器发起的那个任务，于是同一次生成弹两次提示。
- 反向缺口：在阅读器里生成的对齐、保存的校正，都不会立刻更新作品页的逐对状态与「N 处待检查」。

处理：

- 监听收归 reader.js 一份（`index.html` 与独立阅读窗口都装 reader.js，作品页模块只在主窗口里有——方向只能这样）：`MEFinderReader.alignmentJobs.watch/subscribe/running`。已在监听的任务不改归属，后认领者共享同一份监听；结束后广播 `{jobId, meta, outcome, error}`。
- 提示只由 `meta.origin` 指向的一方给出；作品页与阅读器各自按同一次事件刷新自己的视图。作品页删除 `watchRunningJob` 与 `POSITION_POLL_MS`。
- 新增宿主回调 `onAlignmentDataChanged`：阅读器保存校正/暂缓后作品页立即失效逐对统计。

守卫：`tests/test_reader_comparison_state.py` 的 `test_one_job_keeps_one_watcher_and_its_first_owner`（两处认领同一任务只轮询一次、只广播一次、归属归第一个认领者、非发起方不提示）与改写后的 `test_completed_alignment_invalidates_links_and_relocates_open_pair`；`tests/test_translation_works_frontend.py` 断言作品页不再出现 `/api/text-alignments/status` 且订阅共享监听。

浏览器核实（`serve` + 预览库，用注入的 fetch 桩模拟任务状态，未真跑模型）：同一任务两处认领 → 状态请求 1 次、提示 1 条、归属 `works`；阅读器发起的任务结束后作品页重读一次 overview；控制台无报错。

未做：③ 阅读会话状态。

## 2026-09-20 — 第五阶段业务重构（三）：阅读会话状态

事实（改前）：

- 一次阅读的位置有五处记录、三种形状：地址栏深链（只有左栏 + 锚点）、`state.lastSession`（另写一份字面量）、`currentLocationOptions()`（交接新窗口用，含右栏）、服务端 `document_group_reading_positions`（含右栏，按作品）、`works.positions` 缓存。
- 具体缺口两处：① 独立阅读窗口重载或地址栏刷新后右栏丢失（深链不带对照目标，`restore()` 恢复不出来）；② `onReaderOpenChange` 关闭时清空 `works.positions` 再 `setTimeout(…, 400)` 重新查询，赌 `closeReader()` 里那次 fire-and-forget POST 已经落库。

处理：

- `currentReadingSession()` 成为唯一的会话形状（sourceId / title / targetIndex / anchorId / groupId / compareWith），深链、`lastSession`、「在新窗口打开 / 回到主窗口」、服务端位置四处共用。
- 深链新增 `c` 参数（对照版本，沿用 source 的 id 校验，等于自身或重复即整条链接作废）；`noteReadingSessionChanged()` 把「开关右栏」同时反映到地址栏与服务端位置。
- `saveReadingPositionNow()` 返回刚写出的记录（形状与 GET 响应一致），`closeReader()` 经 `config.onOpenChange(false, savedPosition)` 交回宿主；作品页直接采用，删除清空 + 定时重查。
- 恢复优先级在 `currentReadingSession()` 上方注释成文：显式 options > 深链 > lastSession > localStorage 对照记忆；阅读器不主动读服务端位置跳转（跳不跳由入口决定），本轮不改这一点。

守卫：`tests/test_reader_comparison_state.py` 的 `test_deep_link_carries_the_comparison_pane`、`test_closing_hands_the_position_it_just_saved_to_the_host`（都执行真实函数体）；`tests/test_structured_reader_frontend.py` 与 `tests/test_translation_works_frontend.py` 钉死会话形状、`c` 参数与「作品页不再出现 works.positions = {} 与 setTimeout」。

限制：本轮浏览器只做了加载冒烟（预览库为空，没有可打开的文献），会话行为的证据来自 Node 执行真实函数体的回归。真实库副本上的「刷新保留右栏 / 关闭后继续阅读」仍待在有数据的环境验收。

三条线到此收敛完毕（① 校正读取、② 任务完成刷新、③ 阅读会话状态），业务重构未扩大到全仓拆分。

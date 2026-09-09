# MEFinder v0.5.3

2026-09-09：修复作品组译本对齐约一分钟后误报 `Load failed`，并补齐导论旧对齐结果的恢复路径。
MiniLM／E5 在《谁在害怕性别》中英 EPUB 与 MinerU PDF 的三种配对上均生成成功；
24 次开篇与截图选句双向定位检查通过。E5 仍为实验档，尚未正式发布。

## 2026-09-09：长任务超时与导论存量对齐恢复

- 复现 WKWebView 本地长请求在 61.006 秒返回 `Load failed`，后台却继续计算。
  生成改为后台任务与每秒短请求查询状态；相同运行中请求去重，取消和真实错误照常返回。
  70 秒桌面等待实验在 70.284 秒收到成功结果，没有提前报失败。
- 旧自动正文范围误排除正文时，提示重新生成，不再误导用户去人工修正正文；
  区域版本升为 2，防止生成复用旧范围。人工复核范围和置信度门槛保持原样。
- 新增 `/api/text-alignments/start`、`/api/text-alignments/status`，原同步 `/generate` 兼容。
  见 `docs/contracts/v0.5.3-alignment-jobs.md` 与 `v0.5.3-http-api.json`。
- 真实库副本六组生成、24 次定位检查通过；全量 unittest 2082 项通过（21 skip），
  ruff 与前端守卫通过。详细证据见 `reports/gender-alignment-recovery-2026-09-09.md`。

## 2026-09-09：对齐正文区域纳入作者导论

- 修复：作者**导论/绪论/引言/Introduction**（位于首个编号章之前）此前被当作前置副文本
  排除出对齐正文区域，导致导论内的跨版本定位报「所选文字属于副文本区域」。现将其纳入
  正文起点；`导读`（编辑导读）与 `前言/序言` 维持原判定。详见
  `docs/issues/alignment-introduction-excluded-from-body.md`。
- 区域判定逻辑拆到 `alignment_regions.py`（对应 `test_alignment_regions.py`）。
- 注意：需**重新生成对照**后新的正文范围才生效（存量运行仍是旧范围）。

## 2026-09-09：对齐卡死修复与界面修正

- 译本对齐的 ONNX 推理线程数改为「留出余量」：Apple Silicon 上限为性能核数减一
  （如 M4 的 4 性能核 → 3 线程），其他平台为逻辑核数减二。此前固定取到 8，在只有
  4 个性能核的 Mac 上过度占用，SME 矩阵核把每个性能核跑满、饿死 WindowServer，
  导致整机转彩虹圈。Windows 核多且无此系统级卡顿，故此前未暴露。
- 对齐与「重新对齐已有译本」运行中按钮翻转为「取消对齐」，可随时停止；后端在嵌入
  批次边界协作式取消（新增 `POST /api/text-alignments/cancel`），取消不写入结果。
- 运行时策略拆入 `embedding_runtime.py`（线程预算 + 取消原语），与对齐算法模块分离。
- 界面：文献检索设置的「繁简统一检索」补齐图标／勾选槽，修一字一行塌缩；按页导出
  对话框改主流打印式分段控件（原书页码／PDF 物理页码）并统一间距与聚焦态。

## 本次变更

- PDF 重复页眉清理保留正文 `§N`／`第N节` 结构标记，不再把连续节号数字归一后误删。
- 标题锚点先在完整文档上提取，再映射进复核正文切片，使目录可恢复 CJK 无编号／并段章标题。
- 书中间的 `注释` 不再提前截断后续章节扫描；尾注排除门保持生效。
- 分段器版本 12→13，标题锚点修复将对齐算法 20→21；软锚点筛选接入后再升至 22。
  v20／v21 对齐配方继续可恢复，v21 已存结果继续可读。
- Windows 发布冒烟产物名同步到 v0.5.3；正式产物与哈希留待发布构建后写入总发布说明。

## 验证摘要

- 三类最小复现夹具先红后绿，相关对齐模块 78 项测试通过。
- 与 CI 一致的全量 `unittest` 1994 项通过、1 项按既有条件跳过；跟踪 Python 文件 Ruff F 规则全绿。
- 生产索引副本 20 本重新分段、17/17 个 E5 run 重跑且 SQLite `quick_check=ok`。
- R9 chapter 锚点 0→6、unmatched 3913→170；n93 错节候选退出错误 automatic，并落入正确
  §202↔§204 走廊。
- 37/34/50/100 四套既有金标中所有“正确”样本保留，已排除错配无回潮。详见
  `reports/issue-17-heading-anchor-validation-2026-09-07.md`。

## 2026-09-08：PR #21 繁简统一检索（开发中，未发布）

- 默认启用 OpenCC 查询变体，在“设置 → 文献检索”即时切换；不转换源文档或导出内容。
- 修复多变体排序、字符区间去重、限额与截断统计；兼容存量索引。
- 固定 OpenCC 0.1.7，补 Windows/macOS 词典收集与第三方许可。
- 验证详见 `reports/issue-16-script-search-validation-2026-09-08.md`；macOS arm64 成品构建已通过。
  大库性能已实测(63,344 段落 / 64 部快照副本):精确/短语检索双轨开销可忽略(≈0.1ms),
  宽泛高频词最坏约翻倍(p50 594→1224ms,~2.07x)仍在 ~1.2s 交互区间,开关前后命中 0 变化;
  详见 `reports/script-search-large-library-perf-2026-09-09.md`。

## 2026-09-08：Markdown 按页导出（开发中）

- 文献库增加“按页导出 Markdown”，支持原书数字范围、不连续页、单独页码标签，以及 PDF 物理页。
- PDF 先恢复整书脚注关系，再按正文来源选页；只带出所引用定义，保留章末组织及链接。
- 独立文件名保留整书导出；范围缺失/歧义、合页半页或无法重建的跨页文本明确报错。
- EPUB 只接受出版方页码，明确提示原始超链接脚注关系尚未入库。
- 详见 `reports/markdown-page-export-validation-2026-09-08.md`；macOS arm64 成品构建已通过，Windows 成品尚未验证。

## 2026-09-08：锚点候选接入后的版本与试用验证（未发布）

- 为已接入正式调用链的软锚点筛选补齐版本：对齐算法 21→22，语义参数 19→20。
- v21 已存结果继续可读；重新生成不再复用 v21。v21 配方仍可恢复，恢复时使用当前算法。
- 正式入口在新建副本上重算 16 个书对，完整链接成员、接受状态、分数及标题锚点均与保存的最终候选一致。
- 全量 unittest 2051 项，结果 OK（条件跳过 50 项）；Ruff F 全绿。跳过原因及证据见
  `reports/alignment-v22-trial-validation-2026-09-08.md`。
- 已准备本地粗定位试读入口；本次未重算生产库，未改变默认模型，未引入翻译模型或继续扩大 DP 实验。

## 2026-09-09 17:27：旧 macOS arm64 候选包（已被本日晚间修复包替代）

- 使用 Python 3.12.10 arm64 与 PyInstaller 6.21.0 运行官方 `build_macos.sh`；全量
  `unittest` 2078 项通过，21 项按条件跳过，Ruff F 与全部前端 JavaScript 语法门禁通过。
- 主应用与 `MEFinderMCP` 均为 arm64；独立扫描包内 127 个 Mach-O，无 x86_64。
  ZIP 解包与 DMG 挂载后的严格签名、sidecar 冒烟、DMG 校验、Applications 捷径、
  AppleDouble 与私人状态文件反查均通过。
- ZIP：165,962,362 bytes，SHA-256
  `4a15e83ca3d4066182e927953b7bf4206659a5b9ab37ff21228c638a60f8ec18`。
- DMG：176,519,724 bytes，SHA-256
  `92ad9073cacc3a342d6f57f7afc594483858a7a729717e6f0fa1d1af2f062e80`。
- 当前是 ad-hoc 签名的本地验收候选；未做 Developer ID 公证，未构建 Windows 产物，未发布 Release。

## 2026-09-09 20:37：当前 macOS arm64 修复包（未发布）

- 官方构建完整测试 2082 项通过（21 skip），93.898 秒；主应用／MCP sidecar、Node 语法、
  严格签名、ZIP 解包、DMG 挂载与拖出副本、checksum 门禁通过，Ruff F 全绿。
- ZIP：165,965,692 bytes，SHA-256
  `947ab126184360b75e5b06a6659d05cf71e3442fcd2e7c422be7d64fa62bdee6`。
- DMG：175,840,379 bytes，SHA-256
  `237a09388d56b5c69b8ee93999d98d0a952fbe6a3b217e7fa39d19b2c99c4456`。
- 已安装到本机 `/Applications/MEFinder.app`；旧应用与数据库已分别备份。
  原生界面实测中文 EPUB 导论显示英文对照，作品组 PDF→英文 EPUB 后台生成完成并恢复按钮。
- 本地修复候选，ad-hoc 签名；未构建 Windows 包，未合并 PR、打 tag 或发布 Release。

## 2026-09-09 23:16：Windows x64 候选包（含译本对照两处修复，未发布）

- 在译本对照两处修复(默认目标记忆、两跳中转提示+一键直接对照)之后重建;
  官方 `build_windows_installer.ps1` / `build_portable_release.ps1`，Python 3.12.14 x64
  与 PyInstaller 6.21.0；全量 `unittest` 2084 项通过、1 项按条件跳过(装 OpenCC 后其余
  条件跳过项均执行)，全部前端 JavaScript `node --check` 通过。
- 主应用与 `MEFinderMCP.exe` 均由 PyInstaller 生成；打包后 MCP sidecar STDIO 冒烟、
  空索引 FTS5 trigram 校验、隐私/许可材料门禁通过。安装包经 Inno Setup 编译。
- 安装包：`MEFinder-v0.5.3-windows-setup.exe`，140,329,373 bytes，SHA-256
  `502ef4b09385f1b3fef8be9831c6e0848d4e9c3a81c45e24b0f1e728d95449f8`。
- 便携包：`MEFinder-v0.5.3-windows-portable.zip`，164,119,527 bytes，SHA-256
  `d85db9577334466caba8de5eed89811a570b904aa0700b012ba692832e3aadb3`。
- 本地验收候选，未做代码签名，未合并 PR、打 tag 或发布 Release。
- (前一版 21:30 候选构建于 `c9c6de1`，早于上述阅读器修复，已被本次重建取代。)

## 2026-09-09：译本对照两处修复

- **默认对照目标记忆**:此前默认取对齐目标列表首个(按作品组成员顺序,常是英文版),每次打开
  都要手动切回想看的版本。现按源文献 id 用 localStorage 记住上次选择,优先级:正在对照的版本 >
  本书上次选择且仍有效 > 成员顺序首个。
- **两跳中转对照提示 + 一键直接对照**:当两个非基准版本(如中文 PDF 与中文 EPUB)之间没有直接
  对齐时,阅读器原先经基准(英文)两跳中转,交集丢弃大量段落——即便两侧是一比一同一译本也「漏配」。
  现检测到走 `via_source_file_id` 中转即提示「经…中转,可能漏配」,并提供「生成直接对照」一键在两
  版本间直接对齐(后台任务 + 轮询,完成后刷新并重新定位)。实测直接对齐覆盖率 100%(中文 PDF↔中文
  EPUB,4000/3951 段 0 未覆盖)。根因分析见 `reports/gender-zhzh-alignment-routing-2026-09-09.md`。
- `/api/text-alignments/targets` 响应新增 `document_group_id`,供阅读器发起直接对齐。

## 2026-09-09：候选汇总议题(#23)未决项收尾

- **繁简大库性能**:已实测(见上「Markdown/繁简」段与 `reports/script-search-large-library-perf-2026-09-09.md`)。
- **E5 四条争议样本**:已逐条读原文判定(不再悬置)——4 条在粗定位标准下均可接受、无严重错配,
  根因是 EPUB 内联脚注 vs PDF 译本重定位脚注的**分段不对称**,**非 E5 阈值问题**;故 **E5 仍维持
  实验档**,不据此宣称新增准确率。详见 `reports/e5-disputed-four-adjudication-2026-09-09.md`。
- 余下 macOS Developer ID 公证与正式 Release 发布仍为未决项,不在本地候选范围内。

## 2026-09-10 01:13：前一版 macOS arm64 候选包（已被页码文案修订包替代）

- 基于 `80ecc93`重建，已包含译本对照默认目标记忆与两跳中转提示／一键生成直接对照。
- 官方 `build_macos.sh` 完整测试 2084 项通过（21 skip）；Ruff F、全部前端 JavaScript 语法、
  主应用／MCP sidecar arm64、严格签名、ZIP 解包、DMG 挂载与拖出、checksum 门禁通过。
- ZIP：165,967,350 bytes，SHA-256
  `d982123f197bb5d6a1ced26a1f4b3fdea16d6c93a1178c390803e371babaff75`。
- DMG：175,841,115 bytes，SHA-256
  `7a2ab82c1dded37c70f208e50e19d6e29deb4c0213c581a7ad310375911223e0`。
- 已替换并启动本机 `/Applications/MEFinder.app`；原生界面显示 `MEFinder v0.5.3`。
  当前仍为 ad-hoc 签名候选，未做 Developer ID 公证，未创建 tag 或正式 Release。

## 2026-09-10：按页导出的页码说明

- 页码输入框同时示例单页、连续范围和组合输入：`5, 12-18, 25`，不再让用户猜测是否支持单页。
- “页码按”改为“页码依据”；选择原书页码时说明印刷页及罗马数字标签，选择 PDF 物理页时说明从文件第 1 页计数。

## 2026-09-10 01:46：当前 macOS arm64 候选包（未发布）

- 包含按页导出页码说明修订；已在源码预览和安装后原生应用中验收单页示例、组合输入与页码依据动态说明。
- 官方 `build_macos.sh` 完整测试 2085 项通过（21 skip）；Ruff F、前端 JavaScript 语法、
  主应用／MCP sidecar arm64、严格签名、ZIP 解包、DMG 挂载与拖出、checksum 门禁通过。
- ZIP：165,967,546 bytes，SHA-256
  `b293b191f5c512976c13a0be9e272c1abafb137461e62fa1e9acdeaa8218c68e`。
- DMG：175,841,243 bytes，SHA-256
  `b9773adcd3683176b6ac9205e3c1a24629809665a6156238d2638ed748782fdc`。
- 已替换并启动本机 `/Applications/MEFinder.app`；替换前应用备份位于
  `~/Library/Application Support/MEFinder/app-backups/20260910-014701/MEFinder.app`。
  当前仍为 ad-hoc 签名候选，未做 Developer ID 公证，未正式发布。

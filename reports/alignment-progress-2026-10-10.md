# 对齐进度显示核验

日期：2026-10-10；环境：Windows，项目 Python 3.12.14，Chrome / Playwright。

## 进度与估时

计算 worker 的原有 NDJSON 通道现在报告真实计数：文本计算按未缓存的去重文本批次，段落匹配按正文源段落处理位置。主进程根据当前阶段的计数增量与单调时钟计算平均速度，仅估算该阶段剩余时间。准备、模型加载、检查和保存没有可测分母，不显示虚构百分比。

缓存完整命中时跳过文本计算。阶段切换重置速度样本；未获得有效速度时显示“剩余时间正在估算”。算法、阈值、结果定位信息、身份核验与数据库 schema 均未改变。

## 自动化与可见行为

- 全量 unittest：2,750 项通过，27 项条件跳过，227.212 秒。
- Chrome 回归：9 项通过，29.791 秒。新增用例核验文本计算百分比及时间、匹配阶段重置、保存阶段移除进度条、结束清除任务状态，并确认进度更新时取消按钮保留焦点。
- 跟踪 Python 文件及新增测试 Ruff(F) 零告警，修改的 JS 语法通过；前端指纹为 1,519,152 字节 / `6391d42703ed064d5a446c9537859b025a827010e2256d9b669d24819a0d8aaf`。
- 新增 4 个行为测试验证 HTTP 估时与刷新快照、真实批次计数、缓存跳过与算法结果一致、worker 消息到 runner 回调的转发；任务服务的既有 Node 测试同时核验进度广播不重复触发终态。
- 真实 E5 小样本：既有独立运行时完成 6 条中英合成文本计算，报告 `embedding: 0/6 → 6/6`、`matching: 0/3 → 3/3` 及检查阶段，返回 1 条 link。这个样本只核验计算链路及进度，不证明整书对齐质量。未重跑真实库对齐。

目视截图与日志：`.codex-tmp/alignment-progress-20261010/progress.png`、`model-smoke.log`；源码 / 浏览器日志分别为 `.codex-tmp/alignment-progress-tests.log`、`.codex-tmp/alignment-progress-browser.log`。截图中的 42% / 2 分钟来自受控浏览器测试响应，用于展示和验证 UI，不是《异化》的真实进度。

## 本机程序交付

官方 `build_windows_dist.cmd` 在完整独立检出通过：2,750 项测试，45 项条件跳过，224.745 秒；逐文件 Node 语法、桌面主程序与 MCP 侧车打包、空库 FTS5 / 短词索引、MCP 冒烟均通过。

交付目录：`dist/MEFinder-progress-20261010/`，主程序为 `文献原句定位器.exe`。`data_root.txt` 与原 `dist/MEFinder/` 逐字节一致，仍指向 `D:/ME_Finder/dist/MEFinderData`；原程序与活动桌面 / MCP 进程保留。先关闭旧程序，再启动新程序使用原文献库。

包内 3 份前端资产与 7 份相关 worker 源文件逐字节等于已测源码；主程序 PYZ 中的控制器包含 `_update_progress`、`progress_callback` 与状态快照。主程序 SHA-256：`586be5e4cfa54eb9f6940baae0360453bf6eee73f736ca7a13dc066b05693cca`。

包内外置 worker 在既有独立 Python / E5 模型上完成 4 条合成文本，实际报告 `embedding: 0/4 → 4/4`、`matching: 0/2 → 2/2`、检查阶段并返回 1 条 link。包内核验日志为 `.codex-tmp/alignment-progress-20261010/package-verify.log`。未重打 release 安装包、便携包或 macOS 包，未正式发布；原生窗口与整书进度的人工验收留待用户。

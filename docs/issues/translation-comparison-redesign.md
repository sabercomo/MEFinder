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

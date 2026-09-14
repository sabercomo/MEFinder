# 阶段2B 对齐计算组件安装管理 —— 验收报告

- 日期：2026-09-14
- 分支：`codex/v0.5.4-integration`
- 结论：**源码层与生产启动接入完成并通过全量门禁；跨平台真机安装与冻结冒烟未做，设置页 UI 未做。** 不具备声称“跨平台/端到端交付完成”的证据。

## 1. 已实现（源码层）

- `managed_alignment_runtime.py`：`ManagedAlignmentRuntime` 组件——uv 隔离 venv 安装/升级/卸载/验证、staging→publish→previous 原子换装、回执与 identity、跨进程操作锁、崩溃恢复、计算中延迟卸载、卸载删模型边界。
- `managed_component_assembly.py`：受管组件装配从 `web_runtime.py` 抽出（web_runtime 735→690 行，回到 725 预算内；`test_architecture_boundaries` 绿）。
- `local_ocr_manifest.json` + `component_catalog.validate_component_catalog`：新增并校验 `alignment` 运行时块。
- `embedding_runtime.py`：新增计算活跃计数（`enter/exit/embedding_run_active`）。
- `managed_embedding_models.py`：新增 `delete_all_models()`（组件卸载时删所属模型）。
- `text_alignment_coordinator.py`：`build_compute_runner` 优先用已装独立运行时启动 worker；计算期 enter/exit 活跃计数；`resolve_installed_runtime_launch` 只读解析。
- HTTP：`/api/text-alignment/runtime`（GET summary / POST perform），`http_contract.py` + `docs/contracts/v0.5.4-http-api.json` 同步。

## 2. 验收场景与本轮覆盖

| # | 场景 | 状态 | 证据 |
|---|---|---|---|
| 1 | 无组件：核心导入/搜索/定位/阅读/导出/已有对齐可用；新计算明确提示安装 | 源码保留（2A 门 + 503 提示）+ 无栈冷启 | `test_core_without_alignment`、`test_alignment_component_isolation`、2A `TextAlignmentComponentUnavailable` |
| 2 | 主程序无计算依赖，装独立组件+模型后离线生成发布 | **部分**：启动解析 + 无栈拒绝验证已测；**真机装真栈后离线生成未测** | `test_managed_alignment_runtime`、`resolve_installed_runtime_launch` |
| 3 | 安装失败/中断：重启不误报已装、可恢复重装、已有版本不损 | 源码层已测 | `test_half_written_directory_is_not_reported_installed`、`test_receipt_without_interpreter_is_not_installed`、`test_install_cleans_stale_staging_directory` |
| 4 | 升级失败：旧版仍可用；新版实际验证通过前不删唯一旧版 | 源码层已测（staging 验证后才 publish；失败回退 previous） | `_install` publish 顺序 + `test_update_available_when_pins_change_and_update_swaps` |
| 5 | 计算中卸载：当前任务按约定结束后自动卸载；新任务不进；成果保留 | 源码层已测 | `test_uninstall_defers_until_compute_task_finishes` |
| 6 | 卸载后重装：功能恢复、状态准确 | 源码层已测 | 安装/卸载/再装状态 = 回执+解释器真值判定 |
| 7 | 多实例同时操作：不互删安装/模型/临时目录 | 源码层已测（跨进程文件锁） | `test_cross_process_lock_blocks_concurrent_operation` |
| 8 | 组件崩溃/取消/应用关闭：搜索仍可用、不发布半成品、无遗留进程 | 2A 保留 + 本轮取消/超时/回收 | 2A `test_alignment_compute_lifecycle`；本轮 `_run_command` 取消+超时+回收 |
| 9 | 主程序/组件版本兼容与不兼容：分别复用与明确拒绝 | 源码层已测（identity/protocol） | `resolve_installed_runtime_launch` 拒绝协议/schema/identity 不符；2A worker 版本校验 |
| 10 | 操作对齐组件时其他已装组件仍可用 | 组件相互独立（各自目录/锁/回执） | 装配隔离，未新增耦合 |

## 3. 门禁

- 全量 unittest：**2326 通过，23 跳过，0 失败**（`.venv-macos312-arm64`，`PYTHONUTF8=1`，`NO_PROXY` 含 localhost；含设置页状态行后 +4）。
- Ruff（pyflakes F）：`ruff check src tests` **All checks passed**。
- 架构边界：`test_architecture_boundaries` 绿（web_runtime 690 行）。
- HTTP 契约：`test_http_api_contract` 绿。

## 4. 未通过 / 未做的交付门禁（不虚报）

| 平台 | 源码测试 | 独立运行时测试 | 冻结产物测试 |
|---|---|---|---|
| macOS ARM（本机） | ✅ 全量 2322 | ⚠️ 仅注入假 uv/假 worker 的生命周期 + 缺栈拒绝的真 worker；**真机 uv 装真栈未做** | ❌ 未做 |
| macOS Intel | ✅（CI 同源） | ❌ 未做 | ❌ 未做 |
| Windows | ✅（应在 windows-tests） | ❌ 未做（含中文路径、msvcrt 锁真机） | ❌ 未做 |

- **真机 uv 安装（网络）**：未做——`alignment.packages` pin 组合可解析性、离线加载、与主应用嵌入逐位一致性均**待核实**。
- **冻结态 worker 源交付**：独立 venv 需 me_finder 纯 Python 计算源随组件交付，属 **2C** 打包接线，本轮仅定义解析路径。
- **设置页 UI**：本轮与用户确认后**只加诚实的「对齐计算：可用」状态行**（自带栈老包显示「可用 · 随应用提供」，不误报未安装）；安装/升级/卸载入口按“独立运行时 2B 可选、2C 才刚需”的判断**留到 2C**。端点 `/api/text-alignment/runtime` 已就位。状态行由 `/api/text-alignment/models` 响应折入的 `compute` 字段驱动。

## 5. 是否具备进入 2C 的条件

具备**架构条件**：独立运行时组件、生产启动解析、跨进程与恢复语义、卸载/模型删除边界已就位且门禁通过。进入 2C（正式精简主包 + sidecar + 冻结 worker 源交付 + 真机安装/冻结冒烟）前，仍需先补真机 uv 安装验证与设置页 UI，否则 2B 的“用户可在设置中安装/升级/卸载”这一用户可达性尚未闭合。

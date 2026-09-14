# 阶段2B —— 对齐计算组件安装管理(独立运行时)

- 日期：2026-09-14
- 范围：把 2A 的“独立计算进程”升级为**受管组件**——安装在运行时目录下的独立 uv 虚拟环境，拥有固定版本的数值栈（numpy / onnxruntime / fastembed）。主程序不再依赖自身环境里碰巧存在的数值栈来计算；安装/升级/卸载不修改主程序或其他组件的依赖环境。本轮**不做**：正式精简主包与 sidecar（留 2C）、冻结产物的 worker 交付真机验证、设置页 UI（见“未完成项”）。
- 不变量：算法、阈值、默认 batch64、缓存版本、成果格式**均未改动**。本轮只新增“独立运行时”这一分发/安装边界与生产启动接入。

## 1. 组件与目录

`ManagedAlignmentRuntime`（`managed_alignment_runtime.py`，`component_id = "text-alignment-runtime"`），遵循既有 `ManagedComponent` 契约（`summary` / `diagnostics` / `perform`），实现机制沿用 `managed_mineru.py` 的成熟范式（uv 下载 + 隔离 venv、staging→publish→previous 原子换装、回执、取消、下载校验）。

目录（与模型缓存同处稳定机器目录 `component_runtime_root(runtime_root)/components/text-alignment/`，切换书库不迁移、不误删本机组件）：

| 路径 | 内容 |
|---|---|
| `runtime/` | 已发布的独立 venv（`venv/…`）+ 回执 `installed.json` |
| `runtime/venv/<venv_python>` | 独立解释器（平台矩阵的 `venv_python`） |
| `models/` | 既有 `ManagedEmbeddingModels` 模型缓存（MiniLM/E5 ONNX） |
| `_tools/uv-<ver>-<key>/` | 共享 uv 工具 |
| `_python/` | uv 托管 Python |
| `.operation.lock` | 跨进程操作锁文件 |

## 2. 清单：`alignment` 块

`local_ocr_manifest.json` 新增顶层 `alignment`（由 `component_catalog.validate_component_catalog` 校验，依赖必须固定版本且含 numpy/onnxruntime/fastembed）：

```json
"alignment": { "version": "1", "python": "3.11",
  "packages": ["numpy==2.5.2", "onnxruntime==1.29.0", "fastembed==0.8.0"] }
```

- `packages` **自描述整条数值栈**（含 ONNX Runtime），与 OCR 组件的 onnxruntime 选择解耦；共享 `platforms` 矩阵只提供 uv 下载与 `venv_python`。
- **待核实**：上述 pin 组合按“与当前主应用一致”选取（本机 `.venv-macos312-arm64` 实测 numpy 2.5.2 / onnxruntime 1.29.0 / fastembed 0.8.0），**尚未做真机 uv 安装验证**其可解析、可离线加载、且与主应用嵌入逐位一致。真机安装后须复核并按需调整 pin。

## 3. 安装/升级/卸载/验证（`perform` 动作）

- **install / update**：`.staging-*` → uv `venv` → uv `pip install <packages>` → **验证** → 写回执 → 原子 `replace` 发布 → 删 `previous`。任何失败或中断都不留半发布目录；已有可用版本失败时原样回退（`previous` 换回）。
- **验证不是文件存在/`find_spec`/`--help`**：用**新装的独立解释器**跑计算 worker 的 `--probe`，在隔离解释器内真正 import numpy/fastembed/onnxruntime 并完成版本化协议握手（覆盖“运行时加载”）。缺栈的隔离 venv 会验证失败、不发布（`test_real_worker_probe_rejects_stackless_runtime` 钉死）。
  - **未验证**：真机上“装好真数值栈后 probe 通过 + 最小计算闭环逐位一致”的**正向**用例需真机 uv 安装（网络/平台），本轮以缺栈**拒绝**的负向用例证明验证是真运行时加载。
- **uninstall**：删 `runtime/` + `_tools/` + `_python/`，随后按**已确认产品规则删除组件所属模型**（先 `ManagedEmbeddingModels.delete_all_models()` 复位回执/状态，再整体删 `models/`；该目录为对齐组件专属，不碰其他组件）。**文献、笔记、已有对齐成果、人工修正与定位信息保留**（都在库数据库里，不在此）。删除失败显式传播，界面绝不在运行时仍在磁盘时显示“已卸载”。
- **计算中卸载**：`is_compute_active`（默认 `embedding_runtime.embedding_run_active`，由 coordinator 在计算期 enter/exit 计数）为真时，卸载进入 `uninstall_pending`（`uninstall_deferred=True`，界面显示“任务结束后卸载”），等当前任务结束后自动完成（`test_uninstall_defers_until_compute_task_finishes`）。

## 4. 跨进程与恢复

- **跨进程操作锁** `_CrossProcessOperationLock`：POSIX `fcntl.flock` / Windows `msvcrt.locking` 的文件级 OS 锁，持有于整个安装/卸载操作；第二个应用实例或独立进程无法并发改坏同一组件（**不是**用进程内 `threading.Lock` 冒充；`test_cross_process_lock_blocks_concurrent_operation`）。持有者崩溃由 OS 释放，不会永久卡死。
- **恢复**：`_installed()` 仅在“回执 schema 匹配 **且** 回执命名的解释器存在 **且** 有 identity”时为真——崩溃留下的半写 staging / 无回执目录**永不**报“已安装”（`test_half_written_directory_is_not_reported_installed`、`test_receipt_without_interpreter_is_not_installed`）。每次安装先清 `.staging-*` 陈留。
- **升级复用**：回执记 `identity`（runtime_version+python+packages+protocol 的 sha256）；`update_available` 比 identity，pin 集变化才提示更新，主程序升级后兼容组件与已装模型继续复用、不无故重下（`test_update_available_when_pins_change_and_update_swaps`）。

## 5. 生产启动接入（用独立运行时计算）

- `resolve_installed_runtime_launch(runtime_root)`（`managed_alignment_runtime.py`）：**只读**检查回执 + 解释器，装好则返回 `<venv_python> -m <worker_module>` + `PYTHONPATH`/cwd 指向 worker 源；未装或半装返回 `None`。
- `build_compute_runner`（`text_alignment_coordinator.py`）：装了独立运行时就用**它**启动 worker（主进程无需数值栈）；否则回退 2A 行为（主运行时自带解释器）。组件失败仍**绝不静默退回进程内计算**（由 runner 与 coordinator 的错误分类保证，非此处）。
- **worker 源交付**：开发态 `-m src.me_finder.alignment_compute_worker`（venv 有数值栈、`PYTHONPATH=repo`）已通；**冻结态**独立 venv 需要 me_finder 纯 Python 计算源随组件交付——该打包接线属 **2C**，本轮定义解析路径并使其可测，未做冻结真机验证。

## 5b. 设置页(本轮只做诚实状态行,安装/卸载 UI 留 2C)

与用户确认后定的产品判断:2B 的独立运行时是**可选**的——当前发布包**自带**数值栈,不装独立运行时也能算(装了优先用),**老用户升级不受影响、无需安装任何东西**。因此设置页本轮**不加安装/卸载大按钮**(那在 2C 主包精简、独立运行时成为刚需时才做),只加一条**诚实的可用状态行**。

- 位置:`译本对齐模型` 分类(`text-alignment-settings`)顶部,`计算组件` subhead + `#alignment-compute-status` 状态行;其下 `对齐模型` subhead + 原有模型列表。
- 后端:`ManagedAlignmentRuntime.compute_status()` → `{available, provider}`;`provider` = `independent`(已装独立运行时,装时已验证)/ `builtin`(自带栈可导入,`_builtin_stack_present` find_spec numpy/fastembed/onnxruntime)/ `none`。**只作 settings 预检指示,权威能力检查仍是计算时 probe。** 关键:自带栈的老包显示`可用 · 随应用提供`,**不是**`未安装`。
- 接线:`parser_settings_controller.text_alignment_models_component()` 把 `compute` 折进 `/api/text-alignment/models` 响应(免第二次请求);前端 `renderAlignmentComputeStatus` 渲染(`可用 · 随应用提供` / `可用 · 独立运行时` / `不可用 · 缺少计算依赖,请更新应用`)。
- 未加新全局符号(渲染在既有 IIFE 内);装配指纹基线已更新(`test_frontend_assets`)。

## 6. 验收与未完成项

见 `reports/alignment-compute-runtime-2b-2026-09-14.md`。要点：源码层 11 项新测 + 全量 2322（23 跳过）绿、Ruff 零告警；**未做**：真机 uv 安装（网络）、Windows / macOS Intel / macOS ARM 冻结产物冒烟、设置页安装/升级/卸载 UI。不声称跨平台或端到端交付完成。

# 阶段2A 验收报告 —— 本地独立对齐计算闭环

2026-09-14：后续第三轮复审及直接修复见 [最新复审报告](alignment-compute-2a-final-review-2026-09-14.md)。以下保留前两轮测试和冻结构建的历史验证范围。

- 日期：2026-09-14（含针对 Astra 审计的修复复核）
- 范围：`generate_alignment` 的**计算相位**移入独立本地进程;主程序准备输入 → 计算进程执行 → 主程序发布结果。协议与职责见 [`docs/issues/note-alignment-compute-process-2a.md`](../docs/issues/note-alignment-compute-process-2a.md)。
- 工具链(固定)：Python 3.12.10 arm64 / numpy 2.5.2 / onnxruntime 1.29.0 / fastembed 0.8.0 / tokenizers 0.23.1。模型：`minilm-l12-v2`。
- 不变量：算法、阈值、默认 batch64、成果格式、缓存语义均未改动。

## 0. 针对审计的修复(本轮)

| 审计项 | 修复 |
|---|---|
| P1 Windows worker:windowed `console=False` 下 `sys.stdout/stderr` 为 None;`os.killpg` 不存在 | 协议**改为文件传输**(request/result/control 文件),worker 自身 stdio 指向空设备,不依赖继承 std 流;进程回收**分平台**(POSIX `killpg` / Windows `CREATE_NEW_PROCESS_GROUP`+`terminate`) |
| P1 stderr 管道写满死锁 | **不建任何管道**(stdout/stderr=DEVNULL),控制走文件增量 tail;新增"大量诊断不阻塞探测"回归测试 |
| P1 CI 失败(probe 测试假设装了计算栈) | 能力存在断言 `skipUnless(_DEPS_PRESENT)`;新增"缺依赖时正确拒绝"测试(CI 无栈时运行) |
| P2 版本/身份未真正校验 | worker 计算前核对请求版本与**自身实现版本**,不符 `protocol_incompatible` 拒绝;主进程核对结果文件 `protocol`+`task_id`+`input_identity` |
| P2 缺"无 NumPy 主进程 + 真实外部运行时成功生成发布"闭环 | 新增该真实闭环测试(子解释器禁 import numpy/fastembed/onnx,经真实子进程生成并发布,断言主进程从未 import 计算栈) |
| P2 对照用用户缓存、共享缓存削弱验证 | 测试与脚本改用**隔离缓存**(模型只读 symlink + 全新 document-vectors);验证冷缓存生成与暖缓存复用;实测用户向量缓存文件数不变 |

## 0b. 针对复审的修复(第二轮)

| 复审项 | 修复 |
|---|---|
| P1 探测未纳入生命周期,关闭时 worker 可成孤儿 | `probe()` 移入 `durable_operations.operation()` + `index_runtime.mutation()`;close 的 drain 会等待、`request_embedding_cancel` 会取消它。新增回归:probe 期间 gate.active==1、取消后 drain 返回、结果为 Cancelled |
| P2 启动失败遗留含正文的临时文件 | `__call__`/`probe` 的 try/finally **从建临时目录起**覆盖清理(写入/启动失败也清)。新增回归:不存在的 worker 路径 → `worker_start_failed` 后无残留临时目录 |
| P3 隔离缓存经符号链接改写用户 installed 回执 | 只 symlink `models--*` 只读模型;`installed/` 与 `document-vectors/` 为**全新可写本地目录**。测试/脚本/闭环三处同步。新增回归:真实推理后用户 `installed/` 回执 mtime/大小不变 |
| P4 组件错误与探测取消在界面显示为解析失败 | 新增 `TextAlignmentComponentUnavailable`;coordinator 用统一映射:CANCELLED→取消、component_missing/protocol/worker_start_failed→组件不可用(可展示消息)、其余→失败;controller 对组件不可用返回 **503 + 具体原因**(非"检查解析文本")。新增 coordinator/controller 回归 |

## 1. 对照结论:进程内 vs 独立进程完全一致(无容差)

| 对照 | 样本/缓存 | 结果 |
|---|---|---|
| 冷缓存直接计算(in-process vs 子进程,各自独立冷缓存) | 合成 fixture | **逐 SemanticLink、逐 HeadingAnchor 完全相等**(含字符区间/cost/confidence/review_status/anchor_key) |
| 暖缓存复用 vs 冷缓存 | 合成 fixture,同缓存二次 | 结果完全相等;冷跑后 `document-vectors` 已写入 |
| 端到端(`generate_alignment` in-process vs 子进程,各自隔离缓存) | 合成 fixture | 返回摘要一致;持久化 links(order/cost/confidence/anchor_key/review_status + 成员分段身份)完全相等 |
| **真实样本**(2047×1901 段) | 私有库快照只读拷贝 + **隔离冷缓存** | **967 链接;in-process 与子进程持久化摘要 sha256 完全相同** `1f4ea08d95557291…`;**用户向量缓存文件数 23→23 未变(无污染)** |
| **无 NumPy 主进程闭环** | 子解释器禁 import numpy/fastembed/onnxruntime | 经真实外部子进程**成功生成并发布** completed run(links>0);断言主进程 `sys.modules` 从未含 numpy/fastembed/onnxruntime |

- 真实样本摘要:accepted 795 / rejected 94 / unmatched 78 / heading_anchor 92 / folio_anchor 30。
- 报告(脱敏,仅哈希/计数)：[`reports/alignment-compute-parity-real-2026-09-14.json`](alignment-compute-parity-real-2026-09-14.json);脚本 `scripts/alignment_compute_parity_report.py`(隔离缓存,不改用户库/模型/向量缓存)。
- **未加任何舍入或容差**;排除的非确定字段:`alignment_run_id`/`alignment_link_id`(UUID)、`created_at`/`completed_at`(时间戳);真实样本另脱敏私有 id(存 `pair_hash`)。

## 2. 真实冻结产物冒烟(macOS arm64,新文件传输)

- 冻结产物：本轮由 `build_macos.sh` 从含**修复后**代码的工作区重建的 `MEFinder.app`(arm64,onedir,ad-hoc 签名)。**本地冒烟用构建,非发布**;已公开 v0.5.4 权威 SHA 不变;`release/` 被 gitignore。
- 冻结 worker 探测:`MEFinder.app/Contents/MacOS/MEFinder alignment-compute-worker --probe <control>` → hello,capabilities 全 true。
- 冻结 worker 计算:经 runner 以 `[<app-exe>, alignment-compute-worker]` 驱动,**隔离冷缓存**,输出逐链接、逐锚点与 in-process 完全一致。
- **范围说明**:冻结冒烟验证的是**冻结 worker 传输 + 计算路径**(`c61b627` 构建)。第二轮复审的四项修复是**主进程侧**逻辑(coordinator 生命周期、runner 临时清理、缓存隔离、错误映射),不改变冻结 worker 的传输/计算行为(worker 仅新增测试用 `noisy` 分支),由单元/集成测试验证,未再单独重打冻结包。

## 3. 错误、生命周期与失效保护(自动化测试)

`tests/test_alignment_compute_protocol.py`(故障注入,多数无需模型)+ `tests/test_alignment_compute_parity.py`(需模型)+ 既有守卫:

| 场景 | 结果 |
|---|---|
| 组件缺失(探测) | `component_missing`,不静默退回 ✓ |
| 协议不兼容(探测 + 请求) | `protocol_incompatible` ✓ |
| **worker 声明错误算法版本** | worker 核自身版本,`protocol_incompatible` 拒绝,不发布 ✓ |
| **结果文件 protocol / task_id / input_identity 任一不符** | 拒绝发布(`protocol_incompatible`/`result_mismatch`)✓ |
| worker 崩溃 | `worker_crashed`,不发布 ✓ |
| 取消 / 关闭应用 | 结束计算进程(跨平台),`cancelled`,不发布 ✓ |
| **worker 大量诊断输出** | 探测不阻塞(DEVNULL,无管道背压)✓ |
| **windowed 无继承 std 流(sys.stdout/stderr=None)** | worker 仍经控制文件正常应答 ✓ |
| 端到端崩溃/失配 → 无半成品 | DB 无新 completed run ✓ |
| **worker 启动失败** | `worker_start_failed`,**含正文的临时文件不残留** ✓ |
| **探测在生命周期内且可取消** | probe 期间 gate.active==1,取消后 drain,结果为取消(不留孤儿)✓ |
| **组件不可用 / 探测取消的界面映射** | 组件不可用→503 具体原因、探测取消→取消(非"检查解析文本")✓ |
| **隔离缓存不改用户缓存** | 真实推理后用户 `installed/` 与 `document-vectors/` 不变 ✓ |
| 自定义 embedding_provider 跨进程 | 明确拒绝 ✓ |
| 无计算依赖冷启后端 | 搜索/读取已存对齐/优雅退出可用(`test_core_without_alignment`)✓ |
| 协调门禁(mutation/durable/write_window,不 suspend/reopen,不放松锁) | ✓（`test_text_alignment_coordinator`、`test_alignment_write_window_availability`) |

## 4. 门禁清单

| 门禁 | 状态 |
|---|---|
| 全量 unittest（PYTHONUTF8=1） | **2306 通过 / 23 skip / 0 失败** |
| ruff（pyflakes F） | **通过** |
| macOS arm64 `build_macos.sh`（含门禁 + PyInstaller + 签名 + DMG verify） | **通过**(本地冒烟构建,含修复代码) |
| CI(远端) | 需重跑核验;probe 依赖已按 `_DEPS_PRESENT` 分流,缺栈时运行"正确拒绝"分支 |

## 5. 已测 / 未测平台(不外推)

- **已测**：macOS arm64 —— 全量测试、真实样本冷缓存对照、无 NumPy 主进程真实闭环、冻结产物(新文件传输)worker 探测与计算冒烟。
- **代码已实现但未在目标主机验证**：
  - **Windows**：worker 不再依赖继承 std 流(文件传输)、进程回收走 `CREATE_NEW_PROCESS_GROUP`+`terminate`;None-std-流已用子进程模拟测试。**仍未在 Windows 主机构建/冒烟冻结 worker**——不声称 Windows 交付完成。
  - **macOS x86_64**：未构建/未冒烟 2A 改动。
- 默认 page cache 真实大书写锁等待墙钟时长(既有待验证项,非本轮引入)。

## 6. 边界(本轮未做)

下载安装 UI、正式精简主包发布、batch16、跨窗口对照、原生宿主迁移、通用 IPC/插件框架、无关业务重构 —— 均未进行。text_alignment.py 仅按计算相位拆出 `alignment_kernel.py`,再导出保持兼容,非全仓移动。

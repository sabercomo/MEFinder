# 阶段2A —— 本地独立对齐计算闭环(进程协议与职责)

- 日期：2026-09-14
- 范围：打通 **主程序准备输入 → 本地独立计算进程执行 → 主程序接收并发布结果**。不做下载安装 UI、正式精简包、batch16、跨窗口对照、原生宿主、通用插件框架。
- 不变量：算法、阈值、默认 batch64、既有成果格式与缓存语义**均未改动**;仅把 `generate_alignment` 的**计算相位**移出主进程。

## 1. 三相位与职责边界

`generate_alignment`(`text_alignment.py`)保持既有三相位;本轮只把**计算**换成可注入的 `compute_runner`(默认仍为进程内)。

| 相位 | 归属 | 职责 |
|---|---|---|
| 准备 prepare | **主进程** | 打开 DB、校验作品组/文献/分段身份(`_require_pair`)、载入复核正文范围、检测折页锚候选、命中可复用既有 run 时短路(`reused`)、组装 `AlignmentPreparation`;全部只读并提交关闭 |
| 计算 compute | **独立计算进程** | 模型加载、嵌入、对齐数值计算(`align_segment_sequences`,现居 `alignment_kernel.py`);NumPy / ONNX Runtime / fastembed 只在此进程 |
| 发布 publish | **主进程** | 在既有写入协调(`index_runtime.mutation()` + `durable_operations.operation()` + `_write_window`,`BEGIN IMMEDIATE`)内写入正式库并提交 |

**主进程保留**:文献/分段身份检查、任务/进度/取消/生命周期协调、既有写入协调、正式数据库事务与结果发布。
**计算进程负责**:仅计算;返回可被**无 NumPy** 主进程消费的纯数据(`SemanticLink`/`HeadingAnchor` 数据类)。计算进程**不**碰正式数据库、**不**重解析原文、**不**触发 OCR 或模型下载。

> 关键:不是"只把 fastembed 调用移走而核心生成路径仍依赖 NumPy"。整条生成路径(准备+发布)在主进程内 lazy-import 层面即 NumPy-free;NumPy 只在计算进程实际 import(由 `tests/test_core_without_alignment.py` 在禁 import numpy/fastembed/onnxruntime 下冷启后端佐证)。

## 2. 能力判定改为探测外部运行时

旧实现用主进程 `find_spec(numpy/fastembed/onnxruntime)` 判断能否生成。**已移除**。现由 `TextAlignmentCoordinator` 构造 `SubprocessAlignmentComputeRunner` 并 `probe()` 外部计算进程,由**将真正执行计算的运行时**自报能力。探测失败是明确的本地错误(`TextAlignmentFailed`),**绝不静默退回主进程计算**。模型文件存在(`model_component_installed`)与计算依赖可用是两道独立门,均须通过。

## 3. 最小、版本化的进程协议

不造通用 IPC 框架。**传输全部走文件,不用继承的 stdin/stdout/stderr 作协议通道**——这样在 windowed 冻结应用(Windows `console=False` 时 `sys.stdout/stderr` 为 `None`)可用,且不会因 stderr 管道写满而死锁。worker 启动即把自身 `sys.stdout/stderr` 指向空设备(库输出既不阻塞也不污染协议);主进程以 `stdout=DEVNULL, stderr=DEVNULL` 启动 worker,**不建任何管道**。

- **请求**(临时 `request.json`):`protocol`、`task_id`、`cache_dir`、`model_identity`(embedding_model_id + embedding_runtime / semantic_alignment / algorithm / region 版本)、`input_identity`(输入+版本 sha256)、`inputs`。
- **结果**(临时 `result.json`):`protocol`、`task_id`、`input_identity`(worker 就其实际收到、**用自身版本**计算的输入重算)、`links`、`anchors`。
- **控制文件**(临时 `control.ndjson`,worker 追加行分隔 JSON,主进程增量 tail):`hello`(协议版本 + 能力 + pid;probe 只发这一条)、`progress`、`result`、`error`(`code` + `message`,诊断 traceback 折进 message)。

**错误码**:`component_missing` / `protocol_incompatible` / `worker_start_failed` / `worker_crashed` / `compute_failed` / `cancelled` / `result_mismatch`。

**版本身份强校验(双向)**:worker 计算前核对请求 `model_identity` 四个版本字段与**自身实际实现版本**一致,否则 `protocol_incompatible` 拒绝(独立组件升级后错误版本结果不会被当成当前成果);主进程收到结果后核对结果文件的 `protocol`、`task_id`、`input_identity` 三者与请求一致,任一不符即拒绝发布。

**启动**:开发态 `python -m src.me_finder.alignment_compute_worker <req> <res> <control>`;冻结态复用应用可执行文件的 `alignment-compute-worker` 子命令(`desktop.py` 顶部在导入桌面壳之前分派,worker 精简不开窗)。

## 4. 生命周期与失效保护

- **取消/退出(跨平台)**:POSIX 用独立会话/进程组(`start_new_session=True`)+ `killpg`;Windows 无 `killpg`,用 `CREATE_NEW_PROCESS_GROUP` + `terminate()`(worker 无自建子进程)。runner 轮询 `cancel_check`(= `embedding_cancel_requested()`,用户取消与应用关闭都置位),命中即结束 worker,报 `cancelled`。计算期主进程**不持** DB 锁(准备相位已提交关闭),硬杀 worker 不影响库,也不影响其他任务/进程。
- **不发布半成品**:发布在计算成功返回**之后**才发生;崩溃、协议不兼容、启动失败、取消都在发布前中止,DB 无新 completed run。
- **不发布过期结果**:runner 校验结果 `input_identity` 与请求一致,否则 `result_mismatch` 拒绝发布。既有写入协调(串行阻止修改)**保留不放松**。
- **自定义 embedding_provider** 无法跨进程序列化:子进程 runner 显式拒绝,不静默退回进程内。

## 5. NumPy 缺失时的既有对照定位降级(查清,不夸大)

- 缺 NumPy:搜索、读取/定位**已存**对齐结果、优雅退出全部可用(`test_core_without_alignment`)。
- 可选向量细化(corridor refine 等)在缺 NumPy 时跳过。
- **未验证**"有/无组件时定位结果必然完全一致"——不作此声称。

## 6. 已验证 / 未验证(平台与门禁见验收报告)

- 已验证(macOS arm64,2026-09-14):进程内 vs 子进程**逐链接、分数、分类、锚点、字符区间完全一致**(无容差,含冷缓存直接对照、暖缓存复用、真实书对 967 链接 sha256 相同、无 NumPy 主进程真实闭环);协议/版本不符/崩溃/取消/组件缺失/结果 protocol·task·input 失配错误明确且不发布;windowed 无继承 std 流与大量诊断不阻塞已测;全量 unittest 2299 通过(23 skip)、ruff 通过;冻结产物(文件传输)worker 探测+计算与进程内一致。
- 未在目标主机验证:Windows(代码已分平台实现,未在 Windows 构建/冒烟)、macOS x86_64。
- 见 [`reports/alignment-compute-2a-acceptance-2026-09-14.md`](../../reports/alignment-compute-2a-acceptance-2026-09-14.md):完整对照、冻结冒烟、已测/未测平台与针对审计的修复清单。

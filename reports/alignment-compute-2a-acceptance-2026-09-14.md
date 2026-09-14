# 阶段2A 验收报告 —— 本地独立对齐计算闭环

- 日期：2026-09-14
- 范围：`generate_alignment` 的**计算相位**移入独立本地进程;主程序准备输入 → 计算进程执行 → 主程序发布结果。协议与职责见 [`docs/issues/note-alignment-compute-process-2a.md`](../docs/issues/note-alignment-compute-process-2a.md)。
- 工具链(固定)：Python 3.12.10 arm64 / numpy 2.5.2 / onnxruntime 1.29.0 / fastembed 0.8.0 / tokenizers 0.23.1。模型：`minilm-l12-v2`(本地 app 运行时组件目录)。
- 不变量：算法、阈值、默认 batch64、成果格式、缓存语义均未改动。

## 1. 对照结论:进程内 vs 独立进程完全一致(无容差)

| 对照 | 样本 | 结果 |
|---|---|---|
| 直接计算(`align_segment_sequences` vs 子进程 runner) | 合成公共 fixture | **逐 SemanticLink、逐 HeadingAnchor 完全相等**(含 source/target 字符区间、cost、confidence、review_status、anchor_key) |
| 端到端(`generate_alignment` 进程内 vs 子进程,两份相同 fixture) | 合成公共 fixture | 返回摘要一致;持久化 links(order/cost/confidence/anchor_key/review_status + 成员分段身份)**完全相等** |
| **真实样本**(真实书对,2047×1901 段) | 私有库快照(只读拷贝到临时库) | **967 链接;进程内与子进程持久化结果摘要 sha256 完全相同** `1f4ea08d95557291…`;`rows_identical=true`、`return_summary_identical=true` |

- 真实样本摘要:accepted 795 / rejected 94 / unmatched 78 / heading_anchor 92 / folio_anchor 30;algorithm v22;model minilm-l12-v2。
- 报告数据(已脱敏,仅哈希/计数)：[`reports/alignment-compute-parity-real-2026-09-14.json`](alignment-compute-parity-real-2026-09-14.json)。复现脚本：`scripts/alignment_compute_parity_report.py`(拷贝快照到临时库,不改用户库/模型/快照)。
- **未加任何舍入或容差**:比较为精确相等。
- **排除的非确定字段(附理由)**:`alignment_run_id` / `alignment_link_id`(每 run/link 新 UUID)、`created_at` / `completed_at`(墙钟时间戳)。真实样本报告另脱敏 `document_group_id`/`pivot_source_file_id`/`target_source_file_id`(私有标识,存为 `pair_hash`)。

## 2. 真实冻结产物冒烟(macOS arm64)

- 冻结产物：本轮由 `build_macos.sh` 从含 2A 改动的工作区重建的 `MEFinder.app`(arm64,onedir,ad-hoc 签名)。**这是本地冒烟用构建,不是发布**;已公开的 v0.5.4 权威 SHA 不变。
- 冻结 worker 探测：`MEFinder.app/Contents/MacOS/MEFinder alignment-compute-worker --probe` → `hello protocol=1 capabilities={numpy,fastembed,onnxruntime: true}`。
- 冻结 worker 计算：经 runner 以 `[<app-exe>, alignment-compute-worker]` 为启动命令驱动真实计算,输出**逐链接、逐锚点与进程内路径完全一致**。
- 证明:冻结应用可执行文件复用为计算进程(desktop.py 顶部子命令分派),不只在开发 venv 中可跑。

## 3. 错误、生命周期与失效保护(自动化测试)

`tests/test_alignment_compute_protocol.py`(无需模型,故障注入)+ `tests/test_alignment_compute_parity.py`(需本地模型)+ 既有守卫:

| 场景 | 期望 | 结果 |
|---|---|---|
| 组件缺失(探测) | 明确 `component_missing` 错误,不静默退回进程内 | 通过 |
| 协议不兼容(探测 + 请求) | 明确 `protocol_incompatible`,不计算 | 通过 |
| worker 崩溃(运行中退出) | 明确 `worker_crashed`,**不发布** | 通过 |
| 取消 / 关闭应用 | `killpg` 结束计算进程组,报 `cancelled`,不影响他任务;不发布 | 通过 |
| 结果输入身份不符(过期/失配) | `result_mismatch` 拒绝发布 | 通过 |
| 端到端崩溃/失配 → 无半成品 | `generate_alignment` 抛错后 DB 无新 completed run | 通过 |
| 自定义 embedding_provider 跨进程 | 明确拒绝,不静默进程内 | 通过 |
| 无计算依赖冷启后端(禁 import numpy/fastembed/onnxruntime) | 搜索、读取已存对齐、优雅退出可用 | 通过(`test_core_without_alignment`) |
| 协调门禁(mutation/durable/write_window;不 suspend/reopen) | 保持既有写入协调,不放松锁 | 通过(`test_text_alignment_coordinator`、`test_alignment_write_window_availability`) |

## 4. 门禁清单

| 门禁 | 状态 |
|---|---|
| 全量 unittest（`discover -t . -s tests`，PYTHONUTF8=1） | **2291 通过 / 22 skip / 0 失败** |
| ruff（pyflakes F） | **通过（新增/改动文件零告警）** |
| macOS arm64 `build_macos.sh`（自带门禁 + PyInstaller 主应用/sidecar + 签名 + DMG verify + SHA-256） | **通过**(本地冒烟构建) |
| fd 卫生 | 子进程管道显式关闭;`-W error::ResourceWarning` 下协议测试通过 |

## 5. 已测 / 未测平台(不外推)

- **已测**：macOS arm64 —— 全量测试、真实样本对照、冻结产物 worker 探测与计算冒烟。
- **未测(明确列出,不声称已交付)**：
  - **macOS x86_64**：未构建/未冒烟本轮 2A 改动。
  - **Windows**：未构建/未冒烟;`desktop.py` 子命令分派与 `packaging/desktop.spec` 的 hiddenimports 已就位,但**未在 Windows 主机验证**冻结 worker。
  - 默认 page cache 下真实大书写锁等待墙钟时长(沿用既有待验证项,非本轮引入)。

## 6. 边界(本轮未做)

下载安装 UI、正式精简主包发布、batch16、跨窗口对照、原生宿主迁移、通用 IPC/插件框架、无关业务重构 —— 均未进行。text_alignment.py 仅按计算相位边界拆出 `alignment_kernel.py`(`align_segment_sequences`),`text_alignment` 保留再导出,未做全仓移动。

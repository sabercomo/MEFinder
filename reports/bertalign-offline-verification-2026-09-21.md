# Bertalign 后端真实离线验证报告

- 日期：2026-09-21
- 结论（一句话）：真实本地 LaBSE + 原版 Bertalign 在**断网**条件下，对生产库一致性副本上的
  一个真实书对完成计算、经产品发布路径落库、并由阅读器读取路径读回定位；后端身份隔离与定位契约成立。
- 复现脚本：`scratchpad/verify_step_a_compute.py`（计算）、`verify_step_b_publish.py`（发布+读回）。
  本报告只记录事实与数值，不据此声称 Bertalign 质量优于默认后端。

## 环境与数据（事实）

- 生产库定位：`~/Library/Application Support/MEFinder/data_root.txt` →
  `.../OneDrive-个人/MeFinder`，实库 `runtime/data/index.sqlite3`（`user_version=7`，
  `journal_mode=delete`，3.47 GB）。桌面 App 与 MCP 进程在运行。
- 验证副本：用 SQLite 在线备份 API（`sqlite3.Connection.backup`，源只读）生成一致性副本
  `~/mefinder-worktrees/verify-index.sqlite3`（19.6 s）。**只在副本上计算，未修改生产库。**
- 模型：`sentence-transformers/LaBSE` 一次性联网下载并 `save()` 为本地自包含目录
  `~/mefinder-worktrees/labse-model`（1.8 GB）。计算阶段断网。
- 计算运行时：独立 venv `~/mefinder-worktrees/.venv-bertalign`
  （torch 2.14 / sentence-transformers 6.1 / faiss-cpu 1.15 / numba 0.67 / numpy 2.5，CPU）。
- 书对：作品组 `document-group-65e69e18973845f5ab68ffe572061bd2`，
  pivot `epub-c3fce4b8...`（日文 EPUB《動物化するポストモダン》，1743 段），
  target `pdf-import-de1cf03d...`（中文 PDF 译本，1499 段）。真实跨语言对。

## 计算（事实）

- **本轮为有边界窗口**：正文范围内每侧取 300 段（pivot body `[42,342]`、target body `[150,450]`）。
  这是真实数据、真实模型、真实 segment_id 与页码锚点的一次有边界运行，用以在 CPU/时间约束下给出
  可复现证据；**未在整本书对的全量正文上跑完**（全量在本机 CPU 上过慢，见「限制」）。
- 耗时 95.9 s。结果状态计数：`automatic=264`、`unmatched=10`、`rejected=2642`
  （窗口外/正文外段作单侧 rejected 保留可检查）。
- m-n 分布（automatic）：`1-1:213, 2-1:29, 1-2:13, 2-2:3, 3-1:2, 1-4:2, 1-3:2`
  → **真实产生多对多 bead 与插入/删除**，索引映射覆盖完整窗口，无字符串查找。
- **离线证明**：计算全程安装 socket 守卫，拦截任何非本地 `connect`；`network_blocked_attempts=[]`，
  即计算期间未发起任何外部连接（`offline_ok=True`），并设 `HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE`。

## 发布 + 读回（事实，经产品路径）

- `generate_bertalign_alignment` 落库：`algorithm=bertalign-labse-two-pass` v1，
  `embedding_model_id=labse-bertalign`，links=2916（accepted 264 / unmatched 10 / rejected 2642），
  `reused=False`；二次调用 `reused=True`（缓存命中，见集成测试）。
- 阅读器读取路径：`locate_alignment(source=PDF, target=EPUB)` 选中 PDF 第 26 页一段，
  经 `_resolve_alignment_route` 命中 Bertalign run，读回 EPUB 目标段 1、page_match_spans 1、
  `target_title=動物化するポストモダン…`。**定位可用，非仅底层返回列表。**
- **后端隔离**：该对现存 run 中默认 `chapter-anchored-semantic-dp v22 status=completed readable=True`
  与 `bertalign-labse-two-pass v1 status=completed readable=True` **并存**，互不 supersede；
  历史旧版本仍为 superseded（既有状态，与本轮无关）。

## 有边界回归对照（事实，非质量结论）

窗口内「已接受的 pivot→target 对应成员」对比（不是准确率/F1）：

- bertalign 匹配 pivot 段 300；default（全书）匹配 1422。
- 两者都匹配的同一 pivot：299；其中目标集合**完全一致 113**、**有交集 290**、**完全不相交 9**。
- 仅 bertalign 匹配的 pivot：1；仅 default：1123（因 default 覆盖全书、bertalign 仅窗口）。

解读：两后端在多数共同 pivot 上目标有重叠但不全等（算法与模型不同，预期如此）。**不声称谁更好。**
本库 `alignment_manual_overrides` 全库为 0，无人工校正可作为“已知正确/错例”的金标准；相应回归以
两后端对应关系差异如实呈现，未凭覆盖率下质量结论。

## 已知限制与工程注意（事实）

- **未测**：整本全量正文对齐（CPU 过慢）；Windows/打包链路；in-app 子进程 worker 分发与托管组件安装。
- **torch + faiss OpenMP 冲突**：macOS 上多线程 faiss 与 torch OpenMP 共存会死锁；本轮以
  `OMP_NUM_THREADS=1` + `torch.set_num_threads(1)` + `faiss.omp_set_num_threads(1)` +
  `KMP_DUPLICATE_LIB_OK=TRUE` 规避。**生产 Bertalign 运行时 worker 必须固定同样的单线程策略。**
- **numba `cache=True` 与导入根**：`src.me_finder` 与 `me_finder` 两种导入根会使 numba 磁盘缓存
  互相不可用（`ModuleNotFoundError: 'src'`）。运行环境须固定单一导入根；打包/CI 需清理或隔离
  numba 缓存目录。
- 人工校正读取：本库无 override，未实测“读回人工校正”；集成测试覆盖旧默认 run 与新 bertalign run
  并存且各自可读。

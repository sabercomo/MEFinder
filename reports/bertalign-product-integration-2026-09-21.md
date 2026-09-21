# Bertalign 可选后端 · 产品接入与验收报告

- 日期：2026-09-21
- 结论（一句话）：原版 Bertalign（本地 LaBSE + 两阶段 DP）已作为**可选**后端接入产品链路——
  经现有托管组件机制安装独立运行时与模型、通过应用 HTTP 入口在生产库一致性副本上**断网**完成
  **整本**书对对齐、落库并由阅读器读取路径读回定位；默认 FastEmbed 算法与默认选择保持不变。
- 本报告按用户要求**区分三层验证**：算法窗口验证 / 后端函数读回 / 产品端到端验收。数量只说明
  任务完成，不代表准确率；质量以正例与错例如实呈现，不据此声称优于默认算法。

## 三层验证（分开陈述，勿混为一谈）

### 第 1 层 · 算法窗口验证（离线，有边界）
- 见 `reports/bertalign-offline-verification-2026-09-21.md`。
- 真实日中书对的**有边界窗口**（每侧 300 段）用本地 LaBSE + 原版 Bertalign 断网计算，进程内
  socket 守卫拦截任何非本地连接、实测零外连（`offline_ok=True`）。
- 作用：证明算法与索引映射在真实数据窗口上正确（多对多、插入删除、段内换行、重复文本按索引映射）。
- **不代表**整本或产品链路验收。

### 第 2 层 · 后端函数读回（离线，无重依赖）
- `tests/test_bertalign_backend_integration.py`（注入 compute_runner，免 torch）：落库 → 读回 →
  后端隔离 → 复用；并锁定三处修复（见下）。`tests/test_bertalign_backend_mapping.py` 锁定 bead→
  SemanticLink 纯索引映射；`tests/test_bertalign_vendor_dp.py`（faiss/numba 门控）跑真实两阶段 DP。
- 作用：证明发布/读取/身份/复用契约成立。
- **不代表**真实模型或真实运行时。

### 第 3 层 · 产品端到端验收（真实组件 + 真实模型 + 断网 + 整本）
- 环境：独立验收运行目录 `~/mefinder-worktrees/bertalign-acceptance`；用现有托管组件安装器安装
  独立 Bertalign 运行时（uv venv：torch 2.14.0 / sentence-transformers 6.1.0 / faiss-cpu 1.15.1 /
  numba 0.67.0 / numpy 2.5.3）与固定 revision 的 LaBSE（`836121a0533e5664b21c7aacc5d22951f2b8b25b`）。
  安装回执：`installed=true, has_models=true, compute.available=true, version=bertalign-1-836121a0`。
  模型安装复用本机已缓存的同一固定 revision，并在**离线加载 + 编码自检**通过后才写模型回执发布。
- 数据：生产库（OneDrive `runtime/data/index.sqlite3`）一致性副本，只读来源、只在副本计算，未改生产库。
- 入口：应用 HTTP（`/api/preferences` 设 `alignment_backend=bertalign` → `/api/text-alignments/start`
  → `/api/text-alignments/status` 轮询 → 完成）。**非分步脚本**；可复现脚本
  `scripts/verify_bertalign_product.py`（含 generate→reuse→overview→targets 断言）已入库。
- 真实书对（整本，非窗口）：group `document-group-65e69e18…`，pivot `epub-c3fce4b8…`（日文 EPUB
  《動物化するポストモダン》1743 段），target `pdf-import-de1cf03d…`（中文 PDF 译本 1499 段），
  检测正文范围 pivot `[42,1640]` / target `[150,1434]`。
- 结果：**450 s（≈7.5 分钟）**完成，run `alignment-run-8ad48740…`，
  `accepted=1201, unmatched=195, rejected(正文范围外)=360`，`backend=bertalign-labse-two-pass v1`，
  `embedding_model_id=labse-bertalign`，`model_revision=836121a0…`，`status=200/ok`。计算期间应用接口仍可响应。
- 读回（产品读取路径，只读副本）：`_resolve_alignment_route(backend=bertalign)` 命中该 run；
  作品总览 `backend=bertalign` 显示 `status=direct, stale_reason=None`（不再误报 algorithm_unreadable）；
  阅读器 `alignment_link_window(backend=bertalign)` 正常返回链接与 span。
- 复用：`force=False` 二次请求返回 `reused=true`（同一 run）。
- 断网强制：计算运行时设 `HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE`、只读本地模型目录；验收在 macOS
  `sandbox-exec` 拒绝网络的环境执行；第 1 层另有进程内 socket 守卫实测零外连。

## 本轮修复（复审复现的三项 + 兼容性）
1. **参数变更误用旧 run**：Bertalign 复用查询纳入 `bertalign_params`；任一上游参数变化不复用旧 run
   （`tests/test_bertalign_backend_integration.test_bug1_param_change_forces_new_run`）。缓存身份另含
   模型 revision 与上游 commit。
2. **默认阅读误选较新 Bertalign run**：run 选择按后端限定（`_latest_pair_run`/`_resolve_alignment_route`
   带 `algorithm`），默认读取只取默认算法；后端选择显式传入（`test_bug2_*`）。
3. **总览误报 algorithm_unreadable**：`_run_staleness` 按后端放行；Bertalign 用固定 LaBSE，不套默认
   模型比较（`test_bug3_overview_backend_aware`；真实副本上实测 stale=None）。
- **后端选择贯穿**：持久设置 `alignment_backend`，生成/作品总览/阅读器同读一后端；已启动任务固定其启动
  时的后端。
- **人工校正/快照兼容**：校正按 segment set 共用（两后端同套 segment 集），用后端无关路由解析，
  Bertalign-only 书对亦可校正/延后；备份快照保存 Bertalign 原始结果，源文本与分段身份一致时恢复
  原 run/链接/页码定位、不重算不下载，身份变化则明确失败回滚不静默丢结果（回归测试见
  `tests/test_bertalign_runtime.py` 等）。
- **线程/缓存落到运行时代码**：Bertalign worker 启动即固定 `OMP_NUM_THREADS=1`、
  `torch.set_num_threads(1)`、`faiss.omp_set_num_threads(1)`、`KMP_DUPLICATE_LIB_OK=TRUE`，并按任务隔离
  `NUMBA_CACHE_DIR`。**先前"卡住"是 torch×faiss OpenMP 死锁，修正后整本 450 s 完成——不是 CPU 过慢。**

## 质量：真实抽查（正例与错例，非准确率结论）
- 正例：`第一章 オタクたちの疑似日本` → `第一章 / 御宅族的拟日本`；`第二章 データベース的動物` →
  `第二章 / 数据库动物`；正文长句与其中译一一对应。Bertalign 能补全被拆开的章节标题。
- 错例/局限：PDF 侧小节标题被分成"编号段 + 标题段"，Bertalign 把 `３ 大きな非物語` 只对到 `3.`，
  漏掉标题文字；书名/页眉 `動物化するポストモダン` 对到 `1.`/`2.` 这类编号段。属分段与短标题的已知局限，
  另有个别标题默认算法反而覆盖更全。**保留为可选后端，默认算法不变，不据此声称优劣。**

## 门禁
- 全量 unittest **2507 通过 / 24 跳过**（本机 macOS arm64，`PYTHONUTF8=1`）；Ruff（F）零告警。
- 前端装配指纹、主题快照、偏好快照、设置 UI 断言随本轮 UI 改动同步更新。

## 未测 / 限制（如实标注）
- **未测**：Windows / macOS Intel 真机的组件安装、打包链路（Windows/Linux 选 CPU torch wheel 已在代码中，
  但未在对应真机验收）。
- 本库 `alignment_manual_overrides` 为空，"读回既有人工校正"以回归测试覆盖，未在真实校正数据上实测。
- 质量为单书对抽查，未做多书对统计；不声称 Bertalign 全面优于默认算法。

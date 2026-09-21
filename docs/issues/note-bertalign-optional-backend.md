# 议题：接入原版 Bertalign 作为可选语义对齐后端

- 状态：进行中（本轮交付算法核心链 + 真实离线验证；产品化尾项见「未完成」）
- 相关代码：`src/me_finder/_vendor/bertalign/`、`src/me_finder/bertalign_backend.py`、
  `src/me_finder/text_alignment.py`（`generate_bertalign_alignment` 等）
- 上游：https://github.com/bfsujason/bertalign ，固定提交 `df8c63f51aa203faed9f2fe45ae39e6fca75e667`

## 目标与边界（事实）

停止扩展自研 DP，复用原版 Bertalign（LaBSE 分组嵌入 + 两阶段 DP），通过最小适配接入
既有对齐流程。本轮：

- 保留现有自研算法与默认选择；不自动重跑生产库；不切默认算法。
- 不引入 Starry / Vecalign / 章节切片 / 通用插件框架。
- 第一版保留上游 LaBSE、两阶段搜索、分组嵌入与评分语义，不换 E5、不混自研评分、不调参。

## 复用与修改（事实，逐项见 MODIFICATIONS.md）

vendor 了 `corelib`（两阶段 DP/搜索/回溯）、`aligner`（调度）、`encoder`（分组拼接嵌入）、
`utils`（仅保留 overlap 组装）。相对上游的修改：

1. 删除 `detect_lang()`（googletrans 联网）——语言由 MEFinder 提供。
2. 删除 `clean_text`/`split_sents`（sentence-splitter 重新分句）——MEFinder 传入已分段、
   已定位的 segment，不重分句、不按换行拼接再拆分。
3. 删除包初始化建模（上游 `__init__` 导入即下载 LaBSE）——改为计算进程内显式加载本地模型。
4. `find_top_k_sents` 强制 CPU faiss，删除上游 GPU 分支（不需 CUDA / faiss-gpu）。
5. `Encoder` 接收本地模型路径 + 设备。

被移除的 googletrans / sentence-splitter 不再是后端依赖。

## 接入设计（事实）

- **身份隔离**：Bertalign 是独立算法身份 `algorithm="bertalign-labse-two-pass"`（版本 `1`），
  独立 `embedding_model_id="labse-bertalign"`（独立向量空间）。运行复用查询按
  `(algorithm, algorithm_version)` 命中，因此新旧后端的 run、缓存、向量互不复用。
- **supersede 按后端限定**：默认后端与 Bertalign 后端的 run 互不 supersede，旧 run 与人工
  校正保持可读（`_generate_alignment_on_connection` / `_generate_bertalign_on_connection`
  各自只 supersede 本 `algorithm` 的完成 run）。
- **共同前处理 vs 后端私有**：正文范围（reviewed 优先，否则 `alignment_body_bounds` 检测）
  是两后端共享的前处理；被排除段仍作单侧 `rejected` 行保留可检查。自研后端的标题/folio 锚点、
  注释覆盖、假朋友锚点校验、片段质量降级**不混入** Bertalign——Bertalign 在正文切片上端到端
  跑自己的 DP。
- **诚实的分数**：匹配 bead → `review_status="automatic"`；插入/删除 → `unmatched`。
  `confidence` 是 LaBSE 余弦相似度（单侧为 0），**不是**校准正确率，不伪造 `1.0`，不套
  MiniLM/E5 阈值。`parameters_json` 记录 `score_meaning=algorithmic_similarity_not_calibrated_accuracy`、
  backend、上游 commit、模型、上游参数。
- **定位不破坏**：bead 只带 segment 索引（不做字符串查找），一段仍是一列；段内换行、重复文本
  不改变索引；页码/字符区间仍由既有定位层按 segment_id 提供，EPUB 不生成虚构页码。
- **读取路径**：`_route_run_is_readable` 按后端放行（结构与自研 run 相同，`alignment_links`/
  `alignment_link_members` 一致），Bertalign run 可经既有 reader route + `locate_alignment`
  读取定位。

## 运行时（事实 + 未完成）

- 重依赖（torch / sentence-transformers / faiss-cpu / numba）落在**独立** Bertalign venv
  （`requirements-bertalign.txt`），与默认 FastEmbed 运行时并存、互不影响；主程序不带这些栈。
- **未完成（下一步）**：把该 venv 接入现有托管组件机制（安装/升级/卸载/任务生命周期），
  以及 `alignment_compute_worker` 的按后端能力探测与分发（现有 subprocess/取消/回收机制复用），
  设置页最小后端选择入口。LaBSE 模型走现有用户触发下载，计算阶段禁下载
  （`HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` + 缺模型明确报错）。

## 真实离线验证（事实，见 reports/bertalign-offline-verification-*.md）

在生产库的一致性备份副本上，对真实书对（日文 EPUB ↔ 中文 PDF）用本地 LaBSE + 原版 Bertalign
断网计算，落库并经产品读取路径读回定位。详见报告。

## 2026-09-21：产品接入续作

事实：补齐独立托管运行时（复用 uv 安装、原子发布、跨进程维护锁、取消及关闭）、固定 revision 的 LaBSE 安装验证、后台任务协调、设置页后端选择。删除开发环境变量旁路，产品生成必须从已安装组件启动。Windows/Linux 指定 CPU torch wheel；未据此声称已完成跨平台真机验收。

事实：修复参数变更误用旧 run、默认阅读路径误选较新 Bertalign run、总览误报算法不可读。缓存身份还包含模型 revision 与上游 commit。人工校正仍按 segment set 共用；Bertalign 无 FastEmbed 段落回退。备份快照保存 Bertalign 原始结果，源文本及分段身份一致时恢复原 ID/链接/页码定位，无需模型；身份变化则明确失败并回滚，避免静默丢结果。

事实与限制见 [产品验收报告](../../reports/bertalign-product-integration-2026-09-21.md)。旧报告的 scratchpad 脚本未入库，不能作为复现入口；本轮提供 `scripts/verify_bertalign_product.py`。本轮不合并主分支、不发版、不自动修改生产库。

# 对齐与大书导出内存占用（1.3 GiB 的归因与基线）

- 状态：诊断完成（2026-09-12），未修改产品代码
- 报告：[reports/memory-alignment-export-2026-09-12.md](../../reports/memory-alignment-export-2026-09-12.md)（数据 JSON 同名）
- 起因：2026-09-11/12 真实库基线测得 MiniLM 对齐场景 OS RSS 高水位 ~1.3 GiB，与高频短词查询并列为本轮性能议题；原真实库导出对象仅 474 段，不能代表超大书。

## 事实（2026-09-12，带报告引用）

- 峰值 RSS ~1.3 GiB 全部产生于对齐的嵌入阶段：`model_load`（`TextEmbedding` 构造，0.5 s，~843 MiB）+ batch-64 推理期增长（真实对 ~+440 MiB）；DP 计算（`compute`）与写库（`publish`）的 OS 峰值增量为 0。任务完成后稳定驻留 0.57–0.72 GiB（受机器状态影响，轮间序列为准）。
- 产品常态路径（`.npy` 向量缓存复用）下同进程重复执行无持续增长：4 轮稳定驻留第 3 轮起持平。
- 每轮重新加载模型（删除向量缓存强制重算）时，稳定驻留 ~+17 MiB/轮线性增长（6 轮 642→715.9 MiB 未达平台），重算峰值同步抬升（1177→1348 MiB）；tracemalloc 证明 Python 堆每轮仅 2.7→3.3 MiB，保留在原生侧（ORT/分配器，具体机制未定）。产品触发场景：同一后端进程连续首算多本新书——`FastEmbedEmbeddingProvider.__call__` 每次调用都新建 `TextEmbedding` 会话。
- batch size 单变量对照（64/16/8/4）：任务峰值 1233.9/976.1/911.7/872.1 MiB，嵌入耗时无一致代价；稳定驻留与 batch 无一致关系。
- 独立最小复现：加载 MiniLM 后 `del`，进程 RSS 从 806.3 降至 417.6 MiB 而非回到基线——单次加载/卸载即保留 ~400 MiB。
- 导出：真实 474 段书整任务 0.15–0.17 s、进程 48→61 MiB、4 轮平坦；合成 50k 段（seed 20260910）任务瞬时峰值 ~134–162 MiB，6 轮稳定驻留收敛于 ~175 MiB；Python 堆峰值在 normalize 相位（81 MiB）。导出无泄漏。

## 推断

- 嵌入峰值的机制是 ORT 激活/arena 随 batch 缩放：**强推断**（随 batch 单调缩放、随会话拆除整体消失），无 allocator 级直接证据。
- fresh 路径每轮增长的具体归属（ORT 环境对象 / malloc zone 保留 / arena 释放不完整）：**弱推断**，未做 allocator 级实验，不得写成已确定原因。
- 早期测量中"相位窗口内采样 RSS 高于 ru_maxrss"的反常为本轮测量工具的相位结束事件归属 bug（已修复），不是 macOS 内核口径问题（独立复现中 ru_maxrss 对构造期突发跟踪精确）。

## 后续候选（未实施，见报告"尚未验证的优化建议"）

1. batch 64→16（单常量）：峰值 −21%；前置验证 = 同批文本逐向量比较 + 链接身份比较（纯测量）。
2. 跨任务复用嵌入会话（模型生命周期重构，超出最小改动边界）：需组件管理设计，单独立项。
3. 导出 normalize 分块：当前收益小，不建议。

## 测量设施

`scripts/mem_profile_{common,alignment,export}.py`（本轮入库，unittest 见 `tests/test_mem_profile_harness.py`）；合成 fixture 构建在子进程执行以保持被测进程 OS 高水位干净；真实库 id 与私人数据不入公共报告（写入时长度守卫拒绝 >512 字符字符串）。

## 2026-09-13 更正(工具修复后)

以本节为准,详见 [报告"2026-09-13 更正"节](../../reports/memory-alignment-export-2026-09-12.md)。

- **测量工具缺陷已修复**(提交 `88cdb65`):RSS 最大值漏边界事件、样本无事件抛错、嵌套 tracemalloc 峰值被清零、多轮同名统计串用、导出摘要 whole-file 读入污染常驻测量、`resource` 顶层导入致 Windows 失败。
- **事实(RSS 类)维持**:用唯一存有逐样本时间线的 `real-align-timeline.json` 以修复后工具重算,各相位 rss_max 与原报告一致(仅 model_load +0.7 MiB)。"1.3 GiB=嵌入瞬态峰值、DP/写库 OS 峰值增量为 0、缓存复用路径实测轮数内无持续增长"这些 RSS 结论不受工具缺陷影响。
- **事实(tracemalloc)需分级**:同级相位(导出 load_pages/normalize/render_markdown)峰值有效;**包含子相位的外层**(对齐 embed⊃model_load、task_total;导出 epub_write⊃render_epub_zip)旧峰值被子相位 reset 低报——修复后重跑导出得 render_epub_zip=epub_write=32.6 MiB。逐轮 tracemalloc 峰值可能因旧的按名共用而张冠李戴,不得按轮精确引用。
- **结论收紧**:①"导出无泄漏"→"实测轮数内未观察到持续增长(收敛/持平)",非无泄漏证明;②"batch 无速度代价"→弱观察、受满载漂移与并发负载污染,不作结论;③"跨任务复用会话消除常驻地板"(候选 B)→**未验证推断/预期**,非已测事实(fresh 每轮 +17 MiB 与单次加载卸载保留 ~400 MiB 是观察,"复用即消除"是推断)。
- **未完成(阻塞)**:batch64-vs-16 前置实验(逐向量 + 完整 links/status/scores/anchors 比较、交错运行、固定模型/线程/文本/缓存/环境)因**本地无 `minilm-l12-v2` 模型缓存**(需联网下载,违反本地优先)**未执行**;对齐侧外层 tracemalloc 精确重测同样阻塞。**在实验完成前不得修改产品默认 batch(维持 64),不得据"差异很小"接受 batch16**,须评估质量门槛与缓存版本(`EMBEDDING_RUNTIME_VERSION`)语义。

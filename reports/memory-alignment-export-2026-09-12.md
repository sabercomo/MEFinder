# 对齐与大书导出的分阶段内存测量基线

> **更正在文末("2026-09-13 更正"节)——以该节为准。** 测量工具修复了若干统计缺陷(RSS 边界、嵌套 tracemalloc 峰值、多轮同名、导出摘要污染、Windows 导入);RSS 类结论经时间线重算基本不变,但 tracemalloc 外层峰值曾低报、部分结论(无泄漏/无速度代价/会话复用消除地板)已收紧为观察/推断/未验证。batch64-vs-16 前置实验因**本地无模型缓存**(需联网下载,违反本地优先)**未执行**,产品默认 batch 维持 64。

2026-09-12：建立可重复、可比较、可解释的对齐与导出分阶段内存测量基线。本轮为纯诊断，未修改任何产品代码。核心结论：此前观察到的对齐峰值 RSS ≈1.3 GiB 是**嵌入推理期的瞬态峰值**（模型加载 ~0.5 s 占 ~843 MiB，batch 64 推理再涨 ~430 MiB），任务完成后稳定驻留约 0.57–0.72 GiB；向量化缓存复用路径（产品常态）下重复执行**无持续增长**，而每轮重新加载模型的路径稳定驻留以 **~17 MiB/轮线性增长**（6 轮未达平台），属原生分配保留。导出在真实 474 段书上仅 ~60 MiB，50k 段合成大书任务峰值 ~134–162 MiB、多轮后收敛于 ~175 MiB，无泄漏。

数据：[JSON 汇总](memory-alignment-export-2026-09-12.json)。原始测量 JSON（含逐样本时间线）在未跟踪的 `.codex-tmp/mem-profile/`，真实库 id 不入公共报告。

## 方法

- 测量脚本（本轮新增，入库）：`scripts/mem_profile_common.py`（探针：psutil 25 ms 采样当前 RSS + 子进程树 RSS、`resource.getrusage` OS 历史峰值、可选 tracemalloc 逐相位 Python 堆峰值）、`scripts/mem_profile_alignment.py`、`scripts/mem_profile_export.py`。测量通过模块属性包装（monkeypatch）把产品调用点变成相位，不修改产品代码。
- 对齐相位：`prep`（分段集+folio 候选，写事务 1）→ `embed`（向量缓存加载 + ONNX 推理，内含 `model_load`）→ `post_embed_prep`（folio 验证/正文范围/标题锚）→ `compute`（带状单调 DP + 注释覆盖）→ `publish`（写事务 2）→ `finalize`。
- 导出相位：`snapshot_load`（BEGIN DEFERRED 读事务，含页载荷物化）→ `load_pages`（页列表+导出布局）→ `normalize`（脚注配对/页码档案/导出结构）→ `render_markdown` / `render_epub_zip`+`epub_write` → `finalize_write`。
- 每轮任务后"稳定驻留"= 等待 RSS 自然平稳（阈值 1 MiB 持续 2 s）后读取；**不调用 gc.collect、不清缓存、不卸载模型**。
- 真实库协议沿用冻结快照 `.codex-tmp/real-library-20260911/`（65 部文献；对齐对 2047/1901 segments、967 links，与 [503 修复复测](performance-real-alignment503-fix-2026-09-12.md) 完全一致）；DB 与模型缓存复制到临时目录，未触碰用户库。重复实验输出 counts 逐轮一致（967/795/94/78、92+30 锚）。
- 合成数据：`scripts/performance_fixture.create_fixture`，seed 20260910，fixture_version 1（公开、可重复；段落近似 1 segment）。合成对齐梯度 400/1600 段（真实模型全管线）与 800/3200/12800 段（stub 向量，隔离 DP）；导出梯度 5k/20k/50k 段（Markdown 与 EPUB）。合成 fixture 在**子进程**中构建，避免建库内存进入被测进程的 OS 高水位。
- 证据口径：当前 RSS（psutil，25 ms 采样）、OS 历史峰值（ru_maxrss）、Python 可追踪分配（tracemalloc，单独模式运行）、子进程树 RSS（全程为 0：对齐与导出均无子进程）。NumPy 分配计入 tracemalloc（numpy 自带 tracemalloc 域）；ONNX Runtime 原生分配不可见，用"RSS 增量 − Python 堆增量"差分作为原生证据。

## 已测量事实（对齐）

真实库对齐（MiniLM，自然生命周期 4 轮，2026-09-12 晚，绝对值受机器状态影响较大，趋势为准）：

| 相位（round 1） | 耗时 | RSS 起→止 | 阶段最大 RSS | OS 峰值止（Δ） |
|---|---:|---:|---:|---:|
| prep | 0.36 s | 98.5→115.6 | 115.6 | 98.5（+0） |
| model_load | 0.52 s | 115.6→843.3 | 843.3 | 843.3（**+727.7**） |
| embed（推理） | 73.18 s | 115.6→696.5 | **1277.2** | 1283.0（**+1167.4**） |
| post_embed_prep | 0.45 s | 696.5→696.8 | 696.8 | — |
| compute（DP） | 2.25 s | 696.8→702.7 | 702.7 | —（**Δ0**） |
| publish | 1.41 s | 702.7→702.7 | 702.7 | —（**Δ0**） |

- 任务后稳定驻留（4 轮）：699.7 → 702.5 → 704.5 → 704.5 MiB（第 3 轮起持平）。同实验早些时候的独立进程为 571.3→585.2→586.4→592.2 MiB——**跨进程绝对值可差 ~100 MiB，轮间增量才是泄漏信号**。
- OS 历史峰值 1283 MiB 与此前 bench 报告的 ~1.3 GiB 一致；它只出现在 embed 阶段，任务后不复现（第 2–4 轮 ru 增量为 0）。
- 嵌入推理 RSS 形态（逐样本时间线，独立一轮）：首批后 ~851 MiB，随批次推进缓慢爬升至 ~1173 MiB；推理结束、provider 返回的 ~0.2 s 内 ONNX 会话拆除释放 ~605 MiB → ~568 MiB。
- 对齐 400 段合成对（仅 2×400 segments）任务峰值即达 844.6 MiB——模型加载/会话是峰值与稳定驻留的地板（~843/~540），与书的大小关系不大；1.3 GiB 中的其余部分随 segment 数增长。
- batch size 单变量对照（round 1，真实对）：峰值 batch64 1233.9 / batch16 976.1 / batch8 911.7 / batch4 872.1 MiB；嵌入耗时 65.1 / 55.7 / 58.3 / 53.2 s（受长时满载漂移影响，只能得出"无速度代价"的弱结论）。稳定驻留（571–700 MiB）与 batch 无一致关系。
- DP 单独标定（stub 向量，无模型）：800/3200/12800 对时任务峰值 102.7 / 144.5 / 172.8 MiB，多轮持平——DP 在 2.5 万段对上也不是内存主导。

## 已测量事实（导出）

真实书（474 段 / 242 页）：

| 相位（round 1, Markdown） | 耗时 | RSS 起→止 | 阶段最大 RSS |
|---|---:|---:|---:|
| snapshot_load（含载荷物化） | 0.04 s | 48.1→56.0 | 56.0 |
| normalize | 0.07 s | 56.0→57.9 | 57.4 |
| render_markdown | 0.01 s | 57.9→57.9 | 57.9 |
| task_total | 0.15 s | 48.1→58.0 | 57.9 |

- 4 轮稳定驻留 58.0→60.8 MiB，EPUB（0.17 s/轮，135,612 bytes）同样平坦；子进程树 RSS 恒为 0。
- 合成梯度（干净历史，Markdown；EPUB 同量级）：任务瞬时峰值 5k 47.4 / 20k 74.3 / 50k 133.9 MiB；稳定驻留 5k 46.8→55.9、20k 70.6→100.7、50k 103.5→156.4（3 轮仍在涨）。**50k×6 轮曲线：102→145→169→170→171→178，第 4 轮起收敛**——导出的逐轮增长会饱和，不是泄漏。
- 50k 段 tracemalloc：Python 堆峰值 load_pages 44.6 / normalize 81.0 / render_markdown 64.3 MiB，任务结束 Python 层仅剩 ~0.8 MiB。瞬态 RSS 高于 Python 活对象的差额是分配器（macOS libmalloc）对小对象高频分配的放大/碎片，属原生行为。

## 基于证据的判断

1. **1.3 GiB 是嵌入推理瞬态峰值，不是稳定驻留**（直接证据：三组独立运行的相位表与逐样本时间线一致；峰值全部落在 embed 阶段，DP/写库 ru 增量为 0）。
2. **峰值 = 模型加载地板（~843 MiB）+ batch-64 激活/arena 增长（真实对 ~440 MiB）**。batch 对照（64→16/8/4 峰值 −258/−322/−362 MiB）为直接证据；"arena/激活"的具体机制是强推断（峰值随 batch 缩放、随会话拆除整体消失），无 allocator 级直接证据。
3. **产品常态（向量缓存复用）下重复执行不泄漏**（直接证据：自然生命周期 4 轮稳定驻留第 3 轮起持平，轮间增量 +2.8/+2.0/0.0 MiB，且第 2 轮起不再产生新的 OS 高水位）。
4. **每轮重新加载模型的路径存在原生内存的近似线性保留：+17–20 MiB/轮，6 轮无平台**（直接证据：fresh-vector 对照 6 轮稳定 642→715.9 MiB、重算峰值 1177→1348 MiB 同步抬升；每轮嵌入的是同一批文本，故与数据量无关）。Python 堆每轮结束仅 2.7→3.3 MiB（tracemalloc，直接差分证据）→ 保留发生在原生侧（ORT/分配器），具体归属为**弱推断**（未做 allocator 级实验）。产品影响场景：同一进程内连续对齐多本新书（每本首算都要新加载模型——`FastEmbedEmbeddingProvider.__call__` 每次 `embed_texts` 都新建 `TextEmbedding`）。
5. **导出无泄漏**（直接证据：真实书 4 轮平坦；50k 段 6 轮收敛于 ~175 MiB）。
6. **单次模型加载/卸载后进程仍保留 ~400 MiB**（独立最小复现：加载→del 后 RSS 13.8→806.3→417.6 MiB；直接证据，机制弱推断）。这与对齐任务后 ~500–570 MiB 的稳定驻留地板一致。

## 尚未验证的优化建议（本轮未实施）

| 候选 | 预期内存收益 | 速度影响 | 复杂度/风险 |
|---|---|---|---|
| A. 嵌入 batch size 64→16（`semantic_alignment.py` 单常量） | 峰值 −~258 MiB（−21%）；稳定驻留不变 | 无代价（噪声范围内） | 极低。风险：不同 batch 形状下 ORT 数值可能有低位差异，向量缓存语义需决定是否 bump `EMBEDDING_RUNTIME_VERSION`；边界链接可能翻转 |
| B. 跨任务复用一个 `TextEmbedding` 会话（模型生命周期重构） | 消除每任务 ~0.5–0.8 s 加载与 ~400–500 MiB 保留地板；预期消除 fresh 路径的每轮增长 | 每任务省加载时间 | 高。涉及模型生命周期与组件管理，本轮明确不做，仅设计方案 |
| C. 导出 normalize 分块/流式 | 50k 段时 Python 堆峰值 81 MiB→更低，瞬态 ~134 MiB→更平 | 需重构 normalize 的两遍结构（脚注配对需全局视图） | 高，当前收益小，不建议 |

**推荐优先验证**：候选 A 的前置诊断——同批文本 batch 64 vs 16 逐向量 bitwise 比较 + 真实对链接身份比较（纯测量，不改代码）。若数值一致（或差异可忽略并决定 bump runtime version），A 即满足"根因明确、改动小、收益可用同一套测量验证"的最小改动条件。

## 复现

```bash
PY=.venv-macos312-arm64/bin/python
# 真实库对齐（自然生命周期 4 轮；id 见 .codex-tmp/mem-profile 运行记录，报告 JSON 不含 id）
$PY scripts/mem_profile_alignment.py --db .codex-tmp/real-library-20260911/index.sqlite3 \
  --group <group> --pivot <pivot> --target <target> --rounds 4 \
  --output .codex-tmp/mem-profile/real-align-natural.json
# 每轮重算向量对照 / batch 对照
$PY scripts/mem_profile_alignment.py ... --rounds 6 --fresh-vectors-per-round --output .../fresh6.json
$PY scripts/mem_profile_alignment.py ... --rounds 1 --force-batch-size 16 --output .../batch16.json
# 真实库导出
$PY scripts/mem_profile_export.py --db .codex-tmp/real-library-20260911/index.sqlite3 \
  --source-id <source> --format markdown --rounds 4 --output .../real-export-md.json
# 合成梯度（seed 20260910）
$PY scripts/mem_profile_export.py --synthetic --format markdown --rounds 3 --synthetic-paragraphs 50000 --output .../syn-export.json
$PY scripts/mem_profile_alignment.py --synthetic --provider real --rounds 2 --synthetic-alignment-paragraphs 1600 --output .../syn-align.json
$PY scripts/mem_profile_alignment.py --synthetic --provider fake --rounds 2 --synthetic-alignment-paragraphs 12800 --output .../syn-dp.json
# Python 堆证据（单独模式）
$PY scripts/mem_profile_alignment.py ... --rounds 2 --fresh-vectors-per-round --tracemalloc --output .../traced.json
```

## 测量口径限制（必读）

- macOS `ru_maxrss` 与采样 RSS 在事件边界一致（独立复现验证），但它是单调历史峰值，**不能**把"峰值不回落"解释为泄漏；重复任务增长判据只用每轮稳定后的 current RSS。
- 绝对稳定驻留受机器/进程历史影响（同实验跨进程可差 ~100 MiB）；本轮所有"增长/收敛"结论都基于同进程轮间序列。
- tracemalloc 模式拖慢重相位（embed 73→103 s），其耗时与 RSS 不与未插桩运行混算。
- 长时满载后机器存在 +25% 级均匀漂移（见 [503 复测报告](performance-real-alignment503-fix-2026-09-12.md) 的同刻对照经验），嵌入耗时结论只作"无代价"级使用。
- 本轮为进程内 harness（无 HTTP 层）；与既有 bench 的 ~1.3 GiB 对齐良好（1283 vs 1305 MiB），但空闲基线比完整后端进程低 ~30–40 MiB，比较绝对值时须知。

---

## 2026-09-13 更正:测量工具缺陷修复 + 结论收紧 + 未完成项

本节对上文做三件事:标记哪些统计受工具缺陷影响、能从时间线重算的先重算、把过强结论收紧为观察/推断/未验证三级。上文原始数据与表格保留不删,以本节为准。原始测量 JSON 仍在未跟踪的 `.codex-tmp/mem-profile/`(未删除)。

### 一、工具缺陷(2026-09-13 修复,提交 `88cdb65`)

`scripts/mem_profile_common.py` 修复:①`resource` 顶层导入致 Windows 导入失败(改可选,缺失标 `ru_maxrss_available=false`,不填零);②`stats_between` 的 RSS 最大/最小值只用定时样本、漏掉起止边界事件,且样本无事件时把样本元组当 dict 索引抛错;③嵌套阶段 `tracemalloc.reset_peak` 清零并丢失外层峰值;④多轮重复同名阶段的 traced 计数按名共用一条记录(跨轮串用);⑤导出摘要 `read_bytes` 全量读入撑大 RSS、且在稳定驻留 settle 之前污染常驻测量。复现测试见 `tests/test_mem_profile_harness.py`。

### 二、哪些统计受影响 / 重算与补跑

- **RSS 类统计(阶段最大 RSS、稳定驻留、峰值)——基本未受影响,结论维持**。缺陷②只可能**低报**短相位的 rss_max(漏掉边界瞬时)。用唯一保存了逐样本时间线的 `real-align-timeline.json` 以修复后 `stats_between` **重算**:各相位 rss_max 与原报告完全一致,仅 `model_load` +0.7 MiB(边界事件略高于采样)。embed/embed_compute/compute/publish/task_total 全部 0 变化。故上文对齐相位表与"1.3 GiB=嵌入瞬态峰值"的 RSS 结论不受工具缺陷影响。其余轮次的原始 JSON 未保存逐样本时间线(只存派生统计),无法重算;但缺陷②对充分采样的长相位(embed 73 s)影响可忽略,方向上只会低报而非高报。
- **tracemalloc 峰值(Python 堆峰值)——外层/嵌套相位受缺陷③④影响,须区别对待**。上文导出的 `load_pages/normalize/render_markdown` 是**同级**相位(各自 reset),其峰值不受嵌套 bug 影响;但**包含子相位的外层**(对齐 `embed`⊃`model_load`、`task_total`;导出 `epub_write`⊃`render_epub_zip`)的旧 tracemalloc 峰值被子相位 reset 清零而**低报**。修复后**重跑**导出(EPUB,20k 段,模型无关故可跑)得正确嵌套峰值:`render_epub_zip`=`epub_write`=32.6 MiB、`task_total`=34.3 MiB(外层正确包含子相位峰值)。对齐侧的外层 tracemalloc 峰值**无法从现存数据重算、也无法补跑**——见"未完成项"。原报告"Python 堆每轮结束仅 2.7→3.3 MiB"是 `traced_current`(相位结束存活量,非峰值),不受缺陷③影响;但多轮同名(缺陷④)意味着**逐轮 tracemalloc 峰值可能张冠李戴**,不得按轮精确引用。
- **导出稳定驻留——旧值可能被摘要读入轻微抬高**。缺陷⑤下旧流程在 settle 前用 `read_bytes` 全量读出导出文件。修复后(有界流式哈希 + 摘要移到 settle 之后、独立 `summary_hash` 相位)重跑 EPUB 20k:稳定驻留 132.7→170.2 MiB,与原报告 50k"收敛 ~175 MiB"同量级,差异在采样噪声内;导出"逐轮收敛非泄漏"的形状结论维持,但绝对稳定驻留旧值含摘要读入的少量瞬态,以修复后为准。

### 三、结论收紧(观察 / 推断 / 未验证)

- "**导出无泄漏**" → 收紧为**观察**:"在实测轮数内(真实 4 轮、合成 50k 6 轮)未观察到持续增长,逐轮收敛/持平"。这**不等于证明无泄漏**——未做长时/大量轮数、未做 allocator 级验证。
- "**batch 无速度代价**" → **弱观察,不作结论**:嵌入耗时对照(65.1/55.7/58.3/53.2 s)受本机长时满载 +25% 级漂移污染,且 batch 计时在并发负载下不可靠;只能说"本采样未见明确速度代价",不能断言"无代价"。
- "**跨任务复用会话可消除常驻地板**"(候选 B)→ 明确标为**未验证推断/预期**,非已测事实。fresh 路径每轮 +17 MiB、单次加载/卸载保留 ~400 MiB 是**观察**;"复用会话即可消除"是**推断**,需真正实现并测量才能确认(本轮不实施)。
- fresh 路径每轮增长的**原生归属**(ORT 环境/malloc zone/arena)维持**弱推断**标注,未做 allocator 级实验。
- "1.3 GiB = 模型加载地板 + batch 激活/arena" 的**地板部分是观察**(batch 对照单调缩放 + 会话拆除整体消失),**"arena"机制命名是推断**(无 allocator 级证据)——维持原报告标注。

### 四、未完成项(如实,未绕过)

- **batch64 vs batch16 前置实验(逐向量 + 完整链接身份比较)本环境无法执行**:默认模型 `minilm-l12-v2`(`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`)的本地缓存在本机/仓库树中**不存在**(遍历 `.codex-tmp`/HF 缓存/`components` 均无 `model.onnx`)。真实 batch 数值对比必须用真实 ONNX 会话,缺缓存则需联网下载——违反本地优先红线,**不执行**。该实验(含固定模型文件/线程数/文本与顺序/缓存状态/交错运行,逐向量差异 + 完整 links/status/scores/anchors 比较)推迟到模型缓存就位且机器安静时进行;在此之前**不得**修改产品默认 batch(仍为 64),也不得据"差异很小"接受 batch16。
- **对齐侧外层相位 tracemalloc 峰值的精确重测**同样阻塞于模型缓存缺失(对齐需真实模型);导出侧已用修复后工具重跑确认嵌套峰值正确。
- 正式三轮四场景服务器基准(与搜索验收共用)因并发负载推迟。

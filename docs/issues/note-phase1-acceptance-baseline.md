# 阶段1 验收基线（可复用对照基准）

- 建立日期：2026-09-14
- 目的：为六阶段模块化重构提供**每项改动都能找到前后对照对象**的基线。本阶段以整理已有证据为主，不重跑长实验。
- 版本锚点：当前产品 = 0.5.4（构建源 `b1f0082`），main/集成分支 HEAD `509d20b`。
- 配套交付：包体积清单见 [`reports/package-size-inventory-v0.5.4-2026-09-14.md`](../../reports/package-size-inventory-v0.5.4-2026-09-14.md)。

> 事实与推断分离(§1.2)：本文标注**事实**(带代码/报告引用)与**推断/待验证**。

---

## 1. 固定测量输入（不得随意变更，变更即新协议基线）

### 1.1 测量工具链版本（本机 `.venv-macos312-arm64`，2026-09-14 实测）

| 组件 | 版本 |
|---|---|
| Python | 3.12.10 (arm64) |
| numpy | 2.5.2 |
| onnxruntime | 1.29.0 |
| fastembed | 0.8.0 |
| tokenizers | 0.23.1 |
| PyMuPDF (fitz) | 1.26.5 |
| psutil | 7.2.2 |

> 注意：onnxruntime **1.29.0** 是已知遥测退出崩溃版本（cautions 2026-09-12）；升级前先核对上游 #24579/#26445。

### 1.2 数据集

- **公共合成 fixture**（可入库、CI 可跑）：`scripts/performance_fixture.py` 的 `create_fixture`，确定性生成 2 本书 / 20 段 / 8 对对齐段，经真实对齐 seam 生成一次。用于隔离类验收（`test_core_without_alignment`、`test_alignment_component_isolation`）。
- **私有真实库快照**（性能/对齐基线用，**禁提交**）：`.codex-tmp/real-library-20260911/`（`index.sqlite3` + `manifest.json`）。快照与原句留本地，报告 JSON 只含哈希与计数。`--compare` 会拒绝语料/模型/环境/驱动不一致。

### 1.3 冻结查询集（`scripts/bench_real_library.py`，`fixture_version: real-library-1`，8 条）

`zh_exact`(exact) · `script_variant`=「社會」(exact) · `en_exact`(exact) · `common_zh`=「社会」(auto) · `common_en`=「gender」(auto) · `normalized`(auto，插入中文逗号) · `no_hit`=`MEFinderBaselineNoHit7f83b92c60a4`(exact) · `scoped`=「社会」(auto, source_type=pdf)。每条 `limit=10`。manifest 记 `content_sha256` 与 `database_bytes` 钉死快照。

### 1.4 对齐样本

- batch64-vs-16 前置样本：`scripts/batch_size_compare.py`，模型在 app 运行时目录，**比较对象为两次真实对齐各自写入的实际逐段生产向量**（`document-vectors/*.npy`，即生产 去重→推理→回填 后的向量，非另行原文重嵌入）。见 [`reports/batch-size-compare-2026-09-13.md`](../../reports/batch-size-compare-2026-09-13.md)。
- 结论 **manual-review-required**：链接结构完全一致（967=967，成员/状态/锚/顺序全同，翻转 0），但 confidence/cost 有极小非零差异（`max_confidence_delta=8.68e-5`、`max_cost_delta=1.02e-4`），逐段向量 96.8% 逐位一致（`max_abs 3.5e-4`，差异集中在中/长文本）。工具只报告事实与差异、**不自设阈值判"可采纳"**；**产品默认维持 batch64 不变**。**多对泛化样本尚缺**（阶段4 补）。

---

## 2. 核心使用闭环验收清单（导入 → 搜索 → 定位 → 阅读 → 导出）

每项给出**验收动作**与**权威守卫**（改动后必须仍成立）。

| 环节 | 验收要点 | 权威守卫 |
|---|---|---|
| 导入 | 三格式白名单 `{.pdf,.docx,.epub}` 四处同步(web_http / document_file_store / document_query_service / document_deletion)；EPUB/DOCX 走 `corpus/raw_docx` 文本通道，单本 `index_text_document` 发布不重建全库 | `tests/test_pdf_support.py`、`test_epub_*`、AGENTS §0/§3.5 |
| 搜索 | 与 PDF/Word 同一 `searchable_paragraphs` + FTS5 trigram；`source_type∈{all,word,epub,pdf}`，EPUB 靠 `source_format=='epub'` 区分；**比较必须含完整响应**（非部分字段） | `scripts/ab_search_compare.py`（13 项字段敏感性）、`test_search_controls_and_views.py` |
| 定位 | 结果带页码锚点 + 字符区间（红线3）；EPUB 只用出版方页码，无则 `uncalibrated`（红线4） | `test_alignment_anchor_gates.py`、MCP `locate_quote` |
| 阅读 | 主窗口正文可鼠标框选复制；上下文每侧只显最近真实段并默认折叠；独立阅读窗口开关在设置→PDF阅读 | `test_reader_windows.py`、cautions 2026-09-11 |
| 导出 | Markdown/EPUB 共享 `markdown_export_normalize` + 同一 `PageMarker`；默认页码模式 `printed`；`document_export_service` **纯读**，库写入走 application 层 + durable_operations | `test_markdown_export*`、`test_epub_export*`、cautions 2026-09-12 |

---

## 3. 译本对照 + 计算组件缺失降级清单

阶段2/3 改动**不得破坏**下列既有降级行为。权威守卫：`tests/test_alignment_component_isolation.py`、`tests/test_core_without_alignment.py`、`tests/test_text_alignment_coordinator.py`、`tests/test_backend_standalone_process.py`。

**验证状态**列区分:**已验证**=本轮 2026-09-14 实跑上述守卫(11 项全过)确认的当前行为;**现状/代码**=以当前代码为准的现状描述;**待阶段验证**=下一阶段须实现或补验的验收要求,非既有保证。

| 场景 | 期望行为 | 验证状态 |
|---|---|---|
| 无计算组件（禁 import numpy/fastembed/onnxruntime） | 搜索、读取/定位**已存**对齐结果、优雅退出**全部可用**；`test_core_without_alignment` 冷启 HTTP 后端佐证 | 已验证 |
| 无计算组件下发起生成 | **本地明确失败**并给安装提示；**绝不触发隐藏联网下载** | 已验证（`test_alignment_component_isolation`） |
| 删除模型组件 | **不触碰已存结果**（结果在 DB，不在组件） | 已验证（同上） |
| 仅管理组件（摘要/下载记账） | 不 import 计算栈本身 | 已验证（同上） |
| 缺 NumPy | 保留 stored-link / 字符锚点导航；numpy 仅在 `align_segment_sequences` 内 lazy import，缺失不影响启动与读取。**"有/无组件定位完全一致"未验证**，不得如此声称 | 现状/代码（一致性待阶段验证） |
| 对齐生成 | 必须在 `TextAlignmentCoordinator` 门禁后运行：`index_runtime.mutation()` 写事务 + `durable_operations.operation()` + `_write_window`，可取消。**不使用 suspend/reopen**（503 修复后写窗口不再 suspend/reopen 引擎） | 现状/代码（`text_alignment_coordinator.py`） |
| 生成期间搜索 | `_write_window` 把即时不可读转成 30s busy_timeout 内的有界等待——**不是保证成功**；默认 page cache 首次大书写锁超 30s 仍可能 503 | 现状/代码（大书 503 未实测，待验证） |
| 结果发布前 | 确认对应文献/分段身份仍有效，避免把过期任务结果写回 | 现状/代码（阶段2A 须保持并补测） |

**对照读取（只读）边界**：`find_parallel_passages` 是只读候选查询，只把既有对齐当召回中心，须比较源句/前后文/全部候选；证据充分才 `confirmed`，多候选 `ambiguous`，不足 `unavailable`。持久修正只走 `propose→confirm`(一次性 token)→可撤销，只写 `alignment_manual_overrides`。德/法/日等只有目标译本已导入并完成基础对齐才有候选。

---

## 4. 已有报告目录 + 基线标注（区分「当前版本重复」与「真正前后对照」）

> **关键纪律**：以下多为**同版本重复测量**，只能作**当前版本基线**，不得当作优化前后收益。真正的优化前后对照必须用同一（当前）工具分别 checkout 优化前/后产品代码各建基线再比较——**此项尚未做**。

### 4.1 性能（真实私有库，HTTP 协议）

| 报告 | 性质 | 关键结论 |
|---|---|---|
| `performance-real-4scenario-2026-09-13` | 3轮4场景，当前版本基线 | 12 运行 0 错误 / **0 次 503** / idmis=0；峰值 RSS 1279–1310 MiB |
| `performance-real-4scenario-baseline / -compare-2026-09-13` | **同版本重复**(`109e089`)，非前后 | 只证明工具+当前版本能完成完整四场景测量；对齐重查询两次间波动 2.4–3.0× |
| `performance-real-alignment503-fix-2026-09-12` | 对齐期 503 修复同快照对比 | 61/216(28.2%)→0/166；503 二值对比成立，不等于延迟降幅 |
| `performance-baseline-v0.5.4-2026-09-10` + `performance-repeat-v0.5.4-2026-09-10` | 同版本重复 | 当前版本基线 |
| `search-acceptance-round2-2026-09-13` | 搜索验收（**完整请求**） | 更正“18ms”为单路；用户完整繁简联合请求 ~3.6s（繁体全扫主导） |
| `performance-short-query-recall-2026-09-12` | 短词召回优化 | A/B 同快照结果 SHA-256 等价 |

### 4.2 内存

| 报告 | 结论 |
|---|---|
| `memory-alignment-export-2026-09-12` | 三口径不可混用：psutil RSS(逐相位) / ru_maxrss(单调峰值，非泄漏证据) / tracemalloc(Python 堆，ONNX 原生不可见) |
| `memory-alignment-tracemalloc-2026-09-13` | tracemalloc 外层 embed traced_peak 58.3 MiB，Python 追踪分配~58 MiB 远小于 RSS~1038 MiB。**结论:Python 追踪分配不足以解释 RSS 峰值,原生分配可能贡献较大,但具体归属(是否 ONNX)尚未验证**——tracemalloc 看不到原生分配 |

> **2026-09-19 追加（进程级计算生命周期，不覆写上表结论）**：[`reports/memory-app-alignment-2026-09-19.md`](../../reports/memory-app-alignment-2026-09-19.md) 按当前"每任务一个计算进程"重测**整应用**（外部 psutil 20 ms 采样进程树，驱动走 `me_finder serve` + `/api/text-alignments`，真实快照副本 5 本不同书冷算 + 1 次暖路径）。事实：**冷对齐树峰 1120–1383 MiB，其中 worker 占 1056–1297 MiB，后端进程峰值 ≤96.6 MiB**；任务结束计算进程消失，树驻留回落到 30.3–85.6 MiB（占峰值 94–97%），6 个任务后存活计算进程 0、驻留斜率 −15.3 MiB/轮、末值低于任务前 idle 45.7 MiB；swap 未变。**上文 09-12 的"任务后稳定驻留 571–704 MiB""fresh 路径 +17 MiB/轮"在该两条路径上已无复现窗口（结构性切断，非缓解）**；剩下的唯一内存事实是嵌入推理的瞬时峰值，量级与旧架构相同 → **阶段4（batch16）继续暂缓，其重启理由仍是质量/缓存决策而非内存**。未验证：pywebview 壳与 WebView 进程未计入、Windows/冻结包（本轮计算进程回退主解释器）、≥10 本连续、>1 万段单任务。

### 4.3 对齐质量

| 报告 | 结论 |
|---|---|
| `batch-size-compare-2026-09-13` | batch64 vs 16 单组，**实际生产逐段向量**：链接结构完全一致（967=967），但 confidence/cost 非零差异（8.68e-5 / 1.02e-4）、向量 96.8% 逐位一致（max_abs 3.5e-4）；verdict=**manual-review-required**，**默认维持 batch64** |
| `alignment-v22-trial-validation-2026-09-08` | 当前必须 alignment v22 / semantic v20 |
| `e5-disputed-four-adjudication-2026-09-09` | E5 四争议样本；E5 仍实验档，默认 MiniLM |

---

## 5. 可重复命令目录（仓库根执行，无需 PYTHONPATH；本机跑用 venv 全路径解释器）

> 别把裸 `python` 放进管道（Windows 桩 exit 49 会经 SIGPIPE 打断上游）。碰生产库的长命令尤其不要串裸 `python` 管道。

私有路径一律经本地 shell 变量传入，**不把私有文献路径或内容写进本文件 / 提交**。下列变量按本机实际值设置（示例值为本机路径，非入库内容）：

```bash
PY="$PWD/.venv-macos312-arm64/bin/python"
# 私有真实库快照与其原始来源库(禁提交)：
SNAP="$PWD/.codex-tmp/real-library-20260911"          # 已 prepare 的冻结快照目录
SRCLIB="$HOME/Library/Application Support/MEFinder"    # prepare 的来源库(仅首次 prepare 需要)
MODELS="$HOME/Library/Application Support/MEFinder/runtime/components/text-alignment/models"
# batch/内存脚本用的真实对(取自快照 manifest 的 alignment_request，值为本机私有)：
GROUP="document-group-c7e0214336a240d78b687802319cb1bf"
PIVOT="pdf-import-e63bdea39567e3ac"
TARGET="pdf-import-d574027d045ce657"
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
```

```bash
# 全量测试（发布门禁，与 CI 一致）
PYTHONUTF8=1 "$PY" -m unittest discover -t . -s tests

# 阶段1/2A 相关的隔离与协调守卫
PYTHONUTF8=1 "$PY" -m unittest \
  tests.test_core_without_alignment \
  tests.test_alignment_component_isolation \
  tests.test_text_alignment_coordinator \
  tests.test_backend_standalone_process

# 真实库性能基线：先 prepare 一次冻结快照(私有，只读来源)，再对不变快照多进程测量
"$PY" scripts/bench_real_library.py --snapshot "$SNAP" --source-library "$SRCLIB" \
  --models "$MODELS" --rounds 3 --repeats 5 \
  --output "$PWD/reports/performance-real-$(date +%Y-%m-%d).json"

# 搜索 A/B 完整响应比较（同快照，13 项字段敏感性）
"$PY" scripts/ab_search_compare.py --db "$SNAP/index.sqlite3" --repeats 5 \
  --output "$PWD/reports/search-ab-$(date +%Y-%m-%d).json"

# batch64-vs-16 对齐对比（读两次真实对齐的实际逐段 .npy）
"$PY" scripts/batch_size_compare.py \
  --db "$SNAP/index.sqlite3" --group "$GROUP" --pivot "$PIVOT" --target "$TARGET" \
  --models "$MODELS" --output "$PWD/reports/batch-size-compare-$(date +%Y-%m-%d).json"

# 内存画像（合成 fixture 必须子进程构建，避免污染被测进程 ru_maxrss；报告不含 id）
"$PY" scripts/mem_profile_alignment.py \
  --db "$SNAP/index.sqlite3" --group "$GROUP" --pivot "$PIVOT" --target "$TARGET" \
  --models "$MODELS" --rounds 3 --tracemalloc \
  --output "$PWD/reports/memory-alignment-$(date +%Y-%m-%d).json"

# 包体积盘点（解包本地产物后逐组件 du；SHA-256 见包体积报告）
TMP="$(mktemp -d)"; ditto -x -k "$PWD/release/MEFinder-v0.5.4-macos-arm64.zip" "$TMP" \
  && du -sh "$TMP/MEFinder.app" && du -sh "$TMP/MEFinder.app/Contents/Frameworks/onnxruntime"

# macOS 重建打包（自带全量测试门禁，约 10–15 分钟）
MEFINDER_PYTHON="$PWD/.venv-macos312-arm64/bin/python" ./build_macos.sh
```

---

## 6. 成功标准（阶段1 自检）

- [x] 后续每项改动都能找到对应的前后比较对象（§2 守卫 + §4 基线报告 + §1 固定输入）。
- [x] 搜索比较基于**完整响应**（`ab_search_compare` 13 项字段敏感性），不只比较部分字段。
- [x] 性能报告区分成功 / 失败 / 重叠(503) 请求及不同查询（§4.1，503 不计入成功延迟）。
- [x] 明确区分历史观察、同版本波动、优化收益（§4 抬头纪律；同版本重复 ≠ 优化前后）。
- [ ] **真正的优化前后对照**（同工具、分别 checkout 优化前/后产品代码）——阶段1 未做，留待具体优化落地时执行。
- [ ] sidecar onefile 内部逐依赖字节数——需自解压 `_MEI`（须运行二进制），本轮标记待测。

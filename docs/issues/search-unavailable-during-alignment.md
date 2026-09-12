# 对齐期间搜索短暂返回 503

## 2026-09-10 — 首次可重复性能基线

### 事实

证据：[0.5.4 性能报告](../../reports/performance-baseline-v0.5.4-2026-09-10.md)及其中两份原始 JSON。

- 在 `b3db478` 产品代码、64,640 段固定合成库、真实 MiniLM 后台对齐下，基线 191 个重叠搜索中 5 次 503；独立复跑 207 个中 6 次 503。
- 普通搜索与 Markdown/EPUB 导出期间，两次运行均没有 HTTP 错误。
- 所有成功查询的结果及页码/字符锚点摘要不变；后台任务结束后八类查询全部恢复。
- 基线成功搜索 p95 225.07ms，复跑 225.86ms；快速失败未混入成功延迟。
- `TextAlignmentCoordinator._write_window` 在写入时调用 `IndexRuntime.suspend()`，然后 `reopen()`。
  `IndexRuntime.search()` 在 rebuilding 或 engine 不存在时返回 None，`web_http._post_search()` 将其映射为 503。

### 推断

观测与对齐发布期间主动让索引不可读的机制一致。当前探针覆盖整个生成区间，未独立拆分写库、重开以及推理耗时；不能用本报告直接断言各阶段占比。
2.62% / 2.90% 是固定交互负载下的请求失败率，不是任意真实库的不可用时间占比。

### 后续验证标准

后续优化保持当前夹具、驱动、模型和请求节奏，使用 `--compare` 检查成功延迟、503 率、OS RSS 高水位、启动和退出。
同时保持命中数量、顺序和定位锚点，确认任务后搜索恢复。若更改测量协议，应另建基线，不能声称原协议下已改善。

本轮只记录测量证据，未实施发布窗口改造。

## 2026-09-12 同协议复测

[架构修复后复测](../../reports/performance-real-v0.5.4-2026-09-12.md) 的三轮结果：对齐期间 61/216 次重叠搜索返回 503（28.2%，旧基线 30.5%），其余三个场景无失败，成功搜索身份和锚点保持一致。此次仅补齐导出锁边界、可选计算依赖和原型验收，没有解决对齐发布阶段的搜索暂停。下一步应先定位并缩短不可用窗口；将计算放进另一进程并不自动改变共享索引的暂停语义。

## 2026-09-12 修复：写窗口不再关闭共享引擎

### 根因（代码证据）

- `TextAlignmentCoordinator.generate` 全程持有 `index_runtime.mutation()`，但搜索不可用并非来自这把锁——它只串行化导入/删除/题录等写入方。真正的来源是 `_write_window`：`generate_alignment` 的准备与发布两段 `BEGIN IMMEDIATE` 事务各包一层 suspend/reopen（`text_alignment.py` 两处 `with transaction_window()`，coordinator 的 `_write_window`）。`IndexRuntime.suspend()` 置 `_rebuilding=True` 并把 `_engine` 置空，`IndexRuntime.search()` 随即返回 None，`web_http._post_search` 将 None 映射为 503。
- 名为"短写窗口"不代表实际很短。进程内探针（真实快照副本、真实 MiniLM 对齐、真实引擎并发搜索，`.codex-tmp` 私有诊断）测得：准备窗口 663 ms（suspend 关引擎 138 ms + 窗口内读取 517 ms，其中 folio 候选检测 394 ms），发布窗口 1,549 ms（发布写入 1,340 ms + 提交 166 ms），两次 reopen 各 19/35 ms。窗口内重叠搜索 20/20 全部快速 503；两窗口之间 83.3 s 计算期间 0 次失败。合计约 2.2 s/次对齐不可用，与真实库 28.2% 的重叠失败率吻合。
- 两段窗口之间的一致性由全程持有的 mutation 锁保证（期间不存在其他索引写入方），不是靠窗口 1 持续占用 `BEGIN IMMEDIATE`。

### 修复与为什么安全

- `_write_window` 不再 suspend/reopen。对齐写只触及 `alignment_runs` / `alignment_links`(+members) 与 segment 表，段落、页面、目录元数据均不变，因此活的只读引擎（`query_only` + busy_timeout 30 s）在整个写事务期间继续服务搜索。写者也以 busy_timeout 等待在途读者——这与导出读事务、增量导入等其他并发方早已接受的锁语义一致，不引入新的等待量级。
  - **更正（2026-09-12 审计）**：本条早前写作"读者最多等待写者短暂的 EXCLUSIVE 提交段（实测 ~170 ms）"，此前提不成立，见下方"2026-09-12 审计更正"。SQLite 锁的是整库文件；首次大书分段写入溢出 page cache 后会在提交前即升级到持有 EXCLUSIVE，跨表读被阻塞的是整段写入而非仅提交。修复仍成立（把即时不可读转成有界等待），但读者等待被 30 s busy_timeout 上界约束、并非"仅短暂提交"，也非无条件成功。
- 保留不变：对齐算法/阈值/模型/正文范围/锚点语义、两段事务结构与发布原子性（`BEGIN IMMEDIATE` + commit/rollback）、mutation + durable operation 对并发删除/题录修改/取消/退出的协调、失败回滚后可恢复。`IndexRuntime.replace_source` / `rebuild` 的 suspend/reopen 语义不变——它们发布的是搜索可见的数据变更。
- 不需要工作进程：写窗口收窄后 503 归零，`alignment-component-isolation.md` 提出的进程化触发条件未触发。

### 验证

- 复现测试先行：`tests/test_alignment_write_window_availability.py`（Event 控制交错，真实 DB + 引擎 + coordinator，四类场景）在旧代码上以 "search during window1 returned None (HTTP 503)" 失败，修复后通过。覆盖：准备窗口与发布窗口内真实搜索返回一致结果、计算期间搜索可用、发布失败真实回滚（旧行状态不变）且下一次生成自动恢复、取消不落半成品、并发文献替换被串行化到发布之后且级联数据一致、搜索经 reopen 后反映新数据。
- 旧 coordinator 事件序列测试（钉 suspend/reopen 调用形式）改为钉"全程不触碰运行时引擎"的新契约；可用性行为由上述真实并发测试承载。
- `tests/test_backend_standalone_process.py` 增加"对齐期间并发搜索全部 200"段；真实后台进程退出码 0。
- 正式同快照三轮对比（协议、解释器、模型、查询、节奏不变）：见 [对齐 503 修复复测报告](../../reports/performance-real-alignment503-fix-2026-09-12.md)。

### 验证期间发现并一并修复的独立退出崩溃

首轮正式复测第 3 轮对齐场景进程以非零码退出（对齐、全部 56 个重叠搜索与恢复校验均成功之后、收到 stop 的退出阶段）。macOS 崩溃报告（`Python-2026-09-12-174546.ips`）定位：onnxruntime 1.29.0 内置遥测模块（`Microsoft::Applications::Events`）的原生上传 WorkerThread 在 `recursive_mutex::lock()` 抛出未捕获 `std::system_error` → abort。该线程由任何 ORT 会话创建而起，与 503 修复无关；但它 (a) 使正式协议无法稳定完成，(b) 自带 HTTP 上传路径与本地优先原则相悖。修复：`FastEmbedEmbeddingProvider.__call__` 在创建会话前调用 `onnxruntime.disable_telemetry_events()`（回归测试钉住"先禁用、后建会话"的顺序），移除该线程。

## 2026-09-12 审计更正（`ea26e13` 复审）

外部审计（复审 `ea26e13`）指出前述结论有三处需收紧，均已处理，后续轮次以本节为准，不要再引用被更正的旧表述：

1. **`_write_window` 的"仅短暂提交"前提错误**。SQLite 锁的是整库文件而非单表：首次大书分段写入溢出默认 page cache 后，在提交前即从 RESERVED 升级为持有 EXCLUSIVE，跨表读（搜索命中 `paragraphs`）被阻塞的是整段写入而非仅最后提交。修复方向（不关引擎、把即时不可读转成有界等待）仍成立，但读者等待受 30 s `busy_timeout` 上界约束，**不是"仅短暂提交"，也不保证一定成功**（写锁若超 30 s 仍会 503）。注释已更正（`text_alignment_coordinator._write_window`）。
2. **CI 无 ONNX Runtime 时 5 项模拟测试报 `ModuleNotFoundError`**。provider 新增 `import onnxruntime` + `disable_telemetry_events()` 后，只 stub `fastembed` 的测试在缺依赖环境直接 error。已给这些 `sys.modules` patch 补上 `onnxruntime` stub（保留"不装计算组件"的测试环境，而非强装计算组件）：`tests/test_semantic_alignment_runtime.py`、`tests/test_text_alignment.py`、`tests/test_alignment_offline_boundary.py`。以阻断 `onnxruntime` 导入的元路径 finder 复现并验证修复。
3. **性能报告 503 结论过强、混淆两类耗时**。报告已收紧为"本样本未观察到 503（有界等待窗口内未超时），非普适保证"，并把**搜索延迟**与**对齐任务耗时**分列，两者均标注"归因待定、需交错新旧代码复测"。

### 新增可用性测试（覆盖范围与边界，勿越界引用为"已覆盖"）

`tests/test_alignment_write_window_availability.py` 增 `AlignmentSpillingWriteAvailabilityTests`：把写连接 page cache 缩小（`PRAGMA cache_size`）以在小夹具上确定性复现 EXCLUSIVE 升级。**已覆盖**：持锁期间短超时探针读确被阻塞（证伪"仅提交"前提）、生产 30 s busy_timeout 下共享引擎搜索仍返回一致 payload 而非 503、准备事务提交之后（计算阶段）取消不落已发布对齐。

**待验证（本轮未覆盖，禁止当作已覆盖引用）**：

- 持锁写入进行中（尚未提交）取消或退出——当前取消仅在嵌入 batch 边界检查，事务内无取消钩子可驱动；
- 搜索正在等待该锁时应用退出的行为；
- **默认 page cache 下**首次大书真实对齐的实际锁等待时长——缩小 cache 只是确定性复现溢写，未测量生产规模书目产生的真实等待墙钟时间。

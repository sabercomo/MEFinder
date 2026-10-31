# 架构评估收尾三项核验（0.6.1，2026-10-31）

## 范围与基线

本轮从 `d36ce58` 开始，落实 2026-10-31 架构评估的第 5、6、7 项（剩余 SQL 归属、循环依赖守卫盲区、检索层 JSON 后端）。第 1—4 项已于本日完成并推送。本轮不改检索算法、页码策略、schema、HTTP 路由与 MCP 工具接口，不重解析用户文献库。

| 指标 | 调整前 | 调整后 |
|---|---:|---:|
| persistence 之外待收口的 SQL 调用点 | 8 个文件 68 处 | 7 个文件 25 处 |
| 登记为已核实例外的仓储模块 | 无此概念 | 1 个文件 43 处 |
| 导入图可见的包内相对边 | 726（其中 11 条指向并不存在的模块） | 721（全部指向真实模块，含 6 条此前不可见） |
| 检索后端分支判定点 | 8 处 | 0 处 |
| `search/` 包总行数 | 2,971 | 2,693 |
| 全量 unittest | 2,856 | 2,847 |

## 第 5 项：解析任务账本的 SQL 归属

**事实**：棘轮里 68 处 SQL 中有 43 处在 `large_document/job_ledger.py`。该文件不是“业务代码里散落 SQL”，而是另一个数据库 `data/parser_jobs.sqlite3` 的仓储：自带 `LEDGER_SCHEMA_VERSION = 3` 与迁移，连接已经走 `persistence/connection.py`，并持有 `DocumentJob` / `SliceJob` / `CredentialRecord` / `CredentialPageAttribution` 四个领域 dataclass 与两套状态词表。

**判断**：不整体搬进 `persistence/`。两个理由，都可核对：

1. 它顶层 import `large_document.slicing.SliceDescriptor`。搬过去就是 `persistence/` 里的 `from ..` 上行 import，违反既有红线，并被 `test_persistence_layer_does_not_import_domain_modules` 挡住。
2. 搬过去会把 large_document 的领域 dataclass 和状态词表一并拖进存储层，而 `persistence/` 的职责是连接策略、schema 与迁移。既有的 `persistence/parser_job_reads.py` 已经写明“账本本身由 `large_document.job_ledger` 持有”，本轮沿用该归属。

**调整**：棘轮拆成两张表，让“还剩多少散落 SQL”这个数字可信。

- `SQL_EXECUTE_OUTSIDE_PERSISTENCE`（待收口）：7 个文件 25 处 —— `export/service.py` 10、`data_location.py` 5、`alignment/edition_folio_anchors.py` 4、`parsers/statistics.py` 3、`document_deletion.py` / `index_publisher.py` / `indexer.py` 各 1。
- `SQL_EXECUTE_REVIEWED_STORES`（已核实例外）：`large_document/job_ledger.py` 43。

例外不是口子，由四条新守卫钉住：

| 守卫 | 钉住的前提 |
|---|---|
| `test_reviewed_sql_stores_stay_separate_from_the_backlog` | 同一文件不得同时登记在两张表里 |
| `test_job_ledger_is_the_only_sql_owner_in_large_document` | `large_document/` 下执行 SQL 的模块恰好只有 `job_ledger.py` |
| `test_parser_job_ledger_has_one_schema_owner` | 建表/改表语句提到 `document_jobs`、`slice_jobs` 的文件恰好只有 `job_ledger.py`，且其中确有 `LEDGER_SCHEMA_VERSION` |
| `test_parser_job_ledger_reads_from_outside_go_through_persistence` | `persistence/parser_job_reads.py` 只相对 import `connection`，不上行依赖账本 |

例外表同样是棘轮：计数由同一个断言比对，新增调用点必须改对数字。隔离源码树的元测试 `test_reviewed_store_is_still_a_ratchet` 复现了“例外模块多一处 SQL 就变红”。

**未做**：待收口的 25 处未动，仍按 `.project-memory/todo.md` 的后续阶段推进。

## 第 6 项：循环依赖守卫的解析盲区

**事实**：`test_no_new_import_cycles_appear` 只读 `ast.ImportFrom` 的 `node.module`，不看 `node.names`。因此 `from . import native as pdf_native` 这类裸相对 import 的目标被记成父包（顶层模块因 prefix 为空而整条丢掉）。包内现有 8 处这种写法。`from ..persistence import translation_work_reads` 这种“被 import 的名字本身就是子模块”的写法同样失准。

**调整**：先建包内模块名索引，再按索引解析。对每条相对 import，既记 `prefix.module` 这条边，也对每个 `alias.name` 检查 `prefix.module.alias` 是否是真实模块，是则补一条边；另加绝对写法 `import me_finder.x` / `from me_finder.x import y`（`src.me_finder` 前缀同样识别）的解析，包内当前为零，属于守住未来。根 `__init__.py` 只有 `__version__`、不 import 任何模块，不建节点。

**效果**（脚本比对，`src/me_finder`）：旧解析产生 726 条边，其中 11 条指向并不存在的模块名；新解析产生 721 条边，全部指向真实模块，并包含 6 条此前完全不可见的边：

```
alignment.translation_works -> persistence.translation_work_reads
application.script_search   -> script_conversion
document_outline            -> persistence.structured_reader_reads
structured_reader           -> persistence.structured_reader_reads
tasks.runtime_lifecycle     -> alignment.translation_works
translation_work_controller -> alignment.translation_works
```

**结论**：按完整解析重扫，包内循环依赖仍为 0 —— 这不是修一个现存的 bug，是补上将来会漏报的口子。守卫里钉了两条回归用的边（`pdf.extractors -> pdf.native`、`web_runtime -> alignment_assembly`），只有解析了 `node.names` 才看得见，解析退化会立刻变红。

## 第 7 项：检索层的 JSON 索引分支

**事实**：写入侧 JSON 早已降级 —— `build_index` 默认不写 `data/index.json`，只有 CLI 的 `--export-json` 才写（约 300MB），SQLite 一律构建；`search` / `serve` 子命令与 `web_runtime` 的索引路径都是 SQLite。只有读取侧还留着一条 JSON 检索通道：`SearchEngine` 按后缀选后端，`recall.py` 四处、`engine.py` 两处、`recall_passages.py` 与 `assembly.py` 各一处共 8 个分支判定，外加整个 `search/recall_memory.py`（98 行内存召回）。核查下来，这条通道在产品里没有调用方，只被测试当作第二套实现用于交叉比对。

**调整**：JSON 降为纯导出格式，检索只剩 SQLite 一条路。

- 删除 `search/recall_memory.py`；`CandidateRecall` 不再混入 `InMemoryRecallPasses`，构造参数去掉 `backend` / `paragraphs` / `ngram_index`。
- 删除 `recall_passages._scan_passages`、`assembly.paragraph_context` 的内存分支（引擎改为直接调 `sql_context`），以及随之无人使用的 `_source_allowed`、`_ngrams`。
- `SearchEngine` 不再构造内存索引（`paragraphs` / `by_id` / `by_volume` / `ngram_index` 一并移除）；传入非 SQLite 后缀的索引时抛 `UserFacingError`（“检索索引必须是 SQLite 库……”）。老调用点传 `data/index.json` 时继续重定向到 `DEFAULT_DATABASE_PATH`，与调整前该库存在时的行为一致；该库不存在时不再退回 JSON，而是如实报找不到索引。
- `load_index`、`backup_index` 与 `--export-json` 保留，JSON 仍是可回读的导出格式。

**行数**：`engine.py` 316→281、`recall.py` 398→351、`recall_passages.py` 149→117、`assembly.py` 361→295，另删 98 行；`search/` 包 2,971→2,693 行。四个模块的行数上限按删后行数重封，`recall_memory.py` 条目移除。

**测试取舍**（这是本项唯一的真实代价，如实记录）：JSON 后端在测试里是“第二套实现”，有 14 项测试靠它做交叉比对 —— `test_search_group_scope.InMemoryBackendScopeTests`（6 项契约）与 `test_script_search_integration.JsonScriptSearchTests`（8 项契约）。两套实现跑同一份契约确实能互证，但被比对的那一侧产品里没人跑，它自己漂了也没人发现；一个后端消失后，这 14 项退化为重复，故删除。被比对的断言本身没有丢：契约类仍在 SQLite 侧完整运行。

- `test_json_and_sqlite_backends_return_the_same_anchor_payload` 失去对照对象，改写为 `test_emoji_hit_keeps_codepoint_anchors_through_the_database`，对唯一后端断具体值（表情符号命中 `match_start=2`、`match_end=6`、单位 `unicode_codepoint`、页内区间 27–31、`precise_highlight_available` 为真）——比原来的“两边相等”更强，因为两边同时错时原断言不会红。
- `test_search_match_spans.py` 里 8 处 `for factory in (self._engine, self._sqlite_engine)` 收敛为单工厂；两处 `engine.by_id[...]` 改为新增的 `_stored()` 助手，从数据库读回段落再交给 `_format_result`，锚点因此走了一遍存储往返，比原先直接用夹具字典更贴近真实路径。
- `test_search.py` / `test_pdf_support.py` 原先以 `DEFAULT_INDEX_PATH.exists()` 作为“索引已建好”的判据，而默认构建早已不写该文件；改为判 `DEFAULT_DATABASE_PATH`。
- 新增 5 项守卫（第 5 项四条 + 元测试一条）。合计 2,856 → 2,847（−14 重复 +5 守卫）。

**产品影响**：无可观察差异。shipped 的所有入口都传 SQLite 路径；唯一行为变化是对一个产品里不会出现的输入（JSON 索引路径）从“静默走内存检索”改为“明确报错”。

## 验证

- 全量 unittest（macOS arm64，`PYTHONUTF8=1`）：2,847 项通过，39 项条件跳过，190.6 秒。
- `ruff check .`（排除既有未入库 `.codex-tmp`）：通过。
- 第 5、6 项的计数与边集合由一次性脚本在 `src/me_finder` 上实测，数字即上文表格所列。
- 未做：Windows 实机、打包、真实大库检索对照。本轮不触碰检索算法与排序，真实库对照不在本轮门禁内。

## 补记：macOS arm64 测试包（2026-10-31 15:13–15:18，本地，未上传未安装）

重打的原因是上一版 0.6.1 测试包打于 `d36ce58`，不含本轮三项。**第 5、6 项只改测试守卫，冻结包里看不到；能在包里验的只有第 7 项（检索层）。**

- 源码边界：`e736a9c`。工作区只有既有的 `.hallmark/log.json` 修改与三个未跟踪文件（`.codex-tmp/`、两份 draft），都不进包。
- 构建：`MEFINDER_PYTHON="$PWD/.venv-macos312-arm64/bin/python" MEFINDER_TARGET_ARCH=arm64 ./build_macos.sh`，退出码 0。构建内全量 unittest 2,847 项通过（39 跳过，200.580 秒）。
- 产物（替换了 d36ce58 那一版：ZIP 96,309,782 字节 `a895dd23…`、DMG 104,343,671 字节 `a121106a…`）：

| 产物 | 字节 | SHA-256 |
|---|---:|---|
| `release/MEFinder-v0.6.1-macos-arm64.zip` | 96,295,494 | `e7fbc594bde304a926dbead0d53f4846591ad5318feb4b715ad85c0b49349686` |
| `release/MEFinder-v0.6.1-macos-arm64.dmg` | 104,264,926 | `d03d767871d8f0603f21c8b3a70ab7e657a6e2242f5827403972b80066756509` |

脚本自带门禁（`set -euo pipefail`，全部通过）：5 次 `codesign --verify --deep --strict`、4 次侧车 STDIO 冒烟（构建产物 / ZIP 回解 / DMG 挂载 / DMG 拖出副本）、Finder 元数据与 AppleDouble 检查、DMG verify + 挂载 + `/Applications` 捷径检查、ZIP 与 DMG 的 `shasum -c` 自校。

独立复核（在新空目录里 `ditto -x -k` 解 ZIP 后）：

1. `codesign --verify --deep --strict` 通过；包内 106 个 Mach-O 全为 arm64，无非 arm64 条目。
2. 无 `data_root.txt`、无 `*.local.json`、无 `._*` AppleDouble。
3. **第 7 项已进包**：读两个可执行文件内嵌的 PYZ 模块表——侧车 1,262 个模块里 `search` 子包是 11 个（`anchors / assembly / citation / citations / contract / cross_page / engine / recall / recall_passages / scoring` 加 `search` 本身），主程序 754 个模块里是 10 个；两者都**没有** `recall_memory`，而 `recall_passages` 在，确认这项检查不是空断言。
4. **冻结态检索真跑通**：用一次性数据根（`ME_FINDER_APP_DATA_ROOT`，索引建在 `<root>/runtime/data/index.sqlite3`——冻结态 `runtime_root` 是 `<数据根>/runtime`）驱动包内 `MEFinderMCP` 走 STDIO JSON-RPC：`initialize` 返回 `mefinder 0.6.1`，`tools/list` 17 个工具，`list_documents` → “找到 1 篇文献”，`search_passages`（BM25 通道）→ “召回了 1 段原文”，`locate_quote`（逐字通道）→ “找到 1 个原句候选”。全程不接触用户真实文献库。

未做：未安装到 `/Applications`、未上传、未发布；GUI 未人工点击；Windows 与 Intel 包未构建。

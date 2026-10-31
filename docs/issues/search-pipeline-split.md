# 搜索管线固定契约与职责拆分

2026-09-11：把 1,845 行的 `search.py` 拆为单向依赖的阶段模块;拆分前后搜索输出必须逐字段一致。HTTP API 与响应 JSON 键不变。

## 固定契约

- 响应负载由 `tests/test_search_pipeline_contract.py` 钉死:固定公开语料(`scripts/performance_fixture.py`,seed 20260910)+ 18 条固定查询,完整输出(命中、顺序、去重、页码、字符区间、引文格式、上下文、高亮)与金样 `tests/fixtures/search_pipeline_golden.json` 逐字节比对。
- 查询集覆盖全部匹配路径:`exact`、`normalized_exact`(大写英文触发)、`space_insensitive`(compact)、`punctuation_insensitive`、`ngram_fuzzy`,以及繁简折叠(应用层 `execute_with_script_folding`)、段落实检 `search_passages`、单源/集合(含空集合)作用域、`limit=all`、确定无命中。
- 偏移单位契约保持不变:match_start/end 是 `text_raw` 上的 Unicode 码点偏移(`match_offset_unit: "unicode_codepoint"`),JS 侧须转换 UTF-16;PDF 命中必须携带页内 `page_match_spans`。

## 模块划分

| 模块 | 职责 |
|---|---|
| `search.py` | 兼容门面:持有索引/后端状态,编排三阶段;实现 assembly 所需的 `pdf_page_record`/`context` 查询 |
| `search_contract.py` | 管线常量与 `CandidateSpec`;偏移单位与锚点不变量文档 |
| `search_recall.py` | 候选召回:SQLite(FTS trigram + instr 验证)与内存双通道,exact→compact→punctuation→fuzzy 级联;`search_passages` 的 BM25/trigram 检索接缝 |
| `search_scoring.py` | 排序键、候选去重(含跨页 dedup)、`best_window_ratio` 模糊窗口、相对相关度 |
| `search_anchors.py` | 位置锚点:匹配区间→物理页 `page_match_spans`、双开页(spread)侧别解析 |
| `search_citation.py` | 引文信息:书目元数据合并、马克思恩格斯卷判定、copy_text、引用页码状态 |
| `search_assembly.py` | 结果组装:冻结的响应 dict、页显示字段、相邻段落上下文、高亮 |

依赖单向(边界测试 `tests/test_search_pipeline_boundaries.py` 用 AST 钉死):facade → recall/scoring/assembly;scoring → anchors;recall → scoring;assembly → anchors/citation/scoring;任何阶段禁止 import 门面。

## 行为保持的关键点

- `SearchEngine` 公共入口与 `search`/`search_passages` 签名不变;`_format_result`/`_hit_page` 保留为门面委托(测试 patch/直调兼容)。
- 原实现中 SQL 通道的 FTS 表达式取自调用方预计算的 `q_plain`,不从 compact 查询重新推导——拆分后同样显式传参。
- recall 不缓存数据库连接:FTS 缺失重装会重开连接,连接经 `db_provider` 惰性获取(原 `self.db` 语义等价)。
- 空 query 响应仍只有 `query/mode/total/results` 四键。

## 验证

- 金样测试 + 全量 2115 项 unittest 通过(22 skip),Ruff F 零告警。
- 真实库(65 本、63,994 段)端到端:重构前后各跑一轮 `bench_real_library.py --scenario normal`(40 请求/轮),`--compare` 确认 warmup 结果身份(命中数、顺序、原文、字符区间、页锚点摘要)完全一致,成功延迟 p50 比值 0.99–1.08(噪声范围内)。

## 2026-09-29 追加:`search_passages` 的检索接缝拆出 recall

上面模块表的 `search_recall.py` 一行已不完全准确,以本节为准(旧结论保留不删)。

- 事实:`search_passages` 的 BM25/trigram 检索接缝移出 `search_recall.py`,落到新阶段模块 `search_recall_passages.py`(`PassageRetrieval`,与 `search_recall_memory.InMemoryRecallPasses` 同为 `CandidateRecall` 的叶子混入)。直接原因是 `search_recall.py` 撞到 580 行上限;同轮修好了两字查询在 SQLite 后端返回空的缺陷——回退分支原先只扫描内存段落表,而 SQLite 后端从不加载它。
- 事实:接缝的两条读查询下沉到 `persistence/passage_reads.py`;召回级联共用的范围谓词(书目类型 / 单本文献 / 作品组)下沉到 `persistence/paragraph_scope.py`,`search_recall.sql_source_filter` 保留为委托方法,四条 SQLite 通道的谓词只有一处定义。
- 事实:边界表加两行——`search_recall_passages → {search_contract}`,`search_recall → {search_contract, search_recall_memory, search_recall_passages, search_scoring}`;依赖仍单向,新模块不 import 门面。`test_architecture_boundaries.py` 把 `search_recall.py` 上限从 580 降到 477,并为新模块封顶 135。
- 推断:范围谓词进了 persistence 之后,若再把 recall 其余三条 SQLite 通道也下沉,`search_recall.py` 有机会整份移出「persistence 之外执行 SQL」的棘轮基线;本轮未做。
- 验证:`tests/test_search_passages_short_query.py`(6 项,修复前按预期失败)、`tests/test_search_pipeline_boundaries.py`、`tests/test_architecture_boundaries.py`,加全量 2682 项 unittest(34 跳过)与 Ruff F 零告警。

## 2026-10-31 追加:JSON 后端删除,`search_recall_memory` 不复存在

上面两节提到的 `search_recall_memory`(后迁为 `search/recall_memory.py`)已整份删除,边界表的那一行随之移除;旧结论保留不删。

- 事实:JSON 索引在写入侧早已降级——`build_index` 默认不写 `data/index.json`,只有 CLI 的 `--export-json` 才写,SQLite 一律构建;`search` / `serve` 与 `web_runtime` 的索引路径都是 SQLite。读取侧的 JSON 通道在产品里没有调用方,只被测试当作第二套实现用于交叉比对。
- 事实:删除 `search/recall_memory.py`(98 行)与 `recall.py` 四处、`engine.py` 两处、`recall_passages.py`/`assembly.py` 各一处共 8 个后端分支,以及随之无人使用的 `_source_allowed`、`_ngrams`、`_scan_passages`、`paragraph_context` 内存分支。`CandidateRecall` 的构造参数去掉 `backend` / `paragraphs` / `ngram_index`。
- 事实:`SearchEngine` 拿到非 SQLite 后缀的索引改抛 `UserFacingError`;传 `data/index.json` 时继续重定向到 `DEFAULT_DATABASE_PATH`,该库不存在时如实报找不到索引,不再退回 JSON。`load_index`、`backup_index` 与 `--export-json` 保留,JSON 仍是可回读的导出格式。
- 代价(如实记):14 项靠两个后端互证的契约测试(`InMemoryBackendScopeTests` 6 项、`JsonScriptSearchTests` 8 项)删除。被比对的断言没有丢,契约类仍在 SQLite 侧完整运行;`test_json_and_sqlite_backends_return_the_same_anchor_payload` 改写为对唯一后端断具体值,比"两边相等"更强。
- 验证:全量 unittest 2,847 项通过(39 跳过),Ruff 通过。详见 [架构评估收尾三项报告](../../reports/architecture-tail-2026-10-31.md)。

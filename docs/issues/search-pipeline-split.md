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

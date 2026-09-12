# 高频短词搜索召回优化(A/B 等价 + 计时)

> **更正(2026-09-13,见 [第二轮验收报告](search-acceptance-round2-2026-09-13.md)):** 下文 ~18ms 是**单路变体**(`SearchEngine.search`,单个查询字符串)的耗时,**不是用户实际收到的响应**。默认繁简联合下用户搜"社会"会同时检索"社会"与繁体"社會"并合并,真实用户请求由稀有繁体变体全表扫描主导,约 **3.6s**(round-2 前约 5.2s)。单路数字有效,但不得当作"用户搜索已降至 18ms"。

2026-09-12:高频短查询("社会""社"等 <3 字、全库无过滤)**单路**从 ~0.4s(暖)/ ~2.3s(冷)降到 ~18–28ms,**完整结果逐字节不变**(12 条查询结果摘要 SHA-256 一致 `244f91db…`)。scoped、FTS(≥3 字)、繁简稀有词路径按设计不变。未新增索引、未改 schema,未触碰候选预算/评分/去重/total 语义/上下文/引文/页码/字符锚点。

## 诊断(分段计时,冻结快照 `.codex-tmp/real-library-20260911`,62,729 eligible 段)

对每查询按管线分段计时(recall.collect / rank / merge / format),证据一致指向**单一耗时在 SQL 召回**,其余阶段全部 <3ms:

- "社会" auto/all:recall ≈ 全部耗时;rank 0.03ms、merge 0.01ms、format 3ms。候选解码(`paragraph_from_database_row`)对 81 行仅 ~2ms。
- 根因(EXPLAIN QUERY PLAN):<3 字查询无 trigram MATCH → 走 `instr` 子串扫描,`ORDER BY p.rowid`。SQLite 选 `idx_paragraphs_searchable(eligible_for_search, source_type)` 驱动,但该索引非 rowid 有序,为满足 ORDER BY 必须 `USE TEMP B-TREE`,即**把全部 eligible 行灌入临时 B 树排序后才 LIMIT**——对 20,852 个"社会"命中行全扫 + 排序,无早停。
- 对照:3 字"社会学"走 `paragraphs_fts` trigram MATCH,10–18ms——**证明 trigram FTS 不是瓶颈**,瓶颈是 <3 字的 instr 全扫 + temp-btree。
- scoped "社会"(source_type=pdf)用复合索引且无 temp-btree,21ms——scoped 路径本就快,不能动。

## 修改(一处机制,最小改动)

`search_recall.py`:`_sql_exact_pass` / `_sql_mapped_substring_pass` 的非 FTS(instr)分支,**仅当全库无过滤**(source_type=all 且无 source_file_id、无 scope)时,把 `p.eligible_for_search = 1` 改为 `+p.eligible_for_search = 1`。一元 `+` 抑制 `idx_paragraphs_searchable`,SQLite 改按 rowid 主键顺序扫描——`ORDER BY p.rowid` 由扫描顺序天然满足(无 temp-btree),`LIMIT budget+1` 命中足量即早停。

- 全库无过滤时该索引的首列(eligible_for_search)非选择性(62,729/63,994 几乎全 eligible),抑制它零损失。
- scoped 时保留索引(source_type/source_file_id 有选择性,强制全 rowid 扫会更慢)。
- 结果集完全不变:返回的仍是**最小 budget+1 个 rowid 的命中行、同序**;`total`/`total_is_exact`/`has_more` 语义不变;候选预算 `SQL_CANDIDATE_FLOOR=64 / ×8` 不变。

## A/B 验收(同一冻结快照,search() 全管线)

结果等价:12 条查询(8 条基线 + 一字/三字/短英文/繁体常用)的结果摘要(命中 id、顺序、match_type、score、字符起止、页码、`page_match_spans`)SHA-256 **两版一致** `244f91db3c72dd9b…`。稀有词也一致。

计时(warm p50 / max,毫秒;暖态):

| 查询 | 旧 p50 | 新 p50 | 旧 max | 新 max | 说明 |
|---|---:|---:|---:|---:|---|
| common_zh「社会」 | 396.7 | **18.3** | 419.5 | 18.5 | 目标,~22× |
| one_char「社」 | 385.3 | **21.9** | 415.8 | 32.7 | 高频单字 |
| scoped_pdf「社会」 | 11.2 | 11.7 | 11.5 | 12.0 | 不变(保留索引) |
| three_char「社会学」 | 15.7 | 17.7 | 15.8 | 19.2 | 不变(FTS) |
| common_en「gender」 | 184.0 | 206.0 | 187.1 | 209.0 | 不变(FTS,噪声) |
| short_en「the」 | 37.3 | 40.1 | 39.4 | 40.8 | 不变(FTS) |
| script_variant「社會」 | 617.4 | 635.6 | 1746.5 | 729.2 | 稀有(69 命中<81),无早停,持平 |
| trad_common「國家」 | 678.5 | 461.9 | 718.7 | 513.5 | 稀有,省去排序略快 |
| zh_exact / en_exact / normalized / no_hit | 5–10 | 5–10 | — | — | 不变 |

冷态(fresh 连接,纯 SQL,目标查询):"社会" 2315ms→24ms、"社" 2324ms→28ms(数据见诊断脚本)。

## 时间下降 / 保持 / 代价

- **下降**:高频短词全库查询 SQL 召回(暖 ~0.4s→18ms、冷 ~2.3s→24ms),来自消除全扫 + temp-btree、改早停。
- **保持不变**:scoped、FTS(≥3 字)、精确长句、无命中、繁简稀有词;所有结果身份、顺序、去重、total、页码、跨页与字符锚点(SHA-256 一致)。
- **代价**:内存不增反降(早停使常见词物化的行更少);稀有短词(命中<budget)仍全扫,与基线持平,**无回归**;未新增索引/迁移,同步目录(OneDrive)兼容性不受影响。

## 限制与未完成(待验证)

- 本轮以**逐查询 A/B**(冻结快照、search() 全管线、结果 SHA-256 等价 + 冷/暖计时)为证据。**正式 `bench_real_library.py` 三轮四场景服务器级基准(`--compare`)本轮未跑**:测量当时本机有并发编辑/负载,正式计时不可靠,推迟到机器安静时用标准协议复测;届时以 `reports/performance-real-alignment503-fix-2026-09-12.json` 为兼容基线。
- 结果等价以 12 条查询覆盖(含繁简、一/二/三字、英文、无命中、pdf-scoped);更广的模糊(fuzzy)与 compact/punctuation 通道未逐一 A/B,但本次改动只影响 exact 与 mapped-substring 两个非 FTS 分支的**访问路径**、不改其结果构造逻辑。

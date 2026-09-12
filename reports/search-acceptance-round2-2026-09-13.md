# 第二轮搜索优化验收(高频短词,真实繁简联合路径)

2026-09-13。**核心更正:round-2 的"18ms"是单路变体(`SearchEngine.search`)的耗时,不是用户实际收到的响应。** 默认繁简联合(script folding)下,用户搜"社会"会同时检索简体"社会"与繁体"社會"并合并——真实用户请求由稀有繁体变体的全表扫描主导,服务器级 p50 **≈2.4 s(机器安静时,见"附")**/函数级 A/B ≈3.6 s(并发负载下),均 **远非 18 ms**。本轮验收如实记录单路与联合两个量,不沿用 18ms 结论。

## 一、真实应用路径 A/B(冻结快照 `.codex-tmp/real-library-20260911`,62,729 eligible 段)

工具:入库的 `scripts/ab_search_compare.py`,走与 HTTP `/api/search` 相同的 `execute_with_script_folding` 联合路径(非裸 `SearchEngine.search`)。每条查询记录:联合响应总耗时/total、各变体单路耗时/total、以及联合响应的完整规范化摘要(仅排除非确定字段 `index_metadata`,保留命中、顺序、去重、total、total_is_exact、has_more、match_type、score、字符起止、page、page_match_spans、context、citation)。

各变体单路 p50(繁简折叠关闭,单查询):

| 查询 | 简体变体 | 繁体变体 | 说明 |
|---|---:|---:|---|
| 社会 | 社会=21.5ms/80 | 社會=**3014ms**/24 | 繁体稀有(<budget),全表扫描无早停 |
| 國家 | 国家=27ms/70 | 國家=**546ms**/29 | 同理,繁体主导 |
| 社会学(3字) | 社会学=20ms/37 | 社會學=0.7ms/1 | 走 FTS trigram |

联合请求(用户实际收到)p50 与 round-2 前后对比(暖态,同快照):

| 查询 | 旧(round-2 前)联合 | 新(round-2 后)联合 | 单路"社会" |
|---|---:|---:|---:|
| 社会(auto,全库) | ~5236ms | ~3684ms | 2436→22ms |
| 國家 | ~1821ms | ~586ms | — |
| scoped_pdf 社会 | ~1332ms | ~2643ms* | 15ms |

\* 计时受本机并发负载漂移影响(繁体 pdf 变体全扫两版 SQL 相同,差异是噪声非回归);见"限制"。

**结论**:round-2 的 `+rowid 早停` 把**简体变体**从 ~2.4s 降到 ~22ms,联合请求从 ~5.2s 降到 ~3.7s(约 1.4×),但**联合仍由繁体稀有变体的全表扫描主导**(~3s)。高频短词"用户完整请求"并未降到 18ms;18ms 只是简体单路。

## 二、结果等价(与耗时无关,负载无关)

`ab_search_compare.py --compare` 比较 round-2 前后 10 条查询的完整联合响应摘要:**全部逐字段一致**(`OK: 10 query digests identical to baseline`)。round-2 只改访问路径,未改任何契约字段;繁简变体一个不少(禁止跳过繁简提速这条守住)。

## 三、候选预算边界(`tests/test_search_candidate_budget.py`,8 项)

budget=`max(64, limit*8)`。钉住:命中 <budget → total 精确、`total_is_exact=True`;==budget → 不截断;>budget → total=budget、`total_is_exact=False`、`has_more=True`;覆盖 exact/compact/punctuation 三通道、source_type 范围过滤(只召回过滤内)、`eligible_for_search=0` 段永不召回、结果按 score 非增序。

## 四、锁超时的 HTTP 503 契约(`tests/test_search_http_lock_contract.py`,3 项)

真实 `ThreadingHTTPServer`,在引擎接缝注入 `sqlite3.OperationalError` 驱动整条 HTTP 栈:锁/busy 超时 → 503 `{retriable:true}`(与重建期 503 区分),非锁 OperationalError → 500 记日志,均不吞错、不返回空 `results`。契约文档见 `docs/issues/search-unavailable-during-alignment.md`(2026-09-13 节)。此前 `_post_search` 未捕获,锁超时会掉连接/未处理 500。

## 五、限制与未完成(如实)

- **服务器级基准:normal(搜索)场景已在机器安静后补跑**(见"附",3 轮 valid=true,逐查询 p50 坐实"社会"联合请求 ~2.4 s);但**完整四场景 `--compare` 仍未跑**——alignment 场景需模型缓存(本地缺失),export 两场景本轮未纳入。完整四场景 + `--compare performance-real-alignment503-fix-2026-09-12.json` 待模型缓存就位后补,届时须继续区分单路与用户完整请求。
- **繁体稀有短词全表扫描未解决**:这是联合请求的真实瓶颈。`+rowid 早停`对命中<budget 的稀有变体无效(永远填不满、无法早停),2 字子串又无 trigram 可用。**不在本轮实施**任何新索引(遵守"先交方案"约束),见下"建议"。

## 六、建议(仅方案,未实施,待授权)

要让默认繁简联合的高频短词真正变快,需要能服务"2 字子串 + 跨简繁"的索引,候选:
1. **脚本折叠规范列 + 其上的短-gram 索引**:存一列把简繁折叠到同一规范形(社会/社會→同形),单次查询即覆盖两变体(一次扫描替代两次),并可在该列上建 bigram/FTS。收益:联合从两次全扫降到一次、且可走索引。代价:新增一列(DB 体积增)、一次迁移(全库回填)、写入路径多算一列、需评估折叠对定位锚点/字符区间的影响(折叠必须保持到原文的字符映射),以及缓存/schema 版本语义。
2. **bigram FTS 辅助表**:对 <3 字查询提供 2-gram MATCH。代价:额外 FTS 表(体积、写入、迁移)。

两者都涉及 schema 迁移与同步目录(OneDrive rollback-journal)兼容性评估,须先出"收益/体积/迁移时间/写入成本/兼容性"完整方案并获授权,再实施与验收;数值收益不能仅凭"很小/很大"接受。

## 附:正式服务器级基准(normal 场景,3 轮,真实 localhost HTTP)

2026-09-13 机器安静后补跑 `bench_real_library.py --scenario normal --rounds 3 --repeats 5`(同冻结快照,`valid=true`,全 200,`identity_mismatches=0`,`overlapping_requests=0`;数据 [JSON](search-acceptance-round2-normal-bench-2026-09-13.json))。**四场景中的 alignment 场景因本地无模型缓存未跑**,仅 normal(搜索)场景;故此非完整四场景 `--compare`,是搜索半场的服务器级绝对基准。

真实 HTTP 逐查询 p50(毫秒,3 轮):

| 查询(query_id) | 类型 | r1 | r2 | r3 |
|---|---|---:|---:|---:|
| common_zh(社会,auto) | **用户完整联合请求** | 2334 | 2496 | 2519 |
| scoped(社会,pdf) | 联合(pdf 域) | 2214 | 2258 | 2459 |
| script_variant(社會,exact) | 繁体单变体 | 2430 | 2411 | 2552 |
| common_en(gender) | FTS | 198 | 196 | 215 |
| zh_exact / en_exact / normalized / no_hit | 精确长句/无命中 | 32–50 | 30–68 | 42–58 |

**关键读法(区分单路与用户完整请求)**:
- 用户搜"社会"的**完整请求**(common_zh,含繁简两变体)服务器级 p50 **≈2.4 s**,不是 18ms;繁简任一含全表扫描变体的查询(common_zh/scoped/script_variant)都在 ~2.2–2.6 s。
- **聚合 `search_ms` p50(47/68/162ms)具误导性**:它是 40 个样本(8 查询×5)的中位数,而 8 条里 5 条是快查询(~30–50ms),中位数落在快组,掩盖了高频短词请求实为 ~2.4 s。报告与门禁必须用**逐查询**而非聚合中位数。
- 进程峰值 RSS ~89–92 MiB(纯搜索、无对齐/无模型),startup ~330–360ms、shutdown ~310–490ms。
- 机器非完全空闲(system 进程占用,common_zh 逐轮 2334→2519 轻微漂移),但足以支撑"用户完整短词请求 ~2.4 s"这一量级结论。

这坐实上文:round-2 把简体单路降到 ~22ms 有效,但**用户完整繁简联合请求仍 ~2.4 s**,瓶颈是繁体稀有变体全表扫描;真正加速需先落"六"的索引方案。

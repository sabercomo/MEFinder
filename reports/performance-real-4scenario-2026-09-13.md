# 真实书库三轮四场景服务器基准(2026-09-13,机器安静)

模型缓存就位、机器安静后执行 `bench_real_library.py --snapshot <冻结快照> --models <app 运行时模型目录> --rounds 3 --repeats 5`,四场景 `normal / export_markdown / export_epub / alignment`,同冻结快照(`content_sha256 5670b0d1…`,63,994 段)。

> **更新(2026-09-13,审计后)**:
> 1. **`--compare` 现已正式通过**(见"正式通过比较"节):用**修复后工具**建了一份**全新同-harness 基线**(`valid=true`,12 运行 8/8),再跑一次 `--compare` 该基线(`valid=true`)→ 比较成功写出。数据:基线 [JSON](performance-real-4scenario-baseline-2026-09-13.json)、对比 [JSON](performance-real-4scenario-compare-2026-09-13.json)。
> 2. **基准工具修复**:`bench_real_library.py` 原先"先比较再落盘",比较失败会丢完整测量。已改为**先落盘完整结果(含 `valid`)再比较**,比较失败记 `comparison_error` 且仍以退出码 1 返回失败(提交 `109e089`)。
> 3. 下面首次贴出的"12 运行 343 重叠搜索"是**审计前的一次运行**(其 `--compare` 因验证门中止,汇总数据经工具修复后已能完整落盘);"正式通过比较"节是审计后**修复工具 + 全新基线**的合格对比。

## 头条结果:全部 12 个(轮×场景)运行 0 错误、0 次 503、identity_mismatches=0

- **对齐期间搜索无 503(当前修复后代码)**:三轮 alignment 场景共 **343 个重叠搜索(103+152+88)全部 200**,零 503、零 identity 漂移;进程峰值 RSS **1279–1310 MiB**(与"1.3 GiB=嵌入瞬态峰值"一致)。**分别陈述**:旧记录(修复前产品代码)对齐期间 61/216=28.2% 快速 503;当前修复后代码本轮 0 次。二者是不同代码版本的两次测量,可分别陈述,但降幅未由本报告的同版本 `--compare` 证明(见下"同版本重复测量"节)。
- 导出(markdown/epub)期间 26–31 个重叠搜索全部 200;normal 场景 0 重叠(基线搜索)全部 200。

## 失败/超时/异常退出样本口径(不只统计成功)

- `errors=0`、`failure_ms=null`、`http_status_counts` 仅含 `{200: n}`——即无失败、无超时样本;若有会计入 failure 分桶并单列。三轮全部进程正常退出(无 `failed-server.log` 从本次写入;快照目录里的 `failed-server.log` 是 2026-09-12 17:45 旧运行遗留的 ORT 退出崩溃,与本次无关)。

## 逐查询搜索耗时(区分单路 vs 用户完整联合请求)

按查询报告 p50(不用聚合中位数,后者被快查询拉低而误导)。跨轮有明显机器漂移,故给区间:

| 场景 | common_zh(社会,**用户完整联合**) | scoped(社会 pdf) | script_variant(社會) | 快查询(en_exact/no_hit/zh_exact/normalized) |
|---|---|---|---|---|
| normal | 530 / 2340 / 1868 ms | 512 / 2214 / 556 | 530 / 2362 / 692 | ~23–110 ms |
| alignment 期间 | 1973 / 999 / 3074 ms | 2010 / 984 / 2940 | 2021 / 1027 / 2622 | ~38–1113 ms |
| export_markdown 期间 | 773 / 2489 / 2439 | 862 / 2358 / 2406 | 1442 / 2552 / 2457 | — |
| export_epub 期间 | 603 / 2526 / 2377 | 691 / 2387 / 2246 | 655 / 2736 / 2442 | — |

- **用户完整"社会"联合请求**(繁简两变体)在各场景 p50 约 **0.5–3.1 s**(强机器漂移),**远非 18 ms**;18 ms 只是简体单路(见搜索验收报告)。繁体稀有变体全表扫描仍是联合瓶颈。
- 快查询(精确长句/无命中)~几十 ms;normal 场景峰值 RSS ~80–88 MiB,export ~130–139 MiB,alignment ~1.3 GiB。

## 同版本重复测量 + `--compare` 跑通(不是优化前后对照)

> **口径更正(审计后)**:这两份 JSON 的 `revision` 都是 `109e089`、`source_tree_sha256` 完全相同——**是同一版本的两次重复测量,不是旧/新版性能对照**。它证明的是:①`bench_real_library.py` 先落盘后比较的修复有效(比较跑通、无 `comparison_error`);②**当前版本能完成完整四场景测量**,可作为**后续优化的"当前版本基线"**。它**不能**替代旧版与新版的性能验收;要衡量优化收益,须用同一工具分别跑优化前/后的产品代码再比较。

`bench_real_library.py` 修复"先落盘后比较"后,重跑两次(均 `revision 109e089`):
- **基线**(`performance-real-4scenario-baseline-2026-09-13.json`):`valid=true`,12 运行全 8/8 覆盖、0 错误、0 次 503。
- **对比运行**(`performance-real-4scenario-compare-2026-09-13.json`):`valid=true`,`--compare` 上述基线**成功写出 `comparison`**(无 `comparison_error`)。四场景全 0 错误、**0 次 503**、idmis=0;对齐 256 重叠搜索全 200,峰值 RSS 1314 MiB。

**逐场景延迟波动(同版本两次测量,非回归)**:normal 与 export 场景逐查询 p50 约 ±10%(如 normal common_zh 2474→2388 ×0.97);但**对齐场景的重查询在两次测量间波动很大**——common_zh 1364→3225ms(**×2.36**)、带范围 scoped 1038→3103ms(**×2.99**)、繁体 script_variant 1181→3291ms(**×2.79**);快查询(common_en/no_hit/zh_exact/normalized)~×1.0–1.2。**这些波动原因尚未定位**:不能直接归因机器漂移,也不能据此认定代码回归(两次是同版本)。

**503 分别陈述,不由本次同版本比较证明降幅**:旧记录(修复前产品代码)对齐期间 61/216 = 28.2% 快速 503;本轮两次运行对齐期间(336、256 次重叠)全 200、0 次 503。二者可分别陈述,但**同版本重复测量不能证明"28.2%→0"这一降幅**——那需要用同一工具跑旧/新版产品代码对照。

**为何不能对旧基线跑 `--compare`(如实)**:`compare_results` 校验 `harness_sha256` 一致;修复落盘 bug 必然改动 `bench_real_library.py` → harness 指纹变化 → 协议**正确地**拒绝跨-harness 比较 `performance-real-alignment503-fix-2026-09-12.json`(旧基线)。

(下方"头条"表是审计前的一次运行,其 `--compare` 因当时 2 个 export 轮偶发覆盖 7/8 + 未落盘而中止——重叠时序脆弱性,非错误/503;现工具已先落盘,且全新基线两轮均 8/8。)

## 复现

```bash
PY=.venv-macos312-arm64/bin/python
MODELS="$HOME/Library/Application Support/MEFinder/runtime/components/text-alignment/models"
COMMON="--snapshot .codex-tmp/real-library-20260911 --models $MODELS --rounds 3 --repeats 5"
# 1) 用当前工具建一份基线(不带 --compare)
NO_PROXY=localhost,127.0.0.1 $PY scripts/bench_real_library.py $COMMON --output base.json
# 2) 再跑一次并对上一步基线比较(两次须 valid=true)
NO_PROXY=localhost,127.0.0.1 $PY scripts/bench_real_library.py $COMMON --output cmp.json --compare base.json
```

注意:**不要** `--compare reports/performance-real-alignment503-fix-2026-09-12.json`(旧基线)——修复落盘 bug 改了 harness 指纹,协议会正确拒绝跨-harness 比较。要做**真正的优化前后对照**(而非同版本重复),须用**同一(当前)工具**分别 checkout 优化前/后的产品代码各建一份基线,再比较;本报告两份 JSON 都是当前版本 `109e089`,只是重复测量。`--compare` 需两次运行都 `valid=true`(每轮 export/alignment 覆盖全 8 查询,时序偶发但本轮两次均满足)。

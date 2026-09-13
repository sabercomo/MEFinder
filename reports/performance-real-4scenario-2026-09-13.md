# 真实书库三轮四场景服务器基准(2026-09-13,机器安静)

模型缓存就位、机器安静后执行 `bench_real_library.py --snapshot <冻结快照> --models <app 运行时模型目录> --rounds 3 --repeats 5`,四场景 `normal / export_markdown / export_epub / alignment`,同冻结快照(`content_sha256 5670b0d1…`,63,994 段)。数据:[JSON](performance-real-4scenario-2026-09-13.json)。原始逐轮 stdout 在未跟踪的 `.codex-tmp`/scratchpad。

## 头条结果:全部 12 个(轮×场景)运行 0 错误、0 次 503、identity_mismatches=0

- **对齐期间搜索无 503**:三轮 alignment 场景共 **343 个重叠搜索(103+152+88)全部 200**,零 503、零 identity 漂移;进程峰值 RSS **1279–1310 MiB**(与"1.3 GiB=嵌入瞬态峰值"一致)。对照修复前基线(`performance-real-alignment503-fix` 前身)对齐期间 61/216(28.2%)快速 503——**修复在真实四场景协议下依旧成立**。
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

## `--compare` 未完成的原因(如实,非数据错误)

工具的 `--compare reports/performance-real-alignment503-fix-2026-09-12.json` **中止**于 `ValueError: Cannot compare an invalid run`。根因:harness 的有效性门要求**每条查询在每个 export/alignment 轮都与任务重叠**;而 export 任务很短(474 段书 ~0.15 s),重叠搜索仅 ~30 个,本次 **2 个 export 轮覆盖 7/8**(`no_hit` 未在该轮 export 窗口内被发出),`covered_query_ids != 全部 8` → `valid=false` → `--compare` 拒绝对比无效运行。其余 10 个运行 8/8。**这是重叠时序的门槛脆弱性,不是错误或 503**;全部逐轮逐查询数据与 0-503 结论已在上表与 JSON 保留。与基线的**关键对比**(对齐 503:28.2% → 0)是二值量,直接成立,不依赖该门。

## 复现

```bash
PY=.venv-macos312-arm64/bin/python
MODELS="$HOME/Library/Application Support/MEFinder/runtime/components/text-alignment/models"
NO_PROXY=localhost,127.0.0.1 $PY scripts/bench_real_library.py \
  --snapshot .codex-tmp/real-library-20260911 --models "$MODELS" \
  --rounds 3 --repeats 5 --output <new.json> \
  --compare reports/performance-real-alignment503-fix-2026-09-12.json
```

如需 `--compare` 成功产出,需该协议下每轮 export 都恰好覆盖全部 8 查询(时序偶发);多次重跑或延长 export 源可提高命中,本轮未反复重跑以避免无谓的重负载。

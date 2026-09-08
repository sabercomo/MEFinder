# 译本粗定位 v22 正式入口验证

2026-09-08：可在独立本地副本试读；16/16 个书对复现最终候选结果。未发布，未据此宣称全部译文对应正确。

## 问题与最小改动

基线 `25a1812` 已在正式 `_validate_soft_anchors` 调用软锚点筛选，但算法版本仍为 21，
会使旧 run 被新生成操作复用。对齐算法版本升至 22，语义参数版本升至 20；分段器仍为 13。
v21 结果仍可读取，v21 配方仍能按当前算法恢复。配方恢复不是逐位还原旧算法输出。

新增两个测试先失败再修复通过：

- `test_version21_remains_readable_but_is_not_reused_for_new_generation`
- `test_version21_recipe_is_restored_using_current_algorithm`

没有更改既有筛选阈值、默认模型、前端资源、schema 或 API 字段。

## 输入、方法与结果

- 输入为本机已有的 `.codex-tmp/d-experiment/index-v21.sqlite3` 实验库，以 SQLite 只读 URI 打开。
- SQLite backup 创建新的 `.codex-tmp/alignment-v22-trial/runtime/data/index.sqlite3`。
- 书对名单来自 `reports/d-anchor-regression-2026-09-07.json` 的 `all_pairs`，每对取实验库最新完成的 v21 run。
- 固定原 run 的已复核正文区域，使用已有 multilingual-e5-large 模型及向量缓存。
- 经 `text_alignment.generate_alignment` 正式入口执行，不 monkeypatch；所有输出均为新建 v22 run，未复用旧结果。
- 比较所有链接两侧的完整有序分段成员、review_status、confidence，以及参数中的 heading_anchors。
  忽略每次生成的随机 run/link ID，不只比较 accepted 数量。
- 16/16 对逐项完全一致，SQLite `quick_check=ok`。逐对结果与摘要哈希见同名 JSON。
- R9 为 2468 条链接、1811 accepted；DE→ZH 为 6003 条链接、1998 accepted。

该结果验证正式实现复现候选；不构成新的人工金标准确率。保存候选自身的质量限制仍以
`reports/d-anchor-acceptance-2026-09-07.md` 为准。

## 阅读流程验证

独立服务使用试用 runtime 作为工作目录，显式指定副本数据库，监听 `127.0.0.1:8876`。
已在真实阅读器加载日中对照，并显示原文及中文目标；德文日期案例的原文深链接也能定位并高亮。
8 个试读位置通过 `/api/text-alignments/locate` 返回 v22 结果和页码字符区间。

本机 `.codex-tmp/alignment-v22-trial/试用入口.md` 包含 2 个改善案例、2 个保护案例及
4 个争议案例的阅读器入口、原文、机器当前目标及附近上下文。原文摘录仅保留在本地试读材料。
JA 源分段 549、551、483、487 仍待人工判定，不能因同章、同主题或脚注编号相近就算对应。
试读应判断能否在附近找到对应论述，而不要求每句边界完全吻合。

## 工程验证

在独立 worktree 根目录使用现有 Python 3.12.14 测试环境：

```powershell
& D:\ME_Finder\.venv-windows\Scripts\python.exe -m unittest discover -t . -s tests
& D:\ME_Finder\.venv-windows\Scripts\python.exe -m ruff check .
```

- unittest：2051 项，126.418 秒，OK（skipped=50）；没有失败或错误。
- 条件跳过：私有 corpus/raw_pdf 夹具 14 项，缺 OpenCC 依赖 31 项，未随基线提供的可选本地
  Bertalign 实验驱动 4 项，macOS PyObjC 专属测试 1 项。未将这些跳过项表述为验证通过。
- 相关对齐模块 54 项通过；全量运行包含前端守卫；Ruff F 全绿，`git diff --check` 通过。
- 本地日志：`.codex-tmp/v22-full-tests.log`、`.codex-tmp/v22-trial-validation.log`。

可复现入口为 `scripts/validate_alignment_v22_trial.py`，参数依次指定已有实验副本、全新输出目录、
本地模型缓存及候选报告；不得把输出指向已有数据库。

## 边界与下一步

本轮没有向生产库执行写入或重算，没有构建安装包或发布，没有启动翻译模型及新算法实验。
E5 继续为实验档。下一步是本地粗定位试读，收集实际找不到对应论述的案例；四条争议仍保持未决。

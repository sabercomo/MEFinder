# SQL 架构守卫核验（2026-10-27）

## 问题与事实

基于 `e5aeccd` 的静态核对：旧检查将所有 `.execute` / `.executemany` / `.executescript` 调用归入 SQL 文件集合，得到 14 个文件。其中四个应用服务模块里的五处调用实际是 `SearchService.execute` 或 `self._parser_executor.execute`。由于基线只比较集合，同一文件里增加 SQL 调用不会改变结果。

## 修正边界

只按文件与完整调用目标豁免已核实的非 SQL 调用，不豁免整个应用模块。其他 `execute*` 调用按文件计数，当前基线如下；此计数包括 SQL 事务、PRAGMA 与 schema 安装，不等同于业务查询数。

| 文件 | 调用数 |
|---|---:|
| data_location.py | 5 |
| document_deletion.py | 1 |
| document_export_service.py | 10 |
| edition_folio_anchors.py | 4 |
| index_publisher.py | 1 |
| indexer.py | 1 |
| large_document/job_ledger.py | 43 |
| parser_statistics.py | 3 |
| search_assembly.py | 2 |
| search_recall.py | 2 |
| 合计 | 72 |

该规则并非完整类型分析器：未知的同名服务也会触发检查，需审阅后登记。没有扩大到方法别名或任意动态 Python 调用的追踪；旧 `sqlite3.connect` 检查范围保持不变。

## 复现与验证

- 先增加隔离源码树用例再改扫描器：旧实现出现五项断言失败和一项旧集合不支持计数读取的错误，其中非 SQL 服务用例明确复现误报。
- 修正后用实际仓库门禁检查隔离源码树：已有 `indexer.py` 的一个调用可通过，增加第二个调用必须失败；新模块内的 SQL 也必须失败。
- 服务模块内混入 SQL、连接/游标/动态 SQL、`executemany`、`executescript`、未知执行器及 persistence 目录排除均有回归覆盖。
- 在独立 Python 进程内替换扫描函数做变异验证，未改写源码：取消服务目标豁免产生 2 项断言失败；把计数改成每文件固定记 1 产生 4 项断言失败。两种变异均被检出，且没有靠执行错误判失败。
- `PYTHONUTF8=1 .venv-macos312-arm64/bin/python -m unittest discover -t . -s tests`：2,832 项通过，39 项条件跳过，187.352 秒；其中新增守卫回归 7 项。
- `MEFINDER_BROWSER_TESTS=1 MEFINDER_BROWSER_TESTS_REQUIRED=1 .venv-macos312-arm64/bin/python -m unittest tests.test_browser_regression`：16 项通过，51.734 秒。
- Ruff：原始 `check .` 在既有未入库 `.codex-tmp/bertalign-upstream` 与 `.codex-tmp/time-startup.py` 报 21 条告警；确认该目录无跟踪文件后，`check . --extend-exclude .codex-tmp` 通过，未修改这些临时文件。`git diff --check` 通过。
- 前端装配：1,533,323 字节，SHA-256 `12f6f39c446e764f602281bb6db3a4785a41adcc58d435a1b5de967986262c63`；把装配字符串中的 0.6.1 还原为 0.6.0 后，摘要与原基线完全相同。HTTP 契约除 release 字段外与 0.6.0 相同。
- 本地完整日志位于 `.codex-tmp/architecture-0.6.1-20261027/`（不入库）。本轮未打包、安装、发布，也未修改用户文献库；Windows 实机验收未执行。

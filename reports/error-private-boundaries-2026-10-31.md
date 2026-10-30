# 通用错误与私有辅助函数边界核验（0.6.1，2026-10-31）

## 范围与基线

本轮从 `83ad26c` 开始，落实 2026-10-31 架构评估的第 1、2 项。只调整错误类型归属与辅助函数的命名、导入路径，不修改检索、对齐、页码、schema、HTTP 路由或 MCP 工具接口，不重解析用户文献库。

| 指标（`src/me_finder`，AST 统计） | 调整前 | 调整后 |
|---|---:|---:|
| 导入 `MinerUError` 的文件 | 26 | 5 |
| 导入 `mineru_api` 的模块 | 30 | 13 |
| 跨模块导入下划线私有名 | 152 处 | 0 |
| 包内循环依赖（含函数内导入） | 0 | 0 |

## 第 1 项：通用的用户可见错误

**事实**：调整前，备份、删除、数据根准入、导入任务、页码映射、各 controller、CLI 与 web 装配共 21 个与 MinerU 无关的文件借用 `mineru_api.MinerUError` 表示“消息可直接给用户看的预期错误”，例如 `BackupQueueError(MinerUError)`、`raise MinerUError("备份文件不存在。")`。controller 依赖它返回 400 及原文案。

**调整**：新增 `src/me_finder/errors.py` 的 `UserFacingError(RuntimeError)`；`MinerUError` 改为它的子类，保留 `retry_with_new_task` / `allow_parser_fallback` 两个 MinerU 专属字段。21 个文件的抛出、子类化与捕获改用 `UserFacingError`；`UploadImportController` 的注入参数由 `mineru_error` 改名为 `user_error`。仍导入 `MinerUError` 的 5 个文件都与 MinerU 解析直接相关：`mineru_provider.py`、`mineru_local_settings.py`、`pdf_parser_adapters.py`、`large_document/mineru_accounts.py`、`application/import_parser_executor.py`。

**等价性论证**：

1. 调整后 `UserFacingError` 的子类集合，恰好等于调整前 `MinerUError` 的子类集合加上原先直接抛出的 `MinerUError`（现改为直接抛 `UserFacingError`）。没有其他既有异常改挂到 `UserFacingError` 下。因此源码中全部 43 处原 `except MinerUError` 改为 `except UserFacingError` 后，捕获的异常集合与原来一致。调整后源码中不再有 `except MinerUError`。
2. 6 处 `isinstance(exc, MinerUError)` 只用于读取两个 MinerU 专属字段，形式均为“是 MinerUError 且字段为真”或“不是 MinerUError 或字段为假”。被迁移的抛出点从未传入这两个字段（脚本核对为 0 处），原值恒为假，因此两种形式对它们的判定结果不变。
3. 唯一可观察差异：有 3 处失败提示会把异常类名拼进消息（`import_orchestrator` 的“未能进入索引：类名: 消息”、文档包导入失败、PDF 页内文字刷新失败）。若失败原因是本轮迁移的通用错误，类名从 `MinerUError` 变为 `UserFacingError`；MinerU 自身的错误仍显示 `MinerUError`。HTTP 状态码与 controller 返回的错误文案不变。

新增守卫 `test_mineru_error_is_imported_only_by_mineru_modules`：导入 `MinerUError` 的文件必须恰好是上述 5 个。

## 第 2 项：跨模块私有辅助函数

**事实**：调整前有 152 处跨模块导入下划线私有名，涉及 98 个定义。其中不少经旧模块转手：`text_alignment.py` 用 `_now as _now` 等写法重新导出 `alignment_generation.py` 的私有函数，`translation_works.py`、`alignment_overrides.py`、`alignment_body_range.py`、`alignment_snapshots.py` 再从 `text_alignment` 取用。类似的转手还有 `database.py`（转手 `persistence.index_build` / `source_replace`）、`semantic_alignment.py`（转手 `alignment_structure`）和 `web.py`（转手 `web_assets`）。

**调整**：

- 96 个被跨模块使用的私有定义改为公开名（去掉前导下划线），2 个原本已是公开名、只是被私有别名转手的定义（`latest_pair_run`、`ordered_segments_in_set`）保持原名。去掉下划线后会与局部变量或标准库重名的 8 个改用新名：`_source_kind`→`source_kind_of`、`_canonical_metadata`→`canonicalize_metadata`、两处 `_json`→`compact_json`、`_backup_database`→`create_database_backup`、`_match`→`match_book_candidate`、`_open_url`→`book_open_url`、`_physical_page`→`physical_page_number`、`_clean`→`clean_text`。
- 所有使用方（`src`、`tests`、`scripts`）直接从定义模块导入，不再经旧模块转手；不再有人使用的转手导出被删除（`text_alignment.py` 9 个、`database.py` 4 个）。`pdf_import_service.__all__` 同步为公开名。各模块自己内部使用、未被其他模块引用的私有函数不动。
- 改名按 Python 词法单元进行，属性访问与字符串不做自动替换。6 个测试文件中 15 处 `patch` 目标与模块属性访问、1 个性能剖析脚本的包装目标经逐条核对后手动更新；相关注释与 docstring 同步更新。

**行为证据**：改名只改变标识符与导入路径，函数体逻辑未改。重定向到定义模块不会新增循环：原路径 A→B→C 已存在，新边 A→C 不会形成新环；完整导入图（含函数内导入）复扫仍为 0 个强连通分量。测试的 `patch` 目标若仍指向旧名会因属性不存在而报错，不会悄悄失效。

新增守卫 `test_modules_do_not_import_each_others_private_names`：包内相对导入不得引用其他模块的下划线名。

## 验证

1. 基线：在 `83ad26c` 的干净导出副本上跑全量 unittest，2,837 项中仅 `test_cask_is_tracked_in_git` 失败（导出副本不是 Git 仓库，属环境原因），其余通过。
2. 迁移错误类型后，引用 `MinerUError` 的 25 个测试文件里有 8 个、共 24 项失败，原因都是断言 `MinerUError` 的代码路径现在抛出 `UserFacingError`，符合预期。这些断言改为 `UserFacingError`；测试 MinerU 以外模块时伪造的错误也一并改用它，以确认 controller 捕获的是通用错误而不只是 MinerU 错误。携带 `allow_parser_fallback` 的伪造 MinerU 错误保持 `MinerUError`。改动后重跑受影响的测试模块，共 320 项通过。
3. macOS 全量 `PYTHONUTF8=1 .venv-macos312-arm64/bin/python -m unittest discover -t . -s tests`：2,839 项通过（39 项条件跳过，191.954 秒），比基线多出的 2 项是本轮新增守卫。
4. Chrome 回归 `MEFINDER_BROWSER_TESTS=1 … tests.test_browser_regression`：16 项通过（52.880 秒）。
5. `ruff check src tests scripts` 通过（不含既有未入库 `.codex-tmp`）；`git diff --check` 通过。静态资源与模板未改，前端装配指纹不变。

本轮未打包、安装或发布 0.6.1；Windows/Intel 实机与原生双窗口人工验收不在本轮范围内。

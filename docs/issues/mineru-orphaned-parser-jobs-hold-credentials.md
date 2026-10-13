# 已取消导入的解析任务长期占用 MinerU 账号

## 2026-10-13 — 现场与原因

**事实**：用户截图中《黑格尔著作集 第3卷 精神现象学》（553 页，3 个分段）停在“等待可用的 MinerU 账号：0/3 个分段已完成”，持续不前进。只读查询 `dist/MEFinderData/runtime/data/parser_jobs.sqlite3`：

- 配置 2 个 MinerU 账号（`mineru-8a574ba61c05`、`mineru-7dd3e1dad79f`），均 `healthy`，每账号并发上限 1，`current_in_flight` 均为 1。
- 占用者是 2026-10-12 18:37–18:38 导入的两篇论文（`pdf-import-e6f21bd1a3c0c4e6` 34 页、`pdf-import-822b62e6ed79e83c` 261 页）：解析账本中 document job 仍为 `waiting`，各有一个绑定账号的远端任务（slice `waiting` + `remote_task_id`），最后更新停在 18:50。
- 两篇在导入任务日志（`corpus/processed/import_jobs/`）与索引 `source_files` 中都已不存在；`desktop.log` 18:50:56 记有 “background imports are still stopping”。
- 黑格尔任务 3 个分段的 `last_error` 均为 “all configured parser credentials are disabled, cooling down, or busy”。

**事实（代码）**：取消导入时 `on_progress` 抛 `ImportJobCancelled`，直接冒出 `_parse_pdf_with_mineru_accounts` 循环；`finish_cancelled_job` 与 `dismiss_job` 只清理导入日志与内存任务，从不触及 `parser_jobs.sqlite3`。之后再无代码轮询该 document job，但 `CredentialPool.reconcile_in_flight` 每次开解析都按 `active_remote_counts` 把它计入在途，账号永久“忙”。该漏洞早于 `2e6ded1`（重复导入修复），与其无关。

**推断**：两篇论文是用户在 18:50 左右取消或关闭前放弃的导入；具体点击顺序无法从日志确认，复现测试不依赖此推断。

## 2026-10-13 — 修复

- `JobLedger.cancel_open_document_jobs`：把未结束（`preparing/queued/running/waiting/retryable_failure`）的 document job 及其未结束分片标为 `cancelled`，并按其持有的远端任务数扣回账号 `current_in_flight`；可按 `source_file_id` 定点取消，或以 `keep_source_ids` 清扫其余所有未结束任务。
- `ImportOrchestrator` 新增 `parser_jobs` 端口（装配时注入同一 `JobLedger`）：
  - 取消完成（`finish_cancelled_import_job`）和移除非活动任务（`dismiss_import_job` 返回 `dismissed`）时，释放该 source 的解析任务；
  - 启动恢复导入任务后，清扫所有不属于任何已恢复导入任务的未结束解析任务——此时尚无解析在跑，没有日志的任务不会再被轮询。可续传的失败任务仍保留其解析进度。
- 远端 MinerU 任务本身不调用取消接口（云端无对应接口），仅在本地停止占用。

复现测试 `tests/test_import_parser_job_release.py`（6 项）在旧代码上全部失败，修复后通过；其中 `test_orphaned_remote_tasks_starve_every_credential` 复现“两个账号都被孤儿任务占满、acquire 失败”。

**用户现存数据**：重建本机 `dist/MEFinder/` 后首次启动即由启动清扫自动修复，无需手工改库；被取消的两篇论文需重新导入。

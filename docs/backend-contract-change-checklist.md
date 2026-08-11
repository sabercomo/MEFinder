# MEFinder 0.4.x 后端契约变更清单

> 交接范围：`5154306` → 当前后端分支
>
> 用途：前端、HTTP API 和最终集成线的单一事实源。
>
> 重要：本文同时区分“已实现的应用层契约”和“尚未实现的 HTTP 契约”。

## 1. HTTP 端点变更

| 类型 | 数量 | 结论 |
|---|---:|---|
| 新增 HTTP 端点 | 0 | 未添加 `/api/v1` 或其他 web route |
| 修改 HTTP 端点 | 0 | 现有 request/response 字段和 HTTP status 未改 |
| 删除 HTTP 端点 | 0 | 无 |
| FastAPI/Uvicorn | 0 | 未引入 |

`src/me_finder/web.py` 相对 checkpoint 字节级未变。因此，前端现在不得假设以下路由已存在：

- 多 MinerU 账号的 list/create/update/delete route
- 大文档 job create/status/resume/cancel route
- 单书 export route
- provider list/capability route

这些均属于后续 HTTP adapter 工作，不是本分支已交付端点。

## 2. 新增应用层调用入口

### 2.1 多 MinerU 独立账号

`MinerUAccountService` 已实现：

| 方法 | 用途 | 输出 |
|---|---|---|
| `save_account(...)` | 新增/更新一个独立 MinerU 账号 | `MinerUAccountSummary` |
| `get_account(account_id)` | 获取一个安全摘要 | `MinerUAccountSummary` |
| `list_accounts()` | 列出 N 个独立账号 | `list[MinerUAccountSummary]` |
| `resolve_secret(secret_ref)` | 仅供后端调度器解决 Token | raw Token，不得向 UI 暴露 |
| `create_pool(...)` | 用已保存账号构造 CredentialPool | `CredentialPool` |
| `usage_statistics()` | 获取独立于账号配置的本地成功解析统计 | `MinerUUsageStatistics` |

`save_account` 输入字段：

| 字段 | 类型 | 必填/默认 | 契约 |
|---|---|---|---|
| `account_id` | `string?` | 可选，缺省自动生成 | 1–64 位字母/数字/`.`/`_`/`-` |
| `display_name` | `string` | 必填 | 1–120 字符 |
| `token` | `string?` | 新账号必填 | 接受 raw Token 或 `Bearer ...`；更新时空值保留旧 Token |
| `enabled` | `boolean` | `true` | 是否参与调度 |
| `max_concurrency_override` | `integer?` | `null` | 正数；空值使用 provider capability |
| `expires_at` | `YYYY-MM-DD?` | `null` | 空白表示未设置；更新时未传保留原值 |

`MinerUAccountSummary` 安全输出字段：

- `account_id`
- `display_name`
- `enabled`
- `configured`
- `current_in_flight`
- `max_concurrency_override`
- `health_status`
- `cooldown_until`
- `last_401_at`
- `last_429_at`
- `expires_at`

明确不返回：`token`、`secret`、`secret_ref`、MinerU 官网用量。每个 credential 对应一个独立 MinerU 账号。MinerU 的“优先解析 1000 页”不是每日配额，后端不设置 1000 页门槛，也不会因累计到 1000 页自动切换账号。

本地统计使用独立对象，不混入账号配置摘要：

- `MinerUUsageStatistics`: `provider_id`, `parsed_book_count`, `parsed_page_count`, `credentials`
- `MinerUCredentialUsageStatistics`: `account_id`, `display_name`, `parsed_book_count`, `parsed_page_count`, `books`
- `MinerUBookUsage`: `document_job_id`, `document_id`, `source_file_id`, `source_file_name`, `parsed_page_count`, `page_ranges`, `completed_at`

统计只计算已成功完成并通过归一化覆盖检查的 slice。`page_ranges` 使用 1-based 原书物理页码。整书由多个账号共同完成时，全局书数只计一个 document job，各账号分别显示自己完成的页数和范围。它是 MEFinder 本地归属统计，不是 MinerU 官网计费或优先页用量。

### 2.2 大文档任务

`LargeDocumentJobEngine` 已实现：

| 方法 | 主要输入 | 输出/效果 |
|---|---|---|
| `prepare` | `source_path`, `source_file_id`, `document_id`, `model?`, `options?` | 创建或恢复 `DocumentJob`，并生成实体 slice |
| `run_once` | `job_id` | 推进提交/poll/fetch/merge 一轮，返回 `DocumentJob` |
| `publish` | `job_id`, `manifest`, `destination`, `publish_index?` | 仅 validated job 可原子发布；同目标重试幂等 |

Document status 枚举：

`preparing | queued | running | waiting | retryable_failure | permanent_failure | cancelled | validated | published`

Slice status 枚举：

`queued | running | submitted | waiting | completed | retryable_failure | permanent_failure | cancelled`

Publish status 当前值：

`not_published | publishing | failed | published`

### 2.3 Parser Provider

`ParserProvider` 新契约：

- `capabilities()`
- `prepare(request)`
- `submit(request, credential?)`
- `poll(remote_task_id, credential?)`
- `fetch_result(submission, request, credential?)`
- `cancel(remote_task_id, credential?)`
- `normalize_result(raw_result, request)`

已注册的 provider ID：

- `mineru-cloud`
- `mineru-local`
- `qwen-ocr`

`ProviderCapabilities` 字段：

- `max_pages_per_file`
- `max_bytes_per_file`
- `max_concurrency`
- `supports_scanned_pdf`
- `supports_bbox`
- `supports_page_ranges`
- `supports_async_jobs`
- `supports_stream_upload`
- `supported_models`
- `optional_limits`

Parser task status：

`queued | submitted | waiting | completed | retryable_failure | permanent_failure | cancelled`

### 2.4 单书导出

新 schema：`mefinder.document.v1`

普通 JSON/Zip manifest 字段：

- `schema_version`
- `document`
- `source_sha256`
- `source_file`
- `bibliographic_metadata`
- `external_ids`
- `parser.provider`
- `parser.model`
- `parser.version`
- `parser.options`
- `parser.provenance`
- `parsed_at`
- `page_count`
- `warnings`
- `missing_ranges`

页级字段：

- `physical_pdf_page`（1-based）
- `logical_page`
- `pdf_page_label`
- `text`
- `blocks`
- `bbox`
- `reading_order`
- `parser_provenance`
- `warnings`

大书容器契约：

```text
book.mefinder.zip
  manifest.json
  pages.ndjson
```

写入期间仅存在 `<target>.partial`，全部成功后才原子 rename。

### 2.5 Torture/manual runner

新入口：`tools/large_document_torture.py`

主要参数：

- `--pdf`
- `--provider synthetic|mineru-cloud|mineru-local|qwen-ocr`
- `--dry-run` 或显式 `--execute`
- `--credentials`
- `--max-pages`
- `--max-bytes`
- `--max-concurrency`
- `--ledger`
- `--work-dir`
- `--output`
- `--benchmark-output`

dry-run 输出字段：

- `provider`, `total_pages`, `source_bytes`, `capabilities`
- `slice_count`, `slices[]`
- `estimated_upload_bytes`, `estimated_temp_disk_bytes`
- `pages_by_credential`, `unassigned_pages`, `credentials_unavailable`
- `coverage_complete`, `coverage_first_page`, `coverage_last_page`
- `memory_probe`

## 3. 数据库字段变更

新增独立 SQLite ledger：建议路径 `data/parser_jobs.sqlite3`；当前 `PRAGMA user_version = 3`。它不属于可重建的搜索 index DB。

### v1 `document_jobs`

`id, source_file_id, document_id, source_path, source_sha256, provider_id, parser_model, options_fingerprint, status, total_pages, total_slices, completed_pages, completed_slices, publish_status, published_export_path, error_summary, created_at, updated_at`

### v1 `slice_jobs`

`id, document_job_id, page_start, page_end, global_page_offset, slice_path, slice_sha256, size_bytes, provider_id, credential_id, remote_task_id, status, attempt_count, last_error, result_path, result_sha256, created_at, updated_at`

### v2 `parser_credentials`（经 v3 修正规则）

`id, provider_id, display_name, secret_ref, enabled, daily_page_budget, max_concurrency_override, current_in_flight, pages_used_today, usage_date, cooldown_until, last_401_at, last_429_at, health_status, created_at, updated_at`

v3 不重建表，以兼容旧 ledger；`daily_page_budget/pages_used_today/usage_date` 三列仅作为废弃的物理占位保留，迁移时分别清为 `NULL/0/NULL`，应用层不再读写或暴露。逐凭据统计由 `document_jobs + completed slice_jobs.credential_id` 实时聚合，不需要访问 secret，也不新增官网用量表。

## 4. 错误码与错误分类

### 4.1 HTTP 错误码

本分支新增/修改/删除的 HTTP 错误码：**0**。

应用层异常尚未映射为稳定 HTTP error body。后续 HTTP adapter 不得让前端解析英文/中文 message 来判断错误；需要先定义独立的 machine-readable `error.code`。

### 4.2 Coverage validator 机器码

`CoverageValidationError.code` 已稳定输出：

| code | 含义 |
|---|---|
| `invalid_total` | 目标总页数无效 |
| `invalid_range` | slice 范围越界或起止无效 |
| `missing` | slice range 缺口 |
| `duplicate` | slice range 重复 |
| `overlap` | slice range 重叠 |
| `out_of_order` | 输入 range 顺序错误（要求保持输入顺序时） |
| `malformed_result` | normalized NDJSON 损坏/非对象 |
| `missing_result` | slice 没有 normalized result |
| `invalid_page` | 页码无法转换为整数 |
| `duplicate_page` | 物理页重复 |
| `offset` | slice 内页序与全局 offset 不一致 |
| `out_of_range` | provider 返回页不属于当前 slice |
| `missing_page` | slice result 提前结束 |
| `coverage` | 最终集合不严格等于 `1..N` |

### 4.3 Provider-neutral 错误字段

`ParserProviderError` 字段：

- `provider_id`
- `retryable`
- `authentication_failed`
- `rate_limited`
- `remote_task_missing`
- `status_code`（上游 HTTP status，可为 `null`）
- exception message（仅供诊断，不是稳定 machine code）

已实现的关键分类：

| 上游情况 | 分类/动作 |
|---|---|
| 401/403 | `authentication_failed=true`；CredentialPool 将账号停用并标记 `unauthorized` |
| 429 | `rate_limited=true`, `retryable=true`；账号进入 cooldown |
| Local 404/410 | `remote_task_missing=true`, `retryable=true`；允许清除远程 affinity 后重提 |
| 408/429/5xx（Local/Qwen） | retryable |
| timeout/network（Qwen/Local） | retryable |
| malformed result / coverage error | permanent failure 或按 max attempts 终止；禁止 publish |

### 4.4 其他新异常类型

- `DocumentExportError`
- `PDFSlicingError`
- `CredentialPoolUnavailable`
- `AtomicPublishError`
- `MinerUAccountConfigError`

这些异常当前只是 Python 应用层契约，尚无稳定 HTTP error code。

手动 runner 的 credential JSON 若仍包含 `daily_page_budget`，会抛出 `ValueError`，明确提示 priority pages 不是 quota，避免旧配置产生“仍会限额”的误解。

## 5. 已改动的旧行为（非 HTTP 契约）

- MinerU 上传从 `Path.read_bytes()` 改为 file-like 流式 body。
- MinerU 下载改为 1 MiB chunk + partial file。
- 大文件 hash 使用 1 MiB chunk。
- normalized merge/export 改为 NDJSON/增量写入。
- `SimplePDF` 对超过 128 MiB 的文件不再做整文件内存 fallback。
- `VisionProviderConfig.api_key` 不再出现在 dataclass repr。
- 同一 published job 对同一 destination 重试时为幂等返回。
- 旧的单 MinerU Token 配置与现有普通 PDF 路径未删除。
- CredentialPool 只按 enabled、健康状态、cooldown、并发占用和本地成功页数做公平选择；成功页数只用于同等条件下的负载均衡，不构成上限。
- `CredentialPool.acquire(page_count)` 改为 `CredentialPool.acquire()`；`CredentialLease.page_count` 已删除，因为调度不再预扣页数。

## 6. 删除清单

- 删除 HTTP 端点：无
- 删除 HTTP request/response 字段：无
- 删除应用层 `save_account` 输入字段：`daily_page_budget`
- 删除应用层 `MinerUAccountSummary` 字段：`daily_page_budget`, `local_pages_used_today`, `local_pages_remaining_today`
- 删除应用层 `CredentialLease` 字段：`page_count`
- 删除 dry-run 输出字段：`budget_insufficient`（替换为 `credentials_unavailable`）
- 删除 SQLite 表/字段：无
- 删除旧 MinerU 单 Token 路径：无
- 删除旧 resume 逻辑：无

## 7. 前端/集成线必须遵守

- 不得向浏览器返回 Token 或 `secret_ref`。
- 账号配置区不得显示或提交“每日 1000 页预算”。
- 本地解析统计必须放在独立统计区，并明确不是 MinerU 官网用量或计费数据。
- 不得在 HTTP adapter 里写死 1000/200/50 等 provider 限制；必须读 capability/config。
- 不得在所有 slice validated 前显示为“整书已发布”。
- 前端只使用 1-based `physical_pdf_page`，不自行重算 global offset。
- 远程 task 的 poll/result 必须保持原 `credential_id` affinity。
- 正式 HTTP 对接前，需另行冻结 route、request schema、response schema 和 `error.code → HTTP status` 映射。

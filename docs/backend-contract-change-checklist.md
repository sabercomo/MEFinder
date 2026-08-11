# MEFinder 0.4.2 契约变更清单

> 交接范围：v0.4.0 → `0.4.2`
>
> 用途：前端、HTTP API 和后端的单一事实源。

## 1. HTTP 端点变更

| 类型 | 数量 | 结论 |
|---|---:|---|
| 新增 HTTP 端点 | 5 | MinerU 多账号读写、单账号测试、本地统计、单书导出 |
| 修改 HTTP 端点 | 0 | v0.4.0 已有路由的 request/response 保持兼容 |
| 删除 HTTP 端点 | 0 | 无 |
| FastAPI/Uvicorn | 0 | 未引入，继续使用现有 HTTP handler |

### 1.1 `GET /api/mineru-accounts`

用途：设置页一次取回全局 API 地址、安全账号摘要和本地统计。首次读取时，如果 v0.4.0 已存在单 Token 配置而多账号配置为空，则非破坏性迁移为 `mineru-default`。

200 response：

| 字段 | 类型 | 说明 |
|---|---|---|
| `configured` | `boolean` | 至少一个账号已配置且启用 |
| `api_base` | `string` | 所有 MinerU 账号共用的服务地址 |
| `accounts` | `MinerUAccountSummary[]` | 不含 Token/secret reference 的账号列表 |
| `statistics` | `MinerUUsageStatistics` | 本地成功解析归属统计 |

### 1.2 `POST /api/mineru-accounts`

用途：新增或更新一个独立 MinerU 账号。

JSON request：

| 字段 | 类型 | 必填/默认 | 契约 |
|---|---|---|---|
| `account_id` | `string?` | 新建可缺省 | 更新时作为稳定 ID |
| `display_name` | `string` | 必填 | 1–120 字符 |
| `token` | `string?` | 新建必填 | 更新时空字符串保留已存 Token |
| `enabled` | `boolean` | `true` | 是否参与新任务调度 |
| `expires_at` | `YYYY-MM-DD` 或空字符串 | 可选 | 空字符串清除本地到期日 |
| `max_concurrency_override` | `integer?` | `null` | 正数；当前 UI 不展示该高级项 |
| `api_base` | `URL?` | 可选 | 非空时更新全局服务地址 |

200 response 与 `GET /api/mineru-accounts` 相同，另增 `saved_account_id: string`。响应不返回 Token。

### 1.3 `POST /api/mineru-accounts/test`

request：`{"account_id": "..."}`。后端只解决并测试该账号已保存的 Token。

200 response 字段：`ok`, `latency_ms`, `api_base`, `account_id`。

### 1.4 `GET /api/mineru-statistics`

用途：独立刷新本地解析统计。200 response 是 `MinerUUsageStatistics`，字段见 2.1。

### 1.5 `POST /api/document/export`

用途：将当前索引中的一份 PDF 流式导出为 `mefinder.document.v1` Zip64 容器。

request：`{"source_id": "..."}`。

200 response 字段：

- `ok`
- `source_file_id`
- `schema_version`（固定 `mefinder.document.v1`）
- `path`（本机导出文件绝对路径）
- `size_bytes`
- `page_count`
- `warning_count`
- `missing_ranges`

导出目标为应用数据目录下的 `exports/`，后缀 `.mefinder.zip`。后端从 SQLite 逐页读取，写入 `<target>.partial`，成功后原子更名；不向浏览器返回整书文本。当前 HTTP/UI 仅支持已建立页级索引的 PDF，Word 返回 400。

### 1.6 兼容路由和未暴露路由

- v0.4.0 的 `GET/POST /api/mineru-config` 与 `POST /api/mineru-config/test` 保留，便于旧客户端/旧配置迁移。
- 没有新增账号 DELETE 端点；设置页可停用账号。
- `LargeDocumentJobEngine` 已接入现有 PDF 导入流，没有单独暴露 job create/status/resume/cancel HTTP 端点。断点继续由现有导入任务端点驱动。
- provider capability 暂未暴露为 HTTP 端点。

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

0.4.2 新增 `export_indexed_pdf(...)` 应用服务，把当前 SQLite `source_files + pdf_pages + pdf_import_runs + audit_issues` 投影到上述协议；文献详情菜单通过 `POST /api/document/export` 调用。

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

### 4.1 HTTP 状态码与 error body

| HTTP status | 适用情况 |
|---:|---|
| `200` | 读取、保存或连接测试成功 |
| `400` | JSON 不是对象、字段/网址/Token 无效、账号/文献不存在、不支持的单书导出，或连接测试被 MinerU 拒绝 |
| `413` | 请求超过现有 JSON body 大小上限 |
| `500` | 本地配置/数据库无法写入等服务端故障 |
| `503` | 应用已进入关闭流程，拒绝新 POST |

当前为了与 v0.4.0 前端保持一致，错误 body 仍为 `{"error": "可读消息"}`，**未新增 machine-readable `error.code`**。前端只展示 message，不通过解析中英文文案决定程序分支。

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
- 旧的单 MinerU Token 配置与兼容 HTTP 路由未删除；打开 MinerU 设置时会自动迁移为第一个账号。
- 存在多账号配置时，现有 PDF 导入转入 0.4.2 大文档引擎：生成真实子 PDF，按凭据保持远程 task affinity，完成全页覆盖校验后再写入现有索引契约。
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
- 删除旧 MinerU 单 Token 路径：无（保留兼容，设置页使用新多账号端点）
- 删除旧 resume 逻辑：无

## 7. 前后端集成约束

- 不得向浏览器返回 Token 或 `secret_ref`。
- 账号配置区不得显示或提交“每日 1000 页预算”。
- 本地解析统计必须放在独立统计区，并明确不是 MinerU 官网用量或计费数据。
- 不得在 HTTP adapter 里写死 1000/200/50 等 provider 限制；必须读 capability/config。
- 不得在所有 slice validated 前显示为“整书已发布”。
- 前端只使用 1-based `physical_pdf_page`，不自行重算 global offset。
- 远程 task 的 poll/result 必须保持原 `credential_id` affinity。
- 设置页只调用 `/api/mineru-accounts*` 新端点；旧 `/api/mineru-config*` 仅用于兼容。
- 本地解析统计位于设置左侧独立一级“统计”面板，不与 MinerU 账号编辑混排。
- 文献详情的单书导出只提交 `source_id`，不在前端聚合或下载全部页文本。
- 需要程序化区分新错误类型时，先新增稳定 `error.code` 并更新本清单，不得直接解析现有 message。

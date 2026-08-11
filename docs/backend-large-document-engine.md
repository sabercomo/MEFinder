# MEFinder 0.4.2 大文档解析引擎

本文档描述可由未来 HTTP API、桌面端命令或后台 worker 调用的稳定应用层。它不包含前端设置页、FastAPI、Uvicorn 或本地 MinerU 模型依赖。

## 处理边界

```text
LargeDocumentJobEngine
  -> SlicePlanner
  -> PhysicalPDFSlicer
  -> ParserProvider
       -> MinerUCloudProvider
       -> MinerULocalProvider
       -> QwenOCRProvider
  -> CredentialPool (optional)
  -> NormalizedParseResult
  -> coverage validation / merge
  -> AtomicPublisher
  -> mefinder.document.v1 export
```

`LargeDocumentJobEngine` 只读取 `ProviderCapabilities`，不识别 MinerU/Qwen 的 HTTP 协议。它按页数和字节限制规划实体子 PDF，递交后保存远程 task ID，将所有 provider 结果归一为页级 `NormalizedParseResult`。只有页码覆盖通过完整性验证后，才会生成和原子发布整书导出。

## `mefinder.document.v1`

对外协议是 MEFinder 的 provider-neutral 格式，不直接暴露 MinerU 原始 JSON。

普通文档可写成单一 JSON；大文档写成 Zip64：

```text
book.mefinder.zip
  manifest.json
  pages.ndjson
```

manifest 中的稳定字段包括：

- `schema_version`: 固定为 `mefinder.document.v1`
- `document`: `document_id` / `source_file_id` 等本地 identity
- `source_sha256` 和 `source_file`
- `bibliographic_metadata` 和 `external_ids`
- `parser.provider/model/version/options/provenance`
- `parsed_at`、`page_count`、`warnings`、`missing_ranges`

`pages.ndjson` 每行是一页，可表达：

- `physical_pdf_page`: 原书 PDF 的 1-based 物理页号
- `logical_page` / `pdf_page_label`
- `text`、`blocks`、`bbox`、`reading_order`
- `parser_provenance`、`warnings`

不存在的书目、逻辑页或版式数据保持空值，不会伪造。写入器逐页编码，先写 `<target>.partial`，完成并 `fsync` 后才原子 rename。

## 切片、恢复和发布

1. `prepare`: 流式计算源文件 SHA-256，读取页数与 provider capability。
2. `slice`: 先用页数/文件字节粗规划，再根据实际子 PDF 大小递归细分。PyMuPDF `insert_pdf` 复制原 PDF page objects，不栅格化，不重做 OCR。
3. `parse`: 每片记录原书起止页、global offset、物理路径、SHA-256 和字节数。
4. `resume`: completed slice 不重传；有 remote task ID 的 slice 优先继续 poll/fetch；slice/result hash 失配时重建对应本地产物；源 PDF hash 改变时拒绝续接旧任务。
5. `validate/merge`: 纯逻辑 validator 检查目标范围、缺页、重复、重叠和顺序；合并输出是增量 NDJSON。
6. `publish`: 先构建已验证的完整 candidate，再原子替换目标。如果 index callback 失败，恢复前一版文件。恢复过程对同一已发布目标是幂等的。

## SQLite job ledger

任务状态放在独立的 `parser_jobs.sqlite3`，不放在可原子重建的搜索 index 数据库中。SQLite `PRAGMA user_version` 递增迁移；当前版本是 3。

### v1

`document_jobs`:

- source/document identity，`source_path`，`source_sha256`
- provider/model/options fingerprint
- `status`，总/完成页数与 slice 数
- publish status/path，error summary，timestamps

`slice_jobs`:

- document foreign key，原书 `page_start/page_end/global_page_offset`
- `slice_path/slice_sha256/size_bytes`
- provider，credential ID，remote task ID
- status，attempt count，last error
- normalized result path/hash，timestamps

### v2 / v3

添加 `parser_credentials`：

- `id/provider_id/display_name/secret_ref`
- enabled，可选 concurrency override，current in-flight，cooldown
- last 401/429，health status，timestamps

v2 曾误把 MinerU priority pages 当成每日预算；v3 清空并停止使用 `daily_page_budget/pages_used_today/usage_date`。三列仅作为旧 SQLite 的兼容占位保留，不再属于应用层契约。v1 ledger 会保留原 job/slice 记录并新建 credential 表。数据库版本高于当前程序支持时会明确拒绝打开，避免旧程序破坏新 schema。

Document 状态：`preparing/queued/running/waiting/retryable_failure/permanent_failure/cancelled/validated/published`。Slice 状态另包含 `submitted/completed`。

## CredentialPool

CredentialPool 支持 N 个用户明确配置且已授权的 credential，并同时考虑 enabled、provider/用户并发、cooldown 和健康状态。已成功解析的本地累计页数只用于同等条件下的公平调度，不是额度。

MinerU Cloud 的业务假设是“一个 credential 对应一个独立 MinerU 账号”，不对多 Token 做账号分组。MinerU 的“优先解析 1000 页”不是每日配额：系统没有 1000 页硬门槛，不会在达到 1000 页时停用或强制切换账号。`MinerUAccountService` 可保存 N 个独立账号。Token 存在权限为 `0600` 的本地私密配置中，job ledger 只保存 `mineru-account:<id>` 引用。账号配置 summary 只返回配置、in-flight 和健康状态，不返回 Token 或页数统计。

- 只将 `secret_ref` 写入 SQLite，运行时由 resolver 解决真实 secret。
- 401 会标记未授权并停用；429 进入 cooldown。
- 未创建远程 task 的 slice 可换 credential。
- `remote_task_id -> credential_id` 持久化；poll/result 一直使用原 credential。只有 provider 明确返回 remote task missing 才允许清除 affinity 并重提。
- 不读取、抓取或同步 MinerU 官网用量。
- `usage_statistics()` 在独立统计区返回逐凭据成功解析的书数、页数、书名和 1-based 原书页码范围；数据由 completed slice attribution 聚合，不参与额度判断。

验收包含单个账号连续解析 1200 页，证明 1000 页不会触发 cutoff；8000 页/8 账号场景仍验证 40 个实体 slice、远程任务 credential affinity、逐账号书页归属和最终严格覆盖 `1..8000`，但不把 1000 作为额度规则。

credential JSON 只允许引用：

```json
{
  "credentials": [
    {
      "id": "mineru-1",
      "display_name": "MinerU 1",
      "secret_ref": "env:MINERU_TOKEN_1",
      "max_concurrency": 2
    }
  ]
}
```

manual runner 当前只解决 `env:NAME`；正式应用可注入 keychain/credential manager resolver。配置文件如出现 `token`、`secret` 或 `api_key` 明文字段会被拒绝；出现废弃的 `daily_page_budget` 也会被拒绝并说明 priority pages 不是 quota。

## Provider support

### MinerU Cloud

`MinerUCloudProvider` 包装现有 `MinerUClient`，支持批次提交、poll、流式下载、取回 ZIP/内容并归一化页级结果。上传 body 是 file-like stream，不再通过 `Path.read_bytes()` 复制整个 PDF。支持可配置页/字节/并发 capability 和 CredentialPool。

### MinerU Local

`MinerULocalProvider` 把 MinerU 当成独立 HTTP service，支持 health probe、`POST /tasks`、`GET /tasks/{id}`、`GET /tasks/{id}/result` 的异步流程，并保留同步 `/file_parse` 运行模式。上传使用标准库 `http.client` 的 multipart 分段写入。MinerU/PyTorch/CUDA/模型权重不是 MEFinder 依赖。本地 service 重启后返回 task missing 时，ledger 会保留可解释状态并允许重提。

### Qwen OCR

`QwenOCRProvider` 复用已有 OpenAI-compatible vision transport，将实体 slice 逐页渲染为图像并调用 Qwen OCR，不要求将用户 PDF 放到公共 URL。默认配置是 `qwen3.5-ocr`、50 页/100 MiB，只属于 Qwen provider 配置，可随服务限制变化。支持同步结果、超时/429 分类、繁体中文、可选 bbox 和原书页码 offset。API key 不进入 repr/log/export。

## Torture-test 与手动运行

完全离线 dry-run：

```bash
python3 tools/large_document_torture.py \
  --provider synthetic \
  --dry-run \
  --synthetic-pages 7000 \
  --synthetic-bytes 2147483648 \
  --max-pages 200 \
  --max-concurrency 8 \
  --credentials tests/fixtures/torture_credentials_8.json
```

输出包含总页数/字节数、capability、每片范围和估算字节、估算上传/临时磁盘、credential 分配/页数/是否无可用凭据、完整覆盖和流式内存 probe。`--dry-run` 不构建 provider，不解决 secret，不调用 API。

小型真实 PDF 的 opt-in benchmark 必须显式使用 `--execute`：

```bash
python3 tools/large_document_torture.py \
  --pdf /absolute/path/sample.pdf \
  --provider mineru-local \
  --local-endpoint http://127.0.0.1:8000 \
  --execute \
  --output /absolute/path/sample.mefinder.zip \
  --benchmark-output /absolute/path/sample.metrics.json
```

Cloud/Qwen 运行也必须显式 `--execute`；工具不提供默认付费调用路径。metrics 记录 provider/model/pages/wall time/failures/cost 占位/输出大小/warning count/job ID/status。真实 2GB/7000+ 页古籍仅通过此手动入口运行，不进入仓库或 CI。

## 依赖与打包

- 只在实体 PDF 计页/切片或 Qwen 逐页渲染时需要现有 PyMuPDF 运行依赖。
- MinerU Local 是外部服务，没有将重型 GPU 依赖加入 Windows/macOS 桌面包。
- 不配置 Local/Qwen/多 credential 时不会在启动时导入或联网。
- 未引入 Celery、Redis、FastAPI 或 Uvicorn。

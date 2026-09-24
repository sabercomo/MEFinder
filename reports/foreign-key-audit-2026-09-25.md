# 外键实证:foreign_key_check 与写连接外键开关影响面(2026-09-25)

2026-09-25:真实用户库与开发库均 **0 条外键违例**;把 persistence 外写连接统一开 `foreign_keys=ON`,对现有写路径**无可观察行为变化**(唯一例外 `build_database` 临时库保持原样)。另发现统一 `busy_timeout=30000` 会改变读路径的等待时长,属行为变化,待用户确认。

服务于 `docs/refactor-v0.5.7-plan.md` 阶段 A1。原始数据:`reports/foreign-key-audit-2026-09-25.json`;脚本:`scripts/foreign_key_audit.py`。

## 1. 方法

- 源库一律以 `mode=ro` URI 只读打开,经 SQLite 在线备份 API 复制到会话临时目录,所有检查在**副本**上跑;原库大小与 mtime 前后一致,未写入。
- 每库记录:`user_version`、`PRAGMA quick_check`、全部表的 `PRAGMA foreign_key_list`(声明的外键与 `ON DELETE` 动作)、`PRAGMA foreign_key_check` 按(子表, 父表, 外键 id)聚合的违例行数。报告只含表名与计数,不含题名、正文或路径。
- 开发库:本机 macOS 没有 `data/index.sqlite3`(`DEFAULT_DATABASE_PATH`),用 `scripts/performance_fixture.py` 的确定性合成库代替(当前代码 schema)。

## 2. 结果(事实)

| 库 | user_version | quick_check | 声明外键 | 违例行 |
|---|---|---|---|---|
| 真实用户库 `index.sqlite3`(约 3.3 GiB,由已安装 0.5.5 写入) | 7 | ok | 24 | 0 |
| 真实用户库 `parser_jobs.sqlite3` | 3 | ok | 1 | 0 |
| 合成开发库 `index.sqlite3` | 8 | ok | 18 | 0 |

真实库中外键子表行数最多的是 `alignment_link_members` 约 160 万行、`text_segments` 约 39 万行,违例为零。

**全部声明外键都是 `ON DELETE CASCADE`**,只有 `document_group_reading_positions.right_source_file_id` 是 `SET NULL`。因此开外键的风险不在"历史数据插不进去",而在于"删除父行时开始级联"。

## 3. 写连接外键开关影响面(事实,逐调用点审阅)

persistence 外 30 个 `sqlite3.connect` 中,已开外键的写路径:`database.replace_source_in_database`、`database.delete_sources_from_database`、`large_document/job_ledger._connect`。作品组(`document_groups`)的写入经 `open_writable_index`,**同样已开外键**——计划 §3.1 说"`document_groups.py` 写入未开"不成立,以此处为准。

未开外键、但会写库的路径:

| 调用点 | 写什么 | 开外键后 |
|---|---|---|
| `bibliographic_metadata.update_metadata_in_database` | `UPDATE source_files SET payload_json` | 不改键列,外键不参与,无变化 |
| `runtime_page_mapping.apply_mapping_to_database` | `UPDATE source_files SET payload_json` | 同上 |
| `application/document_heading_enrichment._connect` | `UPDATE source_files SET payload_json` | 同上 |
| `database.ensure_database_search_index` | FTS 虚表建/重建 | FTS 表无外键,无变化 |
| `database.optimize_database_storage` | 新建库 + `ATTACH legacy` 按表 `INSERT ... SELECT` 整表复制,无 DELETE | **保持原样**:按表复制的顺序不是为外键设计的,属临时库构建,走专用 helper |
| `database.build_database` | 从空临时库整库写入后原子替换 | **保持原样**:批量插入顺序未按父先子后设计,开外键可能让构建失败;计划本就要求临时库构建走专用 helper |
| `data_location._copy_sqlite_database` | 在线备份到新位置 | `backup()` 页级复制,外键不参与;走专用 helper |

全仓 `INSERT OR REPLACE` 只作用于 `metadata`、`works`、`zotero_*` 表,均不是任何外键的父表,开外键不会触发"REPLACE 先删后插导致级联"。

结论:A2 中把"写连接统一 `foreign_keys=ON`"落到上表前三行与已开的路径,行为不变;`build_database`、`data_location` 备份、`optimize_database_storage` 用专用 helper 保持现有设置。

## 4. 另一处行为变化:`busy_timeout`(事实 + 推断)

- 事实:只有 `persistence/connection.py`、`database.ensure_database_search_index`、`job_ledger` 设 `busy_timeout = 30000`;其余 25 处用 Python `sqlite3` 默认 5 秒。`web_http.py` 检索路径把超时后的 `database is locked` 映射为可重试 503。
- 推断:统一成 30 秒后,写入持锁期间这些读路径会等更久(最多 30 秒)再成功或报错,而不是 5 秒报错。这对用户是"少见报错、偶尔多等"的时序变化,不改结果内容,但**属于行为变化**。
- 建议:A2 默认沿用各调用点现有超时(通用入口显式参数化),统一到 30 秒另作一步并经用户确认。

## 5. 复现

```bash
.venv-macos312-arm64/bin/python -m scripts.foreign_key_audit --workdir <临时目录> --fixture
.venv-macos312-arm64/bin/python -m scripts.foreign_key_audit --workdir <临时目录> --label real <数据目录>/runtime/data/index.sqlite3 <数据目录>/runtime/data/parser_jobs.sqlite3
```

真实库副本约 3.3 GiB,留在会话临时目录,不入库。

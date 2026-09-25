# MEFinder 前后端架构重构计划

2026-09-25:阶段 A、B1/B2 已实施;B3 移出本轮,数据库等待时间保持现状。C1.1—C1.5 已完成,下一步 C2。版本号 v0.5.7 为暂定,尚未发布。

2026-09-25(复测):已获用户授权开工,在 `refactor/v0.5.7-architecture`(自 `771f917` 开出)执行;复测差异见 §3.3,以复测值为准。

2026-09-25(0.5.6 后续合入):从 `origin/codex/v0.5.6-integration` 的 `685d5f1` 合入 0.5.6 后续五提交(含 Zotero 队列续跑、MCP 数据根二级指针与版本号 0.5.6)。`web_runtime.py` 的队列容量接线与本轮架构变更自动合并;前端装配指纹按两边变更后的实际 HTML 重测。0.5.6 分支的发布说明仍属 0.5.6,本轮未发布 0.5.7。

2026-09-25(Windows 修复合入):再次合入 `origin/codex/v0.5.6-integration` 的 `58a4ac0`(译本对照后台预热与构建缓存);新增只读连接改走 `persistence.connection.connect_index`,并重测前端装配指纹。合并后 macOS 全量 unittest 2573 通过、23 跳过。

本文件是给**新会话**用的执行计划。先读"开场 prompt",再按阶段推进。§3 保留重构前基线与复测更正,不是当前代码状态;已完成项以 Git 与各阶段进展为准。动手前复测本步骤相关指标,不要从 A 重新执行。

---

## 0. 开场 prompt(复制到新会话)

```text
按 docs/refactor-v0.5.7-plan.md 执行架构重构。

要求:
1. 先按 AGENTS.md §5 读档并校对工作区;确认 0.5.6(Zotero 同步)已提交,否则停下告诉我。
2. 用计划第 6 节的命令复测基线,和计划里的数字对照,差异先报告。
3. 从当前未完成步骤继续(当前为 C2 `web_runtime.py` 组合根拆分),一次只做一个阶段内的一个步骤;每步:先写/改守卫测试 → 重构 → 全量 unittest 全绿 → 按 AGENTS.md §2.1 提交。
4. 行为不变是硬约束:不改 HTTP 契约语义、不改对齐/检索结果;需要改行为的地方(如外键约束)先出实证报告再问我。
5. 遵守 CLAUDE.md 红线;按路径暂存,不要 git add -A。
6. 每个阶段结束停下来,汇报基线变化和剩余风险,等我确认再进下一阶段。
7. 技术取舍由执行者负责,不再要求用户选择超时秒数、校验模型或拆文件方式。已定取舍见 §0.1;如果候选方案会改行为,优先保留原行为并继续独立步骤。只有确实无法兼容且阻塞目标的产品取舍才带具体影响与推荐方案请用户决定。
```

### 0.1 当前技术决策(2026-09-25)

用户表示不懂技术,委托代理判断。以下是本次代理据此作出的技术决策,取代此前“B3/超时待用户决定”的停点;阶段结束汇报的约定保留。

- **B3 严格入参校验移出本轮**:保留现有 controller 的校验、类型转换、状态码与错误文案。本轮不引入 `parse_payload` 或新 `code` 字段。输入校验本身可以在保持行为的前提下重构;但原 B3 的“字段类型/必填校验”会收紧兼容范围,不应混在结构整理里。以后有具体缺陷时单独立项并更新契约。
- **数据库等待时间保持现状,不再待确认**:各调用点原来等 5 秒或 30 秒就维持原值。等待更久不能消除锁竞争,现有外键审计也没有证明统一 30 秒的必要性;后续如有真实超时故障,先记录锁等待与用户响应时间再决定。
- **接受 B2 的职责拆分结果**:`web_http.py` 当前 401 行,现有守卫上限 405 行保留。原 ≤300 行目标不再作为本轮门禁,不为行数再拆传输细节;路由唯一、业务移出、信任校验/上传排空/Range 行为不变仍是门禁。
- **C1.1 作品组仓储迁移已完成**: `document_groups` 的 SQL 收进 `persistence/document_group_store.py`,原模块保留兼容入口;纯版本名称逻辑随仓储依赖移入 persistence 并保留原导入路径。事务边界、调用接口、返回结果与删除语义保持;SQL 散落白名单删除该文件。下一步 C1.2,不同时启动 C/D 其他步骤。
- **C3 先调查生命周期再设计共用管理器**:统计线程创建点、取消方式、退出等待与进程回收的实际差异,不能只为消除裸 `Thread` 就强行套同一接口。暂不改变现有线程行为,具体迁移范围由调查结果决定。

---

## 1. 目标与不做的事

**目标**:在不推倒重写的前提下,收口四类问题——DB 连接策略分散、HTTP 分发多轨、组合根过重、前端请求与 DOM 构造无统一出口。

**明确不做**:

- 不换 FastAPI / 不上 async / 不引 ORM / 不引 DI 容器(边界测试已禁止)。
- 前端不引框架、不引构建步骤。
- 本轮不切 SQLite WAL:`database.py` 依赖"临时库 + 原子替换文件"发布,WAL 的 `-wal/-shm` 与之冲突,需单独议题 + `reports/` 实证。

---

## 2. 前置条件

1. 0.5.6(Zotero 同步)已提交。它的改动覆盖 `web_http.py` / `http_routes.py` / `migrations.py` / `schema_installers.py` 等,先重构必冲突。
2. 全量测试在当前 `main`(或集成分支)全绿。
3. 本计划涉及大面积重构,按 AGENTS.md §2.2 开 `refactor/*` 分支,各阶段验证后合入。

---

## 3. 现状基线(2026-09-25 实测)

### 3.1 后端

| 指标 | 值 | 说明 |
|---|---|---|
| `src/me_finder` 模块数 / 行数 | 183 / 约 74.7k | |
| 内部 import 环 | 0 | 已有 `test_no_new_import_cycles_appear` |
| `sqlite3.connect` 调用点 | 31(约 20 个模块) | 仅 2 处走 `persistence/connection.py` |
| persistence 外含 `.execute(` 的文件 | 27 | 最多:`database.py` 67、`document_groups.py` 65、`text_alignment.py` 52、`large_document/job_ledger.py` 36、`translation_works.py` 21 |
| `_table_exists` 定义 | 4 份 | `document_groups` / `text_alignment` / `schema_installers` / `migrations` |
| `web_http.py` 内 `parsed.path` 分支 | 31 | 另有 `_POST_ROUTE_TABLE`、controller 路由、shell 路由三套分发 |
| `web_runtime.py` 直接内部依赖 | 63 | `ApplicationRuntime` 28 个字段,几乎全是 `object` |
| 自起线程的模块 | 约 14 | `managed_mineru` 5、`managed_alignment_runtime` 5、`native_document_open` 5、`alignment_compute` 4、`local_ocr_installer` 4 … |
| `except Exception` | 148 | |

**连接策略不一致(事实)**:`busy_timeout=30000` 只在 `persistence/connection.py` 与 `database.py` 部分路径设置,其余用 Python 默认 5 秒;`PRAGMA foreign_keys = ON` 只在 `open_writable_index` 与 `database.py` 部分写路径开启,`document_groups.py`、`alignment_overrides.py` 等写入未开。

**与既有文档的出入**:`docs/refactor-v0.5.0.md` 写"SQL 已收进 persistence",实测不成立(见上表)。执行本计划时在该文件以日期行追加更正,不改旧结论。

**已有的好模式(照抄)**:`persistence/zotero_sync_store.py`、`persistence/document_read_repository.py`(SQL 收口);`zotero_sync_assembly.py`、`managed_component_assembly.py`(按域装配);controller 返回 `(status, dict)`、不碰传输。

### 3.2 前端

| 指标 | 值 | 说明 |
|---|---|---|
| `static/js/*.js` 总行数 | 约 14.1k | 最大:`35-works` 2007、`60-settings` 1954、`80-import` 1514、`70-vision` 1394、`30-library` 1179 |
| `static/reader.js` | 4301 行、约 167 个函数、1 个共享可变 `state` | 已禁 `innerHTML`;公共面 `MEFinderReader` 已冻结;详见阶段 D |
| `fetch(` 调用 | 约 105,分布 14 个文件 | `70-vision` 25、`60-settings` 25、`80-import` 21;无统一客户端,`35-works` 与 `62-zotero` 各有私有 `requestJSON`/`getJSON` |
| 使用 `innerHTML` 的 JS 文件 | 10 | 字符串拼接 HTML |
| `index.html` 内联事件属性 | 182 | `onclick=` / `onchange=` / `oninput=` |

**安全隐患(事实 + 推断)**:动态 HTML 里有 `onclick="fn(event,'` + `esc(id)` + `')"` 形式(如 `20-search.js` 的分组选项)。HTML 实体先解码再进 JS,`esc()` 挡不住单引号。**推断**:目前这些 id 由后端生成,实际利用面低;但同文件已有 `data-value` + `this.dataset.value` 的安全写法,应统一到后者。

### 3.3 复测更正(2026-09-25,macOS `.venv-macos312-arm64`,HEAD `771f917`)

事实(按 §6 命令与 AST 扫描复测):

| 指标 | 原值 | 复测值 |
|---|---|---|
| 全量 unittest | — | 2534 通过 / 23 跳过;ruff `src tests scripts` 零告警 |
| persistence 外 `sqlite3.connect` | 31(约 20 个模块) | 30(17 个模块);A2 清单漏了 `application/document_heading_enrichment.py`、`large_document/job_ledger.py` |
| `fetch(` 分布文件数 | 14 | 13(总数 105 一致) |
| `index.html` 内联事件(click/change/input) | 182 | 205(`771f917` 的 Zotero 设置页新增;推断,未逐条核对) |
| 自起线程 | 约 14 模块、如 `managed_mineru` 5 | §6 命令只得 10 个模块、每个 1 行;表中数字与命令口径不一致,C3 开工前重定统计口径 |

补充事实:persistence 外含 `.execute(` 的 27 个文件中有 5 个在 `application/`(`import_orchestrator`、`literature_verification_service`、`parallel_passage_service`、`script_search`、`document_heading_enrichment`),现有边界测试只禁 application import persistence,未禁直接写 SQL;A0 棘轮先冻结现状。

2026-09-25(A1 实证):真实库与开发库 `foreign_key_check` 均 0 违例;`document_groups` 写入经 `open_writable_index` 已开外键,§3.1 所述不成立。统一 `busy_timeout=30000` 会改读路径等待时长,属行为变化,A2 默认保留各点现值。详见 `reports/foreign-key-audit-2026-09-25.md`。

---

## 4. 分阶段计划

每步一个提交,提交信息按 AGENTS.md §2.1。每步结束:全量 unittest 全绿、ruff `F` 零新增、需要时更新前端指纹/预算。

### 阶段 A — 后端 DB 连接收口(最先做,不影响前端)

**A0 守卫测试(棘轮)**
- 在 `tests/test_architecture_boundaries.py` 新增:
  - persistence 外 `sqlite3.connect` 调用点白名单 = 当前清单,**只许删不许增**;
  - persistence 外含 `.execute(` 的文件白名单,同上。
- 提交:`test(arch): 新增 DB 连接与 SQL 散落棘轮基线`

**A1 外键实证(先于改行为)**
- 对真实用户库(先备份)与开发库跑 `PRAGMA foreign_key_check`,结果写进 `reports/`。
- 有违例:停下,报告给用户,先设计数据修复迁移(走 `migrations.py` + `user_version`),再进 A2。

**A2 统一连接入口**
- `persistence/connection.py` 增加上下文管理器:`open_read(path)`、`open_write(path, *, immediate=False)`、`open_readonly_snapshot(path)`(URI `mode=ro`);集中连接策略,逐点保留原 `row_factory` 与等待时间(5 秒或 30 秒),写连接统一 `foreign_keys=ON`。保留现有 `open_readonly_index` / `open_writable_index`。
- `table_exists` 下沉到 persistence,删除 4 份私有副本。
- 按机械程度迁移调用点:`document_groups` → `alignment_overrides` / `alignment_snapshots` / `alignment_body_range` → `translation_works` / `runtime_page_mapping` / `parser_statistics` → `text_alignment` → `bibliographic_metadata` / `document_export_service` / `document_deletion` / `indexer` / `index_publisher` → `application/document_heading_enrichment` / `large_document/job_ledger`(复测补漏) → `data_location` → `database.py`。
- 每迁一批把 A0 的白名单删掉对应条目。
- 验收:persistence 外 `sqlite3.connect` = 0。
- 注意:`database.py` 的临时库构建与 `ATTACH`、`data_location` 的 `backup()` 属特殊连接,可提供专用 helper,不要硬塞通用入口。

### 阶段 B — HTTP 契约统一(唯一需要前后端联动的阶段,同一版本完成)

**B1 前端统一请求客户端(先做)**
- 新增 `static/js/07-api.js`:`apiGet(url, opts)`、`apiPost(url, payload, opts)`、`apiUpload(...)`;统一 `cache: 'no-store'`、JSON 解析、`!resp.ok || data.error` → 抛带 `status`/`code`/`message` 的错误。
- 把约 105 处 `fetch(` 迁过去;删除 `35-works` / `62-zotero` 的私有 helper。上传与 Range 等特殊请求可保留直接 `fetch`,但须集中在 `07-api.js`。
- 新增前端守卫:`07-api.js` 之外禁止 `fetch(`(棘轮)。
- 同步:全局符号预算、`test_frontend_assets.py` 指纹(命令见 AGENTS.md §3.6)。

**B2 后端单一路由注册表**
- 在 `http_routes.py` 引入 `Route` 数据类:`method`、`path`、`handler`、`body`(`json` / `raw` / `none`)、`mutates_data_root: bool`、可选 `payload_model`。
- `RAW_BODY_POST_PATHS`、`DATA_ROOT_MUTATING_POST_PATHS` 改为由路由属性推导,删除手写集合。
- 把 `web_http.py` 的 31 处 `parsed.path` 分支迁出:`/api/import`、`/api/import-upload/*`、`/api/import-local`(含读偏好、校验扫描目录这段业务逻辑)进导入 controller;`/api/search` 进搜索 controller;其余同理。
- `web_http.py` 只保留:可信来源/Host 校验、Content-Type 门、读/排空请求体(Windows 断连修复保持原样)、分发、Range 流式、关闭中 503。按 §0.1 接受现有 401 行结果,保留 `test_web_boundary_stays_split_by_responsibility` 的 405 行上限。
- 新增测试:由注册表导出路由清单,与 `docs/contracts/` 当前版本契约比对。
- 验收:`test_http_api_contract` 及上传/排空相关测试**不改断言**通过。

2026-09-25(进展):B1、B2 已完成(`7550755` `7b23e8d` `abfc4bb` `f5febf3`)。前端 105 处 `fetch(` 收口到 `07-api.js`;后端 `http_route_table.RouteTable` 为唯一注册表,原始请求体/数据目录名单与请求体上限由路由声明推导,旧手写集合钉在 `tests/test_http_route_table.py` 证明逐项相等;`web_http.py` 763 → 401 行(未到 ≤300 目标:剩余均为计划明确保留的信任校验、读体/排空、Range 流式)。B3 暂停待用户决定:字段类型/必填校验会拒绝现在被宽松接受的输入(如数字标题)并改变部分错误文案,属行为变化。

2026-09-25(技术决策):上述“B3 暂停待用户决定”已由 §0.1 取代。B3 移出本轮,保留现有输入兼容行为;阶段 B 按 B1/B2 收尾。Windows 自动测试通过不等于 Windows 打包/桌面冒烟通过,后者仍未验收。

**B3 统一入参校验(移出本轮,以下保留原提案供后续议题参考)**
- 新增轻量 `parse_payload(Model, payload)`(dataclass + 字段类型/必填校验),失败抛 `PayloadError` → 400。错误体保持 `{"error": "中文消息"}`,可增 `code`,前端 `07-api.js` 已能透传。
- 从 `DocumentGroupController` 开始逐个 controller 迁移,把 `payload_model` 登记到 `Route`。
- 不引 pydantic。

契约若有任何可见变化(新增 `code` 字段等),按 `docs/backend-contract-change-checklist.md` 出新版 `docs/contracts/vX.Y.Z-http-api.json`。

### 阶段 C — 可并行,穿插功能迭代分批做

**后端 C1 SQL 进仓储**(照 `zotero_sync_store.py`)
1. `document_groups` → `persistence/document_group_store.py`
2. `alignment_overrides` / `alignment_snapshots` / `alignment_body_range` → `persistence/alignment_store.py`
3. `translation_works`、`runtime_page_mapping`、`bibliographic_metadata` 写库部分
4. `text_alignment.py` 拆:`alignment_segmentation.py`(`segment_*` 纯函数)、`alignment_generation.py`(`generate_alignment` 编排)、SQL 进 `alignment_store`
5. `database.py` 拆:`persistence/fts_index.py`、`persistence/index_build.py`、`persistence/source_replace.py`、`persistence/storage_optimization.py`;`database.py` 暂留兼容转发
- 顺带处理 `docs/refactor-v0.5.0.md` 记录的残留环根因(`bibliographic_metadata` 顶层依赖 `database.paragraph_payload_for_storage`)。
- 纯搬迁,对齐/检索 golden 与 `tests/fixtures/search_pipeline_golden.json` 不得变化。每批收紧 A0 白名单。

2026-09-25(C1.1 进展):作品组读写、事务、快照恢复与成员展示代码迁入 `persistence/document_group_store.py`;`document_groups.py` 保留原导入面。`document_group_metadata.py` 同样保留兼容导入面,persistence 内部使用本层的纯函数实现。棘轮从 26 个 SQL 散落文件收紧到 25 个。提交与全量门禁以本步骤结果为准。

2026-09-25(C1.2a 进展):`alignment_overrides.py` 的人工校正读写、事务与列表查询进入 `persistence/alignment_store.py`;原层保留路由/分段校验与错误文案。补测“过期提议报错后撤销状态仍入库”,SQL 散落文件 25→24。C1.2 尚未完成,下一批处理 `alignment_snapshots.py`,再处理 `alignment_body_range.py`。

2026-09-25(C1.2b 进展):`alignment_snapshots.py` 的配方读取、存在性查询与整体替换事务收进同一仓储;原层仍决定配方兼容与调用对齐计算。新增失败回滚测试,守住“恢复途中出错时保留原对齐”;SQL 散落文件 24→23。C1.2 最后一批为 `alignment_body_range.py`。

2026-09-25(C1.2c 进展):`alignment_body_range.py` 的段落/页面锚点查询、分段窗口查询与可写审阅事务收进 `persistence/alignment_store.py`;正文范围判断、出版方页码展示和异常文案仍在原层。SQL 散落文件 23→22,C1.2 三批均已完成。下一步 C1.3 按模块分批推进。

2026-09-25(C1.3a 进展):`runtime_page_mapping.py` 的 PDF 页面、段落、来源和映射写库操作进入 `persistence/page_mapping_store.py`;映射计算和备份时序仍在原层。补测段落更新失败时页面改动整体回滚;SQL 散落文件 22→21。C1.3 尚未完成,下一批处理 `translation_works.py`,再处理 `bibliographic_metadata.py`。

2026-09-25(C1.3b 进展):`translation_works.py` 的阅读位置、同名建议忽略、检查暂缓四条写入和事务进入 `persistence/translation_work_store.py`;作品/对齐校验、只读查询和返回结构仍在原层。补测阅读位置写入后报错会回滚。该模块仍有只读 SQL,所以 SQL 散落文件保持 21,不提前缩减棘轮。下一批处理 `bibliographic_metadata.py` 写库。

2026-09-25(C1.3c 进展):`bibliographic_metadata.py` 更新来源、卷、作品、段落的 SQL 与立即事务进入 `persistence/bibliographic_metadata_store.py`;题录归一、payload 变换及更新顺序留在原层。补测卷更新失败时来源更新回滚;SQL 散落文件 21→20。`bibliographic_metadata` 对 `database.py` 的旧环依赖已在此前的 `paragraph_payload` 下沉时消除,本批无需重复修改。C1.3 三批完成,下一步 C1.4。

2026-09-25(C1.4a 进展):PDF/EPUB 纯分段类型、规则与 `segment_*` 函数进入 `alignment_segmentation.py`,`text_alignment.py` 保留导入面。迁移的 10 个函数/类型 AST 相同;原文件 2371→2054 行,行数守卫收紧到 2060,新模块上限 350。下一批再分生成编排和 SQL,此时 C1.4 尚未完成。

2026-09-25(C1.4b 进展):对齐准备、生成、发布流程进入 `alignment_generation.py`,原模块保留旧导入面;生成阶段使用的 SQL 与两段立即事务进入 `persistence/alignment_store.py`。生成模块无直接 `.execute`,`text_alignment.py` 降至约 1207 行。既有测试中对计算函数的替身改指向新定义模块;定位/读取 SQL 仍在 `text_alignment.py`,下一批继续收口,C1.4 尚未完成。

2026-09-25(C1.4c 完成):作品组目标、选区、定位、候选段与人工确认读取 SQL 全部进入 `persistence/alignment_store.py`;`text_alignment.py` 只保留路由判断、回退与展示数据装配,没有直接 `.execute`,`alignment_generation.py` 也没有。原模块 953 行,行数守卫收紧到 960;SQL 散落文件 20→19。C1.4 完成,下一步 C1.5。

2026-09-25(C1.5a 进展):FTS5/trigram 对象检查、安装和增量升级 SQL 进入 `persistence/fts_index.py`;`database.py` 保留 `ensure_database_search_index` 兼容入口,由它把既有整库优化流程作为回调传入,避免 persistence 上行依赖。数据库构建/来源替换仍在原模块,SQL 散落文件暂为 19,C1.5 尚未完成。

2026-09-25(C1.5b 进展):整库重建时的 schema/元数据/来源写入及其后的卷、作品、段落、页码锚点写入进入 `persistence/index_build.py`;`database.py` 仍按原顺序读取备份快照、还原作品组、填库、还原对齐配方与 Zotero 关联、发布临时库。原模块 1146→953 行,行数守卫收紧到 960;优化和来源替换 SQL 尚待迁移,C1.5 尚未完成。

2026-09-25(C1.5c 进展):单来源替换、批量删除及旧版页码锚点清理的 SQL/事务进入 `persistence/source_replace.py`;`database.py` 保留入参处理、UTF-8 清理及先备份再写入的顺序。原模块降至约 617 行,SQL 散落文件仍为 19,优化/目录读取 SQL 尚待迁移,C1.5 尚未完成。

2026-09-25(C1.5d 完成):目录元数据读取 SQL 进入 `persistence/index_build.py`;旧库稀疏化、FTS 完整性检查与临时库替换前的 SQL 进入 `persistence/storage_optimization.py`,文件替换与备份轮转仍由 `database.py` 回调。`fts_index.py` 行数守卫不放宽。`database.py` 不再直接执行 SQL,行数 466,SQL 散落文件 19→18;后端 C1 完成,下一步 C2。

**后端 C2 组合根拆分**
- `build_application_runtime` 按域拆 `library_assembly.py` / `import_assembly.py` / `alignment_assembly.py` / `settings_assembly.py`。
- `ApplicationRuntime` 字段改 `Protocol` 或具体类型。
- 验收:`web_runtime.py` 直接内部依赖 ≤ 20;行数上限同步收紧。

2026-09-26(C2a 进展):导入、索引、备份、来源删除与题录更新的实例装配按原顺序进入 `import_assembly.py`;`web_runtime.py` 保留调用与跨域接线,直接内部依赖 64→28、行数 723→约 486。依赖/行数棘轮逐批收紧,旧测试替身改指向真实定义模块。C2 尚未完成。

**后端 C3 后台任务统一**
- 先按 §0.1 盘点实际线程生命周期,再确定以下抽象是否适合;不得先写通用管理器再强迁全部模块。
- `tasks/` 下提供 `BackgroundTasks`:具名注册、取消、关闭时 join,接入 `close_runtime` / `DurableOperationGate`。
- 迁移约 14 个模块的裸 `threading.Thread`;沿途审 `except Exception`:至少 `logging.exception`,不静默吞。

**前端 C4 事件委托**
- 引入 `data-action="xxx"` + 根节点委托分发,替换 `index.html` 的 205 个内联事件(§3.3 复测基线)与动态 HTML 里的 `onclick=` 字符串;优先修 `onclick="fn('` + `esc(...)` + `')"` 形式。
- 随之收缩全局符号预算(内联事件不再需要全局函数)。
- 守卫:`index.html` 内联事件数、各文件 `innerHTML` 数,棘轮只降不升。

**前端 C5 DOM 构造与拆文件**
- 逐文件把 `innerHTML` 字符串拼接换成已有 DOM 辅助函数(参照 `reader.js`);清零的文件纳入"禁 `innerHTML`"守卫。
- 拆大文件:`60-settings.js`(外观 / 数据位置 / 模型 / 更新)、`35-works.js`、`80-import.js`、`70-vision.js`。新文件沿用编号前缀与 IIFE 模式,更新装配顺序、指纹与预算。

### 阶段 D — `reader.js` 拆分(阶段 B1 之后;可与阶段 C 并行)

**现状(2026-09-25 实测)**

- 单个 IIFE,约 167 个函数共用一个可变 `state` 对象(第 70 行起,约 80 个字段),内部任何函数都能读写任意字段。
- 公共面已经很小、很好:`global.MEFinderReader = Object.freeze({open, openForSearchResult, close, goTo, restore, copyCitation, configure, destroy, isOpen, getState, codePointToUtf16Index, alignmentJobs})`。**拆分期间此公共面必须逐字不变。**
- 已无 `innerHTML`,DOM 走 `createButton` / `createIcon` / `createDropdown` 等辅助函数;`ensureDom`(约 400 行)一次性搭整棵阅读器 DOM。
- 自带 `readJSON` / `postJSON`,端点集中在 `DEFAULTS`(约 20 个)。
- 对齐任务轮询(`watchAlignmentJob` / `subscribeAlignmentJob` / `pollAlignmentJob`)是**全应用唯一的任务监听**,作品页通过 `MEFinderReader.alignmentJobs` 订阅——它其实不属于阅读器。
- 装配:`web_assets.py` 把 `reader.js` 整文件替换进 `//__READER_JS__` 占位(主窗口与独立阅读窗口两处)。
- 测试:`test_structured_reader_frontend.py`、`test_reader_comparison_state.py` 等直接读 `reader.js` 源码(字符串断言 + node 执行)。

**按职责的自然切分(函数行号区间,供定位)**

| 模块 | 内容 | 约在 |
|---|---|---|
| `00-core` | `DEFAULTS`、`state`、码点/UTF-16 换算、锚点解析、`notify`/`setAlert` | 1–290 |
| `05-dom` | `createButton`/`createIcon`/`createDropdown`、`ensureDom` | 290–770 |
| `10-citation` | 引用区间、选区捕获、剪贴板、引用预取 | 766–850、2655–2930 |
| `20-work-context` | 作品/版本/可对照目标、目录、跳章、工具栏与菜单 | 848–1410、1128–1200 |
| `30-alignment-jobs` | 任务启动/取消/轮询/订阅 | 1606–1775 |
| `40-comparison` | 对照阅读:版本选择、链接窗口、高亮、跟随滚动 | 1412–1606、1861–2600 |
| `45-review` | 对齐待复核弹层、纠正保存/暂缓 | 2047–2190 |
| `50-deeplink` | 深链解析/写回/历史,阅读位置保存 | 1775–1860、2925–3130 |
| `60-window` | 虚拟窗口:observer、`shiftWindow`、`loadRange`/`loadWindow`、裁剪 | 3130–3300、3681–3880 |
| `70-render` | 高亮合并、`renderItem`/`renderWindow` | 3293–3680、3878–3933 |
| `90-lifecycle` | `configure`/`openReader`/`goTo`/`close`/`destroy`、公共面、`popstate` | 3933–4301 |

**步骤**

- **D0 守卫先行**:新增测试钉住 `MEFinderReader` 公共面的键集合与 `alignmentJobs` 子键;新增测试辅助函数 `reader_js_source()`,返回"装配后的阅读器 JS"(即 `web_assets` 实际拼入的内容),把所有直接 `read_text("reader.js")` 的测试改用它。这一步不动 `reader.js` 本身。
- **D1 请求改走 `07-api.js`**:`readJSON`/`postJSON` 换成 B1 的统一客户端;独立阅读窗口(`reader-window.html`)同样装配 `07-api.js`。端点仍留在 `DEFAULTS`。
- **D2 抽出对齐任务监听**:`30-alignment-jobs` 迁到 `static/js/` 下独立模块(如 `36-alignment-jobs.js`),暴露 `MEFinderAlignmentJobs`;`MEFinderReader.alignmentJobs` 暂保留为转发别名,作品页改订阅新入口后再评估删除。保留"全应用只有一处轮询"的现有测试断言。
- **D3 物理拆文件**:`static/reader/NN-*.js`,每个文件 `(function (R) { ... }(global.__MEFinderReaderInternal))` 往一个私有命名空间注册;`90-lifecycle` 最后冻结公共面并 `delete` 私有命名空间。`web_assets.py` 按文件名顺序拼接后再替换 `//__READER_JS__`,装配结果仍是一段脚本,**不引构建工具、不引 ES module**。一次只搬一个模块,每搬一个跑全量测试。
- **D4 收拢 `state`**:在拆好的模块边界上,把 `state` 按域分组(`state.window`、`state.comparison`、`state.citation`、`state.deepLink`…),跨域写入改为调用所属模块的函数。这是唯一改代码形状(而不只是搬位置)的一步,放最后,且只在 D3 全绿后开始。
- 守卫:沿用"阅读器禁 `innerHTML`",扩展到 `static/reader/` 全目录;为每个 reader 文件设行数上限(建议 ≤ 800)与全局符号 = 0(除 `90-lifecycle` 的 `MEFinderReader`)。

**验收**

- `MEFinderReader` 公共面与行为不变;对照阅读、深链恢复、引用复制、独立阅读窗口、对齐任务订阅的现有测试不改断言通过。
- `reader.js` 单文件消失或只剩兼容壳;单文件 ≤ 800 行。
- 手测清单(浏览器 + 桌面壳各一次):打开搜索结果 → 滚动加载前后窗口 → 选区复制引用 → 打开对照 → 点击链接高亮 → 复核弹层 → 深链刷新恢复 → 新窗口打开 → 回主窗口。

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| 开外键后历史数据写入失败 | A1 先出 `foreign_key_check` 报告;有违例先迁移修复 |
| 搬迁 SQL 时改变事务边界(`BEGIN IMMEDIATE` 位置) | 仓储函数接收连接而非自开连接;事务由调用方持有,保持原边界 |
| HTTP 重构破坏 Windows 上传断连修复 / Range | 这些路径的测试断言不许改;B2 完成后做一次 Windows 打包冒烟 |
| 前端改动频繁触发指纹/预算基线 | 每步末尾统一更新一次,提交正文注明 |
| 与功能迭代冲突 | 阶段 A、B 独占窗口;阶段 C 按模块小批,避开正在开发的模块 |
| 拆 `reader.js` 后测试仍读旧单文件、或断言落到错误片段 | D0 先把测试切到"装配后源码"辅助函数,再动代码 |
| 阅读器模块间拼接顺序出错导致运行时未定义 | 私有命名空间注册 + `90-lifecycle` 启动时断言所需模块已注册;D3 每搬一个模块跑全量测试 + 桌面壳冒烟 |
| GBK locale 下测试读 golden 失败 | Windows 本机跑门禁前 `$env:PYTHONUTF8="1"`(AGENTS.md §3.6) |

---

## 6. 基线复测命令(仓库根执行,Git Bash / macOS)

```bash
# persistence 外的 sqlite3.connect 调用点
grep -rn --include='*.py' --exclude-dir=__pycache__ "sqlite3.connect" src/me_finder | grep -v "src/me_finder/persistence/"

# persistence 外含 .execute( 的文件
grep -rl --include='*.py' --exclude-dir=__pycache__ "\.execute(" src/me_finder | grep -v "/persistence/" | wc -l

# web_http.py 内按路径分支
grep -c "parsed.path" src/me_finder/web_http.py

# 自起线程
grep -rn --include='*.py' --exclude-dir=__pycache__ -E "threading.Thread\(|ThreadPoolExecutor" src/me_finder | cut -d: -f1 | sort | uniq -c

# 前端 fetch / innerHTML / 内联事件
grep -c "fetch(" src/me_finder/static/js/*.js | grep -v ":0$"
grep -c "innerHTML" src/me_finder/static/js/*.js | grep -v ":0$"
grep -oE "on(click|change|input)=" src/me_finder/templates/index.html | wc -l
```

全量测试与 lint 命令以 AGENTS.md §3.6 为准。

# Zotero 来源同步（0.5.6）

2026-09-24：首版实现。用户在 Zotero 里按分类整理文献，MEFinder 从选定分类拉取 PDF / EPUB，走自己的导入与解析管线入库，使 MCP 核对能带页码锚点与字符区间。本版不做 Zotero 端插件。

## 已拍板的决策（用户给定，不再讨论）

1. 索引只用 MEFinder 的；Zotero 只提供文件、题录和分类结构，不读取 Zotero 全文索引。
2. 只读 Zotero 自带的本机 Local API（`http://127.0.0.1:23119/api/`），不走 Zotero Web API，不读写 `zotero.sqlite`。
3. 分类以 Zotero 为准：MEFinder 只存用户勾选的分类 key；勾父分类等于勾全部子分类（含之后新建的子分类）；同一条目在多个所选分类中只导入一次。
4. 解析方式跟随现有偏好 `pdf_parse_mode`；EPUB / Word 走文本通道。
5. 增删跟随 Zotero：新增 → 导入解析；题录改动 → 覆盖题录不重解析；PDF 附件被替换 → 重新解析；删除 / 移出全部所选分类 / 取消勾选 → 移除（走 `DocumentDeletionCoordinator.remove_many`，对齐记录一并删除），不弹确认，设置页提前显示「同步时移除 N 篇」。
6. 同步时机：仅手动 / 启动时（默认）/ 启动时及每 30 分钟。
7. （2026-09-24 用户补充）有茉莉花（Jasminum）插件的以茉莉花为主，直接导入它抓取的期刊信息。

## 事实（官方文档核实，2026-09-24）

来源：https://www.zotero.org/support/dev/web_api/v3/local_api

- 未开启本机接口时请求返回 `403 Forbidden`；读请求无需认证。
- 结果默认不分页，但 `limit` / `start` 仍有效，并带 `Link` 头。客户端一律显式分页，并用 `Total-Results` 校验完整性。
- `items/<key>/file/view/url` 以纯文本返回附件的 `file://` URL。
- **版本号**：Zotero 10+ 返回 `Zotero-Server-ID`，对象版本为本机版本；更早版本（Zotero 7）返回的是同步版本，未同步对象为 0，且本地修改不改变版本。文档要求客户端丢弃旧版本、按 Server ID 分区。
- 茉莉花插件源码（github.com/l0o0/jasminum，`src/modules/services/index.ts`、`tools.ts`）：用知网等翻译器把题录写进 Zotero 标准字段（题名、拆分后的中文作者、刊名、卷期页、日期、ISSN、DOI），在 `extra` 写 `CNKICite: N`，`libraryCatalog` 为数据源名。

## 设计

- `zotero_local_api.py`：只读客户端，只接受回环地址、不走系统代理；请求头 `Zotero-API-Version: 3`、`zotero-allowed-request: 1`，UA 不以 `Mozilla/` 开头。任何失败抛 `ZoteroUnavailable` / `ZoteroIncomplete`，绝不返回半截列表。
- `zotero_sync.py`：快照读取 → 纯函数差异（`plan_sync`）→ 执行。执行只驱动既有管线：`DocumentImportCoordinator.import_local`（同一导入队列、断点续传日志、MinerU 额度与失败重试）、`DocumentDeletionCoordinator.remove_many`、书目元数据协调器。
- 增量读取：Zotero 10+ 且 Server ID 未变时，用 `format=versions` 列出成员与附件版本，只对新增 / 变化的 key 拉完整 JSON；Zotero 7 的版本不可信，整读（本机接口无网络成本）。成员列表两种模式下都是完整列表——这是能安全移除的前提。
- 数据库 v8（`persistence/schema_installers.install_zotero_sync_schema` + `migrations.py`）：`zotero_items`（条目版本、题录指纹、分类 key、已写入的题录指纹）、`zotero_attachments`（附件 key ↔ `source_file_id`、父条目 key、链接模式、文件签名与 SHA-256、来源 `origin`、状态）、`zotero_sync_state`（Server ID、上次同步分类、附件版本索引、上次结果）。不对 `source_files` 建外键：文献在 MEFinder 里丢失时行要留下，下次同步才能发现并补回。整库重建（`build_database`）时与作品组一样快照并还原。
- 每个 PDF / EPUB 附件是一篇文献，记录父条目 key；非 PDF/EPUB 附件（网页快照等）与 `linked_url` 忽略。存储文件（`imported_file` / `imported_url`）与链接文件（`linked_file`）都支持。
- 同内容去重：附件文件 SHA-256 与文库已有文献相同时直接关联，不再解析（`origin=linked_existing`）。
- 题录：Zotero 有值的字段覆盖 MEFinder 题录，Zotero 留空的字段保留原值（不清空）；来源标「Zotero 元数据」，识别到茉莉花痕迹（`extra` 含 `CNKICite:`，或 `libraryCatalog` 为知网 / 万方等）时标「Zotero 元数据（茉莉花）」。`extra` 本身不进指纹，引用次数变化不会触发题录重写。只有 Zotero 题录变化（指纹变）时才覆盖；用户之后在 MEFinder 手动改的题录保留到 Zotero 下一次改动；整库重建把题录退回自动识别时自动补写。
- 替换附件：新文件先按正常导入解析，解析完成并关联后才移除旧文献，替换期间旧版本仍可检索。
- 解析完成后的关联在同步结束后由状态轮询与每分钟的调度 tick 完成（`resolve_pending`），不需要再跑一次同步。

## 防误删策略（测试：`tests/test_zotero_sync.py::RemovalGuardTests`）

只有一次**完整、成功**的列表对比才能产生移除。以下情况一律暂停同步、不移除任何文献，界面显示「暂停同步：…」：

- Zotero 未运行（连接被拒）、本机接口未开（403）、任何请求失败（非 200）、Zotero 数据库在同步中途更换（412）；
- 分页不完整：`Total-Results` 与实际读到的条数不符、读取中总数变化；
- 所选分类在 Zotero 的分类列表里找不到（包括整个分类列表为空）；
- 列表与 Zotero 自身计数矛盾：分类的 `meta.numItems > 0` 但条目列表为空（异常空列表）。分类在 Zotero 里真的被清空时 `numItems` 为 0，移除照常进行。

另外两条保守规则：

- **附件文件缺失 ≠ 删除**：标「附件不可用」，已关联的文献保留，下次同步重试。
- **同步前已在文库里的文献只解除关联、不删除**（`removal_targets`）：由同步导入的文献才会被移除；多个附件指向同一文献时，最后一个关联离开才移除。解析中的导入不在本轮移除，完成后下一轮再处理。

推断（未实测）：Zotero 7 本机接口对 `collections/<key>/items/top` 是否包含回收站条目未在文档中说明；实现对 `data.deleted` 为真的条目按已删除处理，两种行为都安全。

## 真机验证（2026-09-25，用户本机 Zotero，结果写入开发库副本，未碰真实库）

- Zotero 9.0.6：本机接口未开启时返回 `403 Local API is not enabled`，页面显示「接口未开启」并给出开启步骤；开启后无 `Zotero-Server-ID`，走整读。「耶吉」分类 50 条、50 个 PDF（49 个 `imported_url`、1 个 `imported_file`），文件路径全部可读；50 条全部识别为茉莉花来源。
- 同步 50 篇：读取、哈希、入队约 6 秒；49 篇在导入队列里本机解析完成并关联，题录来源全部为 `zotero_jasminum`，期刊的刊名、卷期、页码来自茉莉花；Zotero 里没有年份的条目保留了 MEFinder 自动识别的年份。1 篇扫描版按「自动选择」送 MinerU，因副本环境未配账号失败，进入可续传队列，与手动导入一致。检索「内在批判」命中 34 条，带页码与字符区间。
- 发现并修复：手动再同步会把仍在导入队列里的失败任务重新导入一次（重复消耗 MinerU 额度）。现在失败任务仍在队列时交给导入页续传，只有导入请求本身失败或任务已清除时手动同步才重试，自动同步从不重试（`test_failed_parse_with_resumable_job_is_not_reimported`）。
- Zotero 10.0.4：返回 `Zotero-Server-ID`，升级后对象版本从 0 起算。从 Zotero 9 的记录切换过来时按「换库」整读一次、不重导；之后无变化的同步只发 4 个请求（分类列表、分类条目版本、附件版本），约 54 KB、0.03 秒。连接状态显示真实版本「已连接 Zotero 10.0.4」（读 `X-Zotero-Version`）。

- Zotero 10.0.4 真机改动（用户操作）：删除 1 条、改 1 条年份。增量读取只拉改动条目（`itemKey=9YRNYZXD`）和连带变化的 1 个附件，其余 48 条未重读；同步结果「题录更新 1，移除 1」：删除条目的文献从 MEFinder 移除，年份 2023 → 2022 写入题录且未重新解析，未触发任何导入。预览「同步时移除 N 篇」只反映勾选变化，Zotero 端删除要同步读到后才出现，这是设计边界。

## 未做 / 后续

- 群组文库不在本版范围，只做「我的文库」（`users/0`）。
- MCP `list_documents` 带 Zotero item key 与 `zotero://select/library/items/<key>`：v0.5.1 MCP 契约的 `document` 定义为 `additionalProperties: false`，加字段需要新版本 MCP 契约与 sidecar 打包联动，本版未做。
- 真机未覆盖：更换 PDF 附件、链接到外部的文件、移出分类（与删除走同一移除路径，已由测试覆盖）。
- 在 MEFinder 里手动删除一篇 Zotero 同步来的文献，下次同步会按「以 Zotero 为准」重新导入；要排除应在 Zotero 里移出所选分类。

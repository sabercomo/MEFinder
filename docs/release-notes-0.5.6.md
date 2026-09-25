# 文献原句定位器 v0.5.6（迭代中）

> 2026-09-25：**迭代中，未发布**。主题是「Zotero 来源同步」：可在「设置 → 来源 → Zotero」勾选 Zotero 分类，把其中的 PDF / EPUB 交给 MEFinder 自己的管线解析入库。本机门禁通过（全量 unittest 2540 例，18 例环境跳过、其余通过；`ruff check src tests` 零告警）；已在用户本机 Zotero 9.0.6 与 10.0.4 上验证读取、增量、删除 / 改题录，并在真实库上跑过 50 篇「耶吉」分类的同步（该库已被写入）。真机暴露并修掉两个缺陷：一次同步打满有界导入队列（`docs/issues/zotero-source-sync.md`）、MCP 侧车停在被遗弃的旧数据根（`docs/issues/existing-library-location.md`）。未打包、未发版。

## 更新内容

### Zotero 来源同步

- 设置目录新增「来源」组与「Zotero」页：连接状态（已连接 / 接口未开启 / 未检测到 Zotero，并给出开启步骤）、可勾选可折叠的分类树（勾父分类等于勾全部子分类，半选显示）、解析方式跟随行、「以 Zotero 为准」四条规则、自动同步（仅手动 / 启动 MEFinder 时 / 启动时及每 30 分钟）、立即同步与逐条明细。
- 只读 Zotero 7 自带的本机接口（127.0.0.1:23119），不连 Zotero 网站、不读写 `zotero.sqlite`、不读 Zotero 全文索引，也不需要装 Zotero 插件。
- 增删与题录以 Zotero 为准：分类里新增条目 → 导入并解析；题录有改动 → 覆盖题录，不重新解析；更换 PDF 附件 → 重新解析，新版本就绪后才移除旧版本；条目删除、移出所选分类或取消勾选 → 从 MEFinder 移除，对齐记录一并删除。取消勾选后分类树与底部会提前显示「同步时移除 N 篇」。
- 茉莉花插件抓取的知网 / 万方期刊信息（刊名、卷期、页码、ISSN、DOI、拆分后的中文作者）直接作为题录导入，来源标「Zotero 元数据（茉莉花）」；Zotero 留空的字段不会把 MEFinder 已有的值清掉。
- 解析方式跟随「导入 → PDF 解析方式」；导入、断点续传、MinerU 额度与失败重试与手动导入是同一条管线。
- 每个 PDF / EPUB 附件单独成为一篇文献并记录所属条目；与文库里已有文件内容相同的直接关联，不重复解析；Zotero 链接到外部的文件同样支持。
- 防误删：Zotero 没开、接口没开、请求失败、分页没读全、所选分类找不到或列表与 Zotero 计数矛盾时，一律暂停同步、不移除任何文献；附件文件缺失标「附件不可用」，不当作删除；同步前就在文库里的文献只解除关联、不删除。
- 连接状态显示 Zotero 真实版本；解析失败且任务仍可续传时同步不重复导入，交给导入页续传；从未进入队列的任务（没有解析进度可保留）由下一次同步直接补上。
- 提交量按导入队列剩余容量分批：一次同步几十篇不会打满队列，被挡下的条目标「排队已满，稍后自动重试」并计入「待同步 N 篇」，下一次同步继续提交。
- 本版只支持「我的文库」，群组文库不在范围内。

### 数据与接口

- 数据库升级到 v8：新增 `zotero_items`、`zotero_attachments`、`zotero_sync_state` 三张关联表（增量迁移，整库重建时保留）。旧版本无法打开升级后的库。
- 偏好新增 `zotero_sync_enabled`（默认关闭）、`zotero_sync_collections`、`zotero_sync_frequency`（默认启动时）。
- 新接口 `GET /api/zotero/overview`、`GET /api/zotero/status`、`POST /api/zotero/preview`、`POST /api/zotero/sync`；契约见 `docs/contracts/v0.5.6-http-api.json`。
- 设计决策与防误删策略见 `docs/issues/zotero-source-sync.md`。

### MCP 侧车数据根

- 修复侧车读到被遗弃的旧库：安装包内的 `data_root.txt` 指向的目录本身可能已再次改址，解析时对它再跟一跳指针，与稳定位置指针同一语义。此前在环境缺少 `LOCALAPPDATA` 时（Qoder 以空 `env` 启动侧车即如此），侧车会停在旧快照库上，桌面端却正常——表现为「新导入的文献没进 MCP 检索索引」，实际是整批文献对 MCP 不可见。见 `docs/issues/existing-library-location.md`。

## 尚未完成

- 真机未覆盖：更换 PDF 附件、链接到外部的文件（题录改动与删除已在 Zotero 10.0.4 上验证）。
- MCP `list_documents` 带 Zotero item key 与 `zotero://select/...` 链接：需要新版本 MCP 契约，本版未做。
- Windows / macOS 打包与冻结验收（本版不打包）。

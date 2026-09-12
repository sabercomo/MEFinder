# 导出写库分离与一致快照

2026-09-11:导出过程中的标题(文档标题级谱)补全原本内嵌在导出里直接写库,且导出各步骤分多次连接读取;与题录修改、删除并发时存在覆盖新数据与输出混合版本两个危害。本轮分离职责并钉死行为。

## 事实(重构前危害,复现测试先行)

- `ensure_document_headings` 在导出内执行:连接 A 读入 source+pages payload → 事务外计算补全 → 连接 B `BEGIN IMMEDIATE` **盲写整份旧 payload**。复现测试 `test_enrichment_write_keeps_concurrent_metadata`:补全计算期间并发保存题录,写回后标题被还原为旧值(旧行为)。
- 三条导出(zip/Markdown/EPUB)的 source/volume/counts 与 pages/paragraphs 分属不同连接、不同隐式事务,Python sqlite3 的 SELECT 不会自动开事务,每次读取都是独立快照。复现测试:source 读取后、pages 读取前并发提交题录修改+页面重写,导出产物为新页面+旧标题的**混合版本**(旧行为)。
- 同一窗口内并发删除:Markdown 导出仍报告成功,产物只有前置元数据、正文为空(旧行为)。

## 方案

- **标题补全成为独立应用操作**:迁移至 `application/document_heading_enrichment.py`(`DocumentHeadingEnrichment.enrich`),以现有写入协调包裹——`durable_operations.operation()`(关机等待)+ `index_runtime.mutation()`(与题录保存、重建、删除互斥)。写事务改为 `BEGIN IMMEDIATE` 内**新鲜读改写**:文档在计算期间被任何写者改动或删除,则放弃本次写入返回 `deferred`/`unavailable`,后续导出/操作自动重试;绝不覆盖新数据。
- **导出纯读一致快照**:三个导出函数移除全部写库;单连接显式 `BEGIN DEFERRED` 读事务覆盖 source/volume/counts/pages(含 zip 流式 `pages.ndjson`)/paragraphs。库为 rollback-journal 模式,读事务把并发写者的提交挡到读取结束之后——产物必然是单一版本;并发删除要么整本可见要么干净报错,不再产生空壳。
- **接线上移**:控制器(`ArchiveTransferController`)新增可选 `prepare_document_export`,在调用只读导出前作为独立步骤执行补全;失败只记日志不阻塞导出。装配抽为 `build_archive_transfer_controller` 工厂(web_runtime 行数上限随之回到阈值内)。

## 验证

- `tests/test_export_snapshot_consistency.py`(8 项):导出零写库、并发题录保存不被还原、Markdown/zip 并发写期间单版本不变体、并发删除不产出空壳、补全操作进入两个协调端口、控制器预备失败不阻塞导出。全部先复现失败再修绿。
- `tests/test_document_heading_lazy.py` 原有幂等/降级行为不变(仅改 import 与 patch 目标)。
- 全量 2122 项 unittest(22 skip)、Ruff F 零告警。
- 架构门禁同步:`search.py` 上限 1850→350(任务 2 拆分后下调),新管线模块与补全模块按当前行数封顶。

## 边界与未验证

- rollback-journal 模式下导出读事务会短暂阻塞并发写者的提交(与旧实现页读取窗口相当);未改 WAL——用户库在 OneDrive 同步目录,WAL 附带文件不安全。
- 导出包含原 PDF(`include_source_pdf`)时源文件读取不受快照保护;并发删除目录文件的窗口返回清晰错误,未做静默降级。
- HTTP 响应键与 `/api/document/export-*` 契约不变;导出产物格式不变。

## 2026-09-12 复核与修正：只在发布标题时进入写入协调

**事实**：原 `DocumentHeadingEnrichment.enrich` 在判断非 PDF／已有标题前就进入全局 mutation；对齐持有该锁计算时，不需标题补全的 EPUB 导出也会等待。标题计算也占用同一锁。

现在先读取并计算，只有确需写回时才进入 mutation 和 durable operation，再以 `BEGIN IMMEDIATE` 重读并比较源/页数据摘要；并发修改或删除后不回写旧结果。非 PDF、已有标题等无需写入路径不接触该锁。纯导出仍通过原有单一只读事务获取一致快照。

回归覆盖已完成 PDF 不抢锁、EPUB 经真实导出控制器完成且不抢锁、计算阶段尚未进入写入协调、写入阶段必须协调、并发题录修改不能被覆盖。另修正旧并发测试时序：删除明确发生于快照已开始读取之后；测试中的新题录与新页面以一次事务发布，避免将两个合法独立提交误判为快照混合。

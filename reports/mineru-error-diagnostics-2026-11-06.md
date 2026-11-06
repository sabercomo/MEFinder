# MinerU 错误提示与账号删除核验

2026-11-06：已复现并修复错误原因被覆盖、永久失败被误判可续传、终态旧任务阻止删除账号三处问题；最新主线全量门禁与本机补丁构建通过，补丁已放回原程序路径，下次启动生效。

## 已核实的运行事实

- 只读核对真实解析账本与导入日志：`me2-38.pdf` 共 599 页，第 201–400 页使用账号 2 提交时返回 HTTP 401 / A0211，底层中文原因是“Token 无效或已过期”；界面最终只显示 `one or more slices failed permanently`。
- 费尔巴哈选集共 1537 页，云端已完成前 1000 页，第 1001–1200 页返回 `parsing failed, please try again later`。该返回未说明更具体原因，不能据此断言文件损坏或服务整体不可用。
- 账号 2 已禁用且标为 `unauthorized`、当前占用为 0；阻止删除的是另外两条 `permanent_failure` 整书下遗留的 `waiting` 分片，删除查询未检查整书状态。
- 用户明确要求删除失效账号。17:09 Asia/Shanghai 用修复后的账号服务删除了账号 2 及其本机 Token；账号 3 的私有配置逐项比较保持一致，运行中程序的账号 API 已只返回账号 3。操作前备份解析账本至本机 `.codex-tmp/mineru-errors-20261106/parser_jobs-before-delete.sqlite3`；备份不包含 Token。
- 本地 MinerU 健康接口返回正常；核验时费尔巴哈任务为已完成 2 段、运行 1 段、排队 5 段、失败 0 段。本轮没有中止或重新提交该本地任务。

## 修复与验证方法

- 分片引擎用失败分片的 PDF 物理页段与原始 `last_error` 生成整书错误摘要；上游没有提供原因时如实显示“解析服务未提供失败原因”。
- 云端整书 `permanent_failure` 进入既有解析失败分支，并显式关闭 `can_resume`，避免显示“暂时中断 / 复用断点”的错误承诺。普通超时中断仍沿用原有逻辑。
- 删除账号只把活跃整书下的远端分片视为占用；整书已失败、取消、验证或发布的历史分片不再挡住删除。仍保留当前占用与真实活跃任务检查，不改历史分片与已完成结果。
- `test_expired_token_reaches_import_message_without_resume_promise` 从模拟 HTTP 401 穿过真实账号服务、云端 provider、分片引擎和导入执行器，断言最终消息含 401、Token 原因、设置入口与页段，不含断点承诺、Token 值或 trace ID。
- `test_permanent_failure_summary_preserves_slice_ranges_and_reasons` 与 `test_upstream_failure_summary_uses_poll_reason_or_reports_missing_reason` 覆盖提交失败、多段失败、轮询失败和上游原因缺失。
- `test_delete_account_only_blocks_remote_slices_in_active_documents` 覆盖活跃整书仍拒删且保留 Token、失败/取消整书允许删除且保留历史记录。既有分片故障注入断言同步为具体页段及注入原因。

## 代码与本机交付范围

远端主线已将解析器与 PDF 模块拆入子包，本轮在以 `d761d8c` 为基线的隔离工作树适配修复。本机原程序仍为 0.5.9：先逐个核对 219 个冻结应用模块均与 `75fbeba` 一致，再仅向该基线应用相同四处逻辑修复，运行官方 `build_windows_dist.cmd` 构建。本轮不发布新版本，不替换 GitHub 资产。

## 门禁与交付结论

- 最新主线：Python 3.12.14，`unittest discover -t . -s tests` 共 2874 项，52 项环境跳过，298.063 秒，最终 `OK`；`ruff check src tests` 通过，前端守卫随全量通过，未改静态资源或接口字段。
- 0.5.9 官方补丁构建门禁：2777 项，48 项环境跳过，280.064 秒，最终 `OK`；官方桌面与侧车构建、JavaScript 语法、空库 FTS5 与 MCP 侧车冒烟均通过。
- 初次旧工作区全量发现故障注入测试仍断言被替换的英文通用错误，已改为断言具体页段与原始原因；另有既有 OCR 安装取消时序用例一次失败，单独重跑通过，随后官方补丁全量通过。未修改 OCR 安装器。
- 冻结代码：比较原程序与补丁的 219 个应用模块，仅四个修复模块发生变化；四个模块逐一与补丁源码编译结果一致。
- 原路径交付：`D:/ME_Finder/dist/MEFinder/文献原句定位器.exe` 已替换为补丁，SHA-256 为 `8a2e2e62de583cbeb131daab1f0aa2b51b0d226dfc58e85c95e43b47cf37e895`，同时同步四份随包 Python 源码。原 EXE 和四份源码备份在 `.codex-tmp/mineru-errors-20261106/original-exe-backup/`。
- 新旧 Python 均为 3.12.14，但部分二进制依赖哈希不同，交付时保留原运行时；新 EXE 配合原 `_internal` 已通过真实冻结进程的无头 worker 握手。未替换依赖、侧车、静态资源、程序内空库与配置；四份数据/配置指针文件哈希保持。
- 部署后原账号 API 仅返回账号 3；本地 MinerU 为健康、完成 5 段、运行 1 段、排队 2 段、失败 0 段。未停止当前桌面或本地解析进程，当前窗口继续使用已加载的旧代码，新提示在用户正常重启后生效；尚未进行重启后的 GUI 人工验收。
- 构建及核验 JSON、完整门禁日志在本机 `.codex-tmp/mineru-errors-20261106/`。仓库根工作区仍保留用户已有改动；最新主线修复位于本次隔离工作树，避免把远端目录重构混入本机旧工作区。

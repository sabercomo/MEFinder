# C3 后台任务生命周期盘点

2026-09-26（事实）：以 `src/me_finder/` 中 `threading.Thread(`、`threading.Timer(`、`ThreadPoolExecutor(` 和 `ProcessPoolExecutor(` 的构造调用为口径，C3 前有 12 个线程创建点，分布于 11 个模块；后三类构造调用为 0。原计划的“约 14 个模块”与此口径不符。`import_queue.py` 的一个创建点每次建立两个工作线程，因此“创建点”不等于“运行时线程数”。

| 创建者 | C3 前的退出归属（事实） | C3 处理 |
|---|---|---|
| `translation_works.py` | 启动时只读预热，会打开 SQLite；调用方丢弃线程句柄 | 交给 `BackgroundTasks`，关闭时请求取消并等待只读连接释放 |
| `zotero_sync.py`（2 处） | 手动同步线程未记录；调度线程只设停止事件，不等待；同步会访问 SQLite | 两种线程交给服务内 `BackgroundTasks`；调度线程收到停止信号，进行中的手动同步走完当前操作后再关闭索引，不中途打断同步写入 |
| `managed_mineru.py`、`local_ocr_installer.py` | 安装线程已有取消事件和子进程管理；MinerU 的 `close` 不等待，本地 OCR 无 `close` | 保留各自安装流程，补取消、子进程停止和限时 `join` |
| `component_catalog.py` | 启动时可能发出最长 20 秒的网络请求，完成后刷新安装器；无退出等待 | 禁止关闭后再启动检查，限时等待请求与刷新回调结束 |
| `managed_embedding_models.py`、`managed_alignment_runtime.py` | 组件自己的 `begin_shutdown`/`close` 已取消并等待线程和子进程 | 保留原所有者 |
| `import_queue.py` | 队列已有停止事件和双工作线程 `join` | 保留原所有者 |
| `text_alignment_controller.py` | 对齐任务在 `DurableOperationGate` 内运行，关闭先取消 embedding 并等待 durable 操作 | 保留原所有者 |
| `desktop_backend.py` | HTTP 服务线程由桌面后端的 `stop` 关闭服务并等待 | 保留原所有者 |
| `onefile_cleanup.py` | 单文件解包清理发生在应用运行时建立前，不使用文献库 | 保留进程级任务 |

2026-09-26（事实）：C3 后直接创建线程的模块为上述 9 个自有生命周期模块，另有 `tasks/background_tasks.py` 一个统一创建点；合计 10 个创建点、10 个模块。预热与 Zotero 的异常均记堆栈；本轮未改变其他模块的异常策略。`close_runtime` 的编排移至 `tasks/runtime_lifecycle.py`：仍先等 `DurableOperationGate` 和导入队列，随后等待预热、Zotero、目录检查及安装器，最后关闭索引。超时返回 `False`，不把仍被后台任务使用的索引报告为已关闭。

2026-09-26（推断）：组件目录的网络请求无法在阻塞的 `urlopen` 内立即取消；当请求超过关闭预算时，本次关闭会返回 `False`，之后可重试。它不访问文献库，但等待它能避免关闭后才触发安装器清单刷新。这个推断由请求最长 20 秒的代码路径和限时 `join` 语义得出，不代表真实网络环境已测过。

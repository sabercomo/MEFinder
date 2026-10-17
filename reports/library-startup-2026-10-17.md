# 默认启动的文献库摘要请求时序

2026-10-17：默认搜索页现在先取得文献摘要，再预取对齐总览；全量 2773 项、Chrome 12 项及 Ruff / 前端守卫通过，原 `dist/MEFinder/` 已同步，正常重新打开后生效。

## 现场和代码证据

- `dist/MEFinderData/runtime/desktop.log` 今日 15:10:13.535 `loading index`，15:10:13.691 `backend ready`，约 156 ms；文献摘要与渲染的完成时间未记录，不能由此断言用户等待时长。
- 真实库约 6.43 GB，目录为 468 篇；摘要单独运行的 cProfile 测量约 1.07–1.11 秒，含分析器开销。作品组读取约 0.012 秒，复用现有正文范围缓存的轻量总览约 0.004 秒。
- 原默认首页没有在途摘要，`Promise.resolve()` 使启动等待直接结束。`works.load()` 的并行装配先调用 `loadGroupsAndOverview()`，再调用 `ensureCatalog()`。
- 修复前 Chrome 新用例观察事件 `['overview', 'summary']` 并失败；Node 新用例观察到摘要未返回就已执行后台任务并失败。修复后两者均通过，相关专项 40 项通过。

## 排队对照

用 Python 3.12.14、现有书库只读数据和生产 `DocumentQueryService.library_summary` / `translation_works.alignment_overview(include_statistics=False)`，以 `RLock` 重现 `IndexRuntime.catalog` / `run_when_ready` 的锁范围。总览先行的情形在取得锁后才调用摘要；摘要先行的情形先取得摘要再允许后续总览。每项三轮，同一书库与同一机器。

| 情形 | 第 1 轮 | 第 2 轮 | 第 3 轮 |
|---|---:|---:|---:|
| 旧顺序，进程内正文范围缓存未命中 | 4.751 s | 4.083 s | 4.146 s |
| 摘要先行 | 0.720 s | 0.720 s | 0.712 s |
| 旧顺序，已读取现有正文范围缓存 | 0.704 s | 0.730 s | 0.726 s |

没有重解析源文件、重算对齐或写入真实库。测量使用独立只读连接；没有删除或改写用户正文范围缓存文件。脚本和原始记录留在本机 `.codex-tmp/profile_library_startup.py`、`startup-library-measurements.json`、`startup-library-profile.log`，不纳入提交。

限制：这是请求排队对照，不是整个桌面程序端到端计时；使用已有 OS 文件缓存，不承诺其他机器或每次启动都得到上述数值。今日冻结程序已有磁盘正文范围缓存，未证明现场等待全部由缓存未命中造成。

## 实现和验证范围

生产改动只有 `90-init.js` 的等待回退及解释注释：`searchStore.libraryCatalogPromise || fetchLibraryCatalog()`。已有文献库请求继续共用；不再在默认搜索页跳过等待。保持空闲预取、显式进入译本对照立即加载和异常处理语义。

新增 Node 测试覆盖“默认搜索页尚无请求”和“初始文献库页已有请求”两种情况；Chrome 用隔离后端/数据库验证摘要响应完成后才发对齐总览，再进入文献库核对实际列表节点。

## 门禁和本机交付

- 使用现有 `.venv-windows/Scripts/python.exe`（Python 3.12.14）执行 `-m unittest discover -t . -s tests`：2773 项通过，30 项环境跳过，217.184 秒。日志中故障注入产生的异常不计作失败，以上以最终摘要为准。
- 设置 `MEFINDER_BROWSER_TESTS_REQUIRED=1` 后执行整个 `tests.test_browser_regression`：12 项通过，38.346 秒，无环境跳过。专项前端测试 40 项、前端装配守卫 30 项通过。
- 全部跟踪 Python 文件的 Ruff(F)、`node --check src/me_finder/static/js/90-init.js`、`git diff --check` 通过。生产全局符号数未变；HTML 指纹已同步。
- 只同步 `dist/MEFinder/_internal/src/me_finder/static/js/90-init.js`。从原 EXE 的 PYZ 中提取并执行真实 `web_assets` 装配代码，核对完整 HTML 与已测源码逐字一致：1,521,915 字节，SHA-256 `754939c4dc7be347262b38d52ddf09f5caa7ef911d31acc639bd9d67f424766f`。
- 两份 EXE、`data_root.txt`、程序目录 `data/` 与 `config/` 共 6 份文件前后 SHA-256 保持。交付备份与核验记录在本机 `.codex-tmp/library-startup-delivery-20261017/`；没有写入真实书库、停止应用或触发重导。
- 未重打 release 安装 / 便携 / macOS 包，未正式发布。冻结 GUI 在用户下次正常启动后进行人工验收，不能把隔离浏览器与装配核验称作已测完整冻结 GUI 启动耗时。

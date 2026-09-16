# 对齐组件 2B / 2C 审计修复

2026-09-16：修复安装依赖、维护恢复、模型下载生命周期与旧清单兼容；macOS ARM 已实际安装独立运行时并验证离线计算、发布结果。完整 UI、sidecar 精简和正式跨平台冻结包仍不在本次范围。

## 问题与改动

1. 清单原选 Python 3.11，但 NumPy 2.5.2 要求 >=3.12。改为 Python 3.12。ONNX Runtime 1.29.0 无 macOS Intel wheel，该平台在对齐组件自己的清单中固定 1.23.2；其他平台保留 1.29.0，不复用 OCR 组件的数值版本。
2. 恢复持有操作锁时清理遗留 `.maintenance`；其他实例仍持锁时不清理。原有 `.previous-*` 恢复保留。
3. 计算和模型下载（包含安装回执发布）共用共享使用锁；维护拿独占锁。POSIX 使用 flock，Windows 使用 LockFileEx；进程退出时系统释放句柄。
4. 模型下载子进程轮询关闭信号，取消后 terminate / kill / wait，再清理临时文件。应用拒绝关闭后的新下载，并等待下载线程和安装线程；超时返回关闭未完成。
5. 卸载等待已准入下载完成后再删除运行时和模型，避免先删除运行时再抛下载中异常。默认删除模型，不加确认弹窗，正式书库不在删除路径内。
6. 缓存清单缺少 alignment 时，仅对齐组件使用当前内置清单；其他组件继续使用缓存清单。
7. 修复回退测试对本机依赖的隐含假设。CI 的 Windows lane 改为 push 也运行，验证真实 Windows 文件锁。

## 回归证据

新增 `tests/test_alignment_runtime_review.py`，覆盖崩溃恢复、活动维护不误清理、旧清单、下载中卸载、跨进程租约、共享读锁、真实应用关闭期间下载回收与拒绝新下载、平台依赖和清单校验。修前运行得到 3 failures / 2 errors（含缺少新增取消接线）；Windows 分支修前由代码审计确认无共享锁。

## 实际组件安装和计算（macOS ARM）

- 使用系统信任库 `SSL_CERT_FILE=/etc/ssl/cert.pem`。首次 uv 下载遇到 TLS `DECRYPTION_FAILED_OR_BAD_RECORD_MAC`，已正确报告安装失败，未启用半成品。
- 使用 curl 从清单中的同一官方地址下载 uv 0.12.1；只替换归档的传输入口，生产安装器仍检查 17,679,560 字节与 SHA-256 `77d2906988e8074fd43f2f329ec452ebbf9b0c257ba1c66451c71de70a6baf42`。后续创建受管 Python、pip 安装、真实 `--verify`、目录发布均走生产实现。
- Python 3.12 / numpy 2.5.2 / onnxruntime 1.29.0 / fastembed 0.8.0 实际安装成功。
- 将 `worker_source_datas()` 提供的源码复制成冻结包同构目录，用已安装的独立解释器执行；`HF_HUB_OFFLINE=1`，复用只读模型快照，向量和回执写临时目录。
- 冷缓存计算对比：**4 条链接、2 个锚点完整相等**，不只比较数量；独立运行时复用已有模型的加载探针通过。
- 实际 ApplicationRuntime 主进程禁止导入 numpy / onnxruntime / fastembed，通过生产 `/api/text-alignments/generate` 路径返回 200；临时书库发布 **8 条链接**，完整持久化链接数据与原进程内计算相等（仅去除任务 UUID 与时间戳）。
- 所有安装和书库实验都在独立临时目录，未变更用户正式组件、文献和模型。

## 限制

- 未进行全新模型的网络下载；已验证现有模型复用、真实加载探针和离线计算。
- Windows 系统锁由远端 CI 验证；正式 Windows / macOS Intel 冻结包、签名及安装体验不以源码测试代替。
- 不把历史 206.4→100.8 MiB 定向构建数字视为本轮新构建结果。本轮未重建发布包。
- 上游元数据：[NumPy 2.5.2](https://pypi.org/pypi/numpy/2.5.2/json)、[ONNX Runtime 1.29.0](https://pypi.org/pypi/onnxruntime/1.29.0/json)、[ONNX Runtime 1.23.2](https://pypi.org/pypi/onnxruntime/1.23.2/json)。

## 本地门禁

- 最终全量 unittest：**2357 项，23 跳过，其余通过**，123.201 秒；设置 NO_PROXY/no_proxy 为 localhost / 127.0.0.1，PYTHONUTF8=1。
- Ruff：`ruff check . --extend-exclude .codex-tmp` 通过；排除已有、未跟踪的上游实验目录，不修改其代码。
- `git diff --check` 通过；架构循环依赖与前端守卫包含在全量测试中。
- macOS Intel：uv 按 Python 3.12 / x86_64-apple-darwin / only-binary 解析 **32 个包成功**。这证明安装依赖可解析，不替代 Intel 真机推理验收。
- Windows / Linux：以本提交远端 CI 为准；Windows lane 在 push 上启用，执行包含跨进程租约测试的全量测试。

### 卸载与测试目录收尾

- 真实安装后通过生产组件装配执行卸载：运行时和所属模型目录均删除，临时正式书库的既有对齐链接 **8→8**，未删除成果。
- 协调器旧测试的 `D:/runtime` 改为 TemporaryDirectory，避免真实锁在假路径创建文件。没有恢复“目录不存在便跳过租约”的旧漏洞。

### Windows 门禁环境修正

- 首次 push CI `35091953613`：Linux / lint 通过，Windows 为 1 failure / 29 errors。逐项核对发现 29 项失败来自 cp1252 读写中文，另 1 项来自路径测试全局修改 `sys.platform`，导致真实 Windows 锁误走 `fcntl`。
- Windows job 设置仓库规范要求的 `PYTHONUTF8=1`；新增清单测试显式用 UTF-8 读取。路径测试只替换 `runtime_location` 内的 `sys` 引用，保留真实操作系统锁，不跳过租约验证。
- 修正后的远端结果以随后 CI 为准；上述失败不计为跨平台验收通过。

## Windows 实机门禁结论(2026-09-16,本机 Windows 10.0.19045)

在真实 Windows(`.venv-windows` Python 3.12,`PYTHONUTF8=1`)上逐用例流式跑全量门禁,推翻此前"卡满 20 分钟被取消"的假设,并把问题收敛为 4 个确定性的测试夹具句柄泄漏。

### 卡死假设被推翻:跑完了,不是挂起

- 全量 **2357 用例 / 150.9s**,逐用例(verbosity=2)全程无停顿;外部卡死探测(同一用例 >200s 不动)未触发。
- 此前重点怀疑的 `test_alignment_compute_protocol`、`test_alignment_compute_lifecycle`、`test_managed_alignment_runtime`、`test_native_host_acceptance`(会真起子进程,Windows 冷启动慢)**全部正常跑完**。
- 结论:CI run `35093899543` 跑满 20 分钟被取消,在本 commit(3561f37)上**未复现**。为便于后续在 CI 上定位任何再次出现的停顿,已在 Windows job 接入流式+`faulthandler.dump_traceback_later` 诊断运行器(`scripts/run_tests_with_faulthandler.py`,只观测、不跳过、不改锁语义),CI 绿后可还原为原始 `python -m unittest`。

### 4 个 `WinError 32`:全为测试夹具句柄未释放,产品无泄漏

四者同源:测试体断言均已在错误前通过,异常只发生在 `tempfile.TemporaryDirectory` 退出清理时——Windows 删不掉 `data\index.sqlite3`(句柄未释放)。POSIX 可删打开中的文件,故 mac/Linux 全绿、仅 Windows 暴露。运行后无残留 worker 进程,确认是**进程内**句柄而非残留子进程占用。

| 用例 | 触发场景 | 根因 | 修法 |
|---|---|---|---|
| `test_alignment_compute_lifecycle::test_runtime_close_reaps_probe_and_compute_before_reporting_success`(probe/compute) | `close_runtime` 收割 worker 后读 DB | `with sqlite3.connect() as conn` 只提交不关闭,`conn` 与 `TemporaryDirectory` 同帧,清理时仍开 | `contextlib.closing` |
| `test_alignment_compute_protocol::NoPublishOnFailureTests::test_crash_publishes_nothing`(WORKER_CRASHED) | worker 崩溃路径 | `assertRaises` 把 `ctx.exception.__traceback__` 及其钉住的栈帧留到方法结束,越过 `TemporaryDirectory` 清理 | 改 `try/except as exc`(块末自动 `del`);`_completed_run_count` 加 `contextlib.closing` |
| `test_alignment_compute_protocol::NoPublishOnFailureTests::test_stale_result_publishes_nothing`(RESULT_MISMATCH) | 结果失效路径 | 同上 | 同上 |

- **产品侧已排除**:`generate_alignment`(`text_alignment.py`)第一个连接在准备阶段结束即 `finally: connection.close()`(约 1140 行),`compute()` 在两连接之间调用(约 1147 行),崩溃/失效时第二个连接尚未打开;传给 worker 的参数不含 `db_path`,worker 不碰 `index.sqlite3`。`close_runtime()` 收割 worker 的断言在错误前通过。故 4 个 error 均为测试写法,非产品缺陷,冒烟担心的"卸载残留锁"不属此列。
- **复现限制**:该锁只在 Windows 文件语义下暴露(POSIX 允许删打开中的文件),无法写跨平台复现测试;修复使连接关闭确定化,由本机 Windows 重跑门禁验证 `WinError 32` 消除。

### 版本落库(打包产物命名)

- 本分支 `src/me_finder/__init__.py` 的 `__version__` 仍为 `0.5.4`;`build_windows_installer.ps1`/`build_portable_release.ps1` 读该值并要求 `-Version` 与之一致、据此命名产物,不修会把 v0.5.5 产物错打成 `v0.5.4`。已改为 `0.5.5`,同步 `test_mcp_v1_baseline` 断言、`test_frontend_assets` 装配指纹(`web_assets` 注入 `__version__`)、`windows-release-smoke.yml` 新构建产物名(升级基线 v0.4.9 不动)与 `test_mcp_packaging` 断言。http-api 契约按 API 变更冻结(0.5.5 未改 HTTP 面)不动。

### 待办(打包在用户本地 Windows 执行)

- 本 Claude 会话运行于云端 Linux,无法执行 `.venv-windows` / PyInstaller / Inno Setup 7 打包;实机打包与"安装组件→生成对齐→卸载(链接 8→8、无残留锁)"冒烟由用户在本地 Windows 按 AGENTS.md §3.6 执行,产物名与 SHA-256 回填本报告与 release notes。

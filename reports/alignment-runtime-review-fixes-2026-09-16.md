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

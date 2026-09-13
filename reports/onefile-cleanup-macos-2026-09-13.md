# macOS onefile 临时目录清扫验证

2026-09-13：源码回归与真实 frozen 程序验证通过；未重建桌面分发包，未替换用户安装。

## 环境与范围

- macOS arm64、项目 `.venv-macos312-arm64/bin/python`、PyInstaller 6.21.0。
- 移植来源：Windows 分支提交 `8afe0f1`；仅移植清扫模块、marker、两个启动入口和对应测试。
- 实验均使用新建的临时根目录；不读取文献库、不清理用户现存 `_MEI`。
- 构建及探针日志保留在本地 `.codex-tmp/onefile-cleanup/`，不作为分发产物。

## 修复前复现

先向原版 `8afe0f1` 添加回归测试，再运行 `tests.test_onefile_cleanup`，出现四个失败断言：

1. `test_real_claim_protects_live_process_and_kill_releases_lock`：子进程调用产品 `claim_current_extraction()` 后仍存活，父进程清扫返回 1，误删其目录。
2. `test_onedir_meipass_never_receives_lock_but_still_sweeps`：Windows `_internal` 与 macOS `Contents/Frameworks` 两种路径均被判为 onefile。
3. `test_keeps_empty_lock_while_owner_is_claiming`：锁文件创建后、尚未上锁的空文件窗口被判为死亡实例。

原测试通过自制夹具主动 `flock`，没有调用真实 claim，因此漏掉第一个缺陷；原 onedir 夹具没有设置 `_MEIPASS`，漏掉第二个缺陷。

## 修复后验证

- 隔离工作树以 `c46354d` 为基线，仅叠加本次修复：全量 unittest 2275 项通过（22 项条件跳过，111.313 秒），Ruff F、前端 Node 语法检查与 diff 空白检查通过。主工作区首轮为 2274 项通过（执行期间补入一项锁访问失败测试，最终以隔离结果为准）。
- 专项 unittest 18 项通过，包含真实子进程持锁/强杀、无锁和空锁保留、文件访问失败保留、claim 写失败关闭句柄并阻止无保护启动、onedir 清扫且不写包内锁、marker 打包守卫。
- 使用正式 `packaging/mcp_sidecar.spec` 构建 `MEFinderMCP`（仅指定隔离的 `--distpath` / `--workpath`）。
- 在单独临时目录中启动 A、B 两个真实 sidecar，分别成功完成 STDIO MCP `initialize`；确认 A 的全部 335 个目录条目没有变化。
- 对 A 的独立进程组发送 `SIGKILL`（包含 bootloader 和解释器），确认 `_MEI` 遗留；启动 C 并完成 MCP 初始化，确认 A 的遗留目录消失，B 仍存活且目录保留。
- 另构建真实 onedir 最小程序，调用同一个产品清扫模块；运行结果：`onefile=false`、`lock_written=false`、`sweep_finished=true`，实际 `_MEIPASS` 指向 `_internal`。
- 源码采用 onedir 路径 + sidecar 专属 marker 的联合判据；macOS `.app/Contents/Frameworks` 路径另由专项测试覆盖。没有声称本轮重建或验收了完整桌面 `.app` 的签名。

## 边界与取舍

- 清扫只处理含专属 marker 且存在非空已释放锁的 `_MEI` 目录；无 marker、无锁、空锁均保留，不按 mtime 猜测进程死亡。
- POSIX claim 先持有 `flock`，再写入非空占用信息；Windows 继续使用句柄禁止删除的机制。占锁失败快速退出，避免 sidecar 在无保护状态继续运行。
- 旧构建和 bootloader 解压途中被杀留下的目录不会自动回收；需停止相关进程并确认目录归属后人工清理。本轮未进行该操作。
- 本轮实机证据来自 macOS。Windows 原提交的构建证据保留在议题历史中；本次修订的 Windows 实机运行和两平台完整分发包仍属于后续验证。
- PyInstaller 官方说明 `_MEIPASS` 同时用于 onedir 与 onefile，见 [Run-time Information](https://pyinstaller.org/en/stable/runtime-information.html)。

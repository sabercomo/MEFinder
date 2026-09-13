# MCP sidecar 在 C 盘 Temp 泄漏 `_MEI` 解压目录

状态：已移植并修正 macOS 锁与 onedir 判定；尚未进入桌面分发包。下方先保留 Windows `8afe0f1` 原始记录，最新行为和边界以文末追加记录为准。

## 事实（2026-09-13 确认）

- 用户 C 盘 Temp 曾堆积 **120+ 个 `_MEI…` 目录、每个约 0.16G、共约 19G**，已手工清空（清后实测 Temp 中 `_MEI` 数为 0）。
- 仓库内唯一的 PyInstaller **onefile** 组件是 MCP sidecar `MEFinderMCP.exe`（开发构建 80,278,596 字节，解压后约 0.16G，与泄漏单目录体积吻合）。桌面主程序是 onedir 构建（`packaging/desktop.spec`），从不产生 `_MEI`。
- 解析 worker 不是泄漏源：托管 MinerU 从 venv 启动 `mineru-api`（`managed_mineru.py` 的 `_venv_executable`），本地 OCR 也走独立 venv Python，均不产生 `_MEI`。
- PyInstaller bootloader 只在解释器**正常退出**时删除自己的 `_MEI` 目录；进程被 `TerminateProcess` 强杀即泄漏。MCP 客户端在会话结束/超时时普遍强杀子进程，所以泄漏会持续累积。

## 根因与修复设计

每次 `MEFinderMCP.exe` 启动向 `%TEMP%\_MEI<随机>` 解压全部依赖，被强杀后无人清理。修复（`src/me_finder/onefile_cleanup.py`）：

1. **识别自己的目录**：sidecar 打包时内置 marker 文件 `packaging/mefinder-onefile.marker`（`mcp_sidecar.spec` datas），清扫时只处理含该文件的 `_MEI*` 目录，其他应用的 `_MEI` 一律不碰。
2. **判定属主死亡**：sidecar 启动时在 `sys._MEIPASS` 写 `mefinder-sidecar.lock` 并持有句柄到进程结束；OS 在进程死亡时释放句柄。清扫者尝试删除锁文件——Windows 上被占用即 `PermissionError`（属主还活着，跳过），删除成功即属主已死（可清）。无锁文件的目录按 mtime 保守处理：1 小时内视为并发实例正在启动，跳过。
3. **不阻塞启动**：清扫在 daemon 线程执行；sidecar 的 `mcp_server.main()` 与桌面端 `desktop.main()`（frozen 时）各挂一个清扫入口。sidecar 若拿不到自己目录的锁则跳过清扫（宁可不清也不能误删活实例）。
4. **错误即停**：`rmtree` 首个失败即中止该目录，留给下次清扫重试，避免把还活着的目录删一半。

## 验证（2026-09-13，正式 spec 构建的 MEFinderMCP.exe）

- 专项测试 `tests/test_onefile_cleanup.py` 15 项通过；全量 2100 项 OK（1 条件跳过）。
- stdin EOF 正常退出 → bootloader 自删自己的 `_MEI`（卸载耗时数秒属正常）。
- `taskkill /F` 强杀 → `_MEI` 泄漏（含 marker + lock），完整复现。
- 再次启动 → daemon 清扫在**服务期间**删掉旧泄漏目录；存活实例的目录因锁被持而正确跳过。
- 多实例并发下锁保护正确；验证结束时 Temp `_MEI` 归零、无残留进程。

## 边界与遗留

- 已装旧版（无清扫机制）的存量泄漏仍需手工清理一次：删 `%TEMP%\_MEI*` 即可（正在运行的删不掉，不会误伤）。
- 清扫靠"每次启动 sidecar 或桌面端"触发；长期不开应用时不新增清扫机会（但也不新增泄漏）。
- 推断（待下次发版后核实）：若某 MCP 客户端以极短会话高频启停 sidecar（进程在清扫完成前退出），单次清扫可能不彻底，由后续启动补齐。

## 2026-09-13 — integration 移植与 POSIX 正确性修复

**事实**（复现与真实构建证据见 [macOS 验证报告](../../reports/onefile-cleanup-macos-2026-09-13.md)）：原 claim 只打开文件，POSIX 探测却使用 `flock`，会误删仍存活的 sidecar；`sys._MEIPASS` 在 onedir 中也存在，原判定会向桌面包内写锁。原专项夹具手工持 flock、onedir 不设 `_MEIPASS`，因此未复现产品路径的这两个问题。

修订：POSIX claim 先持 `LOCK_EX | LOCK_NB`，再写入占用信息；onefile 判定要求 frozen、`_MEI` 目录名和 sidecar 专属 marker 同时成立。桌面只启动清扫，不往 `_internal` 或 `.app/Contents/Frameworks` 写锁。文件访问失败不能作为进程死亡证据；claim 失败关闭句柄并向上传播错误，阻止无保护运行。

**取舍**：取消“无锁但超过一小时即删除”的规则。目录年龄无法证明慢启动/暂停的进程死亡；只回收带 marker、非空 claim 且锁已释放的目录。空锁在 open→flock 之间也必须保留。此规则覆盖 MCP 正常服务期间强杀造成的泄漏，不覆盖解压或 claim 完成前中断的残留。

上方 Windows 历史“直接删 `%TEMP%\\_MEI*`”的建议不适用于跨平台操作：POSIX 可删除活进程仍使用的文件，且 `_MEI` 名称本身不证明归属。人工清理必须先停止相关进程并核实归属。本轮未清理用户旧残留；未发布或替换安装包。

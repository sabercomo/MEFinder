# Windows：MinerU 在线账号测试显示 Failed to fetch

2026-11-17

- **事实**：用户在 Windows 的「MinerU 在线 API → 测试连接」看到 `Failed to fetch`。用户提到 0.6.2，远端此时可核实的最新发布及源码版本为 0.6.1；报错安装包的版本和上游网络原因待核实。
- **复现**：在 macOS 上经真实本机 HTTP 路由测试账号，分别模拟 MinerU 在返回响应前断开、重置连接和响应截断。修复前，三种异常均导致本机请求在返回 HTTP 响应前断开。另用 Chrome 点同一测试入口，并让本机模拟服务在收到请求后直接断开，旧版显示 `Failed to fetch`，修复版显示 `Remote end closed connection without response`。
- **修复**：在线 MinerU 的 JSON 客户端把这些网络和 HTTP 传输异常转换为 `MinerUError`，保留具体原因，测试账号接口可返回原有格式的 JSON 错误。回归用例为 `test_connection_test_returns_json_when_upstream_connection_breaks`。待 Windows 安装包复测原报错。
- **2026-11-17 补充**：已用包含本修复的源码 `3c9d809` 构建 Windows 0.6.2 本地测试包（安装版与便携版），SHA-256 见 [0.6.2 说明](../release-notes-0.6.2.md)；尚未在 Windows 安装复测原报错。
- **2026-11-17 根因与修复（事实）**：ProcMon 定位到构建解释器的 `ssl.py` 被 Codex 运行时补丁为创建 `SSLContext` 时读取同目录 `codex-rejected-cas.pem`。PyInstaller 未收集该文件，冻结版 `urllib.request.build_opener` 内 `ssl.create_default_context` 抛 `FileNotFoundError`，路由未捕获异常，本地连接静默断开。三个 PyInstaller spec 现在仅当构建解释器确有该文件时才打入包根目录，并新增测试钉住。验证：重建后的 dist 冻结版用真实账号调用 `/api/mineru-accounts/test` 返回 HTTP 200（425 ms）；重打的安装版与便携版均通过 2,876 项门禁，包内均含该证书文件。Windows 安装版 GUI 复测仍待执行。

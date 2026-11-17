2026-11-17：v0.6.2 草稿候选。本次修复 MinerU 在线连接测试遇到传输中断时只显示 `Failed to fetch` 的问题，供 macOS 与 Windows 后续验收。

# MEFinder 0.6.2

- 测试 MinerU 在线账号时，遇到对端断开、连接重置或响应截断，会显示具体网络错误，便于定位连接问题。
- HTTP 路由、MCP 工具和文献库格式沿用 0.6.1。

Windows 用户报告的现场故障待实机复测。macOS 安装包为 ad-hoc 签名。

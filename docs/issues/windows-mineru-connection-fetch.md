# Windows：MinerU 在线账号测试显示 Failed to fetch

2026-11-17

- **事实**：用户在 Windows 的「MinerU 在线 API → 测试连接」看到 `Failed to fetch`。用户提到 0.6.2，远端此时可核实的最新发布及源码版本为 0.6.1；报错安装包的版本和上游网络原因待核实。
- **复现**：在 macOS 上经真实本机 HTTP 路由测试账号，分别模拟 MinerU 在返回响应前断开、重置连接和响应截断。修复前，三种异常均导致本机请求在返回 HTTP 响应前断开。另用 Chrome 点同一测试入口，并让本机模拟服务在收到请求后直接断开，旧版显示 `Failed to fetch`，修复版显示 `Remote end closed connection without response`。
- **修复**：在线 MinerU 的 JSON 客户端把这些网络和 HTTP 传输异常转换为 `MinerUError`，保留具体原因，测试账号接口可返回原有格式的 JSON 错误。回归用例为 `test_connection_test_returns_json_when_upstream_connection_breaks`。待 Windows 安装包复测原报错。

## 2026-11-19 — #24 发布包核查与地址修复

- **事实**：[#24](https://github.com/sabercomo/MEFinder/issues/24) 截图显示 0.6.1，服务地址为 `https://mineru.net/api/v4/extract/task`。issue 最新两条回复来自维护者，分别承诺修复与请用户复测 0.6.2，未见报告者的升级复测结果。
- **事实**：下载的 0.6.2 Windows 便携包 SHA-256 与公开发布资产一致；冻结 SSL 模块所需的证书拒绝列表已携带，MinerU 传输异常转换也已进入主程序。将该包的 MinerU 冻结模块接入本地 HTTP 服务，仍会请求 `/api/v4/extract/task/api/v4/file-urls/batch`，返回 HTTP 404。详见 [实证报告](../../reports/issue-24-connection-validation-2026-11-19.md)。
- **修复**：在配置读取、保存和直接连接测试的输入边界移除已知 v4 接口后缀，保留原有服务域名与代理前缀；兼容已有配置，不因读取修改用户文件。未调整前端、HTTP/MCP 契约、数据库或解析流程。
- **验证**：三项新增回归先失败后通过；本地 HTTP 对照确认旧冻结模块返回 404，新源码请求正确路径并成功。其他 API 的 SSL 修复已核对；截图中的具体模型、账号和本地服务是否可用仍待现场复测，不能仅凭地址问题断言它们已全部解决。

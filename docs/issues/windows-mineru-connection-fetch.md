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

### 同日追加：0.6.3 本地测试包

- 用户要求「修复打成0.6.3」后，将版本、HTTP/MCP 契约发布标记、前端版本指纹及 Windows 手动冒烟的制品名同步为 0.6.3，并构建 macOS arm64 DMG/ZIP。
- **事实**：构建内全量 2,881 项通过（40 跳过），325 个产品文件及冻结 MinerU 模块与源码一致；在隔离数据目录启动新原生应用，经其自身 HTTP 设置/测试路由连接本地模拟服务，完整接口地址使用正确路径。详细哈希及验收见 [0.6.3 说明](../release-notes-0.6.3.md)。Windows 同源包和报告者现场连接仍待复测，未公开发布。

## 2026-11-22 — 其他解析 API 的传输异常

- **事实**：#24 的另一张截图显示两个 OpenAI 兼容接口分别使用 HTTPS 地址和 Windows 本机回环地址，截图中均只显示「连接失败」。现场没有保留具体错误或上游响应。
- **复现与修复**：对端断开、连接重置和响应截断可由 urllib 直接抛出 `OSError` 或 `HTTPException`。其他解析 API 原来只处理 `URLError`，这些异常会穿过设置接口。现在将它们转换为 `VisionAPIError`，检测连接和获取模型均返回具体原因。失败时不会另发请求，实际解析使用同一个传输层。回归用例 `test_openai_transport_failures_are_reported_to_settings` 修复前在三种异常下均失败，修复后通过。
- **验证**：用本机模拟上游发起真实 HTTP 请求。修复前的 0.6.4 arm64 成品遇到对端断开时会中断设置请求，客户端拿不到 HTTP 响应。修复后，断开和截断时，检测连接和获取模型均返回带原因的 HTTP 400 JSON。上游恢复后，使用原配置重试，两项均返回 HTTP 200。MinerU 完整接口地址仍会请求正确的批量上传路径。报告者原来的两个上游仍待现场复测。

2026-11-19：**v0.6.2 已发布，但 #24 的完整接口地址拼接问题仍存在。** 后续补修已打成 [0.6.3 macOS arm64 本地测试包](release-notes-0.6.3.md)，尚未公开发布；见本页末尾后续核查。

2026-11-17：**v0.6.2 正式发布。** macOS Apple Silicon 与 Windows 安装版、便携版共八个附件已上传并核对远端 digest。下载见 [GitHub Release v0.6.2](https://github.com/sabercomo/MEFinder/releases/tag/v0.6.2)。

# MEFinder 0.6.2

- 修复 [#24](https://github.com/sabercomo/MEFinder/issues/24)：打包版里 MinerU 与在线 API 的所有 HTTPS 请求都会失败。冻结包没有携带构建机解释器要求的 SSL 证书拒绝列表，请求在建立连接前就抛错，界面只看到笼统的报错。
- 测试 MinerU 在线账号时，遇到对端断开、连接重置或响应截断，会显示具体网络错误。
- MinerU 检索结果会在本机排版化学式、上下标和角度。已有文献无需重解析，框选单段仍复制原始公式文本。

HTTP 路由、MCP 工具和文献库格式沿用 0.6.1。macOS 安装包为 ad-hoc 签名；Windows 为本地未签名测试包。

构建基于 `9f3705b41e2fae00f6964489304cd3d100423bd2`（Windows 检出 `1f8b5d1`，差异仅在发布文档）。全量测试 2,878 项，macOS arm64 40 项、macOS x86_64 45 项、Windows 53 项按条件跳过。公式浏览器回归、Ruff、JS 语法、冻结 MCP 侧车、签名和安装镜像验证通过。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.2-macos-arm64.dmg` | 104,291,268 | `7dd7e868f4e2c8073fe46fdf3e32908698e935f748474cb059d53cfb3ea2ec20` |
| `MEFinder-v0.6.2-macos-arm64.zip` | 96,390,388 | `f31899024dafe7e297c7c930a2d834278efc8eeaafbae5eafdb5a3b3dbc2aa5f` |
| `MEFinder-v0.6.2-windows-setup.exe` | 82,317,088 | `f95a127c95bdd20a08ebc4171499bd01b4be8afcfa99d5e5a8c0b8510a0783e4` |
| `MEFinder-v0.6.2-windows-portable.zip` | 98,143,299 | `4d5f1e46ea4302e97786a473c04455fbb2df4b2bafa0d198adf48c7d866cd247` |

四个包各附同名 `.sha256.txt`。Windows 安装 / 升级 / 卸载与冻结 GUI 人工验收未执行。

macOS Intel 包已完成构建与核验（2,878 项通过、45 跳过；桌面程序与 MCP 侧车均为纯 x86_64、最低 macOS 12.0），因本机上行带宽不足暂未上传，补传后更新本页与 Release。

## 2026-11-19 后续核查（补修进入 0.6.3 本地测试包）

- #24 截图还把完整任务接口 `https://mineru.net/api/v4/extract/task` 填成了服务地址。0.6.2 已发布 Windows 包确实包含 SSL 资源修复，但仍会在这个地址后重复拼接 API 路径，未覆盖该问题。
- 源码已补充地址规范化：接受服务根地址、`/api/v4`、完整任务接口和批量上传接口；保留自定义代理的路径前缀。新保存的配置使用根地址，旧配置只在读取时规范化，无需手工重填，也不因读取而写回文件。
- 证据及验证范围见 [#24 连接核查报告](../reports/issue-24-connection-validation-2026-11-19.md)。同日按用户要求打成 0.6.3 macOS arm64 本地测试包，未替换已发布的 0.6.2 附件。

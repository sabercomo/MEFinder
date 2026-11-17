2026-11-17：v0.6.2 草稿候选，供 macOS Apple Silicon 与 Windows x64 测试。

# MEFinder 0.6.2

- MinerU 检索结果会在本机排版化学式、上下标和角度。已有文献无需重解析，框选单段仍复制原始公式文本。
- 测试 MinerU 在线账号时，遇到对端断开、连接重置或响应截断，会显示具体网络错误。
- 冻结包按构建解释器的实际情况携带 SSL 证书拒绝列表，避免缺少运行时文件时 HTTPS 调用失败。

HTTP 路由、MCP 工具和文献库格式沿用 0.6.1。macOS 安装包为 ad-hoc 签名；Windows 为本地未签名测试包。

构建基于 `9f3705b41e2fae00f6964489304cd3d100423bd2`（Windows 检出 `1f8b5d1`，差异仅在发布文档）。全量测试 2,878 项，macOS 40 项、Windows 53 项按条件跳过。公式浏览器回归、Ruff、冻结 MCP 侧车、签名和安装镜像验证通过。

| macOS Apple Silicon（最低 macOS 14） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.2-macos-arm64.dmg` | 104,291,268 | `7dd7e868f4e2c8073fe46fdf3e32908698e935f748474cb059d53cfb3ea2ec20` |
| `MEFinder-v0.6.2-macos-arm64.zip` | 96,390,388 | `f31899024dafe7e297c7c930a2d834278efc8eeaafbae5eafdb5a3b3dbc2aa5f` |

| Windows x64 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.2-windows-setup.exe` | 82,317,088 | `f95a127c95bdd20a08ebc4171499bd01b4be8afcfa99d5e5a8c0b8510a0783e4` |
| `MEFinder-v0.6.2-windows-portable.zip` | 98,143,299 | `4d5f1e46ea4302e97786a473c04455fbb2df4b2bafa0d198adf48c7d866cd247` |

四个包各附同名 `.sha256.txt`。Windows 两个包均通过 2,878 项 unittest（53 跳过）、Ruff、JS 语法、冻结 MCP 侧车与空白索引检查，包内均含 SSL 证书拒绝文件。Windows 安装 / 升级 / 卸载与冻结 GUI 人工验收未执行；Intel 版本待补。

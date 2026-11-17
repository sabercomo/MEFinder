2026-11-17：v0.6.2 草稿候选，供 macOS Apple Silicon 测试。

# MEFinder 0.6.2

- MinerU 检索结果会在本机排版化学式、上下标和角度。已有文献无需重解析，框选单段仍复制原始公式文本。
- 测试 MinerU 在线账号时，遇到对端断开、连接重置或响应截断，会显示具体网络错误。
- 冻结包按构建解释器的实际情况携带 SSL 证书拒绝列表，避免缺少运行时文件时 HTTPS 调用失败。

HTTP 路由、MCP 工具和文献库格式沿用 0.6.1。macOS 安装包为 ad-hoc 签名。

构建基于 `9f3705b41e2fae00f6964489304cd3d100423bd2`。全量测试 2,878 项，40 项按条件跳过。公式浏览器回归、Ruff、冻结 MCP 侧车、签名和安装镜像验证通过。

| macOS Apple Silicon（最低 macOS 14） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.2-macos-arm64.dmg` | 104,291,268 | `7dd7e868f4e2c8073fe46fdf3e32908698e935f748474cb059d53cfb3ea2ec20` |
| `MEFinder-v0.6.2-macos-arm64.zip` | 96,390,388 | `f31899024dafe7e297c7c930a2d834278efc8eeaafbae5eafdb5a3b3dbc2aa5f` |

两个包各附同名 `.sha256.txt`。Intel 与 Windows 版本待用同一源码构建。

2026-11-17：**v0.6.2 正式发布。** macOS 双架构与 Windows 安装版、便携版共 12 个附件已上传并核对远端 digest。下载见 [GitHub Release v0.6.2](https://github.com/sabercomo/MEFinder/releases/tag/v0.6.2)。

# MEFinder 0.6.2

- MinerU 检索结果会在本机排版化学式、上下标和角度。已有文献无需重解析，框选单段仍复制原始公式文本。
- 测试 MinerU 在线账号时，遇到对端断开、连接重置或响应截断，会显示具体网络错误。
- 冻结包按构建解释器的实际情况携带 SSL 证书拒绝列表，避免缺少运行时文件时 HTTPS 调用失败。

HTTP 路由、MCP 工具和文献库格式沿用 0.6.1。macOS 安装包为 ad-hoc 签名；Windows 为本地未签名测试包。

构建基于 `9f3705b41e2fae00f6964489304cd3d100423bd2`（Windows 检出 `1f8b5d1`，差异仅在发布文档）。全量测试 2,878 项，macOS arm64 40 项、macOS x86_64 45 项、Windows 53 项按条件跳过。公式浏览器回归、Ruff、JS 语法、冻结 MCP 侧车、签名和安装镜像验证通过。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.2-macos-arm64.dmg` | 104,291,268 | `7dd7e868f4e2c8073fe46fdf3e32908698e935f748474cb059d53cfb3ea2ec20` |
| `MEFinder-v0.6.2-macos-arm64.zip` | 96,390,388 | `f31899024dafe7e297c7c930a2d834278efc8eeaafbae5eafdb5a3b3dbc2aa5f` |
| `MEFinder-v0.6.2-macos-x86_64.dmg` | 108,834,536 | `e589290f8caf6cb566a10eee5fd621de2d7e3907d6f15095a607edd85d363d72` |
| `MEFinder-v0.6.2-macos-x86_64.zip` | 102,926,757 | `72579bc836864e74e90748e884ca02d6bb846988387d69edabb7be87ccda0efa` |
| `MEFinder-v0.6.2-windows-setup.exe` | 82,317,088 | `f95a127c95bdd20a08ebc4171499bd01b4be8afcfa99d5e5a8c0b8510a0783e4` |
| `MEFinder-v0.6.2-windows-portable.zip` | 98,143,299 | `4d5f1e46ea4302e97786a473c04455fbb2df4b2bafa0d198adf48c7d866cd247` |

六个包各附同名 `.sha256.txt`。Intel 包在 Apple Silicon 上经 Rosetta 构建，桌面程序与 MCP 侧车均为纯 x86_64、最低 macOS 12.0，未在 Intel 真机验收。Windows 安装 / 升级 / 卸载与冻结 GUI 人工验收未执行。

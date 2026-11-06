# Windows 0.6.1 构建与正式发布

2026-11-06：**v0.6.1 正式发布。** Windows 安装版、便携版及两份 SHA-256 已补齐，与 macOS 双架构共 12 个附件均已上传并核对远端 digest。下载见 [GitHub Release v0.6.1](https://github.com/sabercomo/MEFinder/releases/tag/v0.6.1)。

本版修复期刊 PDF 页脚数字反序存储造成的刊印页码识别错误，并显示 MinerU 失败页段、Token 无效等具体原因；永久失败不再误报可续传，终态历史分片不再阻挡删除空闲账号。内部模块整理保持数据库与接口兼容。

Windows 构建源码 `7cd470fadf87a47ee8bd177ce9e10177d85210d6`；与 Mac 构建源码 `55f6def` 的 `src/` 逐文件相同，后续差异只有工作流、测试和发布文档。installer-build：2,875 项通过（52 跳过，221.724 秒）；portable-build：2,875 项通过（52 跳过，219.422 秒）；browser-tests：16 项通过（0 跳过，52.218 秒）。Ruff(F)、JavaScript 语法、前端守卫、冻结代码与源码比对、空库 / FTS5、MCP STDIO、隐私与许可证检查均通过。Mac 官方构建与冒烟详见 [Mac 报告](../reports/macos-release-2026-11-06.md)。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.1-macos-arm64.dmg` | 103,456,572 | `c2a31f55ae59dd0896f4a18a6d9883426817caa8b10c3e440eccd0976538c6d9` |
| `MEFinder-v0.6.1-macos-arm64.zip` | 96,310,860 | `271af67cd9c60f749fec468fe8829df2986f2616a855a95db24c4422eb413dff` |
| `MEFinder-v0.6.1-macos-x86_64.dmg` | 108,843,061 | `9ff2033d02cf6996d034d667e50fb8e1458dd2a3e48f440062c7a152f6b01087` |
| `MEFinder-v0.6.1-macos-x86_64.zip` | 102,845,652 | `902a7777bcc7f8557705d5a1bcbec11a5ce017121796f30f347e8f2f39d9b4a4` |
| `MEFinder-v0.6.1-windows-portable.zip` | 98,300,339 | `273718be1785bb2f50fe05edd39d96cc570b028a40f0a6867169e1e5a530d8c5` |
| `MEFinder-v0.6.1-windows-setup.exe` | 82,506,344 | `93e9b40a3d1a54bb0cc217b0253b9e136f7d03ef85c1273ecc2ddff10ee42574` |

各文件均附同名 `.sha256.txt`。Windows 两个程序均为 x64 / 0.6.1，ZIP CRC、81 个界面资源、241 份交付 Python 源码及主程序 / MCP 的全部冻结应用模块与构建源码一致。macOS 为 ad-hoc 签名、未公证；Intel 经 Rosetta 构建，未在 Intel 真机验证。Windows 安装 / 升级 / 卸载及冻结 GUI 人工验收本轮未执行。本机运行中的旧程序与文献库未替换。


## 构建证据

- 使用完整独立检出 `.codex-tmp/release-0.6.1-20261106/repo`；Python 3.12.14、PyInstaller 6.21.0、Inno Setup 7，设置 `PYTHONUTF8=1`，顺序运行两个官方 PowerShell 脚本。
- 安装包编译完成后先核验其 payload，便携包构建后解包独立核验，两份冻结代码均与构建提交一致。全量测试由两个官方脚本分别运行，无跳过门禁。
- 主程序冻结应用模块 233 个、MCP 冻结应用模块 104 个逐一匹配。便携根含 `portable.flag`，不含开发用 `data_root.txt`。
- 四个 Windows 文件复制至主工作区 `release/` 后重新实算 SHA-256；远端四资产的大小、状态及 digest 与本地一致；Mac 八资产与发布前快照一致。
- Git HTTPS 连接失败，改用 GitHub Git Data API 恢复最新提交对象，逐对象核对 Git SHA；没有修改远端已有历史。最新源码 CI [42224420375](https://github.com/sabercomo/MEFinder/actions/runs/42224420375) 通过。
- 本轮不启动云端打包。旧 Windows 发布工作流因 UTF-8 失败及后续用户取消的记录保留在 Mac 报告和 `docs/issues/windows-release-smoke-utf8.md`；本机官方双构建已通过。
- 完整日志、两份 payload / ZIP 核验 JSON、上传前后快照与发布核验保存在本地 `.codex-tmp/release-0.6.1-20261106/`。正式 tag 在发布后实时核对；发布文档提交仅改变上述两份说明及本报告，产品源码不变。

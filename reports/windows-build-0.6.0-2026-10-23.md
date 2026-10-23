# 0.6.0 Windows 本地测试包核验

2026-10-23：远端主线已拉取，Windows 安装版与便携版均通过官方本机构建和制品核验，可以用于本机测试；0.6.0 保持开发版。

构建源码：`2371f60dc3b058ef9b945b618545a3562477fac4`，版本 0.6.0；使用完整独立检出与本机 Python 3.12.14、PyInstaller 6.21、Inno Setup 7，设置 `PYTHONUTF8=1`，顺序运行官方 `build_windows_installer.ps1` 与 `build_portable_release.ps1`。

- 安装包：2,825 项 unittest 通过（52 项环境跳过，220.038 秒）。
- 便携包：2,825 项 unittest 通过（52 项环境跳过，217.495 秒）。
- Chrome 浏览器回归：16 项 unittest 通过（0 项环境跳过，52.507 秒）。
- 全部跟踪 Python 文件的 Ruff(F)、逐文件 JavaScript 语法、前端装配与偏好守卫通过。
- 两次官方构建的 MCP STDIO、空库 / FTS5、隐私边界与许可证门禁通过。
- 安装 payload 与便携包均核对了 81 个界面资源、230 份 Python 源文件；桌面程序 222 个、MCP 96 个冻结应用模块与源码编译结果一致，两程序均为 x64 / 0.6.0。
- ZIP CRC 与两个 SHA-256 sidecar 独立核验通过。

| 产物 | 字节数 | SHA-256 |
|---|---:|---|
| `MEFinder-v0.6.0-windows-setup.exe` | 82,513,410 | `052fd6d57ce7259fd9552d2b627d36941218e11024f03cc2dab0feaf917a27db` |
| `MEFinder-v0.6.0-windows-portable.zip` | 98,284,307 | `557ffc04205c3f92c12a28adb25d7020c9ba55b3b9c9a55deabfe5b783fca334` |

产物及两个同名 `.sha256.txt` 位于本机 `release/`。本轮只交付本地测试包，未安装、未上传、未创建 tag 或 GitHub Release，未启动额外云端构建；原 `dist/MEFinder`、真实书库与既有用户修改保持。Windows 安装 / 升级 / 卸载及冻结 GUI 人工验收未执行，不将本地构建门禁视为正式发布门禁全部通过。

已有书籍仍保留旧页内文字记录；本轮未运行 `refresh-page-text --apply`，未移除或重算用户对齐。若后续需要刷新，应关闭 MEFinder、备份并按本版本升级说明单独执行。

构建、浏览器与核验日志：`.codex-tmp/update-0.6.0-20261023/`；关键证据为 `artifact-verification.json`、`installer-payload-verification.json` 与两个官方构建日志。

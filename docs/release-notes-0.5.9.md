2026-10-07：v0.5.9 可本机试用；U 盘文档包弹窗修复、全量门禁及 Windows 安装包 / 便携包构建核验通过，尚未正式发布。

# v0.5.9 迭代说明

- 文献库单篇与批量导出的文档包统一使用 `.mefinder` 后缀，内部仍是原有 ZIP 容器和 `mefinder.document.v1` 数据，不重解析、不改变页码与原 PDF。Windows 不再自动按 ZIP 浏览它及 Mac 创建的同名 `._` 辅助文件。
- 导入页的文件选择、拖入、分块上传与包恢复支持 `.mefinder`，继续支持旧 `.mefinder.zip`；前端忽略 Mac 的 `._` 辅助文件。
- 0.5.8 及更早版本不能直接选择新后缀；需要传回旧版时，可将真正的文档包改名为 `.mefinder.zip`。不要把 `._` 开头的 Mac 辅助文件当作文档包。
- 数据库 schema、MCP 工具与文档包内部契约不变；HTTP 版本快照、MCP 应用版本、Windows 冒烟文件名与前端指纹同步到 0.5.9。
- 实际 U 盘证据与复现测试见 [问题记录](issues/usb-appledouble-zip-dialog.md) 和 [核验报告](../reports/usb-document-package-2026-10-06.md)。先前怀疑强制 ZIP64，经实际文件核验后不采用该修改。

## Windows 本地交付

本机 Python 3.12 / `PYTHONUTF8=1`：源码全量 unittest 2,741 项通过（24 项环境跳过，231.733 秒）；完整独立 Git 检出中，官方安装与便携脚本分别 2,741 项通过（各 42 项环境跳过，231.105 / 218.778 秒）。跟踪 Python 文件 Ruff(F)、独立检出 Ruff、前端守卫和逐文件 Node 语法、空库 FTS5 / 短词索引、17 工具 MCP STDIO、隐私与许可证检查均通过。

包内桌面程序和 MCP 侧车均为 x64 / 0.5.9，桌面程序字节码包含 `.mefinder` 导出后缀；构建源码与已测源码逐文件相等。便携 ZIP CRC、解包侧车初始化版本与 17 工具核验、两个制品实算 SHA-256 与 sidecar 一致。

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-windows-setup.exe` | 82,459,907 | `f42c9c53ed60ffbf36597a45ee4d82e90a4d67b3ba7d0c528c404b20f74a496b` |
| `MEFinder-v0.5.9-windows-portable.zip` | 98,202,784 | `d6d5a98a119ba9d6c5b0cf8f47dff2e36261ab4a9f7fe3fc2f8a13def6a8cf2d` |

两个 `.sha256.txt` 文件同目录。构建与核验日志在 `.codex-tmp/windows-v059-20261006/`。未替换当前安装或开发程序；安装 / 升级 / 卸载及 macOS 0.5.9 构建未验证，未上传 GitHub Release 或正式发布。未启动额外云端构建。

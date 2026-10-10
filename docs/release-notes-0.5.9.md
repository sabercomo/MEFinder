2026-10-10：对齐阶段百分比、进度条与当前阶段预计剩余时间可本机试用；独立 Windows 开发程序已构建核验，关闭原程序后启动 `dist/MEFinder-progress-20261010/文献原句定位器.exe` 即可使用原文献库。现有 release / macOS 包不含此功能，尚未正式发布。

2026-10-10：作品弹窗中文输入、下拉宽度和对齐启动失败提示已修复，本机 Windows 开发程序已重建并核验；现有 Windows 双包与 macOS arm64 包为 10-07 构建，尚未正式发布。

2026-10-07：v0.5.9 Windows 与 macOS arm64 包均已本机打包并通过自动化门禁，尚未正式发布；macOS Intel（x86_64）包未打，安装 / 升级 / 卸载与安装后人工验收未做。

2026-10-07：v0.5.9 可本机试用；U 盘文档包弹窗修复、全量门禁及 Windows 安装包 / 便携包构建核验通过，尚未正式发布。

# v0.5.9 迭代说明

- 生成对齐时显示当前阶段的真实百分比、进度条和预计剩余时间；样本不足时显示正在估算，缓存完整命中时跳过文本计算，支持刷新后恢复。剩余时间仅指当前阶段，见 [任务进度契约](contracts/v0.5.9-alignment-jobs.md)。
- 新建作品的文献搜索保留活动输入框，修复中文组合输入被中断和中间编辑光标跳到末尾；加入作品的下拉菜单恢复到触发器宽度。
- 对齐计算进程在能力探测阶段退出时，提示重新安装计算组件。本机旧路径启动器已离线修复并完成真实 E5 小样本计算；《异化》整书对齐尚未重跑，见 [问题记录](issues/works-dialog-ime-and-alignment-runtime.md) 和 [核验报告](../reports/works-dialog-runtime-2026-10-10.md)。
- 文献库单篇与批量导出的文档包统一使用 `.mefinder` 后缀，内部仍是原有 ZIP 容器和 `mefinder.document.v1` 数据，不重解析、不改变页码与原 PDF。Windows 不再自动按 ZIP 浏览它及 Mac 创建的同名 `._` 辅助文件。
- 导入页的文件选择、拖入、分块上传与包恢复支持 `.mefinder`，继续支持旧 `.mefinder.zip`；前端忽略 Mac 的 `._` 辅助文件。
- 0.5.8 及更早版本不能直接选择新后缀；需要传回旧版时，可将真正的文档包改名为 `.mefinder.zip`。不要把 `._` 开头的 Mac 辅助文件当作文档包。
- 数据库 schema、MCP 工具与文档包内部契约不变；HTTP 版本快照、MCP 应用版本、Windows 冒烟文件名与前端指纹同步到 0.5.9。
- 实际 U 盘证据与复现测试见 [问题记录](issues/usb-appledouble-zip-dialog.md) 和 [核验报告](../reports/usb-document-package-2026-10-06.md)。先前怀疑强制 ZIP64，经实际文件核验后不采用该修改。

## Windows 本地交付

2026-10-10（对齐进度）：源码 2,750 项通过（27 跳过，227.212 秒）、Chrome 9 项通过（29.791 秒）、跟踪 Python 的 Ruff(F) 零告警。官方 `build_windows_dist.cmd` 在完整独立检出通过 2,750 项（45 跳过，224.745 秒）、Node 语法、主程序 / MCP 打包、FTS5 与 MCP 冒烟。交付到 `dist/MEFinder-progress-20261010/`；资产、worker 源码、主程序 PYZ 及原数据目录指针核验通过，包内 worker 使用既有 E5 模型实际完成进度上报小样本。原 `dist/MEFinder/` 和运行中的桌面 / MCP 会话保留，`release/` 双包未重打。见 [核验报告](../reports/alignment-progress-2026-10-10.md)。

2026-10-10：`build_windows_dist.cmd` 重建 `dist/MEFinder/文献原句定位器.exe` 与 MCP 侧车，全量 2,745 项通过（26 项跳过，212.176 秒）；前端文件、主程序编译后的错误分类 / 提示和原数据目录指针均核验通过。源码全量 2,745 项通过，真实 Chrome 回归 8 项通过，Ruff(F) 零告警。重建后既有 E5 模型小样本计算再次通过。此轮更新本机开发程序，`release/` 双包未重打。

本机 Python 3.12 / `PYTHONUTF8=1`：源码全量 unittest 2,741 项通过（24 项环境跳过，231.733 秒）；完整独立 Git 检出中，官方安装与便携脚本分别 2,741 项通过（各 42 项环境跳过，231.105 / 218.778 秒）。跟踪 Python 文件 Ruff(F)、独立检出 Ruff、前端守卫和逐文件 Node 语法、空库 FTS5 / 短词索引、17 工具 MCP STDIO、隐私与许可证检查均通过。

包内桌面程序和 MCP 侧车均为 x64 / 0.5.9，桌面程序字节码包含 `.mefinder` 导出后缀；构建源码与已测源码逐文件相等。便携 ZIP CRC、解包侧车初始化版本与 17 工具核验、两个制品实算 SHA-256 与 sidecar 一致。

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-windows-setup.exe` | 82,459,907 | `f42c9c53ed60ffbf36597a45ee4d82e90a4d67b3ba7d0c528c404b20f74a496b` |
| `MEFinder-v0.5.9-windows-portable.zip` | 98,202,784 | `d6d5a98a119ba9d6c5b0cf8f47dff2e36261ab4a9f7fe3fc2f8a13def6a8cf2d` |

两个 `.sha256.txt` 文件同目录。构建与核验日志在 `.codex-tmp/windows-v059-20261006/`。未替换当前安装或开发程序；安装 / 升级 / 卸载未验证，未上传 GitHub Release 或正式发布。未启动额外云端构建。（该轮 macOS 0.5.9 尚未构建，已于同日在 mac 本机补打，见下节。）

## macOS 本地交付

Apple Silicon 本机构建，解释器 `.venv-macos312-arm64`（Python 3.12.10 / SQLite 3.49.1），`PYTHONUTF8=1 MEFINDER_PYTHON=.venv-macos312-arm64/bin/python ./build_macos.sh`：官方构建内置全量 unittest 2,741 项通过（34 项环境跳过，173.676 秒）；仓库外另跑 `ruff check src tests scripts` 零告警。构建脚本自身门禁全过：逐文件 Node 语法；PyInstaller 主应用与 MCP 侧车；17 工具契约的侧车 STDIO 冒烟在构建后 / 签名后 / ZIP 解包 / DMG 挂载 / 拖出副本五个阶段各跑一次；严格 `codesign --verify --deep --strict`；包内含 PyObjC PDFKit 桥与 certifi CA 且不含 `preferences.json` / `mineru_api.local.json` / `desktop.log` 等本机或生成态；ZIP 条目无 AppleDouble `._` 元数据；DMG 含 `/Applications` 快捷方式且 `hdiutil verify` 校验和有效；两个制品的 SHA-256 与同目录 sidecar 一致。

解包反查：`MEFinder` 与 `MEFinderMCP` 的 `lipo -archs` 均为纯 arm64；`CFBundleShortVersionString` 0.5.9、`LSMinimumSystemVersion` 14.0；主程序可执行文件中含 `.mefinder` 新导出后缀。包为 ad-hoc 签名，未公证。

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-macos-arm64.zip` | 96,208,109 | `315b721016365fe24d7751628385f410401bde24d74bee786ab1327f70ab3d55` |
| `MEFinder-v0.5.9-macos-arm64.dmg` | 104,590,707 | `11f03656c2d6696fd08bd4b83a4aec24ceb48c80552eceef1a151614184a72cf` |

两个 `.sha256.txt` 文件同目录，构建日志留在本地忽略的 `.codex-tmp/macos-v059-20261007/build-arm64.log`。本轮未替换本机 `/Applications/MEFinder.app`（仍为 0.5.8），未做安装后的界面人工验收；macOS Intel（x86_64）0.5.9 包按用户决定本轮未打；未创建 `v0.5.9` tag，未上传 GitHub Release。0.5.9 不含数据库 schema 变化，0.5.8 建成的库预期可被 0.5.9 直接打开，本轮未做升级实测。

2026-10-20：v0.5.9 正式发布，可直接使用。GitHub Release [`v0.5.9`](https://github.com/sabercomo/MEFinder/releases/tag/v0.5.9) 已上线，tag 指向 `19fa89e`（产品源码 `9fe9b03`，两者 `src/` 逐文件相同，`19fa89e` 另含 Windows 测试入口的命令行长度修复与文档）；12 个资产全部 uploaded，digest 与本地实算一致。本版本没有新的功能方向，除对齐进度显示一处新增的界面信息外，全部是 0.5.8 之后实际用起来才暴露出来的问题修复；数据库结构沿用 v11 无迁移，MCP 仍是 17 个工具，0.5.8 建的库可直接打开。未做的项：Windows 安装 / 升级 / 卸载自动冒烟与安装后人工验收、反馈用户实机验收、Intel 真机验收（包经 Rosetta 构建）。发布文案同步在 [RELEASE_NOTES](RELEASE_NOTES.md) 的 v0.5.9 条目。

2026-10-20：已按 `9fe9b03` 重新构建包含本地 OCR Windows 448 修复的安装版与便携版，替换 [v0.5.9 GitHub 草稿](https://github.com/sabercomo/MEFinder/releases/tag/untagged-65703827561537269ea3) 中四个 Windows 文件；两次官方构建各通过全量 2,790 项 unittest，制品与远端 SHA-256 核验通过，草稿保持未发布。反馈用户实机仍待验收。

2026-10-20：v0.5.9 macOS arm64 / x86_64 的 DMG、ZIP 及四个 SHA-256 校验文件已上传 [GitHub 草稿](https://github.com/sabercomo/MEFinder/releases/tag/untagged-c88f9533375e8073107e)，保持未发布。两种架构同源于 `9fe9b03`，各通过全量 2,790 项 unittest、官方构建及制品核验；Chrome 13 项、Ruff 通过。Windows 资产在本轮期间由并行发布流程更新为 `e51af5b` 构建，已保留最新资产与说明。

2026-10-20：本地 OCR 安装遇到 Windows Python 版本目录链接错误 448 时，源码现可复用已下载的 Python 自动恢复一次；全量 2,778 项 unittest（30 项环境跳过）、Ruff 及真实 uv 离线验证通过，反馈用户实机仍待验证。此源码核验时安装包尚未重打，现已由下方重新构建记录覆盖。见[核验报告](../reports/local-ocr-windows448-2026-10-20.md)。

2026-10-20：v0.5.9 Windows 安装版与便携版已按功能提交 `75fbeba` 重建，并连同两个 SHA-256 校验文件上传 [GitHub 草稿](https://github.com/sabercomo/MEFinder/releases/tag/untagged-ce1309a4173f2cfcd647)；两次官方构建各通过全量 2,773 项 unittest（48 项环境跳过），Chrome 12 项、Ruff 与制品核验通过。macOS 本轮最新包尚未上传；Windows 安装 / 升级 / 卸载与安装后人工验收仍待完成，尚未正式发布。

2026-10-17：默认首页启动时文献摘要被对齐总览抢先阻塞的时序已修复，全量 2,773 项（30 跳过）、Chrome 12 项及 Ruff / 前端守卫通过；原 `dist/MEFinder/` 界面资源已同步，正常重新打开后生效。现有 release / macOS 包不含此修复，尚未正式发布。见 [核验报告](../reports/library-startup-2026-10-17.md)。

2026-10-15：导入进度因本机查询断连而误报失败、停止刷新的问题已修复，全量 2,771 项（29 跳过）、Chrome 11 项及 Ruff / 前端守卫通过；原 `dist/MEFinder/` 界面资源已同步，正常重启后生效。现有 release / macOS 包不含此修复，尚未正式发布。

2026-10-13：macOS arm64 安装包已按 `42462d1` 重建，包含三项架构整理和 MinerU 取消释放修复；全量、签名、MCP、ZIP / DMG 与源码一致性门禁通过。仅打包，未安装到本机应用，未正式发布。

2026-10-13：目录与搜索页记录查询已收进持久化读取层，并合入最新 MinerU 取消释放修复；最终工作树全量 2,777 项通过（39 跳过）、Chrome 11 项及 Ruff 通过。三项架构整理均为源码交付，本轮未更新安装包或本机应用。见 [核验报告](../reports/outline-search-reads-2026-10-13.md)。

2026-10-13：导入任务队列已收拢到独立前端模块，重复监听和旧响应覆盖已修复；源码全量 2,770 项通过（39 跳过）、Chrome 11 项及 Ruff / JS 守卫通过。安装包与本机应用尚未更新，未正式发布。见 [核验报告](../reports/import-jobs-state-2026-10-13.md)。

2026-10-13：已取消导入的 MinerU 解析任务长期占用账号、新导入一直“等待可用的 MinerU 账号”已修复，全量门禁通过；原 `dist/MEFinder/` 已按 `f63e683` 更新，下次启动自动释放被占账号。现有 release / macOS 包不含此修复，尚未正式发布。

2026-10-13：普通导入和 Zotero 同步共用文献数据刷新入口，旧摘要响应不再覆盖新目录；源码全量 2,760 项通过（38 跳过）、Chrome 10 项及 Ruff / JS 守卫通过。安装包与本机应用尚未更新，未正式发布。见 [核验报告](../reports/library-refresh-2026-10-13.md)。

2026-10-12（夜）：macOS arm64 安装包已按 `2e6ded1` 重建，含重复导入复用原任务与取消状态保留；全量、Chrome、Ruff 与官方脚本制品门禁全过。本机 `/Applications/MEFinder.app` 已与本次构建制品逐文件相同并启动冒烟通过（应用实际服务的新代码），文献库数据根未受影响。尚未正式发布，macOS Intel 与最新 Windows 发布包仍未打。

2026-10-12：重复导入误报与取消任务提前消失已修复，本机 Windows 原 `dist/MEFinder/` 程序已更新，完整门禁、浏览器与制品核验通过；现有安装 / 便携 / macOS 包尚不含本轮修复。

2026-10-12：macOS arm64 安装包已按 `6afed73` 重建，包含作品弹窗、对齐进度与导入刷新更新，全量与浏览器门禁通过，可本机试用；尚未正式发布。

2026-10-10：导入后的译本对照页刷新遗漏已修复，全量门禁通过；原 `dist/MEFinder/` 外置资源已同步，重启即可生效。现有 release / macOS 包不含此修复，尚未正式发布。

2026-10-10：对齐阶段百分比、进度条与当前阶段预计剩余时间可本机试用；已在原 `dist/MEFinder/` 替换为核验后的新版，原启动路径、文献库指针、数据与配置保留。多建的进度目录已移回临时构建区，`dist/` 不再保留它。现有 release / macOS 包不含此功能，尚未正式发布。

2026-10-10：作品弹窗中文输入、下拉宽度和对齐启动失败提示已修复，本机 Windows 开发程序已重建并核验；现有 Windows 双包与 macOS arm64 包为 10-07 构建，尚未正式发布。

2026-10-07：v0.5.9 Windows 与 macOS arm64 包均已本机打包并通过自动化门禁，尚未正式发布；macOS Intel（x86_64）包未打，安装 / 升级 / 卸载与安装后人工验收未做。

2026-10-07：v0.5.9 可本机试用；U 盘文档包弹窗修复、全量门禁及 Windows 安装包 / 便携包构建核验通过，尚未正式发布。

# v0.5.9 迭代说明

- 默认首页先取得文献摘要，再预取对齐总览，避免启动时文献列表等待后台总览；缓存未命中的请求排队对照见 [核验报告](../reports/library-startup-2026-10-17.md)。
- 普通导入与 Zotero 同步共用文献刷新入口，旧摘要响应不再覆盖新目录，见 [核验报告](../reports/library-refresh-2026-10-13.md)。
- 本机导入进度查询中断时保留原任务状态与步骤并自动重试；恢复连接后继续展示真实进度和终态，不再把一次查询失败当作导入失败。取消中断连、普通 404 和真实解析失败保持各自处理，见 [问题记录](issues/import-progress-local-disconnect.md) 与 [核验报告](../reports/import-progress-reconnect-2026-10-15.md)。
- 取消或移除导入时同步取消其 MinerU 解析任务并释放账号并发；启动时清扫没有对应导入任务的未结束解析任务。修复被放弃的导入占满全部账号、后续导入永远停在“等待可用的 MinerU 账号”。见 [问题记录](issues/mineru-orphaned-parser-jobs-hold-credentials.md)。
- 重复上传同一 PDF 时跟踪已有导入任务，保留原解析方式，合并重复队列行；取消中的任务持续显示“正在停止”，后台清理完成后才移除，随后可重新导入。见 [问题记录](issues/import-duplicate-active-task.md) 和 [接口行为](contracts/v0.5.9-import-active-jobs.md)。
- 导入完成后同步失效译本对照页的旧文献目录，修复新版本的语言显示“未识别”、书名退化成内部 ID、出版与页码信息为空；沿用共享请求，不需重新导入或生成对齐。见 [问题记录](issues/works-import-library-refresh.md)。
- 生成对齐时显示当前阶段的真实百分比、进度条和预计剩余时间；样本不足时显示正在估算，缓存完整命中时跳过文本计算，支持刷新后恢复。剩余时间仅指当前阶段，见 [任务进度契约](contracts/v0.5.9-alignment-jobs.md)。
- 新建作品的文献搜索保留活动输入框，修复中文组合输入被中断和中间编辑光标跳到末尾；加入作品的下拉菜单恢复到触发器宽度。
- 对齐计算进程在能力探测阶段退出时，提示重新安装计算组件。本机旧路径启动器已离线修复并完成真实 E5 小样本计算；《异化》整书对齐尚未重跑，见 [问题记录](issues/works-dialog-ime-and-alignment-runtime.md) 和 [核验报告](../reports/works-dialog-runtime-2026-10-10.md)。
- 文献库单篇与批量导出的文档包统一使用 `.mefinder` 后缀，内部仍是原有 ZIP 容器和 `mefinder.document.v1` 数据，不重解析、不改变页码与原 PDF。Windows 不再自动按 ZIP 浏览它及 Mac 创建的同名 `._` 辅助文件。
- 导入页的文件选择、拖入、分块上传与包恢复支持 `.mefinder`，继续支持旧 `.mefinder.zip`；前端忽略 Mac 的 `._` 辅助文件。
- 0.5.8 及更早版本不能直接选择新后缀；需要传回旧版时，可将真正的文档包改名为 `.mefinder.zip`。不要把 `._` 开头的 Mac 辅助文件当作文档包。
- 数据库 schema、MCP 工具与文档包内部契约不变；HTTP 版本快照、MCP 应用版本、Windows 冒烟文件名与前端指纹同步到 0.5.9。
- 实际 U 盘证据与复现测试见 [问题记录](issues/usb-appledouble-zip-dialog.md) 和 [核验报告](../reports/usb-document-package-2026-10-06.md)。先前怀疑强制 ZIP64，经实际文件核验后不采用该修改。

## Windows 本地交付

### 2026-10-20 OCR 448 修复重打与草稿替换

构建源码：`9fe9b0305a87d519f92954e856fadeb3ebf72dce`，版本 0.5.9，包含已下载 Python 因版本目录链接错误 448 而安装失败的恢复修复。

- 安装版官方脚本全量 unittest 2,790 项通过（49 项环境跳过，216.682 秒）。
- 便携版官方脚本全量 unittest 2,790 项通过（49 项环境跳过，215.594 秒）。
- 全部跟踪 Python 的 Ruff(F)、前端守卫、逐文件 Node 语法、主程序与 MCP 打包、空库 / FTS5、MCP STDIO 冒烟、隐私与许可证门禁通过。
- ZIP CRC 通过；解包后 81 个界面资源与 225 份 Python 源文件逐文件相同；桌面程序 219 个、MCP 92 个冻结应用模块与源码编译结果一致，两程序均为 x64 / 0.5.9。安装版 payload 与便携版中的 OCR 冻结模块均核对通过。
- 四个远端资产状态均为 uploaded，字节数与 GitHub digest 和本地 SHA-256 一致；草稿 target 指向构建源码，draft=true、published_at=null。

本轮同时纳入最新合并的导入任务轮询整理与只读查询重构。新合入的 `test_import_and_zotero_refresh_version_metadata_on_visible_and_hidden_pages` 在 Windows 因 Node `-e` 脚本超出命令行长度报 `WinError 206`；已改用 UTF-8 标准输入执行同一脚本，相关 27 项通过，Chrome 13 项通过（43.896 秒）。构建检出中只补入同一测试入口修复，产品源码固定为上述提交。

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-windows-setup.exe` | 82,477,784 | `dff98d985772a08ae55bef983c9e330e6b014f274a81f5434b1341d598dc47c6` |
| `MEFinder-v0.5.9-windows-portable.zip` | 98,217,930 | `3e11ba2ee61468a97482a0001963d86fb58ce9a38b82353d9a25fef6bbc98692` |

两个同名 `.sha256.txt` 文件同目录。旧四文件备份在 `.codex-tmp/latest-release-0.5.9-20261020/previous-artifacts/`；构建、制品和远端核验记录在同一任务目录。构建使用完整独立检出和本机 Python 3.12.14 / PyInstaller 6.21 / Inno 7，原 `dist/MEFinder/` 和真实文献库未修改。未启动额外云端构建。草稿中 macOS arm64 / x86_64 的八个资产及原构建说明已保留，字节数与 digest 未变；Mac 说明记录同为 `9fe9b03`，本轮未在 Windows 上重复核验 Mac 构建。Windows 安装 / 升级 / 卸载与反馈用户实机验收仍待完成，未正式发布。

### 2026-10-20 GitHub 草稿

构建源码：`75fbeba53f6978128cbf30ca8a04fb5b057d8170`，应用版本 0.5.9。安装脚本全量 2,773 项通过（48 跳过，217.439 秒）；便携脚本全量 2,773 项通过（48 跳过，214.715 秒）。Chrome 12 项通过（39.321 秒），全部跟踪 Python 的 Ruff(F)、逐文件 JS 语法、打包后 MCP STDIO 冒烟、空库 / FTS5 与隐私边界检查通过。

便携 ZIP CRC 通过，解包后 80 个界面资源与 225 份 Python 源文件逐文件相同；本版本变更的 15 个冻结模块字节码与源码编译结果一致，包内版本为 0.5.9。四个上传文件的远端 SHA-256 digest 与本地实算一致，资产状态均为 uploaded，草稿保持 draft=true。

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-windows-setup.exe` | 82,465,866 | `f180bd0093a09e580dfb544aa5fd0c4edb80d9062f6667b2a5345af5660eb243` |
| `MEFinder-v0.5.9-windows-portable.zip` | 98,215,243 | `1d71d90471348357a5d168478a333a7e5e7f819afa8ecfe921043f6314ed3204` |

两个同名 `.sha256.txt` 文件同目录。旧的 10-07 Windows 包已备份至本地 `.codex-tmp/release-0.5.9-20261020/previous-artifacts/`；构建、浏览器与制品核验日志在该任务目录。本轮在完整独立检出中构建，原 `dist/MEFinder/` 与用户文献库未改动。现有 macOS arm64 包仍为 10-12 的 `2e6ded1` 构建，不含之后的修复，未混入本次草稿；Intel 最新包同样待补。未启动额外云端构建，未正式发布。

### 此前本地交付记录

2026-10-15（本机进度查询恢复）：源码全量 2,771 项通过（29 跳过，222.450 秒），Chrome 11 项通过（37.559 秒），前端守卫 251 项、导入状态与译本对照专项 34 项、跟踪 Python 的 Ruff(F) 与 JS 语法通过。仅同步原 `dist/MEFinder/_internal/src/me_finder/static/js/80-import.js`，更新后完整 HTML 与已测源码相同（1,521,829 字节 / SHA-256 `80234fb1a7e1964584c76d8e569faa559600a579761649b80faff6a66590ec23`）；两份 EXE、数据指针与程序目录 data/config 共 6 份文件哈希不变。未中断当前程序或后台导入；正常重启后生效。未重打安装 / 便携 / macOS 包。

2026-10-13（孤儿解析任务）：独立检出 `f63e683` 运行官方 `build_windows_dist.cmd`，全量 2,766 项通过（46 跳过，230.671 秒），打包与 MCP 冒烟通过。按哈希仅同步 12 个应用文件到原 `dist/MEFinder/`（两份 EXE、`base_library.zip`、5 份 Python 源、4 份前端 JS），508 份比对一致；保留原 UCRT/MSVC 运行库与 `data_root.txt` / `data/` / `config/`（哈希不变）。在真实账本与导入日志的副本上演练启动清扫：取消 7 个无导入引用的未结束任务（2 个持有远端任务、5 个无远端任务的历史残留），两账号在途回 0，《精神现象学》任务保留。

2026-10-12（重复导入）：初轮源码全量 2,757 项通过（27 跳过，221.275 秒）；同步远端后补充取消轮询异常的可重试性测试，最终官方 `build_windows_dist.cmd` 全量 2,758 项通过（45 跳过，224.261 秒），打包与 MCP 冒烟通过。最终源码专项 99 项、Chrome 既有 9 项及本轮取消交互 1 项、Ruff(F) / JS 语法通过。原路径 `dist/MEFinder/文献原句定位器.exe` 与 MCP 侧车及内部资源已同步，552 份新制品逐文件哈希一致，原 `data_root.txt` / `data/` / `config/` 共 4 文件保持哈希；冻结后端包含修复，包内完整 HTML 与已测源码相等。未重打 `release/` 双包或 macOS 包，见 [核验报告](../reports/import-active-jobs-2026-10-12.md)。

2026-10-10（导入刷新）：最终源码全量 2,751 项通过（27 跳过，236.080 秒），专项前端 55 项、Chrome 9 项与 Ruff(F) / JS 语法通过。仅同步原 `dist/MEFinder/` 的 `80-import.js` 外置资源；包内资源路径装配的完整 HTML 与已测源码相等，EXE / 数据指针 / 程序目录 `data/` 与 `config/` 的 6 份文件原哈希保持。重启程序生效，`release/` 双包未重打，见 [问题与交付记录](issues/works-import-library-refresh.md)。

2026-10-10（对齐进度）：源码 2,750 项通过（27 跳过，227.212 秒）、Chrome 9 项通过（29.791 秒）、跟踪 Python 的 Ruff(F) 零告警。官方 `build_windows_dist.cmd` 在完整独立检出通过 2,750 项（45 跳过，224.745 秒）、Node 语法、主程序 / MCP 打包、FTS5 与 MCP 冒烟。已将核验后的两份 EXE 与 592 份内部文件替换到原 `dist/MEFinder/`，逐文件哈希相同；原数据目录指针、`data/` 和 `config/` 的 4 份文件保持原哈希，原路径 MCP 冒烟通过。多建的进度目录已移回临时构建区，`release/` 双包未重打。见 [核验报告](../reports/alignment-progress-2026-10-10.md)。

2026-10-10：`build_windows_dist.cmd` 重建 `dist/MEFinder/文献原句定位器.exe` 与 MCP 侧车，全量 2,745 项通过（26 项跳过，212.176 秒）；前端文件、主程序编译后的错误分类 / 提示和原数据目录指针均核验通过。源码全量 2,745 项通过，真实 Chrome 回归 8 项通过，Ruff(F) 零告警。重建后既有 E5 模型小样本计算再次通过。此轮更新本机开发程序，`release/` 双包未重打。

本机 Python 3.12 / `PYTHONUTF8=1`：源码全量 unittest 2,741 项通过（24 项环境跳过，231.733 秒）；完整独立 Git 检出中，官方安装与便携脚本分别 2,741 项通过（各 42 项环境跳过，231.105 / 218.778 秒）。跟踪 Python 文件 Ruff(F)、独立检出 Ruff、前端守卫和逐文件 Node 语法、空库 FTS5 / 短词索引、17 工具 MCP STDIO、隐私与许可证检查均通过。

包内桌面程序和 MCP 侧车均为 x64 / 0.5.9，桌面程序字节码包含 `.mefinder` 导出后缀；构建源码与已测源码逐文件相等。便携 ZIP CRC、解包侧车初始化版本与 17 工具核验、两个制品实算 SHA-256 与 sidecar 一致。

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-windows-setup.exe` | 82,459,907 | `f42c9c53ed60ffbf36597a45ee4d82e90a4d67b3ba7d0c528c404b20f74a496b` |
| `MEFinder-v0.5.9-windows-portable.zip` | 98,202,784 | `d6d5a98a119ba9d6c5b0cf8f47dff2e36261ab4a9f7fe3fc2f8a13def6a8cf2d` |

两个 `.sha256.txt` 文件同目录。构建与核验日志在 `.codex-tmp/windows-v059-20261006/`。未替换当前安装或开发程序；安装 / 升级 / 卸载未验证，未上传 GitHub Release 或正式发布。未启动额外云端构建。（该轮 macOS 0.5.9 尚未构建，已于同日在 mac 本机补打，见下节。）

## macOS 本地交付

### 2026-10-20 双架构 GitHub 草稿（当前制品）

构建源码：`9fe9b0305a87d519f92954e856fadeb3ebf72dce`，版本 0.5.9；合并本地三项架构整理与远端 `e51af5b` 的全部更新。ARM 官方构建全量 2,790 项通过（41 跳过，173.076 秒）；Intel 经 Rosetta 构建，全量 2,790 项通过（46 跳过，200.694 秒）。Chrome 13 项通过（43.353 秒），跟踪 Python 的 Ruff(F)、逐文件 Node 语法、五阶段 MCP STDIO、严格签名、ZIP 解包、DMG 挂载与拖出副本检查通过。两包各 307 份源码、静态资源、模板和 manifest 与构建前清单逐文件相同。

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-macos-arm64.dmg` | 104,634,836 | `6273e47f96917177acc013aec6417c67958f7e5482f45d6b6ec74d6db2111d02` |
| `MEFinder-v0.5.9-macos-arm64.zip` | 96,224,144 | `31ca0c89e4c11055e7c62d32e3b2534813b94d816b2d95d45df2a55d9552f478` |
| `MEFinder-v0.5.9-macos-x86_64.dmg` | 108,623,817 | `ec35a731f97f84e1c6e0023f41e21e2458d6c8583e3b243f41ac093ebc836a53` |
| `MEFinder-v0.5.9-macos-x86_64.zip` | 102,759,767 | `4db29659539c3e1d219c12da9824024212bc4b94bdeb644d4ee375623d8c5e17` |

八个 macOS 上传文件（四个安装/归档文件及其校验文件）的 GitHub digest、大小与本地实算一致，状态均为 uploaded。[草稿](https://github.com/sabercomo/MEFinder/releases/tag/untagged-c88f9533375e8073107e) 保持 draft=true，目标提交为 `9fe9b03`，未创建或移动正式 tag。ARM 主应用与侧车均为纯 arm64，最低 macOS 14；Intel 均为纯 x86_64，最低 macOS 12。包为 ad-hoc 签名，未公证；本轮未覆盖本机应用或文献库。Rosetta 运行不替代 Intel 真机验收。

上传期间 Windows 资产由并行流程更新为 `e51af5b` 构建（含 OCR 448 修复）；本轮保留新 Windows 资产和说明，未重新构建 Windows。当前远端 setup / portable 的 SHA-256 分别为 `d816abcab19bd0a40f3b43d0efb265a25cf0a649704b7b422e94765d2d9260ea` / `3630e12b86b2e0762f4717017f08a22aebb47d0a4f02ffa3fb1b961a24a3d294`。跨平台同源与 Windows 安装/升级/卸载、安装后人工验收仍是正式发布前的未完成项。完整证据见 [核验报告](../reports/macos-release-2026-10-20.md)。

### 2026-10-13 架构整理更新包（历史制品，已被 10-20 同名包覆盖）

使用本地 `42462d1` 与官方 `build_macos.sh`，沿用 0.5.9 版本号，重建 Apple Silicon（arm64）DMG / ZIP。包含 `674d5b9`、`dc06729`、`42462d1` 的架构整理，以及合入的 `f63e683` MinerU 取消释放修复。

门禁：脚本内全量 unittest 2,777 项通过 / 39 项环境跳过 / 190.439 秒；应用与阅读器 JS 语法、构建后 / 签名后 / ZIP 解包 / DMG 挂载 / 拖出副本的五阶段 MCP STDIO 冒烟、严格签名、PDFKit / CA / 隐私检查、ZIP 无 AppleDouble、DMG 应用快捷方式与完整性、两个 SHA-256 sidecar 全部通过。Ruff(F) 零告警。同一源码的上一轮 Chrome 11 项已通过（34.438 秒），本轮未重复浏览器测试。

追加解包核对：主应用与 MCP 侧车均为纯 arm64，版本 0.5.9，最低 macOS 14.0；225 个 Python 源文件、79 个静态资源、2 个模板逐文件 SHA-256 与构建前 `42462d1` 清单一致，包含 `78-import-jobs.js`。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-macos-arm64.zip` | 96,223,447 | `000dc20a0ccb25894d62204f3bcf2a456d999ae9c99ddd202dfb58f0551f28de` |
| `MEFinder-v0.5.9-macos-arm64.dmg` | 104,613,646 | `a2b441a7cb356cbd8de38326f0f895d0a21807eb96d9f3278d9d630f252dd6f7` |

覆盖 10-12 的同名包，旧校验值保留在下一节。日志、构建前清单与解包核验结果位于本机 `.codex-tmp/macos-v059-20261013-architecture/`。本轮没有替换 `/Applications/MEFinder.app`、没有中断活动任务、没有修改用户文献库；包采用 ad-hoc 签名，未公证。未制作 Intel / Windows 包，未发布 tag / Release 或更新官网。

### 2026-10-12 夜更新包（历史制品，已被 10-13 同名包覆盖）

从 `7c39966` 快进拉取远端至 `2e6ded1`，沿用 0.5.9 版本号和官方 `build_macos.sh`，仅重建 Apple Silicon（arm64）DMG / ZIP。相比上一轮包，新增重复上传同一 PDF 时复用已有导入任务、取消中任务持续显示“正在停止”并在后台清理完成后移除的修复。

Python 3.12.10 / SQLite 3.49.1，官方构建全量 unittest 2,758 项通过（37 项环境跳过，168.595 秒）；另启用真实 Chrome 回归 9 项通过（27.707 秒），`ruff check . --extend-exclude .codex-tmp` 零告警。逐文件 Node 语法、构建后 / 签名后 / ZIP 解包 / DMG 挂载 / 拖出副本五阶段 MCP STDIO 冒烟、严格 `codesign --verify --deep --strict`、PDFKit / CA / 隐私文件检查、ZIP 无 AppleDouble 条目、DMG 含 `/Applications` 快捷方式与 `hdiutil verify`、两个制品 SHA-256 与 sidecar 复算一致，全部通过。

追加解包核对：两个可执行文件均为纯 arm64，版本 0.5.9、最低 macOS 14.0；包内交付的 225 个 `me_finder` 源文件与 78 个静态资源与 `2e6ded1` 工作树逐文件相同（`diff -rq` 无差异）。

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-macos-arm64.zip` | 96,215,990 | `a8085b5d6849ad8ac2417e430e66e22c86c95d3fb6035e669f59cb72b8cd9fa9` |
| `MEFinder-v0.5.9-macos-arm64.dmg` | 104,571,055 | `6f6eb129bec8b08bec85953f4d4be7ac8528ab0a86fbb7d4878988a49f3e29b6` |

两个同名 `.sha256.txt` 文件同目录，构建与浏览器日志在本地 `.codex-tmp/macos-v059-20261012b/`。远端 CI 已核对：`7c39966` run 38156029907 与 `2e6ded1` run 38267651616 均为 success。

本机应用：`/Applications/MEFinder.app` 的整棵树与上表 ZIP 解包结果 `diff -rq` 无差异，`codesign --verify --deep --strict` 通过；23:25 启动后日志显示数据根 `/Applications/MEFinder/runtime`、索引正常加载，页面装配已含本轮新增的“正在停止后台解析”与 `already_running` 处理分支。需要说明的口径：该目录内文件的写入时间戳为 23:14，与本轮构建窗口重叠，本机没有执行覆盖安装动作，写入者未核实；因此“已部署”这一结论依据的是逐文件一致性与启动冒烟实测，而不是一次由本轮发起的安装。仍未在冻结 GUI 里做导入与取消交互的人工验收；包为 ad-hoc 签名、未公证。未重打 macOS Intel 或 Windows 发布包，未创建 tag 或上传 GitHub Release。

### 2026-10-12 早些时候更新包（历史记录，制品已被本轮覆盖）

从 `79bc362` 快进拉取远端至 `6afed73`，沿用 0.5.9 版本号和官方 `build_macos.sh`，仅重建 Apple Silicon（arm64）DMG / ZIP。包内包含作品弹窗中文输入与菜单宽度修复、对齐启动失败提示、对齐阶段百分比 / 当前阶段预计剩余时间，以及导入后的译本对照版本信息刷新。

Python 3.12.10 / SQLite 3.49.1，官方构建全量 unittest 2,751 项通过（37 项环境跳过，167.930 秒）；另启用真实 Chrome 回归 9 项通过（28.258 秒），`ruff check . --extend-exclude .codex-tmp` 零告警。修复远端 Linux CI 的浮点数精确比较误报：`test_http_progress_estimates_only_observed_stage_and_survives_reload` 的预计秒数改用 `assertAlmostEqual`；固定时钟起点 `6.004` 时旧断言失败（`29.999999999999996 != 30`），修改后通过，产品计算与显示行为保持原样。

官方脚本的逐文件 Node 语法、五阶段 17 工具 MCP STDIO 冒烟、严格签名、PDFKit / CA / 隐私文件检查、ZIP 无 AppleDouble 条目、DMG 挂载 / 拖出副本与校验和门禁全部通过。追加解包核对：两个可执行文件均为纯 arm64，版本 0.5.9、最低 macOS 14.0；225 个 worker 源文件、78 个静态资源与当前源码逐文件相等；7 个改动的产品模块冻结字节码与源码编译结果相等；337 个构建输入文件在构建前后哈希一致。

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-macos-arm64.zip` | 96,211,583 | `c8a7a2ab1a85ae32a25b4e8e58c7ee2aabfee06eee4ee591359d5b8944c0522d` |
| `MEFinder-v0.5.9-macos-arm64.dmg` | 104,598,191 | `d512c11a62a31dd2a097292177737a4ebea93c4e15aea419642614667f60bf52` |

两个同名 `.sha256.txt` 文件同目录，构建与浏览器日志在本地 `.codex-tmp/macos-v059-20261012/`。本轮更新安装文件，未替换 `/Applications/MEFinder.app`（实查版本为 0.5.9，覆盖了下方 10-07 的旧本机状态记录），未启动冻结 GUI 或做安装后人工验收；包为 ad-hoc 签名、未公证。未重打 macOS Intel 或 Windows 发布包，未创建 tag 或上传 GitHub Release。正式发布门禁仍按下方历史记录中的未完成项执行。

### 2026-10-07 首次打包（历史记录，制品已被本轮覆盖）

Apple Silicon 本机构建，解释器 `.venv-macos312-arm64`（Python 3.12.10 / SQLite 3.49.1），`PYTHONUTF8=1 MEFINDER_PYTHON=.venv-macos312-arm64/bin/python ./build_macos.sh`：官方构建内置全量 unittest 2,741 项通过（34 项环境跳过，173.676 秒）；仓库外另跑 `ruff check src tests scripts` 零告警。构建脚本自身门禁全过：逐文件 Node 语法；PyInstaller 主应用与 MCP 侧车；17 工具契约的侧车 STDIO 冒烟在构建后 / 签名后 / ZIP 解包 / DMG 挂载 / 拖出副本五个阶段各跑一次；严格 `codesign --verify --deep --strict`；包内含 PyObjC PDFKit 桥与 certifi CA 且不含 `preferences.json` / `mineru_api.local.json` / `desktop.log` 等本机或生成态；ZIP 条目无 AppleDouble `._` 元数据；DMG 含 `/Applications` 快捷方式且 `hdiutil verify` 校验和有效；两个制品的 SHA-256 与同目录 sidecar 一致。

解包反查：`MEFinder` 与 `MEFinderMCP` 的 `lipo -archs` 均为纯 arm64；`CFBundleShortVersionString` 0.5.9、`LSMinimumSystemVersion` 14.0；主程序可执行文件中含 `.mefinder` 新导出后缀。包为 ad-hoc 签名，未公证。

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-macos-arm64.zip` | 96,208,109 | `315b721016365fe24d7751628385f410401bde24d74bee786ab1327f70ab3d55` |
| `MEFinder-v0.5.9-macos-arm64.dmg` | 104,590,707 | `11f03656c2d6696fd08bd4b83a4aec24ceb48c80552eceef1a151614184a72cf` |

两个 `.sha256.txt` 文件同目录，构建日志留在本地忽略的 `.codex-tmp/macos-v059-20261007/build-arm64.log`。本轮未替换本机 `/Applications/MEFinder.app`（仍为 0.5.8），未做安装后的界面人工验收；macOS Intel（x86_64）0.5.9 包按用户决定本轮未打；未创建 `v0.5.9` tag，未上传 GitHub Release。0.5.9 不含数据库 schema 变化，0.5.8 建成的库预期可被 0.5.9 直接打开，本轮未做升级实测。

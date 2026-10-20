# Windows OCR 448 修复包与 GitHub 草稿替换核验

2026-10-20：已按 `9fe9b03` 重新构建包含本地 OCR Windows 448 修复的安装版与便携版，替换 [v0.5.9 GitHub 草稿](https://github.com/sabercomo/MEFinder/releases/tag/untagged-65703827561537269ea3) 中四个 Windows 文件；两次官方构建各通过全量 2,790 项 unittest，制品与远端 SHA-256 核验通过，草稿保持未发布。反馈用户实机仍待验收。

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

## 可追溯记录

完成前已同步远端 `19a3e18`；相较实际构建源码 `9fe9b03`，该提交仅修改发布记录和同一 Node 标准输入测试入口。逐项对比 `src`、`assets`、`packaging`、`installer`、`tools`、Windows 构建脚本、依赖清单、空配置及许可证均无差异，制品对应当前最新产品源码。Windows 测试入口修复已由该远端提交收录，本轮只追加交付记录。

- `installer-build.log` / `portable-build.log`：两个官方脚本的完整输出。
- `installer-payload-verification.json`：便携构建覆盖临时 dist 前对安装 payload 的 OCR 模块核对。
- `artifact-verification.json`：实算摘要、CRC、源码与冻结模块比对。
- `draft-before.json` / `release-body.md` / `release-verification.json`：原草稿、更新正文与远端四资产核验。
- 上述文件位于 `.codex-tmp/latest-release-0.5.9-20261020/`，不入库。
- 原生 448 的反馈用户实机触发条件仍待核实；此前受控注入与真实 uv 离线验证见 [修复报告](local-ocr-windows448-2026-10-20.md)。

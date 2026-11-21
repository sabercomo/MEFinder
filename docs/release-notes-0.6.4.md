2026-11-22：**v0.6.4 正式发布。** 素材库添加图片页、设置内搜索、解析 API 改版与其他解析 API 连接错误提示已进入包内；macOS arm64 / x86_64 与 Windows 安装版 / 便携版共 12 个附件。

# MEFinder 0.6.4

## 更新内容

### 素材库

- 添加图片页改为双栏工作台：左侧导入与预览，顶部切换「本地图片」和「从 PDF 截取」，右侧编辑说明和标签，底部固定保存。
- 外部 PNG / JPEG（每次一张，最多 25 MB）与库内 PDF 框选；按内容 SHA-256 去重，说明和三级标签与文字摘录共用。
- 已有标签直接点选，搜索框内可就地新建多级标签；未选标签时保存到「未整理」。保存失败保留图片与已填写的内容。
- PDF 截图可直接翻到上一页或下一页。加载期间保留旧页，新页就绪后替换图片，弹窗和操作按钮保持原位。
- 可在 PDF 上自由框选，移动选区或拖动边角。确认时可「使用选区」或「使用整页」。添加页的「裁剪」会重新打开原 PDF 页和上次选区，并保留填写的说明和标签。
- 保存时记录物理页、可核实的印刷页和归一化裁剪坐标，可回到原文。详情图片可点开按原尺寸查看。
- 图片随备份 ZIP 携带，恢复时核验大小、类型与 SHA-256；旧版文字素材备份保留当前图片。
- 素材库页头顶部由 28 px 改为 12 px、底部留 16 px，标题与「添加图片」上移，按钮不再贴住下方内容栏。

### 设置

- 新增设置内搜索：就地显示匹配的原设置项，保留平台条件、分类和更新后的条目；按 Esc 或清空搜索回到原位置。
- 「其他解析 API」改为紧凑列表与 36 px 等高编辑表单，模型列表为空时提示手动填写。
- 检测连接和获取模型遇到对端断开、连接重置或响应截断时，显示具体的网络错误。
- MinerU 账号的「删除」移到对应行，确认框显示账号名并默认聚焦取消。
- 本地 OCR 支持按单引擎保存启停或路径，另一引擎和全局参数保持不变。

### MCP

- 新增只读 `list_material_tags` 与 `read_tag_excerpts`，复用素材快照；工具总数为 19 个（14 只读）。

## 升级须知

- **数据库升级至 v13**：新增 `material_images` / `material_image_tags` 表，仅新增结构，不改写既有数据；升级后的库无法被 0.6.3 及更早版本打开。升级前请彻底退出应用并完整备份数据目录。
- macOS arm64 最低 macOS 14，x86_64 最低 macOS 12；均为 ad-hoc 签名、未公证。Intel 包经 Rosetta 构建，未在 Intel 真机人工验收。
- Windows 安装版与便携版由云端工作流构建，未签名；全平台验收以发布附件核验和云端检查结果为准。

## 发布包

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.4-macos-arm64.dmg` | 104,663,139 | `ac9f16f7b757445243328fda47ddc937f7636428b6b48e55e2b6e28f0f4775b0` |
| `MEFinder-v0.6.4-macos-arm64.zip` | 96,538,150 | `2b3100bcf391cbdad210be14cc10f042e4acabce55bb93b80177bed32a59d4bc` |
| `MEFinder-v0.6.4-macos-x86_64.dmg` | 109,574,124 | `b8968a1515ca9d78d555fe2fa27388113c7430548ce52c66950999ce855c7608` |
| `MEFinder-v0.6.4-macos-x86_64.zip` | 103,073,377 | `5e8910a6322d67587030197832dff721b12866a7bc4b880b466744259366fae4` |
| `MEFinder-v0.6.4-windows-setup.exe` | 82,256,976 | `164bf721c4ebcec76b86a8dc3752a98411f53e221bbed580a816415322a5d1bb` |
| `MEFinder-v0.6.4-windows-portable.zip` | 96,702,847 | `0208d25564759fb84ab752132cf8aedaa53c963fc5b9c3a61237ae1b505a0ff5` |

每个二进制文件均附同名 `.sha256.txt`，GitHub 附件 digest 与 sidecar 中的 SHA-256 逐项一致。

## 验证结果

- 发布源码 `3b1379e`。macOS arm64 由 `build_macos.sh` 构建，全量 unittest 2,917 项通过（52 项按环境跳过），45 项专项、Ruff 与 JavaScript 语法检查通过；DMG、ZIP、版本、架构、签名与 SHA-256 独立核验通过。
- macOS x86_64 在 `arch -x86_64` 的 `.venv-macos12-x86_64` 下用同一脚本构建，全量 2,917 项通过（57 项按环境跳过，286.888 秒）；主程序与 MCP 侧车均为纯 x86_64，版本 0.6.4，严格验签通过。
- Windows 由 [Windows release smoke 44926529796](https://github.com/sabercomo/MEFinder/actions/runs/44926529796) 云构建；安装版与便携版各运行全量 unittest 2,917 项（66 项按环境跳过）。安装、从 v0.4.9 升级、桌面关闭与开启时的 MCP 会话、卸载保留用户数据、便携版移动和进程回收均通过。
- CI [44925437145](https://github.com/sabercomo/MEFinder/actions/runs/44925437145) 的 tests、browser-regression、lint、windows-tests 四项均通过。
- 解包启动后，模拟上游断开和截断时，检测连接和获取模型均返回带原因的错误；恢复上游后两项重试成功，MinerU 完整地址的检测也通过。报告者的 Windows 环境及原上游仍待复测，见 [连接核查记录](issues/windows-mineru-connection-fetch.md)。
- 两个 mac 包内 `openai_compatible.py`、`32-materials.js`、`42-materials.css` 的 SHA-256 与发布源码一致。

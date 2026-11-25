2026-11-25：**0.6.6 开发中，未发布。** macOS arm64 与 Windows 安装版 / 便携版已上传 v0.6.6 草稿；新增可选的本地 PaddleOCR；Windows 实机安装、启动与 Word 粘贴待验证。

# MEFinder 0.6.6

- 桌面后端导入纳入原有的限时后台等待，素材图片用到 PyMuPDF 时才加载；修复后端在原生窗口创建期间就绪时偶尔不跳转首页的问题。启动日志可区分设置、后端和 WebView 阶段。见 [源码侧对照及 Windows 复测方法](../reports/windows-startup-0.6.6-2026-11-23.md)。
- 素材与检索的纯文本复制按公式结构保留上下标、真实分组和单位间距，不针对某个单位名改写整段文本。例如 `⋅O2⁻`、`¹O2`、`22.7 × 10⁻³ min⁻¹`、`50−450 cm⁻¹`；不改原摘录、页码或数据库。见 [同批工科论文复测](../reports/materials-engineering-copy-2026-11-23.md#066-公式结构转写复测)。
- 后续源码在同一公式转写器内支持矩阵与组合重音；同批 690 处已渲染公式的 LaTeX 回退由 95 处降至 14 处。剩余复杂布局和入库误识别不猜测。这项改动已进入本轮 Windows 包；macOS 试用包尚未包含。见[矩阵与重音复测](../reports/materials-engineering-copy-2026-11-23.md#2026-11-24--用同一套-mathml-结构转写矩阵与重音)。
- 素材库和检索的同一公式入口补齐带空格的行内算式、无外括号的续行等式；保留未闭合的美元符号、普通金额、真实矩阵与原页定位，已存摘录不必重新 OCR。该修复已进入本轮 Windows 包；macOS 试用包尚未包含。见[行内公式边界实测](../reports/materials-engineering-copy-2026-11-23.md#修复未闭合的美元符号与带空格的算式)。
- 素材标签按需读取：划词加标签只读取标签路径，搜索只补读本次命中段落里的摘录和标签。原检索结果、定位和完整素材快照不变。见[本地响应大小对照](../reports/materials-scoped-reads-2026-11-23.md)。
- PDF 截图预览的渲染和编码不再占用搜索锁。截图保存仍检查原件版本，且不接受来自另一文献或另一页的预览凭据；操作界面不变。见[并发回归](../reports/materials-pdf-preview-2026-11-23.md)。
- 已发布的 0.6.5 下载与主页不变。macOS arm64 与 Windows 安装版 / 便携版均在本机构建并上传 v0.6.6 草稿；Windows 实机安装、启动与 Word 粘贴待验证。
- macOS 主窗口的最小宽度从 960 点降为 720 点，便于并排使用；最小高度、默认尺寸和 Windows 不变。
- 「设置 → 本地 OCR」新增 PaddleOCR，可按需安装 PP-StructureV3 CPU 组件，或填写已部署的本机／内网版面解析服务。启用后手动选择「本地 PaddleOCR」导入 PDF；不会替换「自动选择」，不含 GPU 版。见 [CPU 与自部署验收](../reports/paddleocr-local-2026-11-24.md)。
- 源码补齐自部署 PaddleOCR-VL 的行内与独立公式分隔符兼容，仍用现有的 `/layout-parsing` 接口。两页扫描件已通过入库与定位，目标频率和 Buck 公式通过 macOS Word 无格式粘贴。复杂重音对照未全部通过，仍只作为手动实验路线；不含模型或新版试用包。见[实测记录](../reports/materials-engineering-copy-2026-11-23.md#2026-11-24--paddleocr-vl-自部署整段对照)。

## 本地测试包

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.6-windows-setup.exe` | 82,470,320 | `7517132f55c4cda892587b99159b99293585fdf09ebd378a665781e90a9fa0a1` |
| `MEFinder-v0.6.6-windows-portable.zip` | 98,347,570 | `4bf4e563e022bed8b841758d934eac57e0c3e222f92654b74e4a91db2d49d04b` |

两个 Windows 文件各附同名 `.sha256.txt`，SHA-256 与 GitHub 草稿附件 digest 一致。macOS arm64 四个附件由另一台机器构建并上传，本轮未重新核验其内容。

## 验证记录

- Windows 安装版与便携版由本机 `build_windows_installer.ps1` / `build_portable_release.ps1` 从 `a5636be` 构建；两次构建内全量 unittest 均 2,943 项通过（57 项按环境跳过），Node 语法、PyInstaller 桌面端与 MCP 侧车、空库 FTS5、侧车 STDIO 冒烟、隐私清单与 SHA-256 均通过。
- 便携 ZIP 631 个条目 CRC 通过；包内 252 份 `me_finder` Python 源码、87 个静态资源与源码逐文件 SHA-256 一致；新增 PaddleOCR 模块在包内；主程序、MCP 侧车与安装包版本均为 0.6.6。
- 安装、升级、卸载与 Windows 冻结 GUI 未在本轮人工验收。

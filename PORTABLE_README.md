# ME_Finder 文献原句定位器（Windows 绿色版）

## 开始使用

1. 将 ZIP **完整解压**到一个可写目录，例如 `D:\MEFinder`。不要直接在压缩包内运行，也不建议放到 `Program Files`。
2. 双击 `文献原句定位器.exe`。程序会在 WebView 启动前自动清除 ZIP 解压后可能遗留的 Windows 下载标记。
3. 首次打开是空文献库；进入“文献导入”添加自己的 Word 或 PDF 文献。

如果仍提示 `Failed to resolve Python.Runtime.Loader.Initialize`，先关闭程序，再双击 `0-首次启动-程序打不开时运行.cmd`。它只会解除本便携包内运行文件的 Windows 阻止状态，然后启动程序。如果提示 `Python.Runtime.dll` 缺失，请删除当前目录、重新完整解压 ZIP，并在“Windows 安全中心 → 病毒和威胁防护 → 保护历史记录”中确认该文件是否被隔离。

浏览器若直接拦截安装版 `.exe`，可改下载本便携版 ZIP。不要关闭整个 SmartScreen 或杀毒软件；发布页提供的 SHA256 可用于核对文件完整性。

本版本无需安装。`portable.flag` 会让程序把索引、文献、设置、日志和 API 配置全部保存在解压目录中。删除整个目录即可移除程序及其本地数据。

## 文献解析与隐私

- 原生文本 Word/PDF 的索引与搜索在本机完成。
- 扫描版或乱码 PDF 只有在你主动确认后才会提交到 MinerU 或所选的其他视觉解析接口。
- MinerU 密钥保存在 `config\mineru_api.local.json`，请勿将填写过密钥的文件分享给他人。
- 其他视觉接口的密钥保存在 `config\vision_api.local.json`。MinerU 失败时默认只提示；只有你在设置中明确开启后，程序才会自动切换到可能收费的接口。
- 填写其他接口的地址和 API Key 后，程序会尝试自动获取模型列表；不支持 `/models` 的接口仍可手动填写模型 ID。
- 发布包不包含作者的个人文献、搜索索引、OCR 结果、偏好设置或 API 密钥。

## 数据目录

- `corpus\`：导入文献及解析结果
- `data\index.sqlite3`：本地搜索索引
- `config\pdf_imports.json`：书目与页码校准信息
- `config\preferences.json`：界面偏好（首次运行后生成）
- `webview-data\`：便携版窗口运行缓存
- `desktop.log`：运行日志

升级前建议先在“设置 → 数据备份”中导出备份。绿色版的数据与程序位于同一目录，请不要直接覆盖或删除旧目录中的 `corpus`、`data` 和 `config`。

## 系统要求

- Windows 10 或 Windows 11
- Microsoft Edge WebView2 Runtime（Windows 10 新版本与 Windows 11 通常已自带）

如果窗口无法打开，请确认已完整解压，并查看同目录下的 `desktop.log`。

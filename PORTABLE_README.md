# ME_Finder 文献原句定位器（Windows 绿色版）

## 开始使用

1. 将 ZIP **完整解压**到一个可写目录，例如 `D:\MEFinder`。不要直接在压缩包内运行，也不建议放到 `Program Files`。
2. 双击 `文献原句定位器.exe`。
3. 首次打开是空文献库；进入“文献导入”添加自己的 Word 或 PDF 文献。

本版本无需安装。`portable.flag` 会让程序把索引、文献、设置、日志和 API 配置全部保存在解压目录中。删除整个目录即可移除程序及其本地数据。

## 文献解析与隐私

- 原生文本 Word/PDF 的索引与搜索在本机完成。
- 扫描版或乱码 PDF 只有在你主动确认后才会提交到所配置的 MinerU 服务。
- MinerU 密钥保存在 `config\mineru_api.local.json`，请勿将填写过密钥的文件分享给他人。
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

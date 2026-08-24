# Windows 安装版构建与发布

> Windows 构建入口与依赖清单仍位于仓库根目录。

Windows 桌面程序沿用 PyInstaller `onedir` 目录结构；独立的 `MEFinderMCP.exe` 以 onefile sidecar 构建，再由 Inno Setup 一并封装。安装程序只安装应用文件；用户索引、导入文献、设置、日志与 API 配置保存在独立的数据目录，升级应用时不会覆盖这些数据。

全新安装时，安装向导会询问该数据目录的位置，默认 `%LOCALAPPDATA%\MEFinder`，可以改到其他磁盘（例如语料库较大、C 盘空间紧张时）。选择结果写入应用根目录的 `data_root.txt`，供后续静默更新读取，避免更新时重新询问或跳回默认位置。已经在使用旧版（无该文件、数据已在默认位置）的安装会被自动识别并跳过询问，继续使用原有位置。

## 构建环境

- Windows 10（1809 或更高版本）或 Windows 11
- 64 位 Python 3.11 或更高版本（建议 Python 3.12 x64），以及 `py` 启动器
- Inno Setup 6.3 或更高版本（也支持 Inno Setup 7；需要命令行编译器 `ISCC.exe`）
- Microsoft Edge WebView2 Runtime（应用运行时需要，Windows 11 和多数较新的 Windows 10 已安装）

安装 Python 依赖：

```powershell
py -3 -m pip install -r requirements-windows.txt
```

若 Inno Setup 不在默认目录或 `PATH` 中，可设置环境变量：

```powershell
$env:ISCC_PATH = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
```

## 构建安装程序

在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows_installer.ps1
```

如果 `py -3` 没有指向正确的 64 位 Python，可显式指定解释器：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows_installer.ps1 -PythonExe "C:\Python312\python.exe"
```

脚本会在构建前验证 Python 至少为 3.11 且为 64 位，避免意外生成旧版 x86 安装包。

仓库还提供手动触发的 `.github/workflows/windows-release-smoke.yml`。工作流进入默认分支后可在 GitHub Actions 页面手工运行；它固定使用 GitHub 托管 `windows-2022` x64 runner，构建安装版与绿色版，校验并安装 v0.4.5 后覆盖到 v0.4.9，在桌面关闭和开启两种状态下分别建立 MCP STDIO 会话，验证卸载保留用户数据，并在移动绿色版目录后再次冒烟。成功运行会上传未签名的四个发布文件。

托管 runner 是 Windows Server 2022，只用于可重复构建与生命周期门禁，不能替代 Windows 10/11 x64 消费者实机的最终 GUI、WebView2、代码签名和 SmartScreen 验收。工作流只允许手动触发，避免普通提交意外生成发布包。

构建脚本从 `src.me_finder.__version__` 读取唯一版本号。通常不要传 `-Version`；若显式传入，值必须与源码版本完全一致，否则构建会停止。脚本会先运行发布相关测试，然后分别构建桌面程序与 MCP sidecar、生成空索引、用真实 STDIO 客户端冒烟、检查私密数据，最后调用 Inno Setup。

输出位于 `release\`：

- `MEFinder-v<version>-windows-setup.exe`
- `MEFinder-v<version>-windows-setup.exe.sha256.txt`

安装目录默认为 `%LOCALAPPDATA%\Programs\MEFinder`，无需管理员权限。安装器额外写入应用根目录的 `installed.flag`；程序仅在发现该标记时启用安装态自更新。直接运行 `dist\MEFinder` 属于开发构建，不会误判为已安装版。应用内更新会安静安装，并在升级完成后重新启动新版本。

Codex 使用的安装版命令固定为 `%LOCALAPPDATA%\Programs\MEFinder\MEFinderMCP.exe`，覆盖升级不会改变路径。卸载会删除 sidecar，但不会修改 Codex 配置；保留的旧配置会明确报告命令不存在，用户再手工执行 `codex mcp remove mefinder`。

## 发布与应用内更新

将安装程序和同名 SHA-256 sidecar 一起上传到 GitHub Release；文件名必须保持上述格式。应用会寻找比当前版本新的 Windows 安装包，下载后先校验 sidecar 中的 SHA-256，再允许安装。自动更新可自动检查和下载，真正安装仍由用户确认。

SHA-256 校验不能替代 Windows 代码签名。对外发布前建议使用可信代码签名证书签署安装程序，并验证签名与安装、覆盖升级、卸载及自动更新流程。

## 发布包的数据边界

安装包只携带以下初始化数据：

- `data\index.sqlite3`：由 `tools.create_empty_index` 新建的空索引
- `config\pdf_imports.json`：来自 `config\pdf_imports.empty.json`
- `config\mineru_api.local.example.json`：仅包含占位符的示例

构建脚本会拒绝包含个人语料库、真实 API 配置、偏好文件、日志、额外数据库或 `portable.flag` 的 payload。不要绕过该检查，也不要直接把本机 `data\`、`corpus\` 或真实 `config\*.local.json` 复制到 `dist\MEFinder`。

绿色版 ZIP 同样包含根目录 `MEFinderMCP.exe`。`portable.flag` 让 sidecar 读取包内 `data\index.sqlite3`；移动整个绿色版目录后必须更新 Codex 中保存的命令绝对路径。

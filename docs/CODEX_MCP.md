# 在 Codex 中使用 MEFinder MCP

更新日期：2026-08-31

适用版本：MEFinder 0.5.1 源码模式与官方发布包

## 当前可用范围

MEFinder MCP v1 是本地 STDIO 只读服务，向 Codex 提供九个工具：

- `list_documents`：按题名、作者、文件名或类型查找已导入文献；
- `locate_quote`：定位原句、近似引文和多个候选；
- `read_document_window`：从命中位置继续读取有界的 PDF 页或 Word 段落窗口；
- `verify_quotes`：一次核对多条引文，逐条返回 `verified`/`approximate`/`not_found`；
- `diff_quote`：把疑似抄错的引文与最接近的原句逐字符对齐，标注增、漏、改；
- `search_passages`：按自然语言描述或关键词按相关性召回可能相关的原文段落（相关性检索，非逐字命中，`relevance.rank` 取用后可转 `locate_quote` 逐字核验）；
- `find_parallel_passages`：输入任一版本的句子，以已生成的版本对齐为中心返回英文、原文或其他译本的多个附近候选、定位和前后文；Codex 必须重新比较语义与上下文，只在证据唯一时确认，否则报告 `ambiguous` 或 `unavailable`；
- `read_bibliographic_pages`：返回书首与书尾的版权页候选文本并标注书目线索，供从原书自身提取题录（不联网）；
- `read_bibliographic_metadata`：返回已存题录字段与 present/invalid/missing 缺口诊断，配合上一个工具补全。

Windows 安装版、Windows 绿色版和 macOS 安装包都包含独立的 `MEFinderMCP` sidecar。不要把桌面应用主程序配置为 MCP 命令。

MEFinder 桌面软件开启或关闭时，源码 MCP 都直接读取同一 SQLite 索引；它不依赖桌面窗口、前端或临时 HTTP 端口。

官方 Codex MCP 配置说明：<https://learn.chatgpt.com/docs/extend/mcp?surface=cli>

## 1. 使用发布包中的 sidecar

Windows 安装版的稳定命令路径是：

```text
%LOCALAPPDATA%\Programs\MEFinder\MEFinderMCP.exe
```

PowerShell 中添加：

```powershell
$mefinderMcp = Join-Path $env:LOCALAPPDATA "Programs\MEFinder\MEFinderMCP.exe"
codex mcp add mefinder -- $mefinderMcp
```

macOS 把应用拖入“应用程序”后，稳定命令路径是：

```text
/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP
```

添加命令：

```bash
codex mcp add mefinder -- /Applications/MEFinder.app/Contents/MacOS/MEFinderMCP
```

Windows 绿色版使用解压目录根部的 `MEFinderMCP.exe`。必须先完整解压，再把该文件的绝对路径交给 `codex mcp add`。移动绿色版目录后，Codex 保存的旧命令不会自动变化，必须先执行 `codex mcp remove mefinder`，再用新绝对路径重新添加。

发布包 sidecar 会自动读取桌面程序当前的数据位置，不需要 `cwd`、`PYTHONPATH` 或 `--runtime-root`。覆盖升级安装版或替换 `/Applications/MEFinder.app` 后，上述命令路径保持不变。

可直接复制的配置表见 [`mefinder-codex-windows-installed.toml`](examples/mefinder-codex-windows-installed.toml) 和 [`mefinder-codex-macos-installed.toml`](examples/mefinder-codex-macos-installed.toml)。

## 2. 准备源码环境

需要 Python 3.10 或更高版本。以下命令只为 MCP 创建独立虚拟环境，不会修改系统 Python。

macOS / Linux：

```bash
cd "/ABSOLUTE/PATH/TO/MEFINDER"
python3 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install "mcp==2.0.0"
```

Windows PowerShell：

```powershell
Set-Location "C:\ABSOLUTE\PATH\TO\MEFINDER"
py -3.12 -m venv .venv-mcp
.\.venv-mcp\Scripts\python.exe -m pip install "mcp==2.0.0"
```

验证解释器能够加载服务：

```bash
.venv-mcp/bin/python -c "import mcp; import src.me_finder.mcp_server; print('MEFinder MCP import OK')"
```

Windows 对应命令：

```powershell
.\.venv-mcp\Scripts\python.exe -c "import mcp; import src.me_finder.mcp_server; print('MEFinder MCP import OK')"
```

命令必须在 MEFinder 源码根目录执行。不要直接运行 `python -m src.me_finder.mcp_server` 做人工检查，因为 STDIO Server 正常启动后会等待 MCP 客户端输入，看起来像“没有反应”。

## 3. 选择源码索引位置

源码服务器默认读取：

```text
<MEFinder 源码根目录>/data/index.sqlite3
```

如果要读取桌面安装版已经建立的索引，在启动参数末尾增加：

```text
--runtime-root <包含 data/index.sqlite3 的运行时目录>
```

常见运行时目录：

- macOS 默认：`/Users/<用户名>/Library/Application Support/MEFinder/runtime`；
- Windows 安装版默认：`%LOCALAPPDATA%\MEFinder\runtime`；
- Windows 绿色版：绿色版解压目录；
- 自定义数据位置：MEFinder 设置中显示的数据目录下的 `runtime` 子目录。

先确认 `<运行时目录>/data/index.sqlite3` 确实存在。路径中包含空格时必须保留为一个完整参数。

## 4. 使用源码模式的 Codex CLI 添加

下面的命令会修改当前用户的 Codex MCP 配置。确认绝对路径后再执行；MEFinder 不会替用户静默执行它。

macOS / Linux：

```bash
mefinder_project_root="/ABSOLUTE/PATH/TO/MEFINDER"
codex mcp add mefinder \
  --env "PYTHONPATH=${mefinder_project_root}" \
  -- "${mefinder_project_root}/.venv-mcp/bin/python" \
  -m src.me_finder.mcp_server \
  --runtime-root "/ABSOLUTE/PATH/TO/MEFINDER/RUNTIME"
```

Windows PowerShell：

```powershell
$mefinderProjectRoot = "C:\ABSOLUTE\PATH\TO\MEFINDER"
$mefinderRuntimeRoot = "C:\ABSOLUTE\PATH\TO\MEFINDER-RUNTIME"
codex mcp add mefinder `
  --env "PYTHONPATH=$mefinderProjectRoot" `
  -- "$mefinderProjectRoot\.venv-mcp\Scripts\python.exe" `
  -m src.me_finder.mcp_server `
  --runtime-root "$mefinderRuntimeRoot"
```

如果使用源码目录自身的 `data/index.sqlite3`，删除命令末尾的 `--runtime-root` 及其路径。

添加后检查保存的命令：

```bash
codex mcp get mefinder --json
codex mcp list
```

## 5. 使用 `config.toml` 添加源码模式

Codex 默认读取 `~/.codex/config.toml`；受信任项目也可以使用项目内 `.codex/config.toml`。ChatGPT/Codex 桌面端、Codex CLI 和 IDE 扩展在同一 Codex 主机上共享这份配置。

复制示例文件 [`docs/examples/mefinder-codex-source.toml`](examples/mefinder-codex-source.toml) 中的表，并替换所有 `/ABSOLUTE/PATH/...` 占位符：

```toml
[mcp_servers.mefinder]
command = "/ABSOLUTE/PATH/TO/MEFINDER/.venv-mcp/bin/python"
args = [
  "-m",
  "src.me_finder.mcp_server",
  "--runtime-root",
  "/ABSOLUTE/PATH/TO/MEFINDER/RUNTIME",
]
cwd = "/ABSOLUTE/PATH/TO/MEFINDER"
startup_timeout_sec = 10
tool_timeout_sec = 60
```

使用源码目录自身索引时，从 `args` 中删除最后两个元素。不要同时保留旧的同名 `[mcp_servers.mefinder]` 表。

## 6. 在桌面端或 IDE 扩展添加

按照 Codex 客户端当前界面操作：

1. 打开“设置”，选择“MCP servers”；
2. 选择“Add server”；
3. 名称填写 `mefinder`，类型选择 `STDIO`；
4. 发布包填写上一节的 `MEFinderMCP` 绝对路径且不添加参数；源码模式填写虚拟环境 Python 的绝对路径；
5. 只有源码模式才依次填写 `-m`、`src.me_finder.mcp_server`，需要指定索引时再填写 `--runtime-root` 和运行时目录；
6. 只有源码模式才需要把工作目录或 `PYTHONPATH` 指向 MEFinder 源码根目录；
7. 保存并按界面提示重启桌面端或 IDE 扩展。

如果当前客户端没有工作目录或环境变量输入项，改用 CLI 或 `config.toml`，不要依赖客户端碰巧从源码目录启动。

## 7. 健康检查

配置后重新启动正在使用的本地 Codex 客户端或新建一个 Codex 会话。

1. 运行 `codex mcp list`，应看到启用的 `mefinder`；
2. 运行 `codex mcp get mefinder --json`，核对命令、参数和工作目录；
3. 在 Codex TUI 或桌面端输入 `/mcp`，应看到服务器已连接；
4. 工具列表应包含本页“当前可用范围”列出的九个只读工具；
5. 先在 MEFinder 中导入至少一篇文献，再询问：“只使用 MEFinder 核对这句话出自哪篇文献、哪一页。”

建议继续检查以下自然语言任务：

- “只在《指定文献》中查这句话。”
- “这句话可能有两个错字，找最接近的原文。”
- “把命中位置前后再读几段，然后判断上下文。”
- “查出这句中文对应的英文原句；比较全部候选和前后文，不能唯一确定时不要硬选。”
- “如果只有 PDF 物理页、没有书内页码，请明确说明。”
- “有多个来源时不要替我猜，列出全部候选。”

合格回答必须满足：未校准页不称为正式引用页；多个候选不隐藏；无结果不编造；题名、原文、上下文和页码可以追溯到工具结构化输出。

0.4.4 源码模式的真实 Codex 验收结果见 [`mcp-v1-codex-e2e-report.md`](mcp-v1-codex-e2e-report.md)。

## 8. 隐私和只读边界

- MEFinder MCP Server 本身不访问网络，只读取当前本地 SQLite 索引；
- 九个 v1 工具均为只读，不导入、删除、写回校准结果或修改题录；
- MCP 不返回本地绝对文件路径、API Token、内部页哈希或无关设置；
- Codex 调用工具后，查询、命中原文、上下文、题录和页码证据会进入 Codex 对话及模型上下文，并受用户所使用的 Codex/OpenAI 产品数据控制约束；
- 运行本地 MCP Server 不需要 OpenAI API Key，但使用 Codex 本身仍需要对应的 Codex 登录和权限；
- MCP 配置保存在 Codex 客户端一侧，MEFinder 不读取、创建或删除用户的 Codex 配置。

## 9. 故障排查

### `codex mcp list` 中没有 `mefinder`

- 确认执行 `codex mcp add` 的用户与当前 Codex 客户端是同一系统用户；
- 检查 `~/.codex/config.toml` 或受信任项目的 `.codex/config.toml`；
- 保存配置后重启当前桌面端/IDE 扩展，或新建 CLI 会话；
- 工作区管理员策略可能禁用第三方 MCP，请联系管理员确认。

### 服务器启动失败或 `/mcp` 显示错误

在终端中先运行“准备源码环境”一节的 import 检查。然后核对：

- `command` 是虚拟环境 Python 的绝对路径；
- Python 版本不低于 3.10；
- 已安装 `mcp==2.0.0`；
- `cwd` 或 `PYTHONPATH` 指向 MEFinder 源码根目录；
- 参数没有把含空格的路径拆成多个值；
- 没有把桌面应用主程序误当成 `MEFinderMCP`；
- 发布包路径仍然存在，且绿色版没有在配置后被移动。

卸载 Windows 安装版、删除绿色版目录或删除 macOS 应用后，如果 Codex 仍保留旧配置，会得到明确的 `command not found` / “命令不存在”启动错误。此时按“禁用或移除”一节删除旧配置；卸载程序不会静默修改 Codex 配置。

如果 `pip install` 报 TLS 证书链错误，应修复 Python/操作系统证书或使用组织提供的受信任 CA bundle。不要使用 `--trusted-host`、关闭证书校验或改用 HTTP 来绕过错误。

### 返回 `index_not_found`

服务器已经启动，但 `<runtime-root>/data/index.sqlite3` 不存在。检查 `--runtime-root` 是否多写或少写了 `runtime` 层级；源码自身索引则检查 `<源码根>/data/index.sqlite3`。

### 返回 `index_unavailable`

索引可能正在重建、替换或暂时被系统锁定。等待当前导入/迁移完成后重试；不要复制正在写入的 SQLite 文件替代正常索引流程。

### 查到物理页但没有正式引用页

这是正常证据状态，不是连接失败。`physical_page` 只表示 PDF 文件位置；只有 `citation_page.status=calibrated` 的 PDF 页或 `verified` 的 Word 页才能作为已确认引用页。请先在 MEFinder 中完成页码校准。

### 工具调用超时

Codex 默认启动超时为 10 秒、工具超时为 60 秒。本项目示例沿用默认值。先确认索引没有正在替换，并用较短引文、较小候选数复测；不要用增大超时掩盖持续故障。

## 10. 禁用或移除

临时禁用可以在 Codex 设置的 MCP server 列表中关闭 `mefinder`，或在配置表中加入：

```toml
enabled = false
```

通过 CLI 移除：

```bash
codex mcp remove mefinder
codex mcp list
```

移除后重启当前 Codex 客户端或新建会话。此操作只删除 Codex 侧的启动配置，不删除 MEFinder 文献、索引、设置或桌面功能。

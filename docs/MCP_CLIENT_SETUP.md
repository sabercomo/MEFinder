# 在 Codex、Claude Code 和 WorkBuddy 中使用 MEFinder MCP

更新日期：2026-08-15

适用版本：MEFinder 0.4.4 Windows 安装版、Windows 绿色版与 macOS 发布包

MEFinder MCP 是随发布包提供的本地 STDIO 只读服务。它提供三个工具：

- `list_documents`：列出或筛选已导入文献；
- `locate_quote`：定位原句、近似引文和多个候选；
- `read_document_window`：继续读取命中位置附近的有界上下文。

发布包中的 `MEFinderMCP` 是独立 sidecar，不需要安装 Python，MEFinder 桌面窗口也不必保持开启。不要把桌面主程序配置成 MCP 命令。

## 接入前准备

1. 先在 MEFinder 中导入至少一篇 PDF 或 Word 文献；
2. 安装并登录准备使用的 Codex、Claude Code 或 WorkBuddy；
3. 确认 sidecar 文件存在；
4. 如果客户端已经有名为 `mefinder` 的旧配置，先移除旧配置，再重新添加。

Codex 的 MCP 配置格式、客户端重启和 `/mcp` 检查方式以 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) 为准。

## Windows

### 1. 确认 sidecar 路径

安装版使用固定路径：

```powershell
$mefinder = Join-Path $env:LOCALAPPDATA "Programs\MEFinder\MEFinderMCP.exe"
if (-not (Test-Path -LiteralPath $mefinder -PathType Leaf)) {
  throw "没有找到 MEFinderMCP.exe：$mefinder"
}
```

绿色版必须先完整解压。进入解压目录后执行：

```powershell
$mefinder = (Resolve-Path ".\MEFinderMCP.exe").Path
```

下面三种客户端都使用这个 `$mefinder` 绝对路径。移动绿色版目录后，旧路径不会自动更新，需要移除并重新添加。

### 2. Codex

添加：

```powershell
codex mcp add mefinder -- "$mefinder"
```

验证：

```powershell
codex mcp list
codex mcp get mefinder --json
```

重启 Codex 桌面端或 IDE 扩展，CLI 则新建会话。在客户端输入 `/mcp`，应看到 `mefinder` 已连接，并且只提供 `list_documents`、`locate_quote`、`read_document_window` 三个工具。

移除：

```powershell
codex mcp remove mefinder
```

### 3. Claude Code

添加为当前用户配置：

```powershell
claude mcp add --scope user mefinder -- "$mefinder"
```

验证：

```powershell
claude mcp list
```

新建 Claude Code 会话后，让它只使用 MEFinder 查询一条已经导入的引文。如果列表显示服务器未连接，先核对 `$mefinder` 指向的文件是否仍然存在。

移除：

```powershell
claude mcp remove --scope user mefinder
```

### 4. WorkBuddy

1. 打开侧边栏“插件 → MCP 服务器 → 配置 MCP”；
2. 使用该入口打开 WorkBuddy 当前实际读取的 MCP 配置文件；
3. 在 `mcpServers` 中加入 `mefinder`，把 JSON 字段 `command` 的值换成 `MEFinderMCP.exe` 的完整路径；这里不是让你在 Windows 终端执行一条叫作 `command` 的命令；
4. 保存后重启 WorkBuddy 或刷新 MCP 服务器列表；
5. `mefinder` 状态显示绿色后，再新建会话验证。

部分 WorkBuddy 版本使用 `%USERPROFILE%\.workbuddy\mcp.json`，但不同版本的入口或保存位置可能变化，以 WorkBuddy 界面实际打开的配置文件为准。安装版示例：

```json
{
  "mcpServers": {
    "mefinder": {
      "command": "C:\\Users\\<你的用户名>\\AppData\\Local\\Programs\\MEFinder\\MEFinderMCP.exe",
      "args": []
    }
  }
}
```

绿色版需要把 `command` 改成解压目录中 `MEFinderMCP.exe` 的绝对路径。移除时删除 `mcpServers.mefinder` 整项，然后重启或刷新 WorkBuddy。

## macOS

### 1. 确认 sidecar 路径

先把 `MEFinder.app` 从 DMG 拖入“应用程序”。sidecar 的稳定路径是：

```bash
mefinder="/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP"
test -x "$mefinder" || { echo "没有找到可执行的 MEFinderMCP：$mefinder"; exit 1; }
```

如果没有把应用放进 `/Applications`，下面的固定路径不会成立。建议先完成安装再配置客户端。

### 2. Codex

添加：

```bash
codex mcp add mefinder -- "$mefinder"
```

验证：

```bash
codex mcp list
codex mcp get mefinder --json
```

重启 Codex 桌面端或 IDE 扩展，CLI 则新建会话。在客户端输入 `/mcp`，确认 `mefinder` 已连接且只包含三个只读工具。

移除：

```bash
codex mcp remove mefinder
```

### 3. Claude Code

添加为当前用户配置：

```bash
claude mcp add --scope user mefinder -- "$mefinder"
```

验证：

```bash
claude mcp list
```

新建 Claude Code 会话后，再用已导入文献中的引文做一次查询。

移除：

```bash
claude mcp remove --scope user mefinder
```

### 4. WorkBuddy

从“插件 → MCP 服务器 → 配置 MCP”打开 WorkBuddy 当前使用的配置文件，在 `mcpServers` 中加入：

```json
{
  "mcpServers": {
    "mefinder": {
      "command": "/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP",
      "args": []
    }
  }
}
```

部分 WorkBuddy 版本使用 `~/.workbuddy/mcp.json`，但应以界面实际打开的位置为准。保存后重启 WorkBuddy 或刷新服务器列表；状态变绿后再新建会话。移除时删除 `mcpServers.mefinder` 整项。

## 统一验收方法

三个客户端都可以用下面的顺序验收：

1. 确认服务器列表中 `mefinder` 已连接；
2. 确认工具只有 `list_documents`、`locate_quote`、`read_document_window`；
3. 询问：“只使用 MEFinder 核对这句话出自哪篇文献、哪一页。”；
4. 再测试一条有少量错字的引文，确认能返回近似候选；
5. 让客户端继续读取命中位置前后几段，确认上下文来自同一文献；
6. 没有校准书内页码时，回答应明确区分 PDF 物理页和正式引用页。

如果客户端没有调用 MEFinder，先明确要求“只使用 MEFinder”，不要把模型凭自身知识给出的答案当成 MCP 验收结果。

## 常见问题

### 命令不存在

`codex` 或 `claude` 命令不存在，说明对应 CLI 没有安装或不在当前终端的 `PATH` 中。这与 MEFinder sidecar 是否存在是两个问题。

### 服务器启动失败

- 检查配置中的 `command` 是否是 sidecar 的绝对路径；
- Windows 安装版不要把路径写成绿色版目录，绿色版也不要照抄安装版路径；
- macOS 确认应用已经放进 `/Applications`，并且 `MEFinderMCP` 可执行；
- 不要把“文献原句定位器.exe”或 `MEFinder.app` 本身当成 MCP 命令；
- 移动绿色版或 macOS 应用后，移除旧配置并用新路径重新添加。

### 已连接但没有文献

先确认 MEFinder 中已经完成至少一篇文献的导入和索引。发布包 sidecar 会自动读取桌面程序当前的数据位置，不需要额外填写 `cwd`、`PYTHONPATH` 或 `--runtime-root`。

### 返回 `index_unavailable`

索引可能正在重建、替换或暂时被占用。等待当前导入或迁移完成后重试。

## 隐私和只读边界

- MEFinder MCP 进程本身不访问网络，只读取本地 SQLite 索引；
- 三个工具均为只读，不导入、删除、校准或修改题录；
- 查询、命中原文、上下文、题录和页码证据会进入所用 AI 客户端的对话及模型上下文，并受相应服务商的数据控制和隐私条款约束；
- 涉及未公开、敏感或受限文献时，应先确认所用客户端和账号的数据策略；
- 运行 MEFinder MCP 本身不需要 OpenAI、Anthropic 或其他模型 API Key，但使用对应 AI 客户端仍需要其正常登录和权限。

## 源码模式

本文只介绍发布包 sidecar。需要从源码启动、指定 `--runtime-root` 或配置 Codex 桌面端/IDE 的高级选项时，请阅读 [`CODEX_MCP.md`](CODEX_MCP.md)。

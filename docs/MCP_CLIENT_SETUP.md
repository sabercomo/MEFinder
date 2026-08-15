# MEFinder MCP 配置教程

MEFinder 提供本地 MCP 服务，可以连接到 Codex、Claude Code、WorkBuddy 等支持 MCP 的 AI 工具。配置完成后，AI 就可以直接调用 MEFinder 的文献列表、原句定位和上下文读取功能。

下面先完整介绍 Windows 配置，macOS 配置见文末。

## 一、配置前准备

首先找到 MEFinder 目录中的：

```text
MEFinderMCP.exe
```

Windows 安装版通常位于：

```text
C:\Users\<你的用户名>\AppData\Local\Programs\MEFinder\MEFinderMCP.exe
```

Windows 绿色版位于解压目录，例如：

```text
D:\MEFinder\MEFinderMCP.exe
```

请以你自己电脑上的实际路径为准。绿色版可以在解压目录打开 PowerShell，用下面这条命令取得完整路径：

```powershell
(Resolve-Path ".\MEFinderMCP.exe").Path
```

需要选择的是 `MEFinderMCP.exe`，不是桌面主程序 `文献原句定位器.exe` 或 `MEFinder.exe`。

MEFinder MCP 走 **STDIO** 协议，不用手动双击运行 `MEFinderMCP.exe`，Codex、Claude Code 或 WorkBuddy 会在需要时自动启动它。桌面版 MEFinder 可以保持关闭。

## 二、Codex 配置

Codex 支持在设置界面直接添加 STDIO 类型的 MCP 服务器（OpenAI 官方说明见文末链接），填好服务器名称、类型和启动命令即可。

### 1. 打开 MCP 设置

依次点击：

**设置 → 插件 → MCP → 添加 → 添加 MCP 服务器**

如果版本中的文字略有不同，找到“设置”里的“MCP 服务器”和“添加服务器”即可。

### 2. 填写配置

| 项目 | 填写内容 |
| --- | --- |
| 名称 | `mefinder` |
| 类型 | `STDIO` |
| 启动命令 | `MEFinderMCP.exe` 的完整路径 |
| 参数 | 留空 |
| 环境变量 | 留空 |
| 环境变量传递 | 留空 |
| 工作目录 | `MEFinderMCP.exe` 所在文件夹 |

例如，如果文件位于：

```text
D:\MEFinder\MEFinderMCP.exe
```

那么填写：

**名称**

```text
mefinder
```

**类型**

```text
STDIO
```

**启动命令**

```text
D:\MEFinder\MEFinderMCP.exe
```

**参数、环境变量、环境变量传递**

全部留空。

**工作目录**

```text
D:\MEFinder
```

填写完成后点击“保存”，再点击“重启”或重新加载 MCP。

### 3. 检查是否连接成功

回到：

**设置 → 插件 → MCP**

如果能看到 `mefinder` 且处于启用状态，说明已经添加。也可以在对话输入 `/mcp` 查看已连接的服务器。

然后测试：

```text
请只使用 mefinder 搜索这句话来自哪篇文献、哪一页。
```

或者：

```text
请查看 mefinder MCP 提供了哪些工具。
```

### 4. 也可以用 Codex CLI

习惯用命令行的话，在 PowerShell 里运行：

```powershell
codex mcp add mefinder -- "D:\MEFinder\MEFinderMCP.exe"
codex mcp list
```

## 三、Claude Code 配置

打开 PowerShell（按 `Win + R` 输入 `powershell` 回车），粘贴下面这条命令：

```powershell
claude mcp add --transport stdio --scope user mefinder -- "D:\MEFinder\MEFinderMCP.exe"
```

命令里的路径换成你自己的。

- `--transport stdio`：这是本地 STDIO MCP；
- `--scope user`：添加到当前用户，以后打开其他项目也能使用；
- `mefinder`：服务器名称；
- `--` 后面的完整路径：Claude Code 要启动的 `MEFinderMCP.exe`。

检查配置：

```powershell
claude mcp list
```

看到 `mefinder` 已连接后，进入 Claude Code 输入 `/mcp` 再确认一次，然后测试：

```text
请只使用 mefinder 搜索这段引文。
```

## 四、WorkBuddy 配置

进入 WorkBuddy 后，打开：

**插件 → MCP 服务器 → 配置 MCP**

WorkBuddy 会打开它正在用的配置文件，在 `mcpServers` 里加上：

```json
{
  "mcpServers": {
    "mefinder": {
      "type": "stdio",
      "command": "D:\\MEFinder\\MEFinderMCP.exe",
      "args": []
    }
  }
}
```

`command` 里的路径换成你电脑上 `MEFinderMCP.exe` 的实际位置。

Windows 普通路径写成：

```text
D:\MEFinder\MEFinderMCP.exe
```

但在 JSON 字符串中，每个反斜杠需要写两次：

```text
D:\\MEFinder\\MEFinderMCP.exe
```

## 五、最常见的配置错误

1. **启动程序选错。** 必须指向 `MEFinderMCP.exe`，不要填写桌面主程序。
2. **文件路径已经变化。** 移动绿色版、重新安装或更新后，需要重新确认 `MEFinderMCP.exe` 的位置。
3. **Codex 类型选错。** 应选择 `STDIO`，不要选择“流式 HTTP”。
4. **填写了不需要的内容。** 参数、环境变量和环境变量传递默认都留空。
5. **手动双击 MCP 程序。** `MEFinderMCP.exe` 是供 AI 客户端启动的服务程序，一般不需要作为普通软件运行。
6. **文献库还是空的。** 先在 MEFinder 中完成至少一篇文献的导入和索引，再测试搜索。

## 六、Windows 快速配置表

| 软件 | 配置方式 |
| --- | --- |
| Codex | 设置 → 插件 → MCP → 添加 → 添加 MCP 服务器 |
| Claude Code | `claude mcp add --transport stdio --scope user mefinder -- "MEFinderMCP.exe 的完整路径"` |
| WorkBuddy | 插件 → MCP 服务器 → 配置 MCP → 添加 STDIO JSON 配置 |

三种客户端的核心配置相同：

```text
名称：mefinder
类型：STDIO
启动程序：MEFinderMCP.exe
参数：无
环境变量：无
```

## 七、macOS 配置

先把 `MEFinder.app` 从 DMG 拖进“应用程序”。MCP 程序位于：

```text
/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP
```

按 `Command（⌘）+ 空格` 搜索“终端”并打开，再运行命令。

### Codex

在 Codex 的“设置 → 插件 → MCP → 添加”中填写：

| 项目 | 填写内容 |
| --- | --- |
| 名称 | `mefinder` |
| 类型 | `STDIO` |
| 启动命令 | `/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP` |
| 参数、环境变量 | 留空 |
| 工作目录 | `/Applications/MEFinder.app/Contents/MacOS` |

也可以在终端运行：

```bash
codex mcp add mefinder -- /Applications/MEFinder.app/Contents/MacOS/MEFinderMCP
codex mcp list
```

### Claude Code

```bash
claude mcp add --transport stdio --scope user mefinder -- /Applications/MEFinder.app/Contents/MacOS/MEFinderMCP
claude mcp list
```

### WorkBuddy

打开“插件 → MCP 服务器 → 配置 MCP”，在 WorkBuddy 打开的配置文件里找到 `command`，把后面的路径改成 `/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP`，其余不用动。不用管文件保存在哪。

## 八、配置成功后能做什么

MEFinder MCP 提供三个只读工具：

- `list_documents`：列出或筛选已导入文献；
- `locate_quote`：定位原句、近似引文和多个候选；
- `read_document_window`：读取命中位置附近的上下文。

测试时可以直接说：

```text
请只使用 mefinder 核对这句话出自哪篇文献、哪一页，并继续读取命中位置前后的上下文。
```

MEFinder MCP 本身不访问网络，但客户端调用工具后，返回的命中原文和上下文会进入对应 AI 客户端的对话及模型上下文。涉及未公开文献时请留意。

Codex 的界面与 STDIO 配置说明见 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。源码模式、数据位置和高级排错见 [Codex MCP 高级指南](CODEX_MCP.md)。

## 九、以后想解除配置怎么办

上面的配置步骤不需要运行任何“移除”命令。下面这些只在以后不想用 mefinder 时才需要执行，日常使用请忽略。

Windows（PowerShell）：

```powershell
codex mcp remove mefinder
claude mcp remove --scope user mefinder
```

macOS（终端）：

```bash
codex mcp remove mefinder
claude mcp remove --scope user mefinder
```

WorkBuddy 回到“插件 → MCP 服务器 → 配置 MCP”，把之前加进去的 mefinder 配置删掉即可。

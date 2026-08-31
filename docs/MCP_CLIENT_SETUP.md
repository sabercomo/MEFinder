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

Claude Code 用一条命令完成配置，不需要手动编辑配置文件。

### 1. 确认 claude 命令可用

打开 PowerShell（按 `Win + R` 输入 `powershell` 回车），运行：

```powershell
claude --version
```

正常情况下会显示版本号，例如 `claude 1.x.x`。如果提示"找不到命令"，请确认 Claude Code 已经安装，或重新打开一个新的 PowerShell 窗口再试。

### 2. 添加 MCP 服务器

粘贴下面这条命令，把路径换成你自己的：

```powershell
claude mcp add --transport stdio --scope user mefinder -- "D:\MEFinder\MEFinderMCP.exe"
```

- `--transport stdio`：MEFinder 使用本地 STDIO MCP；
- `--scope user`：配置对当前用户的所有 Claude Code 项目生效；
- `mefinder`：服务器名称，可以自定义；
- `--` 后面是 `MEFinderMCP.exe` 的完整路径。

### 3. 确认配置已保存

```powershell
claude mcp list
```

输出里能看到 `mefinder` 就说明已经添加。

### 4. 在 Claude Code 里确认连接

重新打开 Claude Code（或新建一个会话），在对话框输入：

```text
/mcp
```

弹出的列表里能看到 `mefinder`，说明 Claude Code 已经识别到这个 MCP 服务器。

### 5. 测试

先让 Claude 列出工具：

```text
请查看 mefinder MCP 提供了哪些工具。
```

正常情况下会列出九个只读工具：

```text
list_documents
locate_quote
read_document_window
verify_quotes
diff_quote
search_passages
find_parallel_passages
read_bibliographic_pages
read_bibliographic_metadata
```

再确认能读取你自己的文献库：

```text
请只使用 mefinder，列出当前已经导入的文献。
```

如果能返回 MEFinder 里已经索引的文献，说明连接完全正常。

然后可以测试原句定位：

```text
请只使用 mefinder 定位下面这句话，告诉我它来自哪篇文献、哪一页：

<把原句粘贴到这里>
```

找到命中以后继续：

```text
继续使用 mefinder，读取刚才命中位置前后的上下文。
```

这样可以依次测试 `list_documents → locate_quote → read_document_window` 三个核心工具。

### 也可以直接编辑配置文件

如果不习惯用命令行，也可以手动编辑配置文件，效果完全一样。

配置文件位于：

```text
C:\Users\<你的用户名>\.claude\settings.json
```

用文本编辑器打开，在其中加入（或新建）`mcpServers` 字段：

```json
{
  "mcpServers": {
    "mefinder": {
      "command": "D:\\MEFinder\\MEFinderMCP.exe",
      "args": []
    }
  }
}
```

如果文件里已经有其他内容，只需把 `mcpServers` 块加进去，不要覆盖已有字段。Windows 路径中的反斜杠在 JSON 里必须写成两个：

```text
D:\MEFinder\MEFinderMCP.exe  →  D:\\MEFinder\\MEFinderMCP.exe
```

保存后重新打开 Claude Code 即可生效。

## 四、WorkBuddy 配置

WorkBuddy 当前可以从“连接器”界面进入自定义 MCP 配置。下面按三步完成配置。

> 你在网上看到的教程图通常以 **Streamable HTTP** MCP 为例。MEFinder 不使用 HTTP，而是本地 **STDIO** MCP，所以前面的入口操作完全一样，最后粘贴的配置内容换成 MEFinder 的本地启动配置即可。

### 第一步：进入自定义 MCP 配置

打开 WorkBuddy，在首页任务输入框附近点击 **连应用**，然后在弹出的应用列表底部点击 **更多连接器**。

进入“连接器”页面后，点击右上角的 **自定义连接器**，进入 **MCP 服务管理**，再点击右上角的 **配置 MCP**。

如果首页没有看到“连应用”，也可以直接从左侧导航栏进入 **连接器**。两种方式最终都会进入同一个“连接器”页面。

### 第二步：粘贴 MEFinder 配置

Windows 安装版和绿色版的配置方法完全一样，区别只有 `MEFinderMCP.exe` 的实际路径不同。没有其他 MCP 时，直接粘贴下面的配置：

```json
{
  "mcpServers": {
    "mefinder": {
      "type": "stdio",
      "command": "MEFinderMCP.exe 的完整路径",
      "args": []
    }
  }
}
```

将 `command` 的值替换成第一节找到的真实路径。例如，安装版如果位于：

```text
C:\Users\Alice\AppData\Local\Programs\MEFinder\MEFinderMCP.exe
```

则填写：

```json
{
  "mcpServers": {
    "mefinder": {
      "type": "stdio",
      "command": "C:\\Users\\Alice\\AppData\\Local\\Programs\\MEFinder\\MEFinderMCP.exe",
      "args": []
    }
  }
}
```

绿色版如果位于：

```text
D:\MEFinder\MEFinderMCP.exe
```

则填写：

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

Windows 普通路径中的反斜杠，在 JSON 字符串中必须写成两个：

```text
D:\MEFinder\MEFinderMCP.exe  →  D:\\MEFinder\\MEFinderMCP.exe
```

如果配置编辑器中已经有其他 MCP，不要覆盖原内容，只需把 `mefinder` 与其他服务并列放在同一个 `mcpServers` 中：

```json
{
  "mcpServers": {
    "other-tool": {
      "command": "example",
      "args": []
    },
    "mefinder": {
      "type": "stdio",
      "command": "D:\\MEFinder\\MEFinderMCP.exe",
      "args": []
    }
  }
}
```

两个 MCP 条目之间要加逗号；如果没有其他 MCP，则不需要添加 `other-tool` 示例。

### 第三步：保存、启用并测试

粘贴完成后保存配置，回到 **MCP 服务管理**，在“我的 MCP”列表中找到 `mefinder`。如果右侧开关没有打开，将它切换到启用状态。

新建一个 WorkBuddy 任务，先输入：

```text
请查看 mefinder MCP 提供了哪些工具。
```

正常情况下应该能看到九个只读工具：

```text
list_documents
locate_quote
read_document_window
verify_quotes
diff_quote
search_passages
find_parallel_passages
read_bibliographic_pages
read_bibliographic_metadata
```

然后测试是否能读取自己的文献库：

```text
请只使用 mefinder，列出当前已经导入的文献。
```

如果能列出 MEFinder 中已经索引的文献，说明连接成功。

接下来可以复制一段已经导入文献的原句：

```text
请只使用 mefinder 定位下面这句话，告诉我它来自哪篇文献、哪一页：

<把原句粘贴到这里>
```

找到结果后继续说：

```text
继续使用 mefinder，读取刚才命中位置前后的上下文。
```

这样可以依次测试：

```text
list_documents
→ locate_quote
→ read_document_window
```

三个工具都能正常调用，就说明 WorkBuddy 已经完整接入 MEFinder。

### 配置完成以后不用手动启动 MCP

以后使用时直接打开 WorkBuddy 即可，不需要先手动双击：

```text
MEFinderMCP.exe
```

也不需要让 MEFinder 桌面主程序一直保持打开。

WorkBuddy 在调用工具时会根据 MCP 配置自动启动 `MEFinderMCP.exe`。

如果刚刚新导入了文献但 WorkBuddy 搜不到，先回到 MEFinder 确认文献已经完成解析和索引。

## 五、最常见的配置错误

1. **启动程序选错。** 必须指向 `MEFinderMCP.exe`，不要填写桌面主程序。
2. **文件路径已经变化。** 移动绿色版、重新安装或更新后，需要重新确认 `MEFinderMCP.exe` 的位置。
3. **MCP 类型选错。** MEFinder 使用本地 `STDIO`，不是网上 WorkBuddy 教程中常见的 `Streamable HTTP`。入口可以照着操作，但不要照抄 HTTP 的 URL / Headers 配置。
4. **填写了不需要的内容。** 参数、环境变量和环境变量传递默认都留空。
5. **手动双击 MCP 程序。** `MEFinderMCP.exe` 是供 AI 客户端启动的服务程序，一般不需要作为普通软件运行。
6. **文献库还是空的。** 先在 MEFinder 中完成至少一篇文献的导入和索引，再测试搜索。

## 六、Windows 快速配置表

| 软件 | 配置方式 |
| --- | --- |
| Codex | 设置 → 插件 → MCP → 添加 → 添加 MCP 服务器 |
| Claude Code | `claude mcp add --transport stdio --scope user mefinder -- "MEFinderMCP.exe 的完整路径"` |
| WorkBuddy | 首页“连应用” → “更多连接器” → “自定义连接器” → “配置 MCP” → 粘贴 STDIO JSON |

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

按 `Command（⌘）+ 空格`，输入“终端”或 `Terminal` 搜索并打开，再运行命令。

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

打开终端，运行：

```bash
claude mcp add --transport stdio --scope user mefinder -- /Applications/MEFinder.app/Contents/MacOS/MEFinderMCP
claude mcp list
```

看到 `mefinder` 出现在列表里后，重新打开 Claude Code 并输入 `/mcp` 确认，然后依次测试：

```text
请查看 mefinder MCP 提供了哪些工具。
```

```text
请只使用 mefinder，列出当前已经导入的文献。
```

也可以手动编辑配置文件，效果完全一样。文件位于：

```text
~/.claude/settings.json
```

在其中加入 `mcpServers` 字段：

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

macOS 路径使用 `/`，不需要像 Windows 一样改成双反斜杠。保存后重新打开 Claude Code 即可生效。

### WorkBuddy

macOS 下 WorkBuddy 的入口和 Windows 完全一样，也是按照win教程里的三步操作：

**首页“连应用” → “更多连接器” → “自定义连接器” → “配置 MCP”**

如果首页没有看到“连应用”，也可以直接从左侧导航栏进入“连接器”。

> 网上教程图以 Streamable HTTP 为例；MEFinder 仍然使用本地 **STDIO**，所以不要填写 URL 或 Headers。

#### 1. 确认 macOS MCP 路径

MEFinder 安装到“应用程序”后，MCP 程序位于：

```text
/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP
```

可以先在终端确认：

```bash
ls -l /Applications/MEFinder.app/Contents/MacOS/MEFinderMCP
```

WorkBuddy 要启动的是 App 内部的 `MEFinderMCP`，不是整个 `MEFinder.app`。

#### 2. 打开“配置 MCP”

在 WorkBuddy 中依次点击：

**连应用 → 更多连接器 → 自定义连接器**

进入：

**MCP 服务管理**

再点击右上角：

**配置 MCP**

#### 3. 粘贴 macOS 配置

如果当前没有其他 MCP，可以直接粘贴：

```json
{
  "mcpServers": {
    "mefinder": {
      "type": "stdio",
      "command": "/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP",
      "args": []
    }
  }
}
```

macOS 路径使用 `/`，不需要像 Windows 一样改成双反斜杠。

如果已经存在其他 MCP，同样不要覆盖原配置，只把 `mefinder` 加到同一个 `mcpServers` 里。例如：

```json
{
  "mcpServers": {
    "other-tool": {
      "command": "/path/to/other-tool",
      "args": []
    },
    "mefinder": {
      "type": "stdio",
      "command": "/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP",
      "args": []
    }
  }
}
```

#### 4. 保存并启用

保存以后返回：

**MCP 服务管理**

在“我的 MCP”中找到：

```text
mefinder
```

如果右侧开关没有打开，将它切换到启用状态。

然后新建任务测试：

```text
请查看 mefinder MCP 提供了哪些工具。
```

正常情况下应该能看到：

```text
list_documents
locate_quote
read_document_window
```

再测试：

```text
请只使用 mefinder，列出当前已经导入的文献。
```

如果能正常返回 MEFinder 中已经索引的文献，就说明配置成功。

配置完成以后，不需要手动运行 `MEFinderMCP`，WorkBuddy 会在调用时自动启动它。

## 八、配置成功后能做什么

MEFinder MCP 提供九个只读工具：

- `list_documents`：列出或筛选已导入文献；
- `locate_quote`：定位原句、近似引文和多个候选；
- `read_document_window`：读取命中位置附近的上下文；
- `verify_quotes`：一次核对多条引文，逐条返回命中/疑似错引/未找到；
- `diff_quote`：把疑似抄错的引文和原句逐字符对齐，指出增、漏、改；
- `search_passages`：只记得大意或部分关键词时，按相关性召回可能相关的原文段落（相关性检索，非逐字命中，可再转 `locate_quote` 逐字核验）；
- `find_parallel_passages`：输入一句中文或任一译本文本，以已有对齐为中心返回其他版本的多个附近候选和前后文；Agent 比较语义与上下文后才确认，无法唯一确定时会列出歧义或明确说明证据不足；
- `read_bibliographic_pages`：读取版权页候选（书首与书尾），供 AI 从原书自身补全题录；
- `read_bibliographic_metadata`：查看已存题录字段与缺口（缺哪些字段），配合上一个工具补全。

测试时可以直接说：

```text
请只使用 mefinder 核对这句话出自哪篇文献、哪一页，并继续读取命中位置前后的上下文。
```

跨译本查询可以直接说：

```text
请用 mefinder 查这句中文对应的英文原句，比较返回的全部候选和前后文；只有证据唯一时才确认，否则说明 ambiguous 或 unavailable。
```

MEFinder MCP 本身不访问网络，但客户端调用工具后，返回的命中原文和上下文会进入对应 AI 客户端的对话及模型上下文。涉及未公开文献时请留意。

Codex 的界面与 STDIO 配置说明见 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。源码模式、数据位置和高级排错见 [Codex MCP 高级指南](https://github.com/sabercomo/MEFinder/blob/main/docs/CODEX_MCP.md)。

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

WorkBuddy 回到“连应用 → 更多连接器 → 自定义连接器 → 配置 MCP”，把之前加入的 `mefinder` 配置删掉即可。

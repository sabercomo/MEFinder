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

WorkBuddy 当前可以从“连接器”界面进入自定义 MCP 配置。下面按照 WorkBuddy 的实际界面一步一步操作。

你在网上看到的教程图通常以 Streamable HTTP MCP 为例。MEFinder 不使用 HTTP，而是本地 STDIO MCP，所以前面的入口操作完全一样，最后粘贴的配置内容换成 MEFinder 的本地启动配置即可。

1. 从首页进入“更多连接器”

打开 WorkBuddy，在首页任务输入框附近点击：

连应用

在弹出的应用列表最下面，点击：

更多连接器

如果你的 WorkBuddy 版本界面稍有不同，也可以直接从左侧导航栏进入：

连接器

两种方式最终都会进入同一个“连接器”页面。

2. 点击“自定义连接器”

进入“连接器”页面后，可以看到 QQ 邮箱、腾讯文档等已有连接器。

点击页面右上角：

自定义连接器

进入后会打开：

MCP 服务管理

这里就是 WorkBuddy 管理自定义 MCP Server 的页面。

3. 点击“配置 MCP”

进入“MCP 服务管理”后，点击右上角：

配置 MCP

WorkBuddy 会打开 MCP 配置编辑器。

网上教程到这里通常会粘贴 Streamable HTTP 的 URL / Headers 配置；MEFinder 不需要这些内容，因为 MEFinder MCP 是运行在本机的 STDIO 服务。

MEFinder 只需要告诉 WorkBuddy：

MCP 名称：mefinder

启动程序：MEFinderMCP.exe

参数：无

4. 粘贴 MEFinder 配置

Windows 安装版和绿色版的配置方法完全一样，区别只有 MEFinderMCP.exe 的实际路径不同。

配置结构统一为：

{
  "mcpServers": {
    "mefinder": {
      "type": "stdio",
      "command": "MEFinderMCP.exe 的完整路径",
      "args": []
    }
  }
}

实际使用时，把：

MEFinderMCP.exe 的完整路径

替换成第一节找到的真实路径。

例如，安装版如果位于：

C:\Users\Alice\AppData\Local\Programs\MEFinder\MEFinderMCP.exe

则填写：

{
  "mcpServers": {
    "mefinder": {
      "type": "stdio",
      "command": "C:\\Users\\Alice\\AppData\\Local\\Programs\\MEFinder\\MEFinderMCP.exe",
      "args": []
    }
  }
}

绿色版如果位于：

D:\MEFinder\MEFinderMCP.exe

则填写：

{
  "mcpServers": {
    "mefinder": {
      "type": "stdio",
      "command": "D:\\MEFinder\\MEFinderMCP.exe",
      "args": []
    }
  }
}

可以看到，安装版和绿色版只是 command 后面的路径不同，其他内容完全一致。

5. Windows 路径要写成双反斜杠

资源管理器中看到的普通 Windows 路径是：

D:\MEFinder\MEFinderMCP.exe

但 MCP 配置是 JSON，因此每个反斜杠都要转义，配置中必须写成：

D:\\MEFinder\\MEFinderMCP.exe

也就是：

\  →  \\

如果直接把普通 Windows 路径原样粘到 JSON 里，配置可能无法正确解析。

6. 已经配置过其他 MCP 怎么办

如果配置编辑器中原来已经有其他 MCP，不要把原来的内容全部覆盖掉。

例如原配置是：

{
  "mcpServers": {
    "other-tool": {
      "command": "example",
      "args": []
    }
  }
}

加入 MEFinder 后应写成：

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

也就是说，所有 MCP 都放在同一个：

mcpServers

里面并列配置。

两个 MCP 条目之间记得加逗号。

7. 保存并启用 MEFinder

粘贴完成后保存配置，回到：

MCP 服务管理

正常情况下，“我的 MCP”列表中会出现：

mefinder

如果右侧开关没有打开，将它切换到启用状态。

正常连接后，MEFinder MCP 会显示为可用状态。WorkBuddy 官方界面中通常以绿色状态表示连接成功；如果显示异常或红色状态，则需要检查配置。

8. 第一次怎么测试

建议先新建一个 WorkBuddy 任务，输入：

请查看 mefinder MCP 提供了哪些工具。

正常情况下应该能看到 MEFinder 的三个只读工具：

list_documents
locate_quote
read_document_window

然后再测试是否能读取自己的文献库：

请只使用 mefinder，列出当前已经导入的文献。

如果能列出 MEFinder 中已经索引的文献，说明连接成功。

接下来可以复制一段已经导入文献的原句：

请只使用 mefinder 定位下面这句话，告诉我它来自哪篇文献、哪一页：

<把原句粘贴到这里>

找到结果后继续说：

继续使用 mefinder，读取刚才命中位置前后的上下文。

这样可以依次测试：

list_documents
→ locate_quote
→ read_document_window

三个工具都能正常调用，就说明 WorkBuddy 已经完整接入 MEFinder。

9. 配置好以后不用手动启动 MCP

以后使用时直接打开 WorkBuddy 即可，不需要先手动双击：

MEFinderMCP.exe

也不需要让 MEFinder 桌面主程序一直保持打开。

WorkBuddy 在调用工具时会根据 MCP 配置自动启动 MEFinderMCP.exe。

如果刚刚新导入了文献但 WorkBuddy 搜不到，先回到 MEFinder 确认文献已经完成解析和索引。

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

macOS 下 WorkBuddy 的入口和 Windows 完全一样，也是按照网上教程里的三步操作：

首页“连应用” → “更多连接器” → “自定义连接器” → “配置 MCP”

如果首页没有看到“连应用”，也可以直接从左侧导航栏进入“连接器”。

网上教程图以 Streamable HTTP 为例；MEFinder 仍然使用本地 STDIO，所以不要填写 URL 或 Headers。

1. 确认 macOS MCP 路径

MEFinder 安装到“应用程序”后，MCP 程序位于：

/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP

可以先在终端确认：

ls -l /Applications/MEFinder.app/Contents/MacOS/MEFinderMCP

WorkBuddy 要启动的是 App 内部的 MEFinderMCP，不是整个 MEFinder.app。

2. 打开“配置 MCP”

在 WorkBuddy 中依次点击：

连应用 → 更多连接器 → 自定义连接器

进入：

MCP 服务管理

再点击右上角：

配置 MCP

3. 粘贴 macOS 配置

如果当前没有其他 MCP，可以直接粘贴：

{
  "mcpServers": {
    "mefinder": {
      "type": "stdio",
      "command": "/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP",
      "args": []
    }
  }
}

macOS 路径使用 /，不需要像 Windows 一样改成双反斜杠。

如果已经存在其他 MCP，同样不要覆盖原配置，只把 mefinder 加到同一个 mcpServers 里。例如：

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

4. 保存并启用

保存以后返回：

MCP 服务管理

在“我的 MCP”中找到：

mefinder

如果右侧开关没有打开，将它切换到启用状态。

然后新建任务测试：

请查看 mefinder MCP 提供了哪些工具。

正常情况下应该能看到：

list_documents
locate_quote
read_document_window

再测试：

请只使用 mefinder，列出当前已经导入的文献。

如果能正常返回 MEFinder 中已经索引的文献，就说明配置成功。

配置完成以后，不需要手动运行 MEFinderMCP，WorkBuddy 会在调用时自动启动它。

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

WorkBuddy 回到“连应用 → 更多连接器 → 自定义连接器 → 配置 MCP”，把之前加入的 mefinder 配置删掉即可。

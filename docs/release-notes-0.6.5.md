2026-11-23：**0.6.5 macOS arm64 与 x86_64 本地测试包已构建。** 公式复制改为纯文本，素材库列表与详情显示公式，译本对照入口随首页首帧出现。Windows 包留给本机打包。

# MEFinder 0.6.5

## 更新内容

### 译本对照

- 启动时先判断对齐组件和已保存的对齐记录。正常及只读入口随首页显示，紧接着打开可看到「正在读取作品…」，作品加载后导航位置保持固定。
- 默认启动先观察 Zotero 状态，再读取或复用共享摘要。已有同步历史不再触发同次启动内的重复摘要与总览请求；轻量对齐总览仍在摘要完成后加载。
- 此前去除重复摘要的耗时数据见 [计时报告](../reports/translation-work-startup-2026-11-22.md)；0.6.3 起入口晚出现的原因及验证见 [启动记录](issues/library-startup-overview-contention.md)。
- 后续 Zotero 同步仍会刷新目录；检索、页码、数据库、HTTP 与 MCP 契约不变。

### 素材库

- 列表和详情复用检索页的公式排版，支持行内公式和带编号的独立公式。已有摘录只要保留了可识别的 LaTeX，就无需重新导入；Markdown 导出和原文定位仍用保存的文本。
- 检索结果与素材摘录的复制改为纯文本。化学式如 H₂O₂ 粘贴为 `H2O2`，分式如 `(a+b)/c`。已经显示不出来、或无法可靠转写的公式保留原写法。
- 素材库列表中用鼠标划选不会触发打开详情或清空选区，单击打开方式不变。支持复制以 `\(…\)`、`\[…\]` 标记的公式。

## 升级须知

- **数据库仍为 v13**，本版未新增 schema 变更；0.6.3 及更早版本仍无法打开升级后的库。
- macOS arm64 最低 macOS 14，x86_64 最低 macOS 12；均为 ad-hoc 签名、未公证。Intel 包经 Rosetta 构建，未在 Intel 真机验收。
- Windows 安装版与便携版由用户在本机 Windows 电脑打包，本轮未提供；全平台验收以各自构建结果为准。
- 本轮仅本地测试，不涉及标签、Release 或 Homebrew；v0.6.4 的公开标签与 12 个附件保持不变。

## 本地测试包

| 文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.5-macos-arm64.dmg` | 104,668,496 | `aaa127b74f5dfdee290f2487986457a845d950ef70fa5f647be87fc9521c2b88` |
| `MEFinder-v0.6.5-macos-arm64.zip` | 96,541,532 | `458aef36441e94bff3124aec3799504a5c65721a62f5829ffb02f7e7a0215b98` |
| `MEFinder-v0.6.5-macos-x86_64.dmg` | 109,577,911 | `e7463d0460eec32a00e52658616f324e35acf9a909472ba0a8fbe845048bf933` |
| `MEFinder-v0.6.5-macos-x86_64.zip` | 103,078,171 | `0c8063ec001c805c519c1f7c9013bb95c21e71a7d3b8bd3df4d8d4a9ad823c81` |

四个文件各附同名 `.sha256.txt`，`shasum -c` 已通过；GitHub 草稿附件的 digest 与 sidecar 一致。

## 验证记录

- 发布源码 `d219793`。CI [45120973594](https://github.com/sabercomo/MEFinder/actions/runs/45120973594) 的 tests、browser-regression、lint、windows-tests 四项通过；本机浏览器回归 31 项通过。
- 官方 `build_macos.sh` 构建内全量 unittest：arm64 2,921 项通过（55 项条件跳过，201.800 秒），x86_64 2,921 项通过（60 项条件跳过，229.816 秒）。Node 语法、PyInstaller 桌面端与 MCP 侧车、ad-hoc 签名、STDIO 侧车冒烟、ZIP、DMG（挂载与复制验签）和 SHA-256 均通过。
- 两个包独立解包核验：版本 0.6.5；主程序与 MCP 侧车分别为纯 arm64 / 纯 x86_64；严格验签通过；包内 `17-passage.js`、`20-search.js` 的 SHA-256 与源码一致。
- 早前 arm64 包的公式显示与译本首帧修复验证见 [素材库公式记录](issues/mineru-search-formula-display.md)；本轮未重新做 WKWebView 人工验收。

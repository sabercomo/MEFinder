# 检索界面显示 MinerU 公式

## 2026-11-17

**事实**：反馈截图里，化学式下标、角度和波数显示为带 `$…$` 的 LaTeX。MinerU 的 `content_list` 文本进入检索索引后，列表摘要和详情曾直接把它以纯文本显示。浏览器回归用例以反馈中的公式为样本，旧界面没有生成公式节点。

**处理**：搜索列表、命中段落和上下文由随包的 KaTeX 在本机生成 MathML。搜索字符偏移与原始文本仍用于高亮和页码定位。命中跨进公式时高亮整条公式，字符预览边界落在公式内部时完整显示该公式。框选复制单条结果或上下文时保留原始公式文本。普通金额及无法解析的公式保留输入文本。现有索引可直接使用。

**范围**：本次处理的是公式显示。若化学式、数字或运算符被解析错，需要根据原 PDF 核对。发布前需在目标平台的桌面窗口验收。

## 2026-11-19 — 带编号独立公式仍显示为源码

**事实**：反馈中的两条公式分别带有 `\tag{1}` 与 `\tag{3}`，以跨行 `$$…$$` 包围。既有渲染调用始终采用 KaTeX 默认的行内模式，触发 `\tag works only in display equations`，继而按原文本显示。浏览器回归 `test_search_renders_numbered_display_formulas_from_mineru` 在修复前找不到独立公式节点，在修复后通过。

**开源对照**：[KaTeX auto-render](https://github.com/KaTeX/KaTeX/blob/main/contrib/auto-render/auto-render.ts) 将分隔符的 `display` 属性交给 `katex.render` 的 `displayMode`；[markdown-it-texmath](https://github.com/goessner/markdown-it-texmath) 同样区分行内公式与独立公式，并支持多行和编号。本次沿用现有 KaTeX，按 `$$` 分隔符选择独立公式模式。

**验证范围**：反馈截图中转写的两条公式已通过编号、命中高亮、上下文和复制检查。离线 Chrome、360 px 页面与 macOS WKWebView 通过。全量 unittest 2,900 项（42 项条件跳过）、18 项 Chrome 回归、Ruff、JS 语法与装配指纹检查通过。验证用合成输入，报告者原 PDF 与 Windows 现场尚未复测。

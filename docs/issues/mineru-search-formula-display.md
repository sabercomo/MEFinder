# 检索界面显示 MinerU 公式

## 2026-11-17

**事实**：反馈截图里，化学式下标、角度和波数显示为带 `$…$` 的 LaTeX。MinerU 的 `content_list` 文本进入检索索引后，列表摘要和详情曾直接把它以纯文本显示。浏览器回归用例以反馈中的公式为样本，旧界面没有生成公式节点。

**处理**：搜索列表、命中段落和上下文由随包的 KaTeX 在本机生成 MathML。搜索字符偏移与原始文本仍用于高亮和页码定位。命中跨进公式时高亮整条公式，字符预览边界落在公式内部时完整显示该公式。框选复制单条结果或上下文时保留原始公式文本。普通金额及无法解析的公式保留输入文本。现有索引可直接使用。

**范围**：本次处理的是公式显示。若化学式、数字或运算符被解析错，需要根据原 PDF 核对。发布前需在目标平台的桌面窗口验收。

## 2026-11-19 — 带编号独立公式仍显示为源码

**事实**：反馈中的两条公式分别带有 `\tag{1}` 与 `\tag{3}`，以跨行 `$$…$$` 包围。既有渲染调用始终采用 KaTeX 默认的行内模式，触发 `\tag works only in display equations`，继而按原文本显示。浏览器回归 `test_search_renders_numbered_display_formulas_from_mineru` 在修复前找不到独立公式节点，在修复后通过。

**开源对照**：[KaTeX auto-render](https://github.com/KaTeX/KaTeX/blob/main/contrib/auto-render/auto-render.ts) 将分隔符的 `display` 属性交给 `katex.render` 的 `displayMode`；[markdown-it-texmath](https://github.com/goessner/markdown-it-texmath) 同样区分行内公式与独立公式，并支持多行和编号。本次沿用现有 KaTeX，按 `$$` 分隔符选择独立公式模式。

**验证范围**：反馈截图中转写的两条公式已通过编号、命中高亮、上下文和复制检查。离线 Chrome、360 px 页面与 macOS WKWebView 通过。全量 unittest 2,900 项（42 项条件跳过）、18 项 Chrome 回归、Ruff、JS 语法与装配指纹检查通过。验证用合成输入，报告者原 PDF 与 Windows 现场尚未复测。

## 2026-11-22 — 素材库显示公式源码

**事实**：用隔离书库保存含 MinerU 风格 LaTeX 的摘录，接口原样返回公式文本、锚点和页码。素材库列表与详情直接用纯文本构造摘录，旧界面均未生成公式节点。浏览器回归 `test_saved_material_formulas_render_without_changing_the_excerpt` 可复现。

**处理**：列表和详情与检索共用随包的 KaTeX 排版。回归覆盖行内下标、带编号独立公式、金额与不识别公式原样保留，并核对复制、Markdown 导出、已保存字符区间和来源页码。

**范围**：只修正可识别 LaTeX 的显示。参考 [CiteBox 的按页坐标裁图](https://github.com/xuzhougeng/citebox/blob/e46b524579d691ec6391cf7bad7159d279a2854a/internal/service/builtin_llm_extraction.go#L111-L157)，若解析时已丢掉公式结构，可用 MEFinder 现有 PDF 截图保留原页；未加自动识别或截图。

## 2026-11-22 — 复制化学式与分式时不带 LaTeX

**事实**：上一轮修正了公式的页面显示，但选中后 Ctrl+C 和检索详情的「复制」仍写入 LaTeX。浏览器回归以含 H₂O₂、分式、编号和金额的摘录复现。

**处理**：从已排版的 MathML 转写剪贴板文本。普通下标数字（包括已写成 Unicode 下标的数字）改为普通数字，分式保留必要括号，数学运算符和编号不丢。暂不能可靠转写的复杂结构保留源码，不删符号猜测。改动仅用于剪贴板，不改摘录、引文位置和 Markdown 导出。

## 2026-11-23 — 划选预览丢失与另一种公式分隔符

**事实**：反馈描述写论文时希望直接粘贴摘录，不必清理符号，截图没有给出具体出错的原句。隔离库复现两处缺口：实际鼠标拖选列表预览时，松手触发点击并重绘列表，导致选区消失；已有排版和纯文本复制也没识别 `\(…\)` 与 `\[…\]`。

**处理与验证**：在预览中区分拖选与单击，保留已有打开详情方式；支持两种分隔符，复用现有转写，不改已保存的摘录、页码与 Markdown。浏览器回归 `test_drag_copy_material_preview_into_plain_text` 用实际鼠标划选、快捷键及剪贴板结果验证；另一用例用含分式、化学式、金额和未识别命令的入库样本验证。Windows 报告者的原句与原环境未复测。

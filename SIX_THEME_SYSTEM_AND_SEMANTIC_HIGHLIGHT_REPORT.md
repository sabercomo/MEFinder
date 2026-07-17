# 六主题系统与语义高亮实施报告

## 实施范围

本轮在现有 pywebview + 本地 HTTP 后端架构上扩展主题系统，没有复制页面、替换前端框架，也没有修改数据库、搜索、导入、OCR 或页码映射算法。

应用现在支持以下六个主题，并按固定顺序显示：

1. 清霜蓝（`frost-blue`，默认）
2. 鼠尾草（`sage-ivory`）
3. 暖砂金（`warm-sand`）
4. 蔷薇雾（`rose-mist`）
5. 暮云紫（`lavender-purple`）
6. 深海夜（`midnight`）

## 当前样式架构

- 前端仍由 `src/me_finder/web.py` 提供统一 HTML、CSS 和原生 JavaScript。
- 主题挂载在 `<html data-theme="...">`，所有页面继续复用同一套 DOM 和组件样式。
- 基础组件只写一次，各主题仅覆盖 design tokens。
- `src/me_finder/preferences.py` 负责校验和持久化主题 ID。
- `desktop.py` 在 WebView 首屏出现前读取持久化主题，并为六个主题提供对应的加载页和错误页配色。

## 主题令牌

六个主题均定义并复用以下核心令牌：

- 页面与表面：`--app-bg`、`--sidebar-bg`、`--surface-primary`、`--surface-secondary`、`--surface-elevated`。
- 文字与边框：`--text-primary`、`--text-secondary`、`--text-tertiary`、`--border-subtle`、`--border-default`、`--border-strong`。
- 主题强调色：`--accent`、`--accent-hover`、`--accent-soft`、`--accent-contrast`。
- 组件：输入框、菜单、对话框、阴影、焦点环、滚动条和骨架屏令牌。
- 语义状态：info、success、neutral、warning、danger 各自的前景、背景、边框和图标令牌。
- 命中高亮：块背景、块边框、左侧强调、行内背景、行内边框、行内文字和定位焦点环。

## 语义状态系统

主题强调色不再决定业务状态色。所有主题都使用统一语义：

- 正在检测 / 信息：蓝色 `info`
- 已校准 / 成功：绿色 `success`
- 待校准：灰蓝色 `neutral`
- 待确认：橙色 `warning`
- 检测失败 / 删除：红色 `danger`

顶部统计胶囊、筛选标签、卡片状态和详情状态统一使用相同 variant。图标为 inline SVG，使用 `currentColor`，尺寸固定为 16×16，不依赖字体图标或 Unicode 字符。图标、文字、数字、背景和边框均从对应语义令牌取值。

## 命中高亮策略

命中高亮与主题强调色、错误红色相互独立，每个主题均有专用配色：

- 清霜蓝：暖琥珀
- 鼠尾草：柔和紫
- 暖砂金：冷蓝
- 蔷薇雾：蓝绿色
- 暮云紫：琥珀色
- 深海夜：高对比琥珀色

结果详情继续采用两层结构：命中段落使用主题化背景、边框和 3px 左侧强调条；真实命中短语使用更清晰的行内背景与边框。自动滚动后的短暂强调动画也改用 `--match-focus-ring`，不会继承页面 accent 或 danger。

## 主题预览组件

设置页使用 `THEME_OPTIONS` 数据注册表和一个共享的 `themePreviewMarkup()` 渲染器生成六张卡片，没有维护六份重复 DOM。

每张预览包含：

- 约 26% 宽度的迷你侧栏、品牌标记和三项导航；
- 一项使用主题 accent 的选中导航；
- 主内容标题、副标题和状态胶囊；
- 带搜索图标和强调色按钮的搜索框；
- 三张迷你文献卡片；
- success、danger 状态与独立 match 高亮示例。

预览直接在自身 `data-preview-theme` 作用域内复用正式 design tokens，因此用户切换当前主题时，其他五张预览仍准确展示各自配色。卡片支持整卡点击、Enter、Space、radio 语义、焦点环和即时选中标记。

响应式布局经验证为：

- 1440px：3 列
- 1100px：2 列
- 800px：1 列

六张卡片等高且没有横向溢出。

## 持久化与首屏

- 主题选择继续复用现有 preferences JSON 持久化服务，同时保留 localStorage 作为前端即时状态。
- 后端仅接受六个已注册主题 ID，非法值回退到清霜蓝。
- 桌面壳在创建 WebView 前读取主题，为六个主题分别设置首屏背景、表面、文字、边框和强调色。
- 深海夜启动时直接使用深色壳层，不会先闪白再切换。
- 切换主题不刷新页面，因此不会清空搜索、筛选、排序或滚动位置。

## 修改文件

- `src/me_finder/web.py`
- `src/me_finder/preferences.py`
- `desktop.py`
- `tests/test_theme_system.py`
- `tests/test_calibration_library_ui.py`
- `tools/verify_theme_preview.py`
- `SIX_THEME_SYSTEM_AND_SEMANTIC_HIGHLIGHT_REPORT.md`

## 测试结果

### 自动测试

执行：

```text
py -3 -m unittest discover -s tests -v
```

结果：92 项测试全部通过。

覆盖范围包括 DOCX/PDF 搜索、SQLite、PDF 类型检测、MinerU 配置、自动页码映射、引文格式、元数据识别、页码显示、校准工作区、主题持久化和六主题令牌契约。

### Playwright 视觉与交互验证

执行 `tools/verify_theme_preview.py`，六个主题均通过：

- 点击、Enter 和 Space 立即切换；
- 六张主题预览结构完整；
- 3/2/1 列响应式布局正确；
- success 与 danger 在六个主题中清晰可见；
- match 色不等于 accent 或 danger；
- 五类统计图标尺寸均为 16×16，opacity 为 1；
- 切换主题后“检测失败”筛选保持不变。

截图输出位于 `test-output/theme-preview-*.png`。

### Windows 打包验证

- 使用 PyInstaller 6.21.0 重新构建 onedir 桌面版。
- 新包包含现有 SQLite 索引与 PDF 导入映射配置，不在 EXE 中写入 MinerU 私密 Token。
- 从最终路径 `dist/MEFinder/文献原句定位器.exe` 启动成功。
- 日志确认 bundle root 为最终 `dist/MEFinder`，SQLite 索引加载成功，本地后端正常就绪。
- 已删除旧正式目录、partial 残留和临时构建目录；`dist` 中仅保留一个 `MEFinder`。

## 已知限制

- 当前预览是用于表达层级和配色的 CSS 迷你界面，不复刻真实业务数据或完整页面细节。
- 视觉自动化使用 Chromium/WebView 同源渲染验证；不同 Windows 缩放比例下可能存在亚像素差异，但布局尺寸、语义颜色和交互不受影响。
- 本轮没有新增系统跟随模式；应用只提供六个明确可选主题。

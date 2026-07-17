# 主题预览卡重设计报告

## 实施范围

本轮只重构了“设置 > 外观”中的主题预览卡。主题变量、页面布局、业务功能、主题切换接口和偏好持久化逻辑均未改变。

同时整理了桌面发布产物：`dist/MEFinder/` 现在是唯一保留的正式版本，旧的平行发布目录、PyInstaller 构建缓存和旧启动器组合已清理。

## 原预览异常原因

旧预览的侧栏是一个空色块，主内容区只有一条横线和一个带伪元素的矩形。它缺少导航、标题、搜索和文献卡片之间的空间关系，因此中间边界容易被看成滚动条，整体也更像尚未加载完成的骨架屏。

旧实现还为预览单独维护了 `--preview-*` 颜色变量。两套预览主要通过替换背景色来区分，没有完整展示实际主题中的表面层级、文字、边框、语义状态色和强调色。

## 新预览结构

每张预览现在由同一套 HTML/CSS 迷你组件构成：

- 26% 宽度的迷你侧栏；
- 品牌标记和三项导航，其中一项使用强调色选中；
- 主内容标题、副标题和成功状态胶囊；
- 带搜索 SVG 和强调色操作区的搜索框；
- 三张迷你文献卡片；
- 成功与错误状态点；
- 卡片外部的主题名称、说明和 SVG 勾选标记。

预览不包含真实文献、作者、页数或统计数字，也没有使用截图、Canvas、渐变或骨架动画。

## 主题令牌复用

预览通过以下主题作用域复用应用现有令牌：

```css
:root,
html[data-theme="frost-blue"],
.theme-preview[data-preview-theme="frost-blue"] { ... }

html[data-theme="midnight"],
.theme-preview[data-preview-theme="midnight"] { ... }
```

迷你界面直接使用 `--app-bg`、`--sidebar-bg`、`--surface-primary`、`--surface-secondary`、`--text-*`、`--border-*`、`--accent`、`--accent-soft`、`--success`、`--success-soft`、`--danger` 和 `--danger-soft`。旧的 `--preview-*` 颜色副本已经删除。

## 交互与响应式

- 整张主题卡仍是可点击的 `button`，保留 `radiogroup` / `radio` / `aria-checked` 语义。
- Enter 和 Space 由原生按钮键盘行为触发主题选择。
- `:focus-visible` 使用现有 focus ring。
- 当前主题使用 2px 强调色边框、`accent-soft` 外环和 `currentColor` SVG 勾选。
- hover 上移 1px，过渡为 180ms。
- 宽屏使用等宽多列；视口不超过 720px 时切换为单列。
- 视口不超过 430px 时迷你文献卡改为两列并隐藏第三张，避免内部内容被压扁。

## 修改文件

- `src/me_finder/web.py`：主题令牌作用域、预览卡结构和样式。
- `tests/test_theme_system.py`：预览结构、令牌复用、无业务数据和可访问性回归测试。
- `tools/verify_theme_preview.py`：Playwright 双主题、键盘和响应式验收脚本。
- `tools/ui_test_index.json`：UI 验收使用的最小本地索引。
- `launcher.py`、`me_finder.spec`：已删除的旧桌面启动器组合；当前只使用 `desktop.py` 和 `desktop.spec`。

## 实际测试结果

### 自动测试

- 主题专项测试：12/12 通过。
- 全项目测试：91/91 通过，用时约 68 秒。

### Playwright UI 验收

在 Chromium 1.48、1440×1000 视口下实际打开设置页并切换两个主题：

- 两张预览尺寸均为 312×124 px；
- 每张预览包含 3 个导航项和 3 张文献卡片；
- 清霜蓝主背景为 `rgb(245, 248, 252)`，卡片为白色；
- 深海夜主背景为 `rgb(8, 17, 29)`，卡片为 `rgb(17, 28, 41)`；
- 两个主题中的成功绿、错误红和强调蓝均来自对应语义令牌；
- 点击整卡切换、`aria-checked` 更新和 Enter 键选择均通过；
- 700px 视口下两张主题卡等宽单列排列。

验收截图位于：

- `test-output/theme-preview-frost-blue.png`
- `test-output/theme-preview-midnight.png`

### Windows 桌面包

使用 PyInstaller 6.21.0 重新构建 `desktop.spec`。新包启动日志确认后端正常就绪，最终正式入口为：

`dist/MEFinder/文献原句定位器.exe`

## 版本清理结果

- 保留：`dist/MEFinder/`
- 删除：`dist-next/`、`dist-polish/`、`dist-status/` 和临时 `dist-release/`
- 删除：`build/`、`build-next/`、`build-polish/`、`build-status/` 和临时 `build-release/`
- 删除：旧 `launcher.py`、旧 `me_finder.spec`
- 保留：语料、SQLite 索引、MinerU 配置、用户偏好和 `%LOCALAPPDATA%/MEFinder/runtime` 运行时数据

## 已知限制

预览是用于展示主题配色和信息层级的 CSS 缩略图，不会复刻真实页面文字或业务数据。极窄窗口下会隐藏第三张迷你文献卡，但真实主题卡和主题切换功能不受影响。

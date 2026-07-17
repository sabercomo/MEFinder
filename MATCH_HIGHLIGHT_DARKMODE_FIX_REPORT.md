# Match Highlight Dark Mode Fix Report

## 问题原因

此前左侧摘要和右侧“命中原句”都复用同一个固定黄色高亮变量 `--highlight-soft`。这个策略在浅色背景上足够醒目，但在深海夜主题中会变成低对比的暗黄底，命中短语和上下文段落的层级不清楚。

本轮没有修改搜索逻辑、数据库结构或结果字段，只调整命中原句的视觉呈现。

## 新增高亮令牌

在 `src/me_finder/web.py` 的主题变量中新增了独立的命中高亮语义 token：

- `--match-block-bg`
- `--match-block-border`
- `--match-block-accent`
- `--match-block-flash-bg`
- `--match-inline-bg`
- `--match-inline-border`
- `--match-inline-text`

浅色主题继续使用暖黄色/琥珀色方向。深海夜主题改为蓝青系高对比高亮：

- 段落背景：`rgba(20,41,68,0.82)`
- 段落边框：`rgba(64,145,255,0.78)`
- 左侧强调线：`#4DA3FF`
- 行内命中背景：`rgba(90,168,255,0.22)`
- 行内命中文字：`#F5FAFF`

## 分层高亮实现

右侧详情中的命中区域现在分为两层：

1. 命中段落容器 `.detail-hit`：使用主题感知背景、边框和 4px 左侧强调线。
2. 命中短语 `<mark>`：只包裹真实命中的文字，使用更亮的行内背景、边框和主题专用文字色。

左侧结果摘要 `.result-snippet mark` 也改为使用同一套 `--match-inline-*` 令牌，避免摘要区继续使用固定黄底。

详情渲染后会给 `.detail-hit` 添加一次 `is-locating` 类，触发 620ms 的轻微定位强调动画，并在命中段落不在可视区域时滚动到附近。动画只作用于命中段落，不刷新结果列表。

## 修改文件

- `src/me_finder/web.py`
  - 新增浅色/深色主题命中高亮 token。
  - 替换旧 `--highlight-soft` 高亮。
  - 优化 `.detail-context`、`.detail-hit`、`.result-snippet mark`、`.detail-hit mark`。
  - 增加 `match-locate-pulse` 动画和详情定位逻辑。
- `tests/test_theme_system.py`
  - 增加高亮 token 合约测试。
  - 增加深色高亮不再使用旧黄底的 CSS 结构测试。

## 验证结果

自动测试：

```text
py -3 -m unittest discover -s tests -v
Ran 72 tests in 52.046s
OK
```

视觉验收截图：

- `G:\ME_Finder\build\match-highlight-frost-blue.png`
- `G:\ME_Finder\build\match-highlight-midnight.png`

Edge 计算样式抽样结果：

```text
frost-blue markBackground = rgb(255, 232, 163)
frost-blue markColor = rgb(23, 32, 51)

midnight markBackground = rgba(90, 168, 255, 0.22)
midnight markColor = rgb(245, 250, 255)
midnight markBorderColor = rgba(120, 190, 255, 0.75)
```

深色主题中命中句已不再使用低对比黄底，显示为蓝青系行内高亮；命中段落与普通上下文通过深蓝灰背景、亮蓝边框和左侧强调线区分。

## 桌面包

已重新构建 Windows 桌面包，并显式覆盖正式应用目录：

```text
G:\ME_Finder\dist\MEFinder\文献原句定位器.exe
SHA256: 4FA22873DAB638D76727D250148ADE0764842FF5F091D00B08BE88CD6941C772
```

## 已知限制

本轮截图使用的是临时视觉验收页，它复用当前应用完整 HTML/CSS/JS，并调用现有 `showDetail()` 渲染命中详情；不会写入索引或修改真实数据。搜索、PDF、页码映射和引用功能由完整自动测试继续覆盖。

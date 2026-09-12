# 外观模式卡在 macOS 上塌成 0 高度

2026-09-12：已定位并修复。Windows 正常、macOS 缺失整块预览，根因是 WebKit 对 `<button>` 作 flex 容器时不 stretch 子项，`aspect-ratio` 因宽度不确定解不出高度。

## 事实（本机 WKWebView 实测）

复现环境：pywebview + WKWebView（`AppleWebKit/605.1.15`），与桌面壳同引擎。`CSS.supports('aspect-ratio','16/10')` 返回 `true`，但元素实测 0×0。

结构最小化后逐项对照（父级统一 `display:flex;flex-direction:column;width:200px`）：

| 写法 | WKWebView 实测 |
| --- | --- |
| `<button>` + 子元素 `aspect-ratio:16/10` | **0 × 0** |
| 同上 + `width:100%` | 198 × 124 |
| 同上 + `align-self:stretch` | 198 × 124 |
| 同上但用 `height:0;padding-bottom:62.5%` | 0 × 124（宽度仍为 0）|
| 把 `<button>` 换成 `<div>` | 200 × 125 |

结论：问题不在 `aspect-ratio` 支持度，而在 `<button>` 作为 flex 容器时子项未被 stretch，宽度不确定导致比例无法解算。Chromium（Windows 的 WebView2）会 stretch，因此 Windows 无此现象。

## 影响范围

`.mode-preview`（`30-settings.css`）是仓库内唯一「空盒子 + `aspect-ratio` + `<button>` 父级」的组合：它的子元素全是 `position:absolute`，高度只能来自比例。`.theme-preview` 同样用 `aspect-ratio`，但内部有在流内的内容撑高，macOS 上不受影响。

## 修复

`.mode-preview` 补 `width: 100%`，并在 CSS 注释里写明原因。修复后同环境实测：正式页面中该元素 236 × 147（16:10）。同步更新 `test_frontend_assets.py` 的装配指纹基线。

## 推断（未验证）

其他「按钮内 flex 布局 + 仅靠比例或 stretch 取尺寸」的写法在 macOS 上有同类风险；新增此类组件时应显式写出宽度，不依赖 stretch。

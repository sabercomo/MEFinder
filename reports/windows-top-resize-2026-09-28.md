# Windows 顶部缩放修复

2026-09-28：补齐顶部与两个上角的窗口缩放；原生映射及 WebView2 实际 DOM 命中通过，冻结应用鼠标拖拽验收待完成。

## 原因

现有 `frameless_resize_hit` 明确将顶部留作 HTCLIENT，HTML 只装配 left/right/bottom/bottom-left/bottom-right 五个热区，`_RESIZE_EDGE_HITS` 不接受 top。因此这是代码排除了顶部，不能归因于文献库或机器性能。旧测试也把顶部不缩放当作预期。

## 修改

- 顶部、左上、右上分别映射 Windows HTTOP=12、HTTOPLEFT=13、HTTOPRIGHT=14；原生四边/四角齐全。
- 沿用现有 HTML 透明热区与系统缩放桥，新增三个热区；顶部高 8px、两角宽 12px，层级沿用 `--z-window-edge`。未重新引入有色原生边框。
- 保留标题栏其余区域和按钮，沿用最大化时隐藏热区的规则与控制器禁止最大化缩放的保护。
- 同步前端装配指纹：1509378 字节，SHA-256 `afaf08675c2f0ccc567a31d1eac944b26eca76b1087c88dc0d5e2424233cc0db`；全局函数预算未变。

## 验证

- 修复前 `test_top_edge_resizes_but_titlebar_below_it_stays_client`、`test_top_corners_resize_diagonally`、`test_html_edges_map_to_native_resize_hit_codes` 均失败，修复后通过。
- Node 执行真实 10-shell.js：八个方向均把左键转交对应系统缩放入口，右键不处理，重复 pywebviewready 不重复装配热区。
- 桌面/前端/主题专项 109 项通过，JS 语法检查通过。
- 隔离 WebView2 加载真实装配页面，`elementFromPoint` 实测顶部中点、左上、右上分别命中 top/top-left/top-right；y=20 标题栏仍命中拖动区，关闭按钮中心仍命中按钮；添加最大化类后八个热区计算样式均 display:none。
- Windows 截图工具连续超时，未取得可用截图，因此未实施依赖截图定位的真实鼠标拖拽；不把 DOM 命中检查当成完整人工验收。
- 全量 unittest 2610 项 OK（36 skip，201.292 s），Ruff 零告警，前端指纹、主题及预算守卫通过。

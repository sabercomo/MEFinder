# ME_Finder 多主题系统第一阶段实施报告

## 1. 当前样式架构

- 桌面壳：`desktop.py` + pywebview/WebView2。
- 前端：`src/me_finder/web.py` 中的一套原生 HTML、CSS 和 JavaScript SPA。
- 页面、组件和 DOM 均保持单份，没有为不同主题复制页面。
- 原项目已有少量 CSS Variables，但组件中仍有较多浅色硬编码；本轮将其整理为统一语义令牌。
- 本轮未修改数据库 schema、搜索、PDF 解析、MinerU、页码映射或导入算法。

## 2. 主题系统

主题挂载在根节点：

```html
<html data-theme="frost-blue">
<html data-theme="midnight">
```

实现两套主题：

- `frost-blue`：清霜蓝，新用户默认主题。
- `midnight`：深海夜，完整深色主题。

统一令牌覆盖以下类别：

- 应用、侧栏、卡片、悬浮和选中表面；
- 四级文字颜色和三级边框；
- 强调色、成功、警告、错误和信息状态色；
- 输入框、菜单、对话框和 Tooltip；
- 卡片、Popover 和焦点阴影；
- 滚动条、骨架屏、遮罩和文本命中高亮。

搜索页、文献库、导入页、页码校准、映射详情、设置页、弹出菜单、删除确认框、Toast、空状态和骨架屏均使用同一套令牌。业务状态继续使用绿色、橙色、红色和蓝色，并保留文字说明。

## 3. 设置与持久化

设置页新增“外观”区域，使用两张主题预览卡，不使用原生下拉框。预览卡展示侧栏、页面、卡片和强调色，并标记当前选择。

切换主题时只修改 `document.documentElement.dataset.theme`，不刷新页面，因此不会清空搜索、筛选、排序或滚动位置。

主题偏好保存到：

```text
%LOCALAPPDATA%\MEFinder\preferences.json
```

后端新增：

- `GET /api/preferences`
- `POST /api/preferences`

偏好采用原子写入，并限制主题 ID 为 `frost-blue` 或 `midnight`。打包升级不会覆盖该文件。

## 4. 首屏防闪烁

- pywebview 创建窗口前先读取持久化主题，并按主题渲染加载页和错误页。
- Web 后端在返回第一份 HTML 前注入持久化主题。
- 深海夜启动时，加载页和正式页面使用一致的深色背景，不需要等待页面 JavaScript 再切换。
- 新安装且没有偏好文件时默认使用清霜蓝；已有偏好时尊重用户选择。

## 5. 修改文件

- `src/me_finder/preferences.py`：新增主题偏好读取、校验和原子保存。
- `src/me_finder/web.py`：主题令牌、两套主题、设置页预览卡、切换逻辑和偏好 API。
- `desktop.py`：主题化加载页、错误页及首屏主题读取。
- `desktop.spec`：显式纳入主题偏好模块。
- `tests/test_theme_system.py`：新增主题系统自动测试。
- `dist/MEFinder/文献原句定位器.exe`：已重新打包正式 Windows 应用。

## 6. 实际验证

自动测试：

```text
py -3 -m unittest discover -s tests -v
Ran 70 tests in 49.132s
OK
```

验证结果：

- 70 项测试全部通过，包括原有 Word/PDF 搜索、引文、MinerU 配置、导入、自动页码映射和校准 UI 测试。
- 缺失或非法主题设置会回退到清霜蓝。
- 深海夜保存后重启本地服务，仍恢复为深海夜。
- 首个 HTML 响应直接包含持久化主题，不依赖页面加载后的二次切换。
- 所有内联 SVG 图标使用 `currentColor`，没有固定黑色、白色或蓝色图标。
- 正式打包版成功启动并加载本地 SQLite 索引。
- 正式打包版实际完成 Word 中文引文和 PDF 英文引文搜索；PDF 结果返回正确引用页码。
- 浅色页码校准截图：`build/theme-frost-calibration.png`。
- 深色页码校准截图：`build/theme-midnight-calibration.png`。
- 深色设置页截图：`build/theme-midnight-settings.png`。

## 7. 已知限制

- Windows Graphics Capture 在当前 pywebview 窗口上返回“不支持此接口”，因此桌面窗口的自动截图无法直接取得。本轮视觉检查使用正式应用相同的后端、HTML 和 WebView2/Edge 渲染内核完成；打包应用另行完成了启动和搜索链路验证。
- 第一阶段只提供两套固定主题，不包含跟随系统、定时切换或自定义配色。
- 旧版 WebView2 若不支持部分现代 CSS，可能出现轻微阴影差异，不影响颜色、可读性和功能。

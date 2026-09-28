# Zotero 设置页 UI 验证记录

2026-09-27：源码与自动化验证完成；浏览器视觉验收未完成，现有可执行程序未重打。

## 范围

- 工作树：`D:/CodexHome/worktrees/windows-057/ME_Finder`，分支 `codex/v0.5.7-windows`；本轮基于 `9dec4c9`。
- 用户确认的交互原型：顶部同步入口与状态摘要、分类与设置分栏、规则按需展开、分类操作附近常显移除后果。
- 追加要求：设置目录采用原型中的线性图标，统一采用 Lucide 0.468.0 内联 SVG，保留 Zotero 的 Z 标记；ISC 许可随静态资源分发。
- 未改后端同步、数据库、偏好字段、HTTP/MCP 契约、导入/解析行为或框架。

## 自动化证据

- Python 3.12 测试环境 `D:/ME_Finder/.venv-windows/Scripts/python.exe`，设 `PYTHONUTF8=1`。
- `-m unittest discover -t . -s tests`：2599 项，36 项条件跳过，其余通过。日志在工作树本地忽略目录 `test-output/zotero-redesign/unittest.log`。
- `-m unittest tests.test_theme_system tests.test_frontend_assets tests.test_zotero_settings_frontend`：81 项通过。
- `-m ruff check .`：通过；`node --check src/me_finder/static/js/62-zotero.js`：通过。
- 新增 `test_summary_pause_recovery_and_disconnected_controls`：执行生产 JS，验证包含子分类的选择摘要、取消父分类、后端移除/解除关联预览、关闭与恢复同步、保存失败回退、断连时禁用分类和立即同步但允许调整偏好，以及同步开始后的进度与空明细收起。
- 原 `test_tree_starts_folded_and_parse_mode_updates_after_save` 继续验证初次折叠、展开、解析方式与导入页共享状态。
- 前端装配字节数与 SHA-256 在 `tests/test_frontend_assets.py` 同步；模板动作沿用原有注册，不新增全局入口。

## 未完成的验证

浏览器两次打开本地组件预览均被权限检查阻止：无法核验已保存的浏览器授权。没有绕过检查，没有截图或实际浏览器交互结果；不能把响应式样式和主题 token 的代码检查当成视觉验收。

恢复浏览器能力后需检查：宽窗口双栏、内容宽度不超过 700 px 时单栏、长分类名/嵌套分类、浅色和深色主题、开关/下拉的键盘行为、连接异常、同步进行中及完成明细。临时预览夹具使用合成数据，不接触真实文献库。

## 交付边界

本轮未重打 Windows 安装包、便携包或 dist，也未构建 macOS 包。现有可执行程序仍显示旧 UI，打包前先完成上述视觉验收。正式发布门禁及冻结应用人工验收继续保留。

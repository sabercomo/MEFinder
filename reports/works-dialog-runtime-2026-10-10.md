# 作品弹窗与对齐运行时核验

日期：2026-10-10；环境：Windows，项目 Python 3.12.14，真实 Google Chrome / Playwright。

## UI 复现与修复结果

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| 中文组合输入 | 输入节点被替换，测试失败 | 原节点与焦点保留，拼音组合提交“精神”后检索到文献 |
| 文本中间插入 | 整个弹窗重建，光标被重置 | 在“精神”前插入“现象”，结果为“现象精神”，光标在第 2 字后 |
| 固定定位菜单 | 1280px 视口下菜单 1280px，触发器 263px | 菜单 263px，左边界与触发器一致，可选择既有作品 |

新增真实浏览器用例 `test_assign_search_keeps_ime_input_and_caret` 与 `test_assign_menu_stays_at_trigger_width` 均先失败、后通过。完整浏览器回归 8 项通过（20.557 秒），无未捕获页面错误。HTTP 测试服务中关闭页面导致的连接中止日志不等于用例失败。

截图：本地 `.codex-tmp/works-fixes-20261010/dialog-fixed.png`，已目视核对菜单与弹窗布局。

## 对齐启动故障

14:23 的三次真实任务在能力探测时退出；启动器的旧 Python 路径已失效。修复后独立环境报告 Python 3.12.13、NumPy 2.5.2、ONNX Runtime 1.29.0、fastembed 0.8.0。使用当前桌面包的外置 worker 源码探测，协议为 1，三个依赖能力均为 true。

同一独立环境和现有 `multilingual-e5-large` 模型完成两条中文、两条英文的实际计算，返回 1 条 link、0 个 heading anchor。该样本只证明本地模型加载与计算链路可用，不是整书回归或质量评测。未重解析原书，也未生成或覆写真实库对齐结果。

修复前的 `pyvenv.cfg` 与 `Scripts/` 保存在 `.codex-tmp/works-fixes-20261010/runtime-launcher-backup/`。修复用现有 uv 离线保留包重建启动入口；没有下载模型或改变用户对齐阈值。

## 自动化边界

新增能力探测启动失败用例与协调器提示用例均先失败、后通过。能力探测阶段失败应报告组件问题，已有计算崩溃、取消、协议不兼容和禁止半成品发布用例继续通过。

源码全量 `unittest discover -t . -s tests`：2,745 项通过，26 项条件跳过，214.931 秒。跟踪 Python 文件 Ruff(F) 零告警；前端守卫与装配指纹通过，指纹为 1,516,647 字节 / `46380e0ae174f756f9d47f2d6dad6ee11a422b7ddf524292ea06897cdf47da62`。

远端 `79bc362` 只追加 0.5.9 macOS 打包说明，已快进同步；本轮源码测试与桌面构建中的 Python / 前端文件未因此改变。

## 本机桌面构建

`PYTHONUTF8=1 build_windows_dist.cmd` 完整通过，内置全量 2,745 项通过（26 项跳过，212.176 秒），逐文件 Node 语法、PyInstaller 桌面和侧车、空库 FTS5、MCP 侧车冒烟通过。

`dist/MEFinder/文献原句定位器.exe` 已重建。包内 `35-works.js` / `45-works.css` 与源码逐字节一致；PyInstaller PYZ 反查确认能力探测使用 `WORKER_START_FAILED`，主程序协调器含“重新安装计算组件”提示。`data_root.txt` 与构建前逐字节一致，仍为 `D:\ME_Finder\dist\MEFinderData`。重建后的外置 worker 再次完成既有 E5 模型小样本计算。

`release/` 安装包与便携 ZIP 未重打，本轮未发布。原有用户配置、生产库和既有未提交工件保留。

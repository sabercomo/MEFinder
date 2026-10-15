# 导入进度本机断连恢复核验

## 现场证据

- 日期：2026-10-15；本地与远端 `main` 均为 `a3781e4`，现场不是漏拉更新。
- 当前程序：`D:/ME_Finder/dist/MEFinder/文献原句定位器.exe`；本机服务为 `127.0.0.1:2687`。
- `dist/MEFinderData/runtime/desktop.log` 在 19:46:06–19:46:22 记录截图五份文件均按 `route=native` 提交导入。
- 只读 `GET /api/import-status`：`import-5fb902252b35`（40a）与 `import-40225278abd4`（42）均为 `completed`；`import-fc39b7178066`（38）、`import-06aea6538794`（43）、`import-3f4a6381c11b`（44）仍为 `processing/stored`。随后活动接口中的其他任务从书目识别推进至索引，证明后台仍工作。
- 请求为何中断：待核实；不把缺少证据的可能性当成已确认根因。

## 复现与实现

- 先补复现：`tests.test_import_active_frontend` 旧代码 9 项中 4 项失败，分别覆盖重复断连后的完成、响应正文中断、取消中断连与移除后停止查询。
- Chrome 真实 `fetch` 经 `route.abort('connectionreset')` 注入本机通信中断；旧代码未显示重试，复现测试超时失败。
- 修复只增加状态查询通信边界的 `TypeError` 处理，不重传文件、不重新提交解析、不写真实文献库。
- 源码专项：导入状态与译本对照共 34 项通过；JS 语法通过。
- 装配 HTML：1,521,829 字节，SHA-256 `80234fb1a7e1964584c76d8e569faa559600a579761649b80faff6a66590ec23`。

## 门禁与交付

- Python：现有 `.venv-windows/Scripts/python.exe`，3.12.14，`PYTHONUTF8=1`。
- 全量 `unittest discover -t . -s tests`：2,771 项，29 跳过，222.450 秒，OK；故障注入堆栈不作为失败判断。
- 真实 Chrome：11 项，37.559 秒，OK，包含新增断连恢复用例；前端守卫专项 251 项，9.966 秒，OK。
- Ruff(F)：全部 Git 跟踪 Python 文件零告警；`node --check` 与差异空白检查通过。
- 本机交付：仅同步 `dist/MEFinder/_internal/src/me_finder/static/js/80-import.js`，JS SHA-256 `d8fd40537e3100489202a010687786db5bf58615bd8d7f88238f08113a824e8d`；完整 HTML 与已测源码逐字相同，字节和指纹见上。
- 两份 EXE、`data_root.txt`、程序目录 `data/` 与 `config/` 共 6 份文件哈希不变。没有停止程序或后台任务，没有直接修改真实数据库；需下次正常启动加载新界面。
- 原 JS 备份和交付核验结果在 `.codex-tmp/import-progress-delivery-20261015/`；全量 / Chrome / 前端日志分别为 `.codex-tmp/import-progress-{full,browser,frontend}-20261015.log`。
- 未改 HTTP / MCP / schema / 偏好契约，未新增全局命令，不需修改 README 数量说明、接口快照或偏好基线。未重打 release 安装 / 便携 / macOS 包，未发布新版本。

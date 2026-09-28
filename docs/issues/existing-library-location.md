# 打开已有资料库与迁移当前资料库

2026-09-11：原入口仅支持向空目标迁移；新增独立的已有库入口，解决 Windows 同步库无法选为读取位置的问题。

## 事实

- 原 `migrate_data_root` 明确拒绝非空目标，避免覆盖。Windows `6027a95` 修复的是跳过运行时 WebView 缓存，不包含打开已有库。
- `POST /api/data-location/choose` 接受可选 `mode=migrate|existing`，缺省保留迁移行为；existing 只读检查所选目录下的 `runtime/data/index.sqlite3`，返回文献与段落数量。
- 新增 `POST /api/data-location/switch`，请求 `{target_path: string}`。检查文件、核心表、schema 上界、SQLite quick_check 后，只原子写入稳定位置的 `data_root.txt`，不复制或覆盖任一资料库。
- 成功返回 `ok/current_path/target_path/restart_required/old_data_retained/document_count/paragraph_count/message`。缺失或损坏数据库、更新版本库返回 400；活动上传/导入/索引任务或已切换状态返回 409；文件系统错误返回 500。
- 使用同一数据根准入、持久操作与运行时互斥。操作失败会恢复准入，成功后封闭旧库写入，重启生效。GET 摘要携带 `pending_path` 和 `restart_required`，重新进入设置仍能看到未重启状态。
- 两个设置面板保留主题、字体和现有保存行为，统一小节标题、行距和控件样式；仅阅读与数据位置受新增样式影响。

## 验证

`tests/test_existing_data_location.py` 检查不覆盖、未知/未来数据库拒绝、真实 HTTP 切换、上传阻塞和重启状态；`tests/test_data_location_settings_ui.py` 运行实际 JS 检查两种请求、取消、错误重试、重复点击及重新载入。
真实书库性能结果见 [真实库基线报告](../../reports/performance-real-v0.5.4-2026-09-11.md)；它使用隔离快照，原库未切换。用户安装后的原生目录选择与重启体验仍需实机验收。

## 切换只写稳定位置指针，安装包内指针会留在旧库（2026-09-25）

**事实**：

- 切换数据根只原子写稳定位置的 `data_root.txt`（`%LOCALAPPDATA%\MEFinder`，本机是指向 `D:\ME_Finder\dist\MEFinderData` 的链接），不碰安装包内的 `dist\MEFinder\data_root.txt`；后者每次 Windows 打包都会被 `Restore-LocalDevelopmentDataMarker`（`build_portable_release.ps1:17`）重写成 `dist\MEFinderData`。
- 于是存在两条解析路径：环境里有 `LOCALAPPDATA` 时走稳定指针（两跳到 `E:\OneDrive\MeFinder`）；没有时 `local_app_data_root` 直接返回安装包指针指向的目录，**不再往下跟一跳**（`runtime_location.py:90-93`），落在 9 月 10 日的快照库上。
- MCP 侧车正是后者：Qoder 以 `"args": [], "env": {}` 起 `MEFinderMCP.exe`，`LOCALAPPDATA` 不在其环境里。桌面端一切正常，侧车却读到 65 篇 / schema v6 的旧库，而真实库是 129 篇 / v8；`list_documents(query="耶吉")` 在旧库 0 命中、真实库 16 命中。表现是「新导入的中文期刊没进 MCP 检索索引」，实际是整批 9-10 之后的文献（64 篇，其中约 32 篇期刊论文）对 MCP 不可见。
- 旧库 schema 低于代码版本仍能被静默读出，全程没有任何日志或字段说明侧车连的是哪个库。

**修复**：安装指针指向的目录本身也是一个数据根，可能已被再次改址，因此对它再走一次 `read_data_root`（只跟一跳，与稳定指针同一语义）。修复后两条路径都收敛到 `E:\OneDrive\MeFinder\runtime`。测试：`test_installed_windows_follows_relocation_pointer_inside_selected_data_root`、`test_installed_windows_follows_relocation_when_local_app_data_has_no_marker`。

**未做**：侧车仍未暴露自己解析到的 `index_path` 与 `user_version`，也没有对低于当前 schema 的库给出告警——这类「连错库」只能靠对拍文献数发现。零改动的临时规避是给侧车注入 `ME_FINDER_APP_DATA_ROOT`（`runtime_location.py:86` 短路一切解析）。

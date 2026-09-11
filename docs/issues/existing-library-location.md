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

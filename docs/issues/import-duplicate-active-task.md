# 重复导入报运行冲突与取消任务提前消失

## 2026-10-12 — 现场与原因

**事实**：用户截图中，《黑格尔著作集 第3卷 精神现象学》的导入队列显示“同一文献已有解析任务正在运行”。本机 `desktop.log` 记录 22:38:59 将首个提交排入 `local_ocr`，job ID 为 `import-e7f8b48462e8`；22:39:08 开始上传同一文件的第二份，类型检测结束后没有新的任务排入记录。排查时 `/api/import-active` 与 `/api/import-resumable` 都为空，随后用户关闭程序。完整验证记录见 [核验报告](../../reports/import-active-jobs-2026-10-12.md)。

**事实**：源代码的 `reserve_source` 对 `processing` 和 `cancelling` 都抛同一个错误，上传入口将其当作导入失败；前端 `removeImport` 收到 `state: cancelling` 后立即移除队列行，而后端还会保持文献占用直到解析请求停止并完成清理。

**推断**：现场首个任务可能在再次提交前被取消；只有截图和日志不能确认具体点击顺序。复现测试分别覆盖运行与取消中的任务，不依赖该推断。

## 2026-10-12 — 修复与边界

重复上传时原子地从去重检查取得已有任务快照，返回原 job ID 与解析方式，不创建第二个解析、不改原任务或配置，删除仅此次上传产生的多余副本。前端合并同 job ID 的队列行并继续跟踪。

取消任务保留可见并显示正在停止；停止完成、原任务接口返回 404 后才移除。普通 404 与取消清理失败仍显示错误；查询失败后的取消操作会恢复状态轮询。停止完成后可按重新选择的方式再导入。参见 [接口行为](../contracts/v0.5.9-import-active-jobs.md)。

复现用例先在旧代码失败，再验证修复：`test_duplicate_upload_tracks_active_job_and_cleans_only_new_copy`、`test_cancel_keeps_row_until_worker_has_stopped`、`test_duplicate_upload_reuses_job_route_and_one_queue_row`。测试同时检查历史 source ID、原解析方式、只保留原文件、无新增解析任务，以及停止后能创建新任务。

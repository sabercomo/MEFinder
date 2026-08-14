# MCP v2 写能力决策

更新日期：2026-08-14

决策：0.4.4 保持 MCP v1 只读；本轮不注册写工具。

## 决策依据

四个候选工具的用户目标成立，但现有架构尚未满足写操作前置条件：

| 候选工具 | 用户目标 | 当前阻塞点 | 结论 |
| --- | --- | --- | --- |
| `import_document` | 从明确的本地文件创建导入任务 | 导入队列、任务执行器和 admission/mutation gate 只属于桌面进程；独立 MCP 进程无法与其互斥 | 延期 |
| `get_import_status` | 查询长任务状态 | 只有先确定跨进程持久任务的唯一所有者与任务 ID，状态查询才有稳定语义 | 与导入工具一起延期 |
| `save_bibliographic_metadata` | 保存用户确认的题录 | 直接改 SQLite 会让桌面进程内目录缓存过期，且现有 metadata lock 不是进程锁 | 延期 |
| `apply_page_calibration` | 应用用户确认的页码映射 | 会同时修改配置与索引；现有 mutation gate 不能协调独立 MCP 与桌面进程 | 延期 |

MCP 的 `readOnlyHint`、`destructiveHint` 等 annotation 是模型提示，不是跨进程事务或幂等机制。Codex 配置支持 `default_tools_approval_mode = "writes"`，可对未标记为只读的工具请求客户端审批，但服务端仍必须准确标注并解决一致性问题；仅改 annotation 或审批配置后调用现有函数，不能满足计划中的授权和数据安全要求。参见 [Codex MCP 配置参考](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

## 进入 v2 的必要条件

1. 确定一个跨进程写操作所有者：桌面常驻 broker，或独立的持久任务执行进程；不能让桌面与 MCP 各自执行同一任务；
2. 使用现有导入 journal 扩展出跨进程可恢复队列，并为提交、重复调用、取消、失败恢复定义幂等键；
3. 把索引、题录配置和页码映射写入纳入同一跨进程 mutation 协议，Windows 上实测文件锁和替换；
4. 每个工具冻结独立 schema、写/破坏性标注、审批提示、可逆影响和错误码；发布配置默认使用 `default_tools_approval_mode = "writes"`，并验证逐工具审批覆盖；
5. 长任务只返回任务 ID，由短调用查询状态，不在一个 MCP 调用中等待解析完成；
6. 使用公开夹具验证重复提交、进程崩溃、桌面同时开启、取消、恢复和升级兼容性。

在这些条件完成前，维持只读不是功能缺失的临时回退，而是避免损坏用户文献库的正式边界。删除、批量删除、备份恢复和数据迁移继续排除在 v2 候选之外。

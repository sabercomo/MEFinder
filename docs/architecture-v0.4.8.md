# MEFinder 0.4.8 架构优化

## 结论

Kimi K3 的总判断正确：MEFinder 的模块化单体不需要推倒重写，问题主要是装配、传输、持久化和前端状态的边界还不够硬。建议中有两处与现状不符：四类解析器早已接入 `ParserProvider`；导入队列、可恢复 journal 和大文档 ledger 具有不同的持久化与取消语义，不能用一个新 `TaskRuntime` 粗暴替换。

0.4.8 按目标而非按建议中的示例目录完成三个阶段，保留已验证的原子发布、备份和解析流程。

## 三阶段完成情况

| 阶段 | 结果 | 可执行门禁 |
|---|---|---|
| 1. 核心模块与依赖方向 | `web.py` 拆为装配、HTTP 传输、静态资源装配；application 只依赖 `DocumentReadPort`，SQLite 查询进入 repository；schema、连接策略和迁移登记进入 `persistence` | `test_architecture_boundaries.py`、`test_persistence_migrations.py` |
| 2. 解析器、组件与任务契约 | 复用既有 `ParserProvider`；本地 OCR 与托管 MinerU 实现 `ManagedComponent`；导入和组件进度统一输出 `TaskEvent`；新增组件诊断接口 | `test_task_contracts.py`、组件与导入控制器测试 |
| 3. 演进与前端边界 | v3 增量 DDL 进入事务迁移登记并覆盖失败回滚；五类前端核心状态迁入领域 Store；HTTP 方法—路径集合冻结为 JSON/Python 双契约；复用并运行现有原子发布、恢复、取消和中断故障测试 | `test_http_api_contract.py`、前端专项测试、完整回归 |

## 当前结构

```text
desktop / CLI
  -> web.py                         composition root + lifecycle
     -> controllers                transport-neutral input/error mapping
        -> application             use cases + ports
           -> persistence          connection / schema / migrations / repositories
  -> web_http.py                    Host/Origin/body/routing/response/source stream
  -> web_assets.py                  deterministic HTML/CSS/JS assembly

ParserProvider                      cloud/local parser execution contract
ManagedComponent                    install/update/validate/diagnostics contract
TaskEvent                           shared progress DTO, not a new task runtime

frontend
  -> searchStore / libraryStore / parserStore / settingsStore / importStore
```

`database.py` 仍是批量建库、原子替换、备份和 FTS 发布的兼容门面。把这些写路径机械搬成若干薄类不会改善依赖方向，反而会破坏已有的 Windows 文件替换与恢复门禁；0.4.8 只把能够独立验证的连接、schema、迁移和 application 查询移出。

## 对外契约变化

- 新增 `GET /api/components`，返回托管组件诊断摘要。
- 导入状态、可恢复任务以及组件条目新增 `task_event`；既有字段不删除。
- HTTP 路径与方法集合冻结在 `docs/contracts/v0.4.8-http-api.json`，测试会阻止实现与文档漂移。
- SQLite schema 版本仍为 v3；变化是迁移所有权和回滚测试，不是再次改表。

## 后续约束

1. application 新增持久化需求时，先扩展 Port，再在 `persistence` 实现；不得把 SQL 写回用例层。
2. `web.py`、`web_http.py` 和 `database.py` 达到门禁上限时，应迁出真实职责，不得提高上限。
3. 新解析器必须实现现有 `ParserProvider`；新本地运行时必须实现 `ManagedComponent`。
4. 只有任务的恢复、取消和持久化语义相同，才能共享执行运行时；展示层统一消费 `TaskEvent`。
5. 新增或删除 HTTP 路由时必须同步更新 v0.4.8 契约（或创建下一版本契约）。

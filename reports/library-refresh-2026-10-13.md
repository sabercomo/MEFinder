# 文献数据刷新入口核验

2026-10-13：普通导入和 Zotero 同步已接入同一个刷新入口；源码验证见下文，安装包未重建。

## 范围与实现

- `MEFinder.library.refreshAfterChange()` 协调共享文献摘要、搜索范围和译本对照目录。当前文献库可见时立即重载；隐藏时保留失效标记，下次进入再渲染。译本目录立即重载，维持文献库「加入作品」所需数据。
- 导入完成和 Zotero 观察到同步变化时均调用该入口；同步状态不变时不重复刷新，导入失败时不刷新。
- 搜索范围标签和已打开的下拉列表同步更新。各页面仍共用一份摘要请求。
- 共享摘要请求用 Promise 身份判断是否失效：旧响应不写回缓存，旧消费者跟随当前请求；当前请求失败继续抛错，允许后续显式重试。
- 本轮未迁移书目编辑、删除、备份恢复的既有入口，未改变 HTTP / MCP 接口、数据库、检索、页码或字符区间，也未实施导入任务状态拆分。

## 修复前复现（事实）

1. 扩展 `test_import_and_zotero_refresh_version_metadata_on_visible_and_hidden_pages` 后，Zotero 分支的译本目录仍为空，无法读到新版本的书名、语言、出版与页码来源；旧实现失败。
2. `test_invalidated_request_cannot_restore_old_catalog_or_clear_new_request` 在先失效、后收到旧响应的时序下，观察到旧目录重新写入共享缓存；旧实现失败。

测试环境适配时曾缺少 `window.addEventListener` 替身；补齐后才取得第 2 项产品行为失败，未把测试环境异常作为复现证据。

## 执行型验收

- 两种触发来源 × 导入、搜索、文献库、译本对照四种当前页面：检查新目录、中文语言、书名、出版、格式、页码来源与搜索范围同步更新，一轮仅请求一次摘要。
- 旧摘要请求成功或失败晚到：不回填旧数据、不清掉当前请求；旧消费者收到新目录。当前请求失败仍向消费者报错；被旧消费者跟随的替代请求失败不会自动重试。
- Zotero 原有状态轮询回归保留：计数下降、相同计数的新完成时间、离开设置页、旧状态乱序、手动同步和不变状态。
- 新增 Chrome 场景 `test_zotero_refreshes_search_library_and_versions_across_page_changes`，通过真实后端、隔离数据库和受控同步响应，检查搜索下拉、译本副标题与文献库实际 DOM；不写用户文献库。

## 已执行检查

- 专项前端 unittest：41 项通过。
- Chrome 回归：10 项通过，29.462 秒，启用 `MEFINDER_BROWSER_TESTS_REQUIRED=1`，无环境跳过。
- Ruff(F)：`ruff check . --extend-exclude .codex-tmp` 零告警；排除的是既有用户临时目录。
- 修改的 4 个 JS 文件 `node --check` 通过。
- 前端装配、文献库 UI 与请求时序守卫：72 项通过。
- 前端装配：1,521,380 字节，SHA-256 `b921e29a0cd46fe2d79343bdc8ad757595947f034dd048036e32e2ffb978d87f`；全局符号数量未增加。

首轮全量 unittest 为 2,760 项 / 38 跳过，唯一失败是 `test_completed_import_refreshes_an_open_library_without_restart` 仍断言旧调用位置的源码字符串。该守卫已改为检查统一入口，执行型刷新覆盖保留并扩大。修正后的完整重跑：2,760 项通过 / 38 项环境跳过，173.564 秒。日志暂存于本机 `/tmp/mefinder-library-refresh-*.log`。

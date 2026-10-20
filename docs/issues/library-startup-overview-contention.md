# 默认启动时文献摘要被对齐总览排队

2026-10-17：已修复默认搜索页跳过摘要等待的启动时序；门禁通过，原 `dist/MEFinder/` 外置资源已同步，下次启动生效。测量与验证见 [核验报告](../../reports/library-startup-2026-10-17.md)。

## 事实

- 默认首页是搜索页，初始化时 `searchStore.libraryCatalogPromise` 尚为空。原 `90-init.js` 回退到已完成的 `Promise.resolve()`，因此没有真正等待文献摘要。
- 浏览器空闲预取的 `works.load()` 同时发出对齐总览与摘要；总览经 `IndexRuntime.run_when_ready` 持有索引锁。原代码的 Chrome 复现观察到 `overview → summary`，新增 Node 行为测试与 Chrome 测试均在修复前失败。
- 使用现有 468 篇书库只读数据、相同索引锁规则，清空进程内正文范围缓存后，总览先行时摘要耗时 4.08–4.75 秒；摘要先行时为 0.71–0.72 秒。已有正文范围缓存时原顺序约 0.70–0.73 秒，不能把缓存命中也说成显著提速。
- 今日桌面日志的索引加载至后端就绪约 156 毫秒；该时间不含后续接口和列表渲染。

## 推断与边界

该时序在对齐总览需要重新核对正文范围时会拖慢文献摘要；仅凭现有桌面日志无法确定用户这次感知等待的全部耗时构成。上述对照是隔离的请求排队测量，不是双击程序至列表显示的端到端计时，也没有清空系统文件缓存。

## 修改

没有在途摘要时直接发起共享摘要请求；已有请求时继续复用。摘要完成后启动 Zotero 界面状态观察和空闲对齐预取。摘要失败沿用原异常出口；检索、页码、数据库、HTTP 与 MCP 契约不变。

回归：`test_startup_waits_for_catalog_before_works_on_default_search_page`、`test_default_startup_finishes_catalog_before_alignment_overview`。

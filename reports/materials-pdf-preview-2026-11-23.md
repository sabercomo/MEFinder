# PDF 截图预览与搜索的并发回归

2026-11-23：用项目 Python 3.12、临时 SQLite 和合成 PDF 复现。测试暂停实际 `fitz.Page.get_pixmap` 的入口；不把人工停顿的时间当成实机延迟。

| 检查项 | 改前 | 改后 |
|---|---|---|
| 暂停渲染时，从同一索引搜索已知段落 | 搜索在 5 秒断言窗口内无法返回 | 搜索在放行前返回正确来源 |
| 预览仍未结束时发起索引重建 | 不应并行 | 放行后才进入发布，随后重新打开索引 |
| 预览凭据被拿去保存同一文件的另一页或另一来源 | 被接受 | 拒绝，未写入素材 |
| 预览后替换 PDF / 模拟渲染结束时原件标识改变 | 文件版本校验 | 仍拒绝 |
| 渲染期间启动关停 | 原流程不丢弃渲染结果 | 渲染结束后返回 503，不发布预览 |
| 旋转页框选、印刷页与未校准页 | 原有规则 | 保持不变 |

预览准备阶段在索引状态锁内从一个 SQLite 只读事务取来源、映射，并记下原件标识；渲染及 Base64 编码在状态锁外。数据目录切换的 HTTP 门禁不变，渲染仍持有原有索引变更锁，不尝试让多个 PyMuPDF 素材渲染或索引发布同时进行。截图保存的事务和去重方法不变。无实际 Windows 响应时间数据。

## 复跑

项目 Python 执行以下四项；并发用例的渲染屏障不自解，测试在 `finally` 中释放它。

```text
-m unittest \
  tests.test_materials.MaterialsTests.test_pdf_preview_releases_search_lock_but_serializes_publication \
  tests.test_materials.MaterialsTests.test_pdf_crop_rejects_preview_for_another_page_source_or_file \
  tests.test_materials.MaterialsTests.test_pdf_preview_rejects_file_changed_during_render \
  tests.test_materials.MaterialsTests.test_pdf_preview_discards_result_when_shutdown_starts_during_render
```

浏览器以 `MEFINDER_BROWSER_TESTS=1` 运行 `tests.test_browser_regression`，覆盖换来源、翻页、框选、整页回填和迟到的预览响应。

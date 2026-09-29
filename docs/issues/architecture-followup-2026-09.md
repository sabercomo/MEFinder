# 架构后续整理（2026-09 评估后）

2026-09-29：不做全仓重构；按“先锁行为、再挪边界”分四步推进，每步单独提交、跑全量门禁，完成一步经用户同意再做下一步。

## 背景（事实）

外部只读评估认为数据库连接、HTTP 传输分层、阅读器状态写入归属已经成立，剩余问题集中在三处：

- `structured_reader.py`：`get_document_window` / `get_document_citation` 自行开库并直接执行约 15 处 SQL，与分页、题录、引文规则写在一起；`persistence/document_read_repository.py`、`page_mapping_reads.py` 已存在但未承接。
- `translation_works.py`：`alignment_overview` 等读取路径直接执行 SQL，与对齐状态判断、结果组装混在一起；`persistence/translation_work_store.py` 目前只有写入。
- `static/js/35-works.js`（约 1640 行、100 余个函数）：作品展示、版本管理、批量重新对齐队列、弹窗、阅读器联动集中在一个文件。

判断标准：改一个功能牵动几处、测试能否抓到真实回归、页码锚点与字符区间是否保持一致。文件变短或层数增加本身不算收益。

## 计划

| 步 | 内容 | 分支 | 验收 |
|---|---|---|---|
| 0 | 阅读器与译本对照读路径的行为快照 | `main`（只加测试） | 快照稳定、变异可检出 |
| 1 | 少量真实浏览器回归（模型切换、对齐中按钮禁用、阅读器换书、`hidden` 元素确实不可见） | `feat/browser-regression` | CI 先不阻塞试跑，稳定后纳入门禁 |
| 2 | 上述两文件的查询移入 `persistence/`，规则留原层；边界测试禁止回退 | `feat/reader-query-store` | 第 0 步快照逐字节不变 |
| 3 | 作品页只拆批量重新对齐队列，参照 `15-alignment-jobs.js` | `feat/works-split` | 队列状态单一写入方，前端与浏览器测试通过 |

局限：pywebview 原生双窗口交接不在浏览器测试范围内，仍靠 `test_reader_windows_native` 与手动冒烟。

## 第 0 步结果（事实）

- 新增 `tests/test_reader_translation_characterization.py` 与金标准 `tests/fixtures/reader_translation_characterization.json`（34 个阅读器用例、19 个译本对照用例）。
- 阅读器覆盖：PDF（已校准跨页段、PDF 标签页、空页）、马恩文集 Word（卷/篇目归属）、Word 未验证三态、EPUB 页码表、EPUB 分页标记、无出版方页码的 EPUB；窗口、分页、越界、引文（跨页、反选、跨篇目、缺篇目归属、未校准）及对外错误类型与文案；另含目录与页码映射总览。
- 译本对照覆盖：总览（无/直接/间接/模型变更/轻量/限定对/未入组）、对齐窗口（直接/间接/倒序报错/暂缓/修正后）、复核候选、人工修正、阅读位置、同名建议忽略。时间戳与生成的 id 归一为占位符。
- 变异检查：把阅读器的 `epub_pagebreak` 页首判定改坏，`window/epub-break` 失败；把 `model_changed` 改名，`overview/model-changed` 失败；还原后通过。
- 门禁：macOS 全量 2661 项通过（28 跳过），Ruff 零告警。
- 重新生成（仅在有意改变产品契约时）：`.venv-macos312-arm64/bin/python -m tests.test_reader_translation_characterization --regen`。

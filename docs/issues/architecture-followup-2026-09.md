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

## 第 1 步结果（事实，2026-09-29，分支 `feat/browser-regression`）

- 新增 `tests/test_browser_regression.py`：Playwright 驱动本机已安装的 Google Chrome（`channel="chrome"`，不下载浏览器），连接真实后端与一次性测试库（复用 `TextAlignmentTests` 夹具：德文、贺麟译本、English EPUB 三版本，预先生成德—中对齐）。依赖只写在 `requirements-browser-tests.txt`，不进安装包。
- 默认跳过；`MEFINDER_BROWSER_TESTS=1` 本机开启。CI 新增 `browser-regression` 任务，`continue-on-error: true` 试跑，并设 `MEFINDER_BROWSER_TESTS_REQUIRED=1`，缺 playwright 或 Chrome 时报错而非静默跳过。
- 5 个场景：
  1. 五个主视图中带 `hidden` 的元素确实不占屏幕（计算样式非 `none` 且有布局框即失败），其余场景在关键状态也做同一检查；
  2. 切换对齐模型：只有一项选中、写入偏好；保存被拒（模拟 500）时恢复原选项；
  3. 偏好尚在读取时点另一模型：控制器拒绝，界面仍只选中原模型（Vue 试点曾出现“两项都未选中”）；
  4. 对齐进行中：发起的那一对显示“取消”，其他对的“生成对齐”“正文范围”禁用；任务结束后恢复可用（模型就绪、启动与状态接口由浏览器拦截模拟，其余走真实后端）；
  5. 阅读器换书：左栏版本从德文切到 English EPUB 后只显示英文正文、标题与版本名随之更新；返回作品页再打开中译本，不残留前一本书的正文。
- 变异检查（改坏后失败、还原后通过）：给 `.sidebar-item-tag` 加 `display: inline-flex` 覆盖 `[hidden]` → 场景 1 失败；去掉 `pairActions` 中 `works.running` 的禁用条件 → 场景 4 失败；去掉 Vue 视图 `pick` 后按 store 回写单选框 → 场景 3 失败；阅读器渲染由替换改为追加 → 场景 5 失败。
- 过程发现：场景 3 初版在进入设置页时才拦截偏好读取，但偏好只在启动时读取一次，测试没走到拒绝路径；改为重新加载前拦截后才有效。这是测试写法问题，不是产品缺陷。
- 本机稳定性：连跑 5 轮全部通过，单轮约 12 秒。CI 上的稳定性待多次运行后再决定是否纳入门禁。
- 门禁：macOS 全量 2666 项通过（33 跳过，含本文件 5 项默认跳过），Ruff 零告警。

## 第 2 步结果（事实，2026-09-29，分支 `feat/reader-query-store`）

- 新增 `persistence/structured_reader_reads.py`（15 个只读查询）与 `persistence/translation_work_reads.py`（17 个只读查询）。函数都接收调用方的连接，一次阅读或总览请求仍在同一连接、同一快照内完成；表名、列名只取自模块内常量，调用方输入不拼进 SQL。
- `structured_reader.py`、`translation_works.py` 不再直接执行 SQL；分页、页码显示、题录、引文规则、对齐状态判断与结果组装都留在原文件。两文件合计净减约 265 行。
- 架构边界棘轮 `SQL_EXECUTE_FILES_OUTSIDE_PERSISTENCE` 删去这两个文件（18→16），以后再在其中写 SQL 会让门禁失败。
- 验收：第 0 步快照（阅读器 34 例、译本对照 19 例）逐字节不变。变异检查：把作品成员排序改为倒序，`overview/*` 失败；把“上一段”查询的 `<` 改为 `<=`，`window/epub-*` 失败；还原后通过。
- 门禁：macOS 全量通过（33 跳过），浏览器回归 5 项通过，Ruff 零告警。

## 第 3 步结果（事实，2026-09-29，分支 `feat/works-split`）

- 新增 `static/js/34-works-queue.js`（108 行）：批量重新对齐队列的状态只在该模块内写入，只暴露 `MEFinder.workQueue` 一个命名 API（`active` / `snapshot` / `isQueued` / `start` / `stop` / `onJobEnd` / `recordStartError` / `configure`）。`snapshot()` 返回副本，渲染改它不影响队列。
- `35-works.js` 删去 `works.queue` 与 6 个队列函数，改为经 `workQueue` 读取和驱动；启动单个任务、刷新视图、提示、确认框由 `configure(host)` 注入。“哪些对需要重跑”（`staleDirectPairs` / `staleQueueItems`）仍在作品页，因为它读的是作品页的数据。文件 1639→1587 行。
- 弹窗、版本管理、阅读器联动本步不拆。
- 测试：原节点测试改为装配新模块后驱动；新增“队列状态单一写入方”断言（作品页不再出现 `works.queue`，works 状态对象不含 queue）；浏览器回归新增第 6 个场景：两组因换模型而过期的对齐，点“全部重新对齐”→确认→依次显示 1/2、2/2，运行中所有“生成对齐”禁用、不出现单作品“重新对齐 N 组”按钮，结束只汇总提示一次。
- 变异检查：队列结束一项后不前进 → 浏览器与节点测试失败；去掉结束汇总提示 → 浏览器测试失败；`snapshot()` 改为共享原数组 → 节点测试失败；还原后通过。
- 过程发现：`host.confirm(` 撞上既有守卫（前端禁止 `confirm(`/`alert(`/`prompt(`，防 Windows WebView 黑色系统对话框），宿主能力改名 `askConfirm`。
- 门禁：前端装配指纹与全局命令预算（新增 `34-works-queue.js`: 1）已同步；macOS 全量通过（34 跳过），浏览器回归 6 项本机连跑 4 轮通过，Ruff 零告警。

## 2026-09-29 追加：对照计划的偏差与补漏

- **遗漏（已补）**：第 1 步计划要求“阅读器换书后内容和页码正确”，初版只检查了正文、标题、版本名与地址，没有检查页码。现把德文 PDF 校准为引用页 38、英文 EPUB 设出版方页码 27—28，换书与重开后逐项比对界面页码标签与后端 `get_document_window` 的 `page_display`（德文“引用页码：38”→英文“第 27 页”“第 28 页”→中译本未校准文案）。变异：阅读器对 Word/EPUB 段落忽略 `page_display` 改显示“段落 N” → 该场景失败；还原后通过。
- **偏差（未改）**：第 2 步计划把查询并入现有 `document_read_repository.py` / `translation_work_store.py`，实际新建 `structured_reader_reads.py` / `translation_work_reads.py`，以保持现有 store 只写、repository 服务原有调用方；“两文件禁止再执行 SQL”由既有棘轮基线删项实现，未另写规则。
- **偏差（未改）**：第 1 步计划为“未装 playwright 自动跳过”，实际还需 `MEFINDER_BROWSER_TESTS=1` 才运行，避免试运行期间拖慢或干扰本机全量门禁；CI 专用任务设 REQUIRED 模式。

## 2026-09-29 追加：外部复验后的两处收尾

- **队列快照未完全隔离（事实，已修）**：外部复验发现 `snapshot()` 只复制数组，任务项对象仍与队列共享，改快照中某项的 `target` 会改变下一项实际启动的对齐目标。当前界面没有这样修改，未见用户可见故障，但“改副本不影响队列”的约定不成立。现入队与返回快照时都逐项复制；复现测试先红后绿（改快照项、入队后改调用方原对象，均不影响队列）。
- **浏览器回归纳入门禁**：CI `browser-regression` 在 2026-09-29 共 9 次运行全部通过（3 个分支 + 6 次 main），去掉 `continue-on-error`。原生双窗口交接仍不在其范围，需单独验收。

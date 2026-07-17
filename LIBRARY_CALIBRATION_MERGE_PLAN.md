# 文献库与页码校准合并方案

日期：2026-07-17
状态：待实施

## 1. 动机

文献库和页码校准在"列表层"已经完全同构：搜索、排序（字段 + 方向）、列表/卡片双视图、类型或状态筛选、点击进入右侧详情。差异只剩两点：

- 数据源不同：文献库用 `/api/sources`（原始 source_files/volumes/works），页码校准用 `/api/calibration-library`（`build_calibration_library()` 生成的卡片级投影，含状态、映射摘要、书目元数据合并结果）。
- 详情不同：文献库抽屉显示书目信息编辑器 + 收录文献列表；校准抽屉显示自动检测面板 + 分段映射编辑器。

当前的割裂证据：文献库抽屉里的"自动检测页码"（`openCalibrationAndDetect`）和"编辑区间"（`openCalibrationForSource`）按钮都是跨页跳转——用户在文献库看到一本书想校准页码，应用把他送到另一个页面再找一次同一本书。书目信息编辑器也在两个抽屉里各存在一份入口。

结论：合并为单一"文献库"页面，页码校准降级为 PDF 文献详情中的一个区域。

## 2. 目标形态

### 2.1 侧栏

去掉"页码校准"入口。侧栏变为：文献检索 / 文献库 / 文献导入 / 设置。

### 2.2 列表区（合并后的文献库）

- 顶部统计胶囊沿用校准页的五个语义状态（PDF 总数 / 已校准 / 待校准 / 待确认 / 检测失败），点击即筛选；点击任一状态胶囊时隐含"仅 PDF"。
- 筛选行：类型分段控件（全部 / Word / PDF）+ 状态胶囊 + 搜索框 + 排序（字段/方向）+ 双视图切换，各保留一份。
- 排序字段取两页的并集：导入时间、书名、作者、最近修改时间、来源类型、校准状态。
- 卡片/列表行沿用现有 `.library-card` / `.library-row` 样式；PDF 项显示校准状态徽章和"缺少元数据"警告（数据已在统一投影中）。
- Word 项不参与校准状态体系，状态位置显示来源结构标签（目录页码范围 / 分节推断）。

### 2.3 详情抽屉（点击一条文献后）

垂直分区，自上而下：

1. **标题区**：书名、丛书名、类型徽章、状态徽章。
2. **书目信息**：现有 `bibliographicEditorHTML` 编辑器（PDF），Word 显示只读卷次信息。
3. **收录文献**：Word 卷的 works 列表（现有 drawer-work-item）。
4. **页码校准**（仅 PDF）：整体搬入现校准抽屉内容——自动检测面板（`cal-auto-preview`）、分段映射表（`cal-segments-body`）、页码预览、保存/放弃按钮。DOM id 原样保留以减少 JS 与测试改动。
5. **操作区**：打开原文 / 从文献库移除（危险区）。

抽屉长度增加的缓解：页码校准区域默认折叠为一行摘要（状态 + 映射摘要 + "展开编辑"），点击展开编辑器；`openCalibrationAndDetect` 深链改为"打开抽屉并展开校准区 + 触发检测"。

### 2.4 布局

沿用校准页已验证的 36/64 详情工作区分栏与窄窗口全宽详情面板行为（`test_detail_workspace_uses_a_36_64_split`、`test_narrow_window_switches_to_a_full_width_detail_panel` 继续有效）。

## 3. 数据层

新增统一端点 `/api/library`（或扩展 `/api/calibration-library`）：

- `calibration_library.py::build_calibration_library` 扩展为 `build_library`：不再跳过 Word 源；Word 项输出 `source_type: "word"`、卷次/works 信息、无校准状态字段。
- PDF 项字段不变（status/status_group/mapping_summary/segments/bibliographic 等）。
- `stats` 保持仅统计 PDF。
- 旧 `/api/sources` 与 `/api/calibration-library` 保留一个版本周期（桌面包升级期间的兼容），前端不再调用。

前端删除 `libSources/libVolumes/libWorks` 与校准页各自的状态副本，统一为一份 `libraryItems` 状态 + 一份渲染管线。

## 4. 迁移步骤（每步可独立验证）

1. **git init 并提交当前基线**（见 §6 风险——当前 .git 为空，重构无回退点）。
2. 后端：`build_library` 纳入 Word 源；新增 `/api/library`；单元测试先行（Word 项字段、stats 不变）。
3. 前端列表层：文献库页改用 `/api/library`；把状态胶囊、状态筛选并入文献库工具栏；删除校准页独立的搜索/排序/视图控件状态。
4. 前端详情层：校准编辑器 DOM 整体移入文献库抽屉（id 不变）；实现折叠摘要；改写 `openCalibrationAndDetect` / `openCalibrationForSource` 为抽屉内滚动展开。
5. 删除 `page-calibration` 页面 DOM、侧栏入口、`navigateTo('calibration')`；`calibration_view` 偏好并入 `library_view`（读取旧键做一次迁移）。
6. 测试更新：`test_calibration_library_ui.py`、`test_search_controls_and_views.py`、`test_page_label_and_calibration_layout.py` 中断言页面归属的部分改为断言抽屉归属；Playwright `verify_search_views.py` 更新导航路径。
7. 全量测试 + Playwright 截图验证 + 重建桌面包。

预期净删减：工具栏/列表渲染/状态管理约 300–500 行（web.py 现 5763 行）。

## 5. 不合并的替代方案（已评估，不推荐）

- **仅共享组件、保留两页**：消除代码重复但不消除"同一本书两处入口"的心智负担，跨页跳转仍在。
- **校准改为模态对话框**：分段表 + 自动检测证据面板内容密度高，模态内滚动体验差，且丢失 36/64 工作区布局。

## 6. 风险

- **无版本控制**：`.git` 目前是空目录，5700 行单文件重构无回退手段。第 1 步必须先建立 git 基线。
- **测试断言页面 id**：缓解——校准编辑器 DOM id 全部原样迁移。
- **抽屉过长**：缓解——校准区默认折叠。
- **桌面包滞后**：`dist/MEFinder` 为 7-15 构建，落后于主题修正与本次合并，完成后需 `build_desktop.cmd full` 重建。

## 7. 顺带优化建议（独立于合并，另行排期）

1. **web.py 拆分**：5763 行单字符串（HTML+CSS+JS 内嵌 Python）。建议拆为 `static/app.css`、`static/app.js`、`templates/index.html`，构建时读入拼接（PyInstaller data files 需同步 `desktop.spec`）。合并完成后做，避免两次大 diff 叠加。
2. **书目元数据补全**：多本 PDF 卡片显示"缺少：出版社、出版地、出版年份"（劳动的主权者、伦理学简史、批判理论等），影响 GB/T 引文导出完整性。可在应用内逐本补录。
3. **PDF 卡片"1 篇"指标**：每本 PDF 恒为 1 篇，无信息量；PDF 卡片此位置改显页数（`page_count` 已在投影中），Word 卡片保留篇数。
4. **JSON 导出改为可选**：`data/index.json` 281MB 仅作备份，每次重建索引都全量重写。建议 `build-index --export-json` 显式开启，缩短重建时间、省一半磁盘。
5. **搜索页文献范围下拉复用统一投影**：合并后 `renderSearchDocumentOptions()` 改用 `/api/library` 的标题/作者字段，与文献库显示口径一致。

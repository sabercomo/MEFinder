# Issue #16 / PR #21 繁简统一检索验证

2026-09-08：恢复后的实现通过全量源码门禁，尚未进行 Windows/macOS 成品构建或发布。

## 范围与环境

基于原 PR `709d5244`，合并主线 `1bed83f5` 后恢复修复。此前临时环境中的未推送提交丢失；本报告使用重新执行的验证，不复用旧测试结果。

Python 3.12；opencc-python-reimplemented 0.1.7；Node；SQLite/JSON 两后端。测试数据为自建小型语料，无私人文献数据、无联网搜索依赖。

## 复现证据

将原 PR 的 script_search.py 放回当前测试环境，以下三项真实 SQLite 测试全部失败；恢复修复后通过：

- `test_traditional_exact_hit_beats_original_script_fuzzy_hit`：原字形模糊命中挤掉繁体精确命中。
- `test_total_and_more_include_cross_script_truncation`：返回条数被冒充总数，截断信息失真。
- `test_same_paragraph_different_offsets_are_distinct`：同一段落的不同字形位置被错误合并。

## 修复后验证

- `python3 -m unittest discover -t . -s tests`：2040 项，125.139 秒，OK，跳过 20 项（既有私有语料或环境条件）；无失败。
- `python3 -m ruff check .`：通过。
- 新集成回归 18 项：JSON/SQLite、限额、短词与反向检索、空集合范围、关闭/不可用直通、compact 回退、Unicode 偏移与 PDF 页码字段；原库文件字节保持一致。
- HTTP 回归实际调用 `/api/preferences`、`/api/search` 和 `/api/document/export-markdown`：开关即时生效，繁体 EPUB 原文在 MD 中保留，切换前后导出字节一致。
- Node 执行完整的 `60-settings.js`（只模拟 DOM/网络）：保存期间禁用、重复点击不重复写入、失败回退、组件不可用时禁用；通过。
- 无 OpenCC 的独立环境：转换与包装器 27 项，OK，跳过 14 项转换依赖用例，其余降级用例执行。
- PyInstaller `collect_data_files('opencc')` 收集 26 个文件，确认 t2s/s2t 配置与 ST/TS 字词典在内。
- 偏好全量快照、并发更新、设置分类与前端装配指纹同步通过。

## 尚未验证与功能边界

未进行真实浏览器视觉验收、Windows/macOS 完整安装包构建或大型文献库性能测量。上述词典收集检查不等价于成品冒烟测试。

只扩展查询，保留原文；OpenCC 通用 t2s/s2t 不承诺地区同义词、所有繁简混写或一对多字形全覆盖。MCP 与文本对齐路径未接入偏好。按指定页码导出 MD、跨页脚注依赖补全不在本 PR 中实现。

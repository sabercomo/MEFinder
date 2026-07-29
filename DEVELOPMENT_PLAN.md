# 页码锚点与结构化阅读器开发计划（v0.2.3 → v0.3.0）

定稿日期：2026-07-29
基线：`main` = tag `v0.2.2` = `d1ba838`（Win/macOS 已统一发布，tag 冻结不可移动）

本计划由三轮复核合并而成（Codex 初版 → Opus 复核 → Codex 修订 → 本定稿），已定案条目不再重议。

## 背景

参考 mlread（https://github.com/lldd1226/mlread）的设计思路：页码锚点、滚动页码判定、点击页码生成引文、可恢复深链。

约束：

- mlread 采用 AGPL-3.0。只阅读其 README 与 docs 理解思路，**不阅读、不复制其源码、脚本或资源**，MEFinder 独立实现。
- 动工前先为 MEFinder 本身确定 LICENSE（当前仓库没有 LICENSE 文件，默认保留所有权利；须显式决定）。
- MEFinder 已有的自动页码检测（Page Label / 数字书签 / 页眉页脚候选 / MinerU 块候选 + 分段拟合）优于 mlread 的人工预处理方案，此部分不参考。

## 设计决定（已定案）

1. **页锚点 = 现有 `pdf_page_id`**（`{source_file_id}-PAGE-{pdf_page_index:06d}`）。
   - `source_file_id` 为内容哈希派生（`pdf_extractors.py:149`、`pdf_import_service.py:221`），同一文件跨解析器稳定。
   - 两处生成格式不同（`pdf-{sha[:12]}` 与 `pdf-import-{sha[:16]}`）：锚点一律以 config 持久化的 ID 为准，任何路径不得对已导入文件重算 ID。
2. **偏移单位 = Unicode 码点**，相对该记录的 `text_raw` 原样文本（不 strip、不归一化）。
   - Python 索引即码点；JS 是 UTF-16 码元。前端消费偏移做高亮时必须先做码点 → UTF-16 转换。此规则写入接口文档并有 emoji/增补平面字符测试兜底。
3. **`text_source_spans` 精确区间映射**：每个 PDF paragraph 保存"合成文本区间 ↔ 原页 `text_raw` 区间"。
   - 普通页 paragraph 一个区间；CROSS 两个区间（左页尾、右页头各一段连续区间）。
   - 拼接符（`\n`）等未被区间覆盖的字符**无页映射**；消费方跳过即可。
   - 区间在抽取时计算并落库，消费时不得重推导。
   - **硬性不变式**：每个区间必须满足 `paragraph_text[p_start:p_end] == page_text[page_start:page_end]`（逐字符相等）。实现必须直接保留原始 `text_raw` 的连续切片，不得先重组文本再反推偏移——现有 `strip_pdf_page_header_for_cross`（`pdf_extractors.py:1010`）的 `splitlines()` + `"\n".join()` 会把 CRLF 等分行符归一化，重组结果不再是原文连续切片，须改造为定位原文偏移后从原文直接切片。
4. **页码显示由 `page_source_type` 驱动**，单一 helper 按映射表统一输出（不硬编码状态数量），搜索结果、阅读器页码条、引文入口共用：
   - 已校准（`manual_segment` / `manual_page` / `fixed_offset` / 验证过的 `printed_page_ocr` / 抽验过的 `pdf_page_label`）：`引用页码：38`
   - 仅有未验证 Page Label：`PDF 标签页：38，引用页码尚未校准`
   - 完全未校准 PDF：`PDF 第 38 页，引用页码尚未校准`
   - DOCX 分节推断（`section_break_inferred`）：`第 38 页（分节推断，未验证）`
   - 旧 DOC 目录范围（`toc_range_bound`）：`目录范围 38–45（非段落精确页码）`
   - 无页码来源（`unknown`）：`页码尚未解析`
   - 新增来源类型时扩展 helper 的映射表，不改调用方。
5. **引文只走 `citations.py` 的 `build_citation_formats`**。前端不得另写格式化；未校准时沿用现有"页码未验证"拒绝行为，绝不回落到物理页序冒充。
6. **阅读器渲染源**：PDF 渲染 `pdf_pages`，**绝不渲染 PDF paragraphs**（会重复且 CROSS 是合成文本）；Word 按已抽取 paragraph 顺序渲染。DOCX 可在 `page_label` 变化处插入“分节推断”锚点；旧 DOC 仅有近似文本块且没有段落页锚点，只显示文献级目录范围。
7. **深链恢复阶梯**（v0.3.0 落地，字段 v0.2.3-A 预留）：
   1. `pdf_page_id` + `page_text_hash` 一致 → 按页内码点偏移精确定位高亮；
   2. hash 不一致 → 在同一物理页内检索携带的命中原句片段（≤50 码点）；
   3. 找不到 → 只跳到该物理页，提示"文本已变化，无法精确高亮"。
8. **旧索引降级**：paragraph 缺少 `text_source_spans` 时只支持跳页，不谎称精确高亮。降级按 paragraph 逐条判定（部分重导入后索引可能混合新旧），不设全局开关；`metadata` 表另记 `anchor_spec_version = 1` 供能力探测。
9. **空白页/纯图页**：`pdf_pages` 有记录但 `text_raw` 为空的页，阅读器显示占位（"本页无文本层"），不塌陷、不跳号。
10. **发布纪律**：v0.2.2 资产与 tag 冻结；所有改动进 v0.2.3+。新增测试必须显式加入 `build_macos.sh` 白名单与 Windows 构建脚本（白名单不自动发现新文件）；之后再统一两端测试入口。

## 版本计划

| 版本 | 内容 | 回退面 |
|---|---|---|
| v0.2.3-A | 数据层：spans、命中偏移、页文本哈希、页码显示按 `page_source_type` 统一、旧索引降级 | 无 UI 变化，纯数据层 |
| v0.2.3-B（并行独立提交组） | 产线续跑：MinerU 分段续跑、视觉解析逐页检查点、失败任务恢复 | 独立于 A，可单独回退 |
| v0.2.4 | 结构化阅读器：分页接口、窗口化渲染、搜索结果跳锚点（PDF + Word） | 只回退 UI，数据层保留 |
| v0.3.0 | 滚动页码条、点击页码复制引文、跨页选择页码范围、可恢复深链 | — |

## v0.2.3-A 实施清单

数据层：

- [ ] 改造 `strip_pdf_page_header_for_cross` 为"返回原文偏移"而非"返回重组文本"，CROSS 文本从原页 `text_raw` 直接切片，保证不变式成立。
- [ ] `make_pdf_paragraphs` / `base_pdf_paragraph`：为普通页与 CROSS 段落写入 `text_source_spans`（区间格式见下），存入 paragraph payload。
- [ ] 每个 `pdf_pages` 记录保存 `page_text_hash`（`text_raw` UTF-8 编码后 sha256 前 16 位十六进制）。
- [ ] `metadata` 写入 `anchor_spec_version = 1`。
- [ ] 旧索引兼容：读取路径对缺失字段返回 None，不报错、不静默补假值。

```json
{
  "text_source_spans": [
    {
      "paragraph_char_start": 0,
      "paragraph_char_end": 900,
      "pdf_page_id": "pdf-xxxxxxxxxxxx-PAGE-000010",
      "page_char_start": 1350,
      "page_char_end": 2250
    }
  ]
}
```

搜索接口（`search.py` `_format_result`）新增字段：

- [ ] `match_start` / `match_end`：命中区间，相对 paragraph `text_raw` 的码点偏移（现有 `start`/`end` 已在 `search.py:387` 处按 `text_raw` 长度钳制，直接暴露）。
- [ ] `match_offset_unit`: 固定 `"unicode_codepoint"`。
- [ ] `page_match_spans`：命中区间与 `text_source_spans` 逐段求交的结果，每段 `{pdf_page_id, page_char_start, page_char_end}`；CROSS 命中跨两页时返回两段。求交公式：`overlap = [max(match_start, s.paragraph_char_start), min(match_end, s.paragraph_char_end))`，非空时映射页内偏移 `page_char_start + (overlap_start - s.paragraph_char_start)`。
- [ ] 无 spans 的旧数据：`page_match_spans` 为空数组，另给 `precise_highlight_available: false`。

页码显示：

- [ ] 实现按 `page_source_type` 映射的统一 helper（含 DOCX 分节推断、旧 DOC 目录范围、无来源三种 Word 状态），替换搜索结果与后续阅读器的分散拼串。
- [ ] 引文入口继续调用 `build_citation_formats`，不改 `citations.py` 的未校准拒绝行为。

测试（全部显式加入 Mac/Windows 发布门槛）：

- [ ] **不变式属性测试**：对生成的全部 spans 逐条断言 `paragraph_text[p_start:p_end] == page_text[page_start:page_end]`，样本须覆盖 CRLF、` ` 等 `splitlines()` 会识别的分行符。
- [ ] CROSS 命中 → 两页 `page_match_spans` 正确（含右页删行偏移）。
- [ ] 空白页：paragraph 跳过但 `pdf_pages` 有记录。
- [ ] 同页重复原句：命中区间与 spans 求交唯一确定。
- [ ] emoji / 增补平面字符：码点偏移正确（防 UTF-16 混用）。
- [ ] native → MinerU 重解析：`pdf_page_id` 不变、`page_text_hash` 变化被检出。
- [ ] 旧索引（无 spans）：降级为只跳页，`precise_highlight_available = false`。
- [ ] 页码显示按 `page_source_type` 逐态断言（含 PDF 三态与 Word 三态）。

## v0.2.3-B 实施清单（并行，独立提交与测试组）

- [ ] 导入任务清单持久化：`{file_hash, total_pages, completed_pages, failed_pages, last_updated}`，衔接现有 `import_queue.py` 与 `pdf_import_runs` 表。
- [ ] MinerU 分段续跑：按分段保存检查点；注意 MinerU `page_idx` 从每分段内部 0 开始，叠加分段起始页 offset（已知陷阱）。
- [ ] 视觉 API 逐页检查点：失败页记录原因，续跑只重试失败页。
- [ ] 中断恢复：进程重启后从清单续跑，不整本重解析。
- [ ] 跨页合并保守规则不变：无法判断时保留两段。
- [ ] 不自动上传用户文献；"导出本地 Markdown"留作以后可选功能。
- [ ] 测试组独立命名（如 `test_import_resume*`），与 A 组互不依赖。

## v0.2.4 实施清单（结构化阅读器）

- [ ] 后端分页接口 `GET /api/document/pages?source_id=&start=&count=`：按页返回 `text_raw`、锚点、由 `page_source_type` 产生的页码显示状态、`page_text_hash`；Word 文档按 paragraph 分窗返回。
- [ ] 前端阅读器独立文件（不再塞进 `app.js` 单文件；顺带为 `web.py` 抽路由表）。
- [ ] DOM 窗口化：视口 ±N 页留在 DOM，其余卸载；900 页文档首版即达标。
- [ ] IntersectionObserver 判定当前页（不用 scroll 位置计算）。
- [ ] 每页独立容器 + `id` 锚点；空文本页显示占位。
- [ ] 搜索结果"查看结构化文本"入口：按 `page_match_spans` 打开对应页并高亮（前端做码点 → UTF-16 转换）；无 spans 时只跳页并提示。
- [ ] PDFKit / 系统阅读器仍是默认打开方式，结构化阅读器是可选按钮，不强制切换。
- [ ] Word 阅读器页码标注按 `page_source_type` 区分：DOCX 显示"分节推断，未验证"；旧 DOC 无页锚点（`page_anchors` 为空），只在文献级显示目录范围，不插段落页锚点；均与 PDF 已校准页码视觉区分。
- [ ] 原生 PDF、MinerU、视觉 API 三种来源共用同一渲染格式。

## v0.3.0 实施清单（页码条与交互）

- [ ] 滚动页码条：随当前页更新，严格按 `page_source_type` 映射文案显示，未校准绝不显示裸"第 N 页"。
- [ ] 点击页码 → 调 `build_citation_formats` 复制中文脚注 / GB/T 7714；未校准时显示"页码未验证"并拒绝带页码引文。
- [ ] 跨页选择文字 → 自动生成页码范围（仍走 `citations.py`）。
- [ ] 深链：`/reader?source=&page=&off=&h=&q=`，按恢复阶梯执行；关闭重开回到原位置。
- [ ] 长文档滚动性能复测（上百页、快速拖动滚动条）。

## 发布测试矩阵（v0.3.0 前全部覆盖）

1. 原生文字 PDF
2. 扫描 PDF（MinerU / 视觉 API）
3. 罗马数字前言 + 阿拉伯数字正文
4. PDF Page Label 错误或缺失
5. 书本页码与物理页不一致
6. 跨页段落与跨页引文（CROSS 命中 → 阅读器精确定位）
7. 上百页文献滚动性能
8. native → MinerU 重解析后旧深链行为（恢复阶梯三级各验一次）
9. 未校准文档点击页码：拒绝带页码引文
10. 空白页 / 纯图片页占位渲染
11. Word `.docx` 与 `.doc`（第 2–10 卷路径）
12. 旧索引（v0.2.2 及更早生成）降级行为

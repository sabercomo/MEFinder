# 建议数据结构

审计日期：2026-07-05

本文件只提出后续索引阶段的数据结构，不实现搜索程序，也不设计 UI。

## 设计原则

- 原始 Word 文档只读保存，不写回。
- 所有抽取结果都要记录来源、工具版本和可信度。
- “原书页码”必须和段落序号、PDF 页序号、字符偏移分开存储。
- 文献边界、页码、标题识别都允许多种来源和置信度。
- 检索命中结果要能追溯到卷、文献、页码、段落和原文上下文。

## `source_file`

记录原始文件。

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_file_id` | string | 文件唯一 ID |
| `relative_path` | string | 例如 `corpus/raw_docx/...doc` |
| `volume_number` | integer | 1-10 |
| `file_format` | enum | `docx` 或 `doc` |
| `container_format` | enum | `openxml_zip`、`ole_cfb` |
| `file_name` | string | 原始文件名 |
| `size_bytes` | integer | 文件大小 |
| `sha256` | string | 文件哈希 |
| `last_modified` | datetime | 文件修改时间 |
| `audit_notes` | string | 格式异常、命名异常等 |

## `volume`

记录卷级信息。

| 字段 | 类型 | 说明 |
|---|---|---|
| `volume_id` | string | 例如 `MEWJ-01` |
| `volume_number` | integer | 卷次 |
| `display_title` | string | 例如 `马克思恩格斯文集 第1卷` |
| `period_label` | string | 例如 `1843-1848` |
| `primary_structure` | enum | `article_collection`、`monograph`、`letters`、`manuscript_selection`、`mixed` |
| `source_file_id` | string | 关联原始文件 |

## `work`

记录独立文献、书信、专著或专著内主要单元。

| 字段 | 类型 | 说明 |
|---|---|---|
| `work_id` | string | 文献 ID |
| `volume_id` | string | 所属卷 |
| `parent_work_id` | string/null | 支持篇、章、节层级 |
| `work_order` | integer | 卷内顺序 |
| `title` | string | 标题 |
| `subtitle` | string/null | 副标题 |
| `author_label` | string/null | 如 `卡·马克思`、`弗·恩格斯` |
| `date_label` | string/null | 书信或文献日期 |
| `title_source` | enum | `toc`、`heading_style`、`centered_paragraph`、`manual` |
| `boundary_source` | enum | `toc_page_range`、`heading_match`、`section_break`、`manual` |
| `toc_page_start` | string/null | 目录起始页，字符串以支持罗马数字或特殊页码 |
| `toc_page_end` | string/null | 目录结束页 |
| `start_paragraph_id` | string/null | 正文起始段 |
| `end_paragraph_id` | string/null | 正文结束段 |
| `confidence` | number | 0-1 |
| `notes` | string | 多行标题、疑似注释号等 |

## `paragraph`

记录可检索正文段落。

| 字段 | 类型 | 说明 |
|---|---|---|
| `paragraph_id` | string | 段落 ID |
| `source_file_id` | string | 原始文件 |
| `volume_id` | string | 所属卷 |
| `work_id` | string/null | 所属文献，允许待定 |
| `paragraph_index` | integer | 文件内段落序号，只作内部定位 |
| `section_index` | integer/null | Word 分节序号 |
| `text_raw` | string | 原始抽取文本 |
| `text_normalized` | string | 检索用规范化文本 |
| `text_hash` | string | 段落文本哈希 |
| `style_name` | string/null | Word 样式名 |
| `alignment` | string/null | `center`、`right` 等 |
| `font_summary` | json/null | 主要字体、字号、加粗等 |
| `is_title_candidate` | boolean | 是否标题候选 |
| `is_toc_entry` | boolean | 是否目录项 |
| `is_index_entry` | boolean | 是否索引项 |
| `original_page_start` | string/null | 原书起始页，未验证则为空 |
| `original_page_end` | string/null | 原书结束页 |
| `page_source_type` | enum | 见页码来源分级 |
| `page_confidence` | number | 0-1 |

## `page_anchor`

记录页码锚点。这个表是避免“伪页码”的关键。

| 字段 | 类型 | 说明 |
|---|---|---|
| `page_anchor_id` | string | 页码锚点 ID |
| `volume_id` | string | 所属卷 |
| `source_file_id` | string | 原始文件 |
| `original_page_label` | string | 原书页码，如 `3`、`21` |
| `page_sequence_in_volume` | integer/null | 内部顺序，不等于原书页码 |
| `section_index` | integer/null | 对应分节 |
| `start_paragraph_id` | string/null | 本页起始段 |
| `end_paragraph_id` | string/null | 本页结束段 |
| `anchor_source_type` | enum | `printed_page_marker`、`word_rendered_page`、`section_break_verified`、`toc_range_bound` 等 |
| `anchor_text` | string/null | 页首或页尾校验文本 |
| `confidence` | number | 0-1 |
| `validated_by` | enum | `automatic`、`manual_sample`、`manual_full` |
| `validation_notes` | string | 校验说明 |

## `toc_entry`

记录目录项，用于文献边界和页码范围约束。

| 字段 | 类型 | 说明 |
|---|---|---|
| `toc_entry_id` | string | 目录项 ID |
| `volume_id` | string | 所属卷 |
| `paragraph_id` | string | 目录段落 |
| `level` | integer/null | 目录层级 |
| `author_label` | string/null | 目录中的作者 |
| `title_text` | string | 目录标题 |
| `page_start_label` | string/null | 目录起始页 |
| `page_end_label` | string/null | 目录结束页 |
| `raw_text` | string | 原始目录行 |
| `parse_confidence` | number | 0-1 |

## `title_candidate`

记录正文标题候选，供后续边界识别。

| 字段 | 类型 | 说明 |
|---|---|---|
| `title_candidate_id` | string | 候选 ID |
| `paragraph_id` | string | 候选段落 |
| `candidate_text` | string | 候选标题文本 |
| `candidate_kind` | enum | `work_title`、`chapter_title`、`author_line`、`caption`、`unknown` |
| `evidence` | json | 样式、居中、字号、目录匹配等 |
| `confidence` | number | 0-1 |
| `linked_toc_entry_id` | string/null | 若能匹配目录 |

## `search_hit`

后续搜索阶段的返回结构。

| 字段 | 类型 | 说明 |
|---|---|---|
| `hit_id` | string | 命中 ID |
| `query_text` | string | 用户输入 |
| `volume_id` | string | 所属卷 |
| `work_id` | string/null | 所属文献 |
| `paragraph_id` | string | 命中段落 |
| `original_page_label` | string/null | 原书页码 |
| `page_source_type` | enum | 页码来源 |
| `page_confidence` | number | 页码可信度 |
| `matched_text` | string | 命中原文 |
| `context_before` | json | 前若干段 |
| `context_after` | json | 后若干段 |
| `match_type` | enum | 见下表 |
| `match_confidence` | number | 匹配可信度 |
| `normalization_applied` | json | 标点、空白、异体字等处理 |

匹配类型建议：

| 类型 | 含义 |
|---|---|
| `exact` | 原文完全一致 |
| `normalized_exact` | 规范化后完全一致 |
| `punctuation_variant` | 标点、空白差异 |
| `quote_fragment` | 用户输入为段落片段 |
| `cross_paragraph` | 引文跨段 |
| `fuzzy` | 模糊匹配 |
| `manual_verified` | 人工确认 |

## `audit_issue`

记录抽取过程中的异常，避免静默污染索引。

| 字段 | 类型 | 说明 |
|---|---|---|
| `issue_id` | string | 异常 ID |
| `source_file_id` | string | 原始文件 |
| `severity` | enum | `info`、`warning`、`error` |
| `issue_type` | string | 例如 `page_unverified`、`title_ambiguous` |
| `location_ref` | string | 段落、节、目录项等 |
| `message` | string | 说明 |
| `created_at` | datetime | 记录时间 |

## 推荐最小中间产物

正式搜索前，建议先生成以下中间表：

1. `source_file`
2. `volume`
3. `paragraph`
4. `toc_entry`
5. `title_candidate`
6. `page_anchor`
7. `audit_issue`

只有当 `page_anchor` 达到可接受可信度后，才应允许搜索结果显示“准确页码”。否则搜索结果应显示“页码未验证”，并只返回卷次、文献候选、命中段落和上下文。

# PDF 页码模型

日期：2026-07-07

## 核心原则

PDF 页码必须分层保存，不能混用。

必须区分：

1. PDF 文件物理页序。
2. PDF 自带 Page Label。
3. 页面视觉印刷页码。
4. 最终用于文献引用的页码。

不得把 PDF 物理页序直接作为引用页码。

如果无法确认引用页码，搜索结果必须显示：

```text
PDF 第 X 页，引用页码尚未校准
```

跨页命中显示：

```text
PDF 第 X-Y 页，引用页码尚未校准
```

如果已校准：

```text
引用页码：38
引用页码：38-39
```

## 字段定义

### 页级字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `pdf_page_id` | string | PDF 页唯一 ID |
| `source_file_id` | string | 源 PDF 文件 ID |
| `pdf_page_index` | integer | PDF 物理页序，0-based |
| `pdf_page_number_1based` | integer | PDF 物理页序，1-based，仅用于界面提示 |
| `pdf_page_label` | string/null | PDF 内置 Page Label |
| `printed_page` | string/null | OCR 或页面文本检测到的视觉印刷页码 |
| `citation_page` | string/null | 经映射后用于引用的页码 |
| `page_mapping_method` | enum | 页码映射来源 |
| `page_mapping_confidence` | number | 0-1 |
| `text_raw` | string | 页级原始文本 |
| `text_source` | enum | `native_text`、`mineru`、`ocr` |
| `blocks` | array | 页面文本块，保留 page_idx 和坐标 |

### 段落/搜索单元字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_type` | enum | `word` 或 `pdf` |
| `pdf_page_start_index` | integer/null | 命中起始 PDF 物理页序 |
| `pdf_page_end_index` | integer/null | 命中结束 PDF 物理页序 |
| `pdf_page_start_label` | string/null | 起始页 Page Label |
| `pdf_page_end_label` | string/null | 结束页 Page Label |
| `printed_page_start` | string/null | 起始视觉印刷页码 |
| `printed_page_end` | string/null | 结束视觉印刷页码 |
| `citation_page_start` | string/null | 起始引用页码 |
| `citation_page_end` | string/null | 结束引用页码 |
| `page_display` | string | 结果卡片显示页码 |
| `page_source_type` | enum | 与现有 Word 页码字段兼容 |
| `page_confidence` | number | 页码可信度 |
| `is_cross_page` | boolean | 是否跨页 |
| `page_mapping_method` | enum | 同页级字段 |
| `page_mapping_confidence` | number | 映射可信度 |

## 页码映射方法

`page_mapping_method` 建议取值：

| 值 | 含义 | 是否可作为引用页码 |
|---|---|---|
| `pdf_page_label` | 使用 PDF 自带 Page Label | 可用，但需抽样验证 |
| `printed_page_ocr` | OCR/文本层识别视觉页码 | 可用，但需置信度 |
| `fixed_offset` | 固定偏移映射 | 可用，需记录校准点 |
| `manual_segment` | 人工分段映射 | 可用，推荐 |
| `manual_page` | 人工逐页校准 | 可用，最高可信度 |
| `mineru_page_idx` | MinerU 的 `page_idx` 物理页归属 | 不可直接引用 |
| `pdf_page_index` | PDF 物理页序 | 不可直接引用 |
| `uncalibrated` | 未校准 | 不可引用 |

## 固定 offset 映射

适合正文从固定 PDF 页开始，且页码连续的 PDF。

示例：

```json
{
  "method": "fixed_offset",
  "pdf_page_start": 21,
  "citation_page_start": 1,
  "style": "arabic",
  "confidence": 0.9,
  "evidence": "人工确认 PDF 第 22 个物理页显示原书第 1 页"
}
```

计算：

```text
citation_page = citation_page_start + (pdf_page_index - pdf_page_start)
```

限制：

- 只适合连续正文。
- 遇到插页、彩图、前言罗马页码、重启页码时必须改用分段映射。

## 分段映射

适合存在封面、目录、罗马页码、正文页码等多段结构。

示例：

```json
{
  "source_file_id": "pdf-0001",
  "segments": [
    {
      "pdf_page_start": 0,
      "pdf_page_end": 10,
      "citation": null,
      "label": "front_matter_without_citation"
    },
    {
      "pdf_page_start": 11,
      "pdf_page_end": 20,
      "citation_page_start": "i",
      "number_style": "roman_lower",
      "method": "manual_segment",
      "confidence": 0.9
    },
    {
      "pdf_page_start": 21,
      "pdf_page_end": 420,
      "citation_page_start": "1",
      "number_style": "arabic",
      "method": "manual_segment",
      "confidence": 0.95
    }
  ]
}
```

## PDF Page Label

PDF Page Label 是 PDF 文件内部的页标签，不等于引用页码，但可作为强线索。

处理规则：

1. 如果 PDF 有 PageLabels，保存到 `pdf_page_label`。
2. 不自动提升为 `citation_page`。
3. 只有通过抽样验证后，`page_mapping_method` 才可设为 `pdf_page_label`。
4. 若 Page Label 与视觉印刷页码不一致，以人工校准为准。

## 视觉印刷页码

视觉印刷页码可以来自：

- 原生文本页眉/页脚识别。
- OCR 页眉/页脚识别。
- MinerU 块坐标识别。

识别策略：

1. 只检查页眉/页脚区域的短文本块。
2. 排除正文中的年份、章节号、脚注号。
3. 支持阿拉伯数字和罗马数字。
4. 保存候选，不直接覆盖引用页码。

字段：

```json
{
  "printed_page_candidates": [
    {
      "text": "38",
      "bbox": [280, 760, 310, 780],
      "position": "footer_center",
      "confidence": 0.86
    }
  ]
}
```

## 未校准状态

未校准时：

```json
{
  "pdf_page_index": 37,
  "pdf_page_number_1based": 38,
  "citation_page": null,
  "page_mapping_method": "uncalibrated",
  "page_mapping_confidence": 0.0,
  "page_display": "PDF 第 38 页，引用页码尚未校准"
}
```

搜索结果不得显示“第 38 页”而不说明这是 PDF 物理页。

## 跨页命中

跨页命中字段：

```json
{
  "is_cross_page": true,
  "pdf_page_start_index": 37,
  "pdf_page_end_index": 38,
  "citation_page_start": "38",
  "citation_page_end": "39",
  "page_display": "38-39",
  "page_mapping_method": "manual_segment",
  "page_mapping_confidence": 0.95
}
```

未校准跨页：

```json
{
  "is_cross_page": true,
  "pdf_page_start_index": 37,
  "pdf_page_end_index": 38,
  "citation_page_start": null,
  "citation_page_end": null,
  "page_display": "PDF 第 38-39 页，引用页码尚未校准",
  "page_mapping_method": "uncalibrated",
  "page_mapping_confidence": 0.0
}
```

## 与 Word 页码字段兼容

现有 Word 字段：

- `original_page_start`
- `original_page_end`
- `page_display`
- `page_source_type`
- `page_confidence`

PDF 不应删除这些字段，而是补充 PDF 专属字段。

兼容映射：

| Word 现有字段 | PDF 对应 |
|---|---|
| `original_page_start` | `citation_page_start` |
| `original_page_end` | `citation_page_end` |
| `page_display` | 已校准引用页码或未校准提示 |
| `page_source_type` | `page_mapping_method` 的兼容名称 |
| `page_confidence` | `page_mapping_confidence` |

PDF 结果中，如果 `citation_page_start` 为空，则 `original_page_start` 也应为空，避免误用。

## 校验记录

每次页码校准都应有校验记录：

```json
{
  "validation_id": "VAL-PDF-0001-001",
  "source_file_id": "pdf-0001",
  "pdf_page_index": 21,
  "observed_printed_page": "1",
  "expected_citation_page": "1",
  "validated_by": "manual_sample",
  "validated_at": "2026-07-07T00:00:00Z",
  "notes": "人工打开 PDF，确认正文第一页。"
}
```

最低要求：

- 每个 PDF 至少校验正文首页、目录页、正文中段、末页附近。
- 若使用分段映射，每个分段至少校验起点和终点。

## 搜索结果显示规则

| 状态 | 显示 |
|---|---|
| 已校准单页 | `引用页码：38` |
| 已校准跨页 | `引用页码：38-39` |
| 未校准单页 | `PDF 第 38 页，引用页码尚未校准` |
| 未校准跨页 | `PDF 第 38-39 页，引用页码尚未校准` |
| 只有 Page Label 未验证 | `PDF 标签页：38，引用页码尚未校准` |

## 不允许的行为

- 不允许把 `pdf_page_index + 1` 直接写入 `citation_page`。
- 不允许把 Page Label 自动当成引用页码。
- 不允许只保存 Markdown 文本而丢掉 `page_idx`。
- 不允许跨页搜索命中只显示起始页而隐藏结束页。
- 不允许在引用复制文本中省略页码来源提示。

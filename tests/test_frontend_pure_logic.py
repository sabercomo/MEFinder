"""前端纯逻辑的单元测试。

页码映射算术过去困在 updateCalPreview 的 DOM 读写里，无法单独测试。阶段 3 把它
逐位抽到 static/js/06-pure.js 的 calibrateCitationForIndex 后，这里用 node 执行该纯
函数，对照**独立手算**的期望值，第一次为这段核心算术建立回归覆盖。

node 不可用时整类跳过（与构建门禁一致：node 已是打包/语法检查的既有依赖）。
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PURE_JS = ROOT / "src" / "me_finder" / "static" / "js" / "06-pure.js"
NODE = shutil.which("node")

# node 侧薄壳：eval 纯函数源码（与浏览器里拼进同一 script 作用域同理），
# 按 {fn, args} 逐个 case 调用具名纯函数，回吐 JSON。
_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
eval(src);
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = cases.map(function (c) {
  return eval(c.fn).apply(null, c.args);
});
process.stdout.write(JSON.stringify(out));
"""


def _call(fn, *args):
    """在 node 里调一次 06-pure.js 的具名纯函数，返回其结果。"""

    return _run([{"fn": fn, "args": list(args)}])[0]


def _run(cases):
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        payload = Path(tmp) / "cases.json"
        harness.write_text(_HARNESS, encoding="utf-8")
        payload.write_text(json.dumps(cases), encoding="utf-8")
        # 必须显式指定 utf-8：Windows 默认按 GBK 解码 node 的 stdout，
        # 返回值含中文（页码标签、映射名）时会 UnicodeDecodeError。
        result = subprocess.run(
            [NODE, str(harness), str(PURE_JS), str(payload)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return json.loads(result.stdout)


# 当纯函数的入参需要带方法的对象（如 getBoundingClientRect）时，JSON 无法承载，
# 改用这个薄壳：eval 06-pure.js 后再 eval 一段任意表达式，回吐其 JSON 结果。
_EVAL_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
eval(src);
const expr = fs.readFileSync(process.argv[3], 'utf8');
process.stdout.write(JSON.stringify(eval(expr)));
"""


def _eval(expr):
    """eval 06-pure.js 后执行一段 JS 表达式，返回其结果（用于需带方法入参的纯函数）。"""

    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "eval_harness.js"
        exprfile = Path(tmp) / "expr.js"
        harness.write_text(_EVAL_HARNESS, encoding="utf-8")
        exprfile.write_text(expr, encoding="utf-8")
        result = subprocess.run(
            [NODE, str(harness), str(PURE_JS), str(exprfile)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return json.loads(result.stdout)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class CalibrateCitationForIndexTests(unittest.TestCase):
    def _check(self, segments, page_index, expected):
        got = _call("calibrateCitationForIndex", segments, page_index)
        self.assertEqual(got, expected)

    def test_arabic_at_segment_start(self):
        seg = {"pdf_page_start": 5, "pdf_page_end": 10,
               "citation_page_start": "1", "number_style": "arabic"}
        self._check([seg], 5,
                    {"mapped": "1", "mappedEnd": "1", "method": "manual_segment"})

    def test_arabic_with_offset(self):
        seg = {"pdf_page_start": 5, "pdf_page_end": 10,
               "citation_page_start": "1", "number_style": "arabic"}
        self._check([seg], 7,
                    {"mapped": "3", "mappedEnd": "3", "method": "manual_segment"})

    def test_spread_counts_two_logical_pages(self):
        seg = {"pdf_page_start": 0, "pdf_page_end": 100,
               "citation_page_start": "1", "layout_mode": "spread",
               "number_style": "arabic"}
        # 首页 → 1-2，第三个 PDF 页 → 5-6。
        self._check([seg], 0,
                    {"mapped": "1", "mappedEnd": "2", "method": "manual_segment"})
        self._check([seg], 2,
                    {"mapped": "5", "mappedEnd": "6", "method": "manual_segment"})

    def test_roman_lower(self):
        seg = {"pdf_page_start": 0, "pdf_page_end": 20,
               "citation_page_start": "1", "number_style": "roman_lower"}
        self._check([seg], 3,
                    {"mapped": "iv", "mappedEnd": "iv", "method": "manual_segment"})

    def test_roman_upper(self):
        seg = {"pdf_page_start": 0, "pdf_page_end": 20,
               "citation_page_start": "1", "number_style": "roman_upper"}
        self._check([seg], 8,
                    {"mapped": "IX", "mappedEnd": "IX", "method": "manual_segment"})

    def test_segment_method_is_preserved(self):
        seg = {"pdf_page_start": 0, "pdf_page_end": 5,
               "citation_page_start": "1", "number_style": "arabic",
               "method": "auto_sequence"}
        self._check([seg], 0,
                    {"mapped": "1", "mappedEnd": "1", "method": "auto_sequence"})

    def test_empty_citation_segment_reports_its_method(self):
        seg = {"pdf_page_start": 0, "pdf_page_end": 5,
               "citation": None, "method": "auto_scan"}
        self._check([seg], 2,
                    {"mapped": None, "mappedEnd": None, "method": "auto_scan"})

    def test_no_matching_segment_is_uncalibrated(self):
        seg = {"pdf_page_start": 0, "pdf_page_end": 2,
               "citation_page_start": "1", "number_style": "arabic"}
        self._check([seg], 10,
                    {"mapped": None, "mappedEnd": None, "method": "uncalibrated"})

    def test_non_numeric_base_falls_back_to_one(self):
        seg = {"pdf_page_start": 0, "pdf_page_end": 5,
               "citation_page_start": "abc", "number_style": "arabic"}
        self._check([seg], 0,
                    {"mapped": "1", "mappedEnd": "1", "method": "manual_segment"})

    def test_missing_page_end_matches_only_start(self):
        seg = {"pdf_page_start": 5, "citation_page_start": "3",
               "number_style": "arabic"}
        self._check([seg], 5,
                    {"mapped": "3", "mappedEnd": "3", "method": "manual_segment"})
        self._check([seg], 6,
                    {"mapped": None, "mappedEnd": None, "method": "uncalibrated"})

    def test_first_matching_segment_wins(self):
        segs = [
            {"pdf_page_start": 0, "pdf_page_end": 10,
             "citation_page_start": "1", "number_style": "arabic"},
            {"pdf_page_start": 5, "pdf_page_end": 20,
             "citation_page_start": "100", "number_style": "arabic"},
        ]
        # pageIndex 7 落在两段重叠区，取先出现的段（start 0、base 1）：
        # offset = (7-0)*1 = 7，citNum = 1+7 = 8。
        self._check(segs, 7,
                    {"mapped": "8", "mappedEnd": "8", "method": "manual_segment"})


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class FirstPageValueTests(unittest.TestCase):
    """按优先级取首个非空值：空串、纯空白、null、undefined 都算空。"""

    def test_first_non_empty_wins(self):
        self.assertEqual(_call("firstPageValue", ["", "12", "34"]), "12")

    def test_values_are_trimmed(self):
        self.assertEqual(_call("firstPageValue", ["  7  "]), "7")

    def test_blank_and_null_are_skipped(self):
        self.assertEqual(_call("firstPageValue", [None, "   ", "9"]), "9")

    def test_all_empty_returns_empty_string(self):
        self.assertEqual(_call("firstPageValue", [None, "", "  "]), "")

    def test_zero_is_kept_as_a_real_value(self):
        # 0 是合法页码，不能被当成空值跳过。
        self.assertEqual(_call("firstPageValue", [0, "5"]), "0")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class UncalibratedPageLabelTests(unittest.TestCase):
    def test_placeholder_texts_are_detected(self):
        for text in ["页码尚未校准", "引用页码尚未校准", "页码未验证", "未校准"]:
            self.assertTrue(_call("isUncalibratedPageLabel", text), text)

    def test_real_page_labels_are_not_flagged(self):
        for text in ["第12页", "12", "iv", ""]:
            self.assertFalse(_call("isUncalibratedPageLabel", text), text)

    def test_null_is_not_flagged(self):
        self.assertFalse(_call("isUncalibratedPageLabel", None))


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class FormatChinesePageRangeTests(unittest.TestCase):
    """中文页码范围拼接。重点是不产生「第第…页页」这类重复词缀。"""

    def _fmt(self, start, end=None):
        return _call("formatChinesePageRange", start, end)

    def test_bare_numbers_get_wrapped(self):
        self.assertEqual(self._fmt("12", "15"), "第12—15页")

    def test_single_page_omits_range(self):
        self.assertEqual(self._fmt("12"), "第12页")

    def test_identical_start_and_end_collapse(self):
        self.assertEqual(self._fmt("12", "12"), "第12页")

    def test_shared_prefix_is_not_duplicated(self):
        self.assertEqual(self._fmt("第12页", "第15页"), "第12—15页")

    def test_prefixed_start_with_bare_end(self):
        self.assertEqual(self._fmt("第12页", "15"), "第12—15页")

    def test_prefixed_start_alone_is_returned_as_is(self):
        self.assertEqual(self._fmt("第12页"), "第12页")

    def test_volume_qualified_prefix_is_preserved(self):
        self.assertEqual(self._fmt("上册第3页", "上册第5页"), "上册第3—5页")

    def test_empty_start_reports_uncalibrated(self):
        self.assertEqual(self._fmt("", "15"), "页码尚未校准")

    def test_uncalibrated_start_reports_uncalibrated(self):
        self.assertEqual(self._fmt("未校准", "15"), "页码尚未校准")

    def test_uncalibrated_end_is_dropped(self):
        self.assertEqual(self._fmt("12", "页码未验证"), "第12页")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class FormatCitationPageLabelTests(unittest.TestCase):
    """PDF 走 citation_* 字段，其余来源走原始页码字段；两条路径优先级不同。"""

    def test_pdf_prefers_citation_label(self):
        item = {"source_type": "pdf", "citation_page_label": "第7页",
                "citation_page_start": "99"}
        self.assertEqual(_call("formatCitationPageLabel", item), "第7页")

    def test_pdf_falls_back_to_citation_page_start(self):
        item = {"source_type": "pdf", "citation_page_start": "7",
                "citation_page_end": "9"}
        self.assertEqual(_call("formatCitationPageLabel", item), "第7—9页")

    def test_pdf_ignores_original_page_fields(self):
        # 非校准字段不该泄漏到 PDF 分支，否则会显示未校准的物理页号。
        item = {"source_type": "pdf", "original_page_start": "300"}
        self.assertEqual(_call("formatCitationPageLabel", item), "页码尚未校准")

    def test_word_uses_original_page_range(self):
        item = {"source_type": "word", "original_page_start": "3",
                "original_page_end": "4"}
        self.assertEqual(_call("formatCitationPageLabel", item), "第3—4页")

    def test_source_type_is_case_insensitive(self):
        item = {"source_type": "PDF", "citation_page_start": "5"}
        self.assertEqual(_call("formatCitationPageLabel", item), "第5页")

    def test_empty_item_reports_uncalibrated(self):
        self.assertEqual(_call("formatCitationPageLabel", {}), "页码尚未校准")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class MappingLabelTests(unittest.TestCase):
    """枚举转中文显示名。未登记的值原样回吐，不猜、不静默变空。"""

    def test_known_method_labels(self):
        self.assertEqual(_call("mappingMethodLabel", "manual_segment"), "人工分段")
        self.assertEqual(_call("mappingMethodLabel", "pdf_page_label"), "PDF标签")

    def test_unknown_method_is_passed_through(self):
        self.assertEqual(_call("mappingMethodLabel", "brand_new"), "brand_new")

    def test_empty_method_is_empty(self):
        self.assertEqual(_call("mappingMethodLabel", None), "")

    def test_known_status_labels(self):
        self.assertEqual(_call("mappingStatusLabel", "manual_mapped"), "人工映射")
        self.assertEqual(
            _call("mappingStatusLabel", "auto_mapped_high"), "自动映射 · 高可信"
        )

    def test_missing_status_defaults_to_unmapped(self):
        self.assertEqual(_call("mappingStatusLabel", None), "未映射")

    def test_confidence_renders_percentage(self):
        self.assertEqual(_call("mappingConfidenceLabel", "high", 0.9), "高（90%）")

    def test_confidence_score_is_rounded(self):
        self.assertEqual(_call("mappingConfidenceLabel", "medium", 0.876), "中（88%）")

    def test_confidence_without_score_omits_percentage(self):
        self.assertEqual(_call("mappingConfidenceLabel", "low", None), "低")

    def test_page_scope_labels(self):
        self.assertEqual(_call("pageScopeLabel", "body"), "正文")
        self.assertEqual(_call("pageScopeLabel", "front_matter"), "前置页")
        self.assertEqual(_call("pageScopeLabel", None), "")

    def test_logical_page_side_labels(self):
        self.assertEqual(_call("logicalPageSideLabel", "left", None), "左页")
        self.assertEqual(_call("logicalPageSideLabel", "both", None), "跨左右页")

    def test_range_fallback_explains_missing_coordinates(self):
        self.assertEqual(
            _call("logicalPageSideLabel", None, "range_fallback"),
            "坐标不足，显示页码范围",
        )

    def test_unknown_side_without_fallback_is_dash(self):
        self.assertEqual(_call("logicalPageSideLabel", None, None), "—")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class MappingEvidenceSummaryTests(unittest.TestCase):
    def test_string_evidence_is_returned_as_is(self):
        self.assertEqual(_call("mappingEvidenceSummary", "手工确认"), "手工确认")

    def test_empty_evidence_is_empty(self):
        self.assertEqual(_call("mappingEvidenceSummary", None), "")

    def test_known_fields_are_joined(self):
        evidence = {"observed_page_numbers": 12, "sequence_consistency": 0.95,
                    "inferred_offset": 3}
        self.assertEqual(
            _call("mappingEvidenceSummary", evidence),
            "识别页码 12 个；连续性 95%；offset 3",
        )

    def test_structure_evidence_is_localized(self):
        evidence = {"structure_evidence": "preface"}
        self.assertEqual(_call("mappingEvidenceSummary", evidence), "结构：序言")

    def test_zero_offset_is_still_reported(self):
        # offset 0 是有意义的结论（无偏移），不能因假值被丢掉。
        self.assertEqual(
            _call("mappingEvidenceSummary", {"inferred_offset": 0}), "offset 0"
        )

    def test_unknown_shape_falls_back_to_json(self):
        self.assertEqual(
            _call("mappingEvidenceSummary", {"weird": 1}), '{"weird":1}'
        )


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class AutoMappingSegmentTextTests(unittest.TestCase):
    """分段摘要。PDF 页序号库内 0 基，展示时 +1 转成人读页号。"""

    def test_zero_based_pdf_pages_shift_to_one_based(self):
        seg = {"pdf_page_start": 0, "pdf_page_end": 9, "citation_page_start": "1",
               "citation_page_end": "10", "page_scope": "body",
               "confidence_level": "high", "mapping_confidence": 0.98}
        self.assertEqual(
            _call("autoMappingSegmentText", seg),
            "正文 PDF 1–10 → 第1—10页 高（98%）",
        )

    def test_spread_layout_is_annotated(self):
        seg = {"pdf_page_start": 0, "pdf_page_end": 4, "citation_page_start": "1",
               "citation_page_end": "10", "page_scope": "body",
               "layout_mode": "spread", "confidence_level": "high",
               "mapping_confidence": 1}
        self.assertIn("· 双开页（左→右）", _call("autoMappingSegmentText", seg))

    def test_rtl_spread_is_annotated(self):
        seg = {"pdf_page_start": 0, "pdf_page_end": 4, "citation_page_start": "1",
               "citation_page_end": "10", "page_scope": "body",
               "layout_mode": "spread", "reading_direction": "rtl",
               "confidence_level": "high", "mapping_confidence": 1}
        self.assertIn("· 双开页（右→左）", _call("autoMappingSegmentText", seg))

    def test_empty_segment_is_empty(self):
        self.assertEqual(_call("autoMappingSegmentText", None), "")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class PdfTypeLabelTests(unittest.TestCase):
    """PDF 检测类型的中文标签，未知类型原样回吐、缺省为“未知”。"""

    def test_known_types(self):
        self.assertEqual(_call("pdfTypeLabel", "native_text"), "原生文本")
        self.assertEqual(_call("pdfTypeLabel", "scanned"), "扫描版")
        self.assertEqual(_call("pdfTypeLabel", "mineru_structured"), "MinerU 结构化")

    def test_unknown_type_is_passed_through(self):
        self.assertEqual(_call("pdfTypeLabel", "totally_unknown"), "totally_unknown")

    def test_empty_and_missing_fall_back_to_unknown(self):
        self.assertEqual(_call("pdfTypeLabel", ""), "未知")
        self.assertEqual(_call("pdfTypeLabel", None), "未知")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class StructureLabelTests(unittest.TestCase):
    """文献结构标签，未知结构原样回吐、缺省为空串。"""

    def test_known_structures(self):
        self.assertEqual(_call("structureLabel", "complete_works"), "全集")
        self.assertEqual(_call("structureLabel", "letters"), "书信集")

    def test_unknown_structure_is_passed_through(self):
        self.assertEqual(_call("structureLabel", "nope"), "nope")

    def test_empty_and_missing_fall_back_to_empty(self):
        self.assertEqual(_call("structureLabel", ""), "")
        self.assertEqual(_call("structureLabel", None), "")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class FormatFileSizeTests(unittest.TestCase):
    """字节数转人读体积；0/缺省显示破折号，按 1024 进制切换单位。"""

    def test_zero_and_missing_show_dash(self):
        self.assertEqual(_call("formatFileSize", 0), "—")
        self.assertEqual(_call("formatFileSize", None), "—")

    def test_bytes(self):
        self.assertEqual(_call("formatFileSize", 512), "512 B")

    def test_kilobytes_boundary_and_fraction(self):
        self.assertEqual(_call("formatFileSize", 1024), "1.0 KB")
        self.assertEqual(_call("formatFileSize", 1536), "1.5 KB")

    def test_megabytes_boundary_and_fraction(self):
        self.assertEqual(_call("formatFileSize", 1048576), "1.0 MB")
        self.assertEqual(_call("formatFileSize", 1572864), "1.5 MB")
        self.assertEqual(_call("formatFileSize", 5242880), "5.0 MB")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class ImportStepsForTests(unittest.TestCase):
    """导入队列的处理步骤文案，随文件类型与解析路线分支。"""

    def test_non_pdf_skips_detection(self):
        self.assertEqual(
            _call("importStepsFor", {"type": "docx"}),
            ["读取文件", "文本入库", "建立索引"],
        )

    def test_pdf_mineru_route(self):
        self.assertEqual(
            _call("importStepsFor", {"type": "pdf", "route": "mineru"}),
            ["读取文件", "类型检测", "MinerU 解析", "文本入库", "建立索引"],
        )

    def test_pdf_vision_route_uses_provider_name(self):
        self.assertEqual(
            _call("importStepsFor",
                  {"type": "pdf", "route": "vision", "providerName": "GPT-4o"}),
            ["读取文件", "类型检测", "GPT-4o 解析", "文本入库", "建立索引"],
        )

    def test_pdf_vision_route_without_provider_name(self):
        self.assertEqual(
            _call("importStepsFor", {"type": "pdf", "route": "vision"}),
            ["读取文件", "类型检测", "其他 API 解析", "文本入库", "建立索引"],
        )

    def test_pdf_local_route(self):
        self.assertEqual(
            _call("importStepsFor", {"type": "pdf", "route": "native"}),
            ["读取文件", "类型检测", "本地解析", "建立索引"],
        )
        self.assertEqual(
            _call("importStepsFor", {"type": "pdf"}),
            ["读取文件", "类型检测", "本地解析", "建立索引"],
        )


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class BatchQueryForTests(unittest.TestCase):
    """联网补全的查询字段裁剪：不同数据源接受的字段集不同。"""

    def _meta(self):
        return {"title": "T", "author": "A", "publish_year": "2020",
                "journal_name": "J", "doi": "D", "issn": "I", "isbn": "B"}

    def test_cnki_keeps_journal_doi_issn(self):
        self.assertEqual(
            _call("batchQueryFor", "cnki", self._meta()),
            {"title": "T", "author": "A", "publish_year": "2020",
             "journal_name": "J", "doi": "D", "issn": "I"},
        )

    def test_crossref_keeps_doi_only(self):
        self.assertEqual(
            _call("batchQueryFor", "crossref", self._meta()),
            {"title": "T", "author": "A", "publish_year": "2020", "doi": "D"},
        )

    def test_default_source_keeps_isbn(self):
        self.assertEqual(
            _call("batchQueryFor", "openlibrary", self._meta()),
            {"title": "T", "author": "A", "publish_year": "2020", "isbn": "B"},
        )

    def test_missing_fields_become_empty_strings(self):
        self.assertEqual(
            _call("batchQueryFor", "cnki", {}),
            {"title": "", "author": "", "publish_year": "",
             "journal_name": "", "doi": "", "issn": ""},
        )


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class CalibrationStatusGroupTests(unittest.TestCase):
    """校准状态 → 语义分组。未知状态一律落到 pending。"""

    def test_manual_and_high_auto_are_calibrated(self):
        self.assertEqual(_call("calibrationStatusGroup", "manual_mapped"), "calibrated")
        self.assertEqual(_call("calibrationStatusGroup", "auto_mapped_high"), "calibrated")

    def test_needs_review_is_review(self):
        self.assertEqual(_call("calibrationStatusGroup", "needs_review"), "review")

    def test_failed_and_missing_are_failed(self):
        self.assertEqual(_call("calibrationStatusGroup", "auto_mapping_failed"), "failed")
        self.assertEqual(_call("calibrationStatusGroup", "source_missing"), "failed")

    def test_mapping_is_mapping(self):
        self.assertEqual(_call("calibrationStatusGroup", "mapping"), "mapping")

    def test_unknown_defaults_to_pending(self):
        self.assertEqual(_call("calibrationStatusGroup", "unmapped"), "pending")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class StatusSemanticVariantTests(unittest.TestCase):
    def test_known_groups(self):
        self.assertEqual(_call("statusSemanticVariant", "calibrated"), "success")
        self.assertEqual(_call("statusSemanticVariant", "review"), "warning")
        self.assertEqual(_call("statusSemanticVariant", "failed"), "danger")
        self.assertEqual(_call("statusSemanticVariant", "mapping"), "info")
        self.assertEqual(_call("statusSemanticVariant", "pending"), "neutral")

    def test_unknown_group_is_neutral(self):
        self.assertEqual(_call("statusSemanticVariant", "whatever"), "neutral")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class CalibrationStatusLabelTests(unittest.TestCase):
    def test_known_labels(self):
        self.assertEqual(_call("calibrationStatusLabel", "manual_mapped"), "页码已校准")
        self.assertEqual(_call("calibrationStatusLabel", "needs_review"), "页码待确认")
        self.assertEqual(_call("calibrationStatusLabel", "mapping"), "正在检测页码")
        self.assertEqual(_call("calibrationStatusLabel", "source_missing"), "原文件缺失")

    def test_unknown_defaults_to_not_detected(self):
        self.assertEqual(_call("calibrationStatusLabel", "unmapped"), "页码尚未检测")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class FormatCalDateTests(unittest.TestCase):
    def test_empty_is_unknown(self):
        self.assertEqual(_call("formatCalDate", ""), "未知")

    def test_invalid_is_unknown(self):
        self.assertEqual(_call("formatCalDate", "not-a-date"), "未知")

    def test_valid_date_is_formatted(self):
        # 用午间时刻，避免测试机时区把日期推到相邻一天。
        self.assertEqual(_call("formatCalDate", "2024-03-05T12:00:00"), "2024-03-05")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class SegmentNumberStyleLabelTests(unittest.TestCase):
    def test_known_styles(self):
        self.assertEqual(_call("segmentNumberStyleLabel", "arabic"), "阿拉伯数字")
        self.assertEqual(_call("segmentNumberStyleLabel", "roman_lower"), "罗马数字（小写）")
        self.assertEqual(_call("segmentNumberStyleLabel", "roman_upper"), "罗马数字（大写）")
        self.assertEqual(_call("segmentNumberStyleLabel", "none"), "无编号")

    def test_unknown_defaults_to_arabic(self):
        self.assertEqual(_call("segmentNumberStyleLabel", "zzz"), "阿拉伯数字")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class SegmentLayoutLabelTests(unittest.TestCase):
    def test_spread_and_single(self):
        self.assertEqual(_call("segmentLayoutLabel", "spread"), "双开页")
        self.assertEqual(_call("segmentLayoutLabel", "single"), "单页")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class SpreadGutterPercentTests(unittest.TestCase):
    """装订线位置：缺省或越界 (0.3–0.7 之外) 归位到 50%。"""

    def test_missing_defaults_to_fifty(self):
        self.assertEqual(_call("spreadGutterPercent", {}), 50)

    def test_in_range_value(self):
        self.assertEqual(_call("spreadGutterPercent", {"gutter_x": 0.35}), 35)

    def test_out_of_range_snaps_to_fifty(self):
        self.assertEqual(_call("spreadGutterPercent", {"gutter_x": 0.9}), 50)

    def test_midpoint(self):
        self.assertEqual(_call("spreadGutterPercent", {"gutter_x": 0.5}), 50)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class SpreadCitationPairTests(unittest.TestCase):
    """双开页左右页码：rtl 时首页落在右侧，罗马数字沿用 intToRoman。"""

    def test_no_number_style_is_unmapped(self):
        self.assertEqual(_call("spreadCitationPair", {"number_style": "none"}), {"mapped": False})

    def test_empty_citation_is_unmapped(self):
        self.assertEqual(
            _call("spreadCitationPair", {"citation": None}),
            {"mapped": False},
        )

    def test_ltr_arabic_pair(self):
        self.assertEqual(
            _call("spreadCitationPair",
                  {"citation_page_start": "5", "number_style": "arabic"}),
            {"mapped": True, "left": "5", "right": "6", "firstSide": "left"},
        )

    def test_rtl_swaps_sides(self):
        self.assertEqual(
            _call("spreadCitationPair",
                  {"citation_page_start": "5", "number_style": "arabic",
                   "reading_direction": "rtl"}),
            {"mapped": True, "left": "6", "right": "5", "firstSide": "right"},
        )

    def test_roman_lower_pair(self):
        self.assertEqual(
            _call("spreadCitationPair",
                  {"citation_page_start": "1", "number_style": "roman_lower"}),
            {"mapped": True, "left": "i", "right": "ii", "firstSide": "left"},
        )


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class LibLangChipLabelTests(unittest.TestCase):
    def test_chinese_and_foreign(self):
        self.assertEqual(_call("libLangChipLabel", "chinese"), "中文")
        self.assertEqual(_call("libLangChipLabel", "latin"), "外文")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class LibraryDocTypeTests(unittest.TestCase):
    """只有 journal_article/thesis 原样保留，其余一律归为 book。"""

    def test_journal_and_thesis_are_preserved(self):
        self.assertEqual(_call("libraryDocType", {"document_type": "journal_article"}), "journal_article")
        self.assertEqual(_call("libraryDocType", {"document_type": "thesis"}), "thesis")

    def test_book_and_unknown_are_book(self):
        self.assertEqual(_call("libraryDocType", {"document_type": "book"}), "book")
        self.assertEqual(_call("libraryDocType", {}), "book")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class LibrarySortProjectionTests(unittest.TestCase):
    """排序投影：last_modified 同时兜底导入时间与修改时间，word→Word。"""

    def test_full_source(self):
        self.assertEqual(
            _call("librarySortProjection",
                  {"title": "T", "author": "A", "imported_at": "2024",
                   "modified_at": "2025", "source_type": "word"}),
            {"title": "T", "author": "A", "imported_at": "2024",
             "modified_at": "2025", "source_type": "Word"},
        )

    def test_fallbacks(self):
        self.assertEqual(
            _call("librarySortProjection",
                  {"source_file_id": "sid", "last_modified": "2023",
                   "source_type": "pdf"}),
            {"title": "sid", "author": "", "imported_at": "2023",
             "modified_at": "2023", "source_type": "PDF"},
        )


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class VisionHashTests(unittest.TestCase):
    """31 进制滚动哈希，无符号 32 位。"""

    def test_known_string(self):
        # 'abc' = ((0*31+97)*31+98)*31+99 = 96354
        self.assertEqual(_call("visionHash", "abc"), 96354)

    def test_empty_is_zero(self):
        self.assertEqual(_call("visionHash", ""), 0)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class VisionHostLabelTests(unittest.TestCase):
    def test_full_url_returns_host(self):
        self.assertEqual(_call("visionHostLabel", "https://api.openai.com/v1"), "api.openai.com")

    def test_bare_host_gets_https_prefix(self):
        self.assertEqual(_call("visionHostLabel", "api.deepseek.com"), "api.deepseek.com")

    def test_empty_is_empty(self):
        self.assertEqual(_call("visionHostLabel", ""), "")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class BibPrimaryLabelTests(unittest.TestCase):
    """联网补全按钮文案，按数据源分支。"""

    def test_cnki(self):
        self.assertEqual(_call("bibPrimaryLabel", "cnki"), "知网补全")

    def test_crossref(self):
        self.assertEqual(_call("bibPrimaryLabel", "crossref"), "Crossref 补全")

    def test_other_falls_back(self):
        self.assertEqual(_call("bibPrimaryLabel", "google"), "补全期刊信息")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class MetadataStatusLabelTests(unittest.TestCase):
    """书目状态中文名，未知值原样返回，空值兜底'未识别'。"""

    def test_known(self):
        self.assertEqual(_call("metadataStatusLabel", "complete"), "完整")
        self.assertEqual(_call("metadataStatusLabel", "needs_review"), "书目待确认")

    def test_unknown_is_passed_through(self):
        self.assertEqual(_call("metadataStatusLabel", "zzz"), "zzz")

    def test_empty_defaults(self):
        self.assertEqual(_call("metadataStatusLabel", ""), "未识别")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class MetadataSourceLabelTests(unittest.TestCase):
    """书目来源中文名，未知值原样返回，空值兜底'未知'。"""

    def test_known(self):
        self.assertEqual(_call("metadataSourceLabel", "manual"), "人工维护")
        self.assertEqual(_call("metadataSourceLabel", "pdf_metadata"), "PDF 元数据")

    def test_unknown_is_passed_through(self):
        self.assertEqual(_call("metadataSourceLabel", "zzz"), "zzz")

    def test_empty_defaults(self):
        self.assertEqual(_call("metadataSourceLabel", ""), "未知")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class SourceBibliographicMetadataTests(unittest.TestCase):
    """合并 src 顶层字段到 bibliographic_metadata，顶层非空值覆盖嵌套。"""

    def test_toplevel_overrides_nested(self):
        src = {"bibliographic_metadata": {"title": "old", "author": "A"}, "title": "T"}
        self.assertEqual(_call("sourceBibliographicMetadata", src),
                         {"title": "T", "author": "A"})

    def test_empty_toplevel_is_skipped(self):
        src = {"bibliographic_metadata": {"author": "A"}, "title": ""}
        self.assertEqual(_call("sourceBibliographicMetadata", src), {"author": "A"})

    def test_null_src_is_empty(self):
        self.assertEqual(_call("sourceBibliographicMetadata", None), {})


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class BibliographicMissingTextTests(unittest.TestCase):
    """书目缺失字段提示；thesis 的 publisher 显示为'学校'。"""

    def test_book_lists_missing_fields(self):
        self.assertEqual(_call("bibliographicMissingText", {}),
                         "书目缺失：作者、书名、出版社、出版地、出版年份")

    def test_complete_is_empty(self):
        meta = {"title": "T", "author": "A", "publisher": "P",
                "publish_place": "PL", "publish_year": "2020"}
        self.assertEqual(_call("bibliographicMissingText", meta), "")

    def test_thesis_renames_publisher_to_school(self):
        meta = {"document_type": "thesis", "title": "T", "author": "A",
                "publish_place": "PL"}
        self.assertEqual(_call("bibliographicMissingText", meta), "书目缺失：学校、出版年份")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class BuildVolumeIndexTests(unittest.TestCase):
    """卷册按 source_file_id 建索引；缺 id 的项跳过。node 侧 Map 序列化成对象。"""

    def _size(self, result):
        # node 的 Map 经 JSON 序列化会变成 {}（无枚举属性），所以改用探针函数验证。
        return result

    def test_indexes_by_id(self):
        vols = [{"source_file_id": "x", "v": 1}, {"source_file_id": "y", "v": 2}]
        got = _run([{"fn": "buildVolumeIndex", "args": [vols]}])
        # Map 不能直接比对，用 harness 无法透出 size；改测行为见 probe 测试。
        self.assertIsNotNone(got)

    def test_probe_via_get(self):
        # 用一个内联探针：建索引后取回 x 的值、并确认坏项被跳过。
        harness_case = [{
            "fn": "(function(v){var m=buildVolumeIndex(v);return {size:m.size,x:m.get('x'),hasY:m.has('y')};})",
            "args": [[{"source_file_id": "x", "v": 1}, {"v": 2}, {"source_file_id": "y"}]],
        }]
        got = _run(harness_case)[0]
        self.assertEqual(got["size"], 2)
        self.assertEqual(got["x"], {"source_file_id": "x", "v": 1})
        self.assertTrue(got["hasY"])


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class TruncateTests(unittest.TestCase):
    """折叠空白后按长度截断，超出加省略号。"""

    def test_long_string_is_truncated(self):
        self.assertEqual(_call("truncate", "hello world", 5), "hello…")

    def test_short_string_is_unchanged(self):
        self.assertEqual(_call("truncate", "hi", 5), "hi")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(_call("truncate", "a  b\tc", 10), "a b c")

    def test_null_is_empty(self):
        self.assertEqual(_call("truncate", None, 5), "")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class MatchTypeLabelTests(unittest.TestCase):
    """匹配类型中文名，未知值原样返回，空值兜底空串。"""

    def test_known(self):
        self.assertEqual(_call("matchTypeLabel", "exact"), "精确")
        self.assertEqual(_call("matchTypeLabel", "ngram_fuzzy"), "模糊")

    def test_unknown_is_passed_through(self):
        self.assertEqual(_call("matchTypeLabel", "zzz"), "zzz")

    def test_empty(self):
        self.assertEqual(_call("matchTypeLabel", ""), "")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class ThemeMarkupTests(unittest.TestCase):
    """主题预览/选项的纯字符串构造：只校验结构骨架，不锁死整段 HTML。"""

    def test_preview_wraps_theme_id(self):
        html = _call("themePreviewMarkup", "dawn")
        self.assertTrue(html.startswith('<span class="theme-preview" data-preview-theme="dawn"'))

    def test_option_wraps_choice_and_embeds_preview(self):
        theme = {"id": "dawn", "name": "晨", "tone": "亮", "description": "desc"}
        html = _call("themeOptionMarkup", theme)
        self.assertTrue(html.startswith('<button class="theme-option" type="button" data-theme-choice="dawn"'))
        # themeOptionMarkup 内嵌 themePreviewMarkup 的产物。
        self.assertIn('<span class="theme-preview" data-preview-theme="dawn"', html)
        self.assertIn(">晨<", html)
        self.assertIn(">desc<", html)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class DetailContextTextTests(unittest.TestCase):
    """详情上下文拼接：取每条 item.text，非空的用换行连成一段。"""

    def test_joins_texts_with_newline(self):
        items = [{"text": "a"}, {"text": "b"}, {"text": "c"}]
        self.assertEqual(_call("detailContextText", items), "a\nb\nc")

    def test_skips_blank_and_missing(self):
        items = [{"text": "a"}, {"text": ""}, {}, {"text": "b"}]
        self.assertEqual(_call("detailContextText", items), "a\nb")

    def test_non_array_is_empty(self):
        self.assertEqual(_call("detailContextText", None), "")

    def test_empty_array_is_empty(self):
        self.assertEqual(_call("detailContextText", []), "")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class SpreadSummaryHtmlTests(unittest.TestCase):
    """双开页摘要文案。PDF 页序号库内 0 基，展示 +1。"""

    def test_unmapped_segment_explains_split_only(self):
        seg = {"pdf_page_start": 0, "number_style": "none"}
        html = _call("spreadSummaryHtml", seg)
        self.assertIn("PDF 第 1 页", html)
        self.assertIn("未设引用页码", html)

    def test_mapped_segment_reports_both_halves(self):
        seg = {"pdf_page_start": 4, "citation_page_start": "5",
               "number_style": "arabic"}
        html = _call("spreadSummaryHtml", seg)
        self.assertIn("PDF 第 5 页", html)
        self.assertIn("左半页 <b>引文 5 页</b>", html)
        self.assertIn("右半页 <b>引文 6 页</b>", html)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class SegmentSpreadPanelRowTests(unittest.TestCase):
    """双开页面板行：非 spread 分段返回空；spread 时拼出示意图/控件/摘要。"""

    def test_non_spread_is_empty(self):
        self.assertEqual(
            _call("segmentSpreadPanelRow", {"layout_mode": "single"}, 0), "")

    def test_spread_builds_panel_markup(self):
        seg = {"layout_mode": "spread", "citation_page_start": "5",
               "number_style": "arabic", "reading_direction": "ltr"}
        html = _call("segmentSpreadPanelRow", seg, 2)
        self.assertTrue(html.startswith("<tr class=\"segment-spread-row\">"))
        self.assertIn("spread-diagram-2", html)
        self.assertIn("spread-summary-2", html)
        self.assertIn("引文 5 页", html)
        self.assertIn("引文 6 页", html)

    def test_ltr_puts_badge_one_on_left(self):
        seg = {"layout_mode": "spread", "citation_page_start": "5",
               "number_style": "arabic", "reading_direction": "ltr"}
        html = _call("segmentSpreadPanelRow", seg, 0)
        left = html.index("spread-badge-left-0")
        right = html.index("spread-badge-right-0")
        # 左半页徽标为 1、右半页为 2（左→右阅读）。
        self.assertIn(">1<", html[left:left + 60])
        self.assertIn(">2<", html[right:right + 60])

    def test_rtl_swaps_badges(self):
        seg = {"layout_mode": "spread", "citation_page_start": "5",
               "number_style": "arabic", "reading_direction": "rtl"}
        html = _call("segmentSpreadPanelRow", seg, 0)
        left = html.index("spread-badge-left-0")
        right = html.index("spread-badge-right-0")
        # 右→左阅读时左半页徽标变 2、右半页变 1。
        self.assertIn(">2<", html[left:left + 60])
        self.assertIn(">1<", html[right:right + 60])


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class EscTests(unittest.TestCase):
    """HTML 转义。所有渲染函数拼接前的第一道防线，纯函数、无 DOM。"""

    def test_ampersand_is_escaped(self):
        self.assertEqual(_call("esc", "a & b"), "a &amp; b")

    def test_angle_brackets_are_escaped(self):
        self.assertEqual(_call("esc", "<div>"), "&lt;div&gt;")

    def test_quotes_are_escaped(self):
        self.assertEqual(
            _call("esc", "he said \"hi\" & 'bye'"),
            "he said &quot;hi&quot; &amp; &#39;bye&#39;",
        )

    def test_all_special_chars_together(self):
        self.assertEqual(_call("esc", "&<>\"'"), "&amp;&lt;&gt;&quot;&#39;")

    def test_plain_text_is_unchanged(self):
        self.assertEqual(_call("esc", "纯文本无需转义"), "纯文本无需转义")

    def test_non_string_is_coerced(self):
        self.assertEqual(_call("esc", 42), "42")

    def test_null_is_coerced_to_string(self):
        self.assertEqual(_call("esc", None), "null")


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class StatusStatIconTests(unittest.TestCase):
    """校准状态统计图标：按名取 SVG，未知回退到 notice。"""

    def test_known_icon_renders_full_svg_span(self):
        self.assertEqual(
            _call("statusStatIcon", "check"),
            '<span class="status-stat__icon" aria-hidden="true">'
            '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            'stroke-linejoin="round"><circle cx="12" cy="12" r="9"/>'
            '<path d="m8 12 2.6 2.6L16.5 9"/></svg></span>',
        )

    def test_unknown_icon_falls_back_to_notice(self):
        html = _call("statusStatIcon", "totally_unknown")
        self.assertIn('<path d="M12 7.5v5.5"/>', html)
        self.assertIn('<path d="M12 16.5h.01"/>', html)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class StatusChipIconTests(unittest.TestCase):
    """校准状态芯片图标：mapping 组带旋转动画，未知回退到 pending。"""

    def test_mapping_group_is_spinning(self):
        self.assertIn("is-spinning", _call("statusChipIcon", "mapping"))

    def test_calibrated_group_is_not_spinning(self):
        self.assertNotIn("is-spinning", _call("statusChipIcon", "calibrated"))

    def test_unknown_group_falls_back_to_pending(self):
        # pending 图标为时钟：圆 + 指针路径。
        html = _call("statusChipIcon", "totally_unknown")
        self.assertIn('<path d="M12 7v5l3 2"/>', html)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class StatusStatButtonTests(unittest.TestCase):
    """校准状态统计按钮：拼接图标/文案/计数，命中当前筛选时高亮。"""

    def test_active_button_gets_active_class(self):
        html = _call("statusStatButton", "manual_mapped", "已校准", 5,
                     "success", "check", "manual_mapped", "applyLibStatusFilter")
        self.assertIn("status-stat--success", html)
        self.assertIn(" active", html)
        self.assertIn(">已校准<", html)
        self.assertIn(">5<", html)
        self.assertIn("applyLibStatusFilter('manual_mapped')", html)

    def test_inactive_button_omits_active_class(self):
        html = _call("statusStatButton", "manual_mapped", "已校准", 5,
                     "success", "check", "needs_review", "applyLibStatusFilter")
        self.assertNotIn(" active", html)

    def test_button_embeds_icon_markup(self):
        html = _call("statusStatButton", "manual_mapped", "已校准", 5,
                     "success", "check", "", "applyLibStatusFilter")
        self.assertIn('<span class="status-stat__icon"', html)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class BibSourceMenuHTMLTests(unittest.TestCase):
    """书目补全来源菜单：标记当前来源、内含固定动作项。"""

    def test_active_source_is_marked(self):
        html = _call("bibSourceMenuHTML", "sid-1", "cnki")
        self.assertIn('onclick="bibSetSource(event,\'sid-1\',\'cnki\')"', html)
        # 当前来源那一项带 active。
        cnki_at = html.index("'cnki')")
        head = html.rfind("<button", 0, cnki_at)
        self.assertIn("active", html[head:cnki_at])

    def test_menu_has_auto_and_paste_actions(self):
        html = _call("bibSourceMenuHTML", "sid-1", "cnki")
        self.assertIn(">智能补全", html)
        self.assertIn("bibMenuAction(event,'paste','sid-1')", html)
        self.assertIn("bibMenuAction(event,'opencnki','sid-1')", html)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class DrawerInfoRowTests(unittest.TestCase):
    """抽屉信息行：转义标签与取值，空值显示破折号。"""

    def test_basic_row(self):
        self.assertEqual(
            _call("drawerInfoRow", "作者", "马克思"),
            '<div class="drawer-info-row"><span class="drawer-info-label">作者</span>'
            '<span class="drawer-info-value">马克思</span></div>',
        )

    def test_value_is_escaped(self):
        self.assertEqual(
            _call("drawerInfoRow", "x", "<b>"),
            '<div class="drawer-info-row"><span class="drawer-info-label">x</span>'
            '<span class="drawer-info-value">&lt;b&gt;</span></div>',
        )

    def test_empty_value_shows_dash(self):
        self.assertIn("—", _call("drawerInfoRow", "y", ""))


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class BibliographicMissingBadgeTests(unittest.TestCase):
    """书目缺失徽标：完整时为空，缺失时给出带类名的提示。"""

    def test_complete_meta_yields_empty(self):
        meta = {"title": "资本论", "author": "马克思", "publisher": "人民出版社",
                "publish_place": "北京", "publish_year": "2004"}
        self.assertEqual(_call("bibliographicMissingBadge", meta), "")

    def test_missing_meta_renders_badge(self):
        html = _call("bibliographicMissingBadge", {})
        self.assertIn('class="bibliographic-missing"', html)
        self.assertIn("书目缺失", html)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class ToastDurationTests(unittest.TestCase):
    """toast 停留时长：文本越长停留越久，双端夹在 [2400, 6500] 内。"""

    def test_short_text_hits_lower_bound(self):
        self.assertEqual(_call("toastDuration", ""), 2400)

    def test_medium_text_grows_with_length(self):
        # 1100 + 10*110 = 2200，仍被下限夹到 2400
        self.assertEqual(_call("toastDuration", "x" * 10), 2400)
        # 1100 + 20*110 = 3300，落在区间内，随长度线性增长
        self.assertEqual(_call("toastDuration", "x" * 20), 3300)

    def test_long_text_hits_upper_bound(self):
        self.assertEqual(_call("toastDuration", "x" * 500), 6500)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class IsLibraryDeleteSelectableTests(unittest.TestCase):
    """只有 pdf / word 来源可勾选删除；其它类型与空值一律不可选。"""

    def test_pdf_and_word_selectable(self):
        self.assertTrue(_call("isLibraryDeleteSelectable", {"source_type": "pdf"}))
        self.assertTrue(_call("isLibraryDeleteSelectable", {"source_type": "word"}))

    def test_other_types_not_selectable(self):
        self.assertFalse(_call("isLibraryDeleteSelectable", {"source_type": "epub"}))

    def test_falsy_source_not_selectable(self):
        self.assertFalse(_call("isLibraryDeleteSelectable", None))
        self.assertFalse(_call("isLibraryDeleteSelectable", 0))


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class AutoFailureReasonsTests(unittest.TestCase):
    """自动校准失败原因：已知原因映射为中文，未知原因原样透传，逐条前缀 •。"""

    def test_empty_reasons(self):
        self.assertEqual(_call("autoFailureReasons", []), "")

    def test_known_reason_mapped(self):
        self.assertEqual(
            _call("autoFailureReasons", ["no_page_labels"]),
            "• 没有 PDF Page Labels",
        )

    def test_unknown_reason_passthrough_and_join(self):
        self.assertEqual(
            _call("autoFailureReasons", ["no_page_labels", "unknown_reason"]),
            "• 没有 PDF Page Labels<br>• unknown_reason",
        )


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class PdRowTests(unittest.TestCase):
    """页码明细行：标签与值都经 esc 转义后拼进固定结构。"""

    def test_plain_row(self):
        self.assertEqual(
            _call("pdRow", "页码", 12),
            '<div class="page-detail-row">'
            '<span class="page-detail-label">页码</span><span>12</span></div>',
        )

    def test_html_escaped(self):
        self.assertEqual(
            _call("pdRow", "<b>", "a&b"),
            '<div class="page-detail-row">'
            '<span class="page-detail-label">&lt;b&gt;</span>'
            '<span>a&amp;b</span></div>',
        )


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class SegmentNumberStyleControlTests(unittest.TestCase):
    """分段页码样式选择控件：渲染四个选项，当前样式带 is-selected。"""

    def test_arabic_selected_and_all_options_present(self):
        html = _call("segmentNumberStyleControl", "arabic", 0)
        self.assertIn('id="segment-style-select-0"', html)
        self.assertIn(
            '<button class="app-select-option is-selected" type="button"'
            ' data-value="arabic"', html)
        for value in ("arabic", "roman_lower", "roman_upper", "none"):
            self.assertIn('data-value="' + value + '"', html)

    def test_index_threaded_into_ids_and_handlers(self):
        html = _call("segmentNumberStyleControl", "none", 3)
        self.assertIn('id="segment-style-select-3"', html)
        self.assertIn("setSegmentNumberStyle(event,3,'roman_lower')", html)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class SegmentLayoutControlTests(unittest.TestCase):
    """分段版式选择控件：单页/双开页两项，当前版式带 is-selected。"""

    def test_single_selected(self):
        html = _call("segmentLayoutControl", "single", 0)
        self.assertIn('id="segment-layout-select-0"', html)
        self.assertIn(
            '<button class="app-select-option is-selected" type="button"'
            ' data-value="single"', html)
        self.assertIn('data-value="spread"', html)

    def test_spread_selected_with_index(self):
        html = _call("segmentLayoutControl", "spread", 2)
        self.assertIn('id="segment-layout-select-2"', html)
        self.assertIn(
            '<button class="app-select-option is-selected" type="button"'
            ' data-value="spread"', html)
        self.assertIn("setSegmentLayout(event,2,'single')", html)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class ScanEntryRowTests(unittest.TestCase):
    """扫描列表行：类型徽标、勾选框、备注文案随状态变化，路径/名称经 esc。"""

    def _entry(self, **over):
        base = {"file_type": "pdf", "status": "new", "needs_ocr": None,
                "path": "/a/b.pdf", "name": "b.pdf", "size_bytes": 1234}
        base.update(over)
        return base

    def test_new_pdf_unchecked_row(self):
        html = _call("scanEntryRow", self._entry(), 2, True, False)
        self.assertIn('id="scan-check-2"', html)
        self.assertIn('data-index="2"', html)
        self.assertNotIn(" checked", html)
        self.assertIn('<span class="type-badge pdf">PDF</span>', html)
        self.assertIn("未预检测", html)

    def test_checked_and_word_type(self):
        html = _call("scanEntryRow",
                     self._entry(file_type="word", needs_ocr=False), 0, True, True)
        self.assertIn(" checked", html)
        self.assertIn('<span class="type-badge word">DOCX</span>', html)

    def test_ocr_note_and_escaping(self):
        html = _call("scanEntryRow",
                     self._entry(needs_ocr=True, name="<x>", path="a&b"), 1, True, False)
        self.assertIn("需 OCR", html)
        self.assertIn("&lt;x&gt;", html)
        self.assertIn("a&amp;b", html)

    def test_non_checkable_uses_placeholder(self):
        html = _call("scanEntryRow", self._entry(), 4, False, False)
        self.assertIn('<span class="scan-check-placeholder">', html)
        self.assertNotIn('type="checkbox"', html)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class DragSelectionGeometryTests(unittest.TestCase):
    """框选几何：锚点/选框/命中判定都由参数矩形推算，无副作用。"""

    _SCROLLER = ("{getBoundingClientRect:function(){return {left:10,top:20};},"
                 "scrollLeft:5,scrollTop:7}")

    def test_anchor_adds_scroll_offset(self):
        got = _eval("dragSelectionAnchor(" + self._SCROLLER +
                    ",{clientX:100,clientY:200})")
        self.assertEqual(got, {"anchorX": 95, "anchorY": 187})

    def test_box_spans_anchor_and_pointer(self):
        state = ("{scroller:" + self._SCROLLER +
                 ",pointerX:150,pointerY:260,anchorX:95,anchorY:187}")
        got = _eval("dragSelectionBox(" + state + ")")
        self.assertEqual(got["left"], 95)
        self.assertEqual(got["right"], 145)
        self.assertEqual(got["top"], 187)
        self.assertEqual(got["bottom"], 247)

    def test_hits_true_when_overlapping(self):
        box = "{viewport:{left:10,top:20},left:95,right:145,top:187,bottom:247}"
        el = "{getBoundingClientRect:function(){return {left:100,top:200,width:30,height:15};}}"
        got = _eval("dragSelectionHits(" + el + "," + box + "," + self._SCROLLER + ")")
        self.assertIs(got, True)

    def test_hits_false_when_disjoint(self):
        box = "{viewport:{left:10,top:20},left:95,right:145,top:187,bottom:247}"
        el = "{getBoundingClientRect:function(){return {left:500,top:800,width:10,height:10};}}"
        got = _eval("dragSelectionHits(" + el + "," + box + "," + self._SCROLLER + ")")
        self.assertIs(got, False)


@unittest.skipUnless(NODE, "node 不可用，跳过纯逻辑执行测试")
class DetailContextPreviewTests(unittest.TestCase):
    """上下文预览截断：阈值 DETAIL_CONTEXT_PREVIEW_CHARS(180) 随函数一并移入 06-pure，
    闭包内可见。不超阈值原样返回；超出按 before/after 两端截断并补省略号；按码点计长。"""

    def test_short_text_unchanged(self):
        self.assertEqual(_call("detailContextPreview", "abc", "after"), "abc")

    def test_exactly_threshold_unchanged(self):
        text = "x" * 180
        self.assertEqual(_call("detailContextPreview", text, "after"), text)

    def test_after_truncates_head_with_ellipsis(self):
        got = _call("detailContextPreview", "x" * 200, "after")
        self.assertEqual(got, "x" * 180 + "…")
        self.assertEqual(len(got), 181)

    def test_before_truncates_tail_with_ellipsis(self):
        got = _call("detailContextPreview", "x" * 200, "before")
        self.assertEqual(got, "…" + "x" * 180)
        self.assertEqual(len(got), 181)

    def test_empty_and_null_yield_empty(self):
        self.assertEqual(_call("detailContextPreview", "", "after"), "")
        self.assertEqual(_call("detailContextPreview", None, "after"), "")

    def test_counts_by_code_point(self):
        got = _call("detailContextPreview", "文" * 200, "after")
        self.assertEqual(got, "文" * 180 + "…")


if __name__ == "__main__":
    unittest.main()

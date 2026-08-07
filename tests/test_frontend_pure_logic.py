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


if __name__ == "__main__":
    unittest.main()

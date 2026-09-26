"""C5 DOM 迁移审阅发现的还原偏差：钉住与迁移前 HTML 字符串一致的细节。

每条断言对应一处曾被改变的输出（对照 157257d 的字符串模板）：
- MinerU 账号归属的页数是 ``<b>N 页</b>``，单位在加粗内；账号行与书行是 flex 布局，
  单位若落在 ``<b>`` 外会变成独立 flex 项并多出 12px 间距。
- 「查看识别依据」里「未使用的证据：」后每条原因各占一行，首条前也要换行。
- 检索范围与视觉 API 选项的对勾、检索详情返回按钮、文献库筛选 chip 的 ×、
  视觉 API 搜索图标保持原笔画与类名。
"""

import re
import unittest
from pathlib import Path


JS = Path(__file__).resolve().parents[1] / "src" / "me_finder" / "static" / "js"


def _function_body(file_name: str, name: str) -> str:
    source = (JS / file_name).read_text(encoding="utf-8")
    start = re.search(r"function\s+%s\s*\(" % re.escape(name), source)
    if start is None:
        raise AssertionError(f"{file_name} 缺少 {name}")
    depth = 0
    for index in range(source.index("{", start.end()), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start.start():index + 1]
    raise AssertionError(f"{name} 花括号不配对")


class C5DomFidelityTests(unittest.TestCase):
    def test_mineru_credential_page_count_keeps_unit_inside_bold(self):
        body = _function_body("72-vision-stats.js", "renderMineruCredentialAttribution")
        self.assertNotIn("statsCount(", body)
        self.assertEqual(body.count("statsNode('b', null, Number("), 2)
        self.assertEqual(body.count("toLocaleString() + ' 页'"), 2)

    def test_parser_book_table_keeps_unit_outside_bold(self):
        body = _function_body("72-vision-stats.js", "renderParserProviderBooks")
        self.assertIn("statsCount(pages, book.parsed_page_count, '页')", body)

    def test_calibration_evidence_breaks_before_every_reason(self):
        body = _function_body("50-calibration.js", "showCalibrationEvidence")
        loop = body[body.index("autoFailureReasons(failures)"):]
        self.assertNotIn("if (index)", loop)
        self.assertLess(loop.index("createElement('br')"), loop.index("createTextNode(label)"))

    def test_selected_option_checkmarks_keep_heavier_stroke(self):
        for file_name, name in (("20-search.js", "searchScopeOption"),
                                ("71-vision-providers.js", "providerOptionNode")):
            body = _function_body(file_name, name)
            self.assertIn("m5 10 3 3 7-7", body, name)
            self.assertIn("setAttribute('stroke-width', '2')", body, name)

    def test_search_detail_back_button_is_not_an_action_button(self):
        body = _function_body("20-search.js", "showDetail")
        self.assertIn("back.className = 'detail-back-button';", body)

    def test_library_chip_and_vision_search_icons_keep_original_strokes(self):
        library = _function_body("30-library.js", "renderLibraryFilterBar")
        self.assertIn("remove.setAttribute('stroke-width', '2')", library)
        vision = _function_body("71-vision-providers.js", "renderImportVisionProviderOptions")
        self.assertIn("icon.setAttribute('stroke-width', '1.7')", vision)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
READER_JS = (ROOT / "src" / "me_finder" / "static" / "reader.js").read_text(
    encoding="utf-8"
)
READER_CSS = (ROOT / "src" / "me_finder" / "static" / "reader.css").read_text(
    encoding="utf-8"
)
APP_CSS = (ROOT / "src" / "me_finder" / "static" / "app.css").read_text(
    encoding="utf-8"
)
APP_JS = (ROOT / "src" / "me_finder" / "static" / "app.js").read_text(
    encoding="utf-8"
)


class StructuredReaderFrontendTests(unittest.TestCase):
    def test_exposes_a_small_non_module_api_for_app_js(self) -> None:
        self.assertIn("global.MEFinderReader = Object.freeze", READER_JS)
        for method in (
            "open:",
            "openForSearchResult:",
            "close:",
            "goTo:",
            "configure:",
            "isOpen:",
        ):
            self.assertIn(method, READER_JS)
        self.assertIn("endpoint: '/api/document/pages'", READER_JS)
        self.assertIn("source_id:", READER_JS)
        self.assertIn("start:", READER_JS)
        self.assertIn("count:", READER_JS)

    def test_windowing_mounts_only_the_bounded_api_page_range(self) -> None:
        self.assertIn("function windowBounds(centerIndex)", READER_JS)
        self.assertIn("(config.radiusBatches * 2 + 1) * config.batchSize", READER_JS)
        self.assertIn("Math.min(100, count)", READER_JS)
        self.assertIn("state.items.clear()", READER_JS)
        self.assertIn("Array.from(state.items.keys())", READER_JS)
        self.assertIn("function trimMountedItems(mode)", READER_JS)
        self.assertIn("beforeSpacer", READER_JS)
        self.assertIn("afterSpacer", READER_JS)
        self.assertNotIn("for (index = 0; index < state.total;", READER_JS)
        self.assertNotIn("state.total - state.windowEnd", READER_JS)

    def test_current_page_and_window_boundaries_use_intersection_observers(self) -> None:
        self.assertGreaterEqual(READER_JS.count("new global.IntersectionObserver"), 2)
        self.assertIn("entry.intersectionRatio", READER_JS)
        self.assertIn("updateCurrentFromObserver()", READER_JS)
        self.assertIn("dataset.readerBoundary", READER_JS)
        self.assertNotIn("offsetTop", READER_JS)
        self.assertNotIn("getBoundingClientRect", READER_JS)
        self.assertNotIn("scroll-behavior: smooth", READER_CSS)
        render_start = READER_JS.index("function renderWindow(")
        render_end = READER_JS.index("function findAnchorNode(", render_start)
        render_body = READER_JS[render_start:render_end]
        self.assertLess(
            render_body.index("target.scrollIntoView"),
            render_body.index("state.boundaryObserver = createBoundaryObserver()"),
        )

    def test_each_item_has_an_independent_safe_dom_anchor(self) -> None:
        self.assertIn("article.id = 'mef-reader-anchor-'", READER_JS)
        self.assertIn("article.dataset.readerAnchor = anchorId", READER_JS)
        self.assertIn("item.anchor_id", READER_JS)
        self.assertIn("item.pdf_page_id", READER_JS)
        self.assertIn("item.paragraph_id", READER_JS)

    def test_empty_pdf_pages_render_an_explicit_placeholder(self) -> None:
        self.assertIn("if (item.is_empty || !text)", READER_JS)
        self.assertIn("'本页无文本层'", READER_JS)
        self.assertIn("body.classList.add('is-empty')", READER_JS)

    def test_codepoint_offsets_are_converted_before_utf16_slice(self) -> None:
        helper = re.search(
            r"function codePointToUtf16Index\(text, codePointOffset\) \{"
            r"(?P<body>.*?)\n  \}",
            READER_JS,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(helper)
        body = helper.group("body")
        self.assertIn("for (character of String(text || ''))", body)
        self.assertIn("utf16Index += character.length", body)
        self.assertIn("codePointsSeen += 1", body)
        self.assertGreaterEqual(
            READER_JS.count("codePointToUtf16Index(text,"),
            5,
        )
        self.assertIn("offsetUnit === 'unicode_codepoint'", READER_JS)

    def test_highlighting_is_safe_and_does_not_inject_search_html(self) -> None:
        self.assertIn("document.createElement('mark')", READER_JS)
        self.assertIn("mark.textContent = text.slice(", READER_JS)
        self.assertIn("document.createTextNode", READER_JS)
        self.assertNotIn(".innerHTML", READER_JS)
        self.assertIn("span.page_text_hash", READER_JS)
        self.assertIn("item.page_text_hash", READER_JS)
        self.assertIn("matchQuote: String(span.match_quote", READER_JS)
        self.assertIn("text.indexOf(quote)", READER_JS)
        self.assertIn("utf16ToCodePointIndex", READER_JS)

    def test_cross_page_results_keep_every_page_span(self) -> None:
        self.assertIn("spans.forEach(function (span)", READER_JS)
        self.assertIn("state.highlights.get(anchorId).push", READER_JS)
        self.assertIn("pageMatchSpans: spans", READER_JS)
        self.assertNotIn("pageMatchSpans: [firstSpan]", READER_JS)

    def test_word_results_jump_to_and_highlight_the_matching_paragraph(self) -> None:
        self.assertIn("(sourceType === 'word' ? item.paragraph_id : '')", READER_JS)
        self.assertIn("paragraphId: item.paragraph_id", READER_JS)
        self.assertIn("matchStart: item.match_start", READER_JS)
        self.assertIn("matchEnd: item.match_end", READER_JS)
        self.assertIn("paragraphAnchor", READER_JS)
        self.assertIn("anchor_id: paragraphAnchor", READER_JS)
        self.assertIn("candidate != null", READER_JS)
        self.assertIn("candidate !== ''", READER_JS)
        self.assertIn("paragraphIndex: item.paragraph_index", READER_JS)
        self.assertIn("targetIndex: sourceType === 'word'", READER_JS)

    def test_legacy_doc_range_is_document_level_not_a_paragraph_page(self) -> None:
        self.assertIn("item.document_page_range", READER_JS)
        self.assertIn("item.page_source_type === 'toc_range_bound'", READER_JS)
        self.assertIn("item.page_source_type === 'unknown'", READER_JS)
        self.assertIn("'段落 ' + (absoluteIndex + 1)", READER_JS)
        self.assertIn("inferredPageContinuation", READER_JS)
        self.assertIn("!item.anchor_id", READER_JS)

    def test_fast_navigation_aborts_and_ignores_stale_requests(self) -> None:
        self.assertIn("state.abortController.abort()", READER_JS)
        self.assertIn("serial !== state.requestSerial", READER_JS)
        self.assertIn("error.name === 'AbortError'", READER_JS)

    def test_forward_pagination_uses_server_next_start_only_when_more_exists(self) -> None:
        self.assertIn("!state.hasMore || state.nextStart === null", READER_JS)
        self.assertIn("loadRange(state.nextStart, batchSize, 'forward'", READER_JS)
        self.assertIn("payload.next_start", READER_JS)

    def test_backward_pagination_uses_server_cursor_for_sparse_indices(self) -> None:
        self.assertIn("payload.previous_start", READER_JS)
        self.assertIn("state.previousStart === null", READER_JS)
        self.assertIn("loadRange(\n        state.previousStart,", READER_JS)
        self.assertNotIn("state.windowStart - batchSize", READER_JS)

    def test_old_indices_degrade_to_page_jump_with_an_explicit_notice(self) -> None:
        self.assertIn("options.precise_highlight_available === false", READER_JS)
        self.assertIn("此文献使用旧索引", READER_JS)
        self.assertIn("无法精确高亮", READER_JS)
        self.assertIn("重新导入后可启用精确定位", READER_JS)

    def test_styles_only_use_the_existing_theme_variable_family(self) -> None:
        self.assertIn(".mef-structured-reader", READER_CSS)
        self.assertIn("var(--surface-primary)", READER_CSS)
        self.assertIn("var(--text-primary)", READER_CSS)
        self.assertIn("var(--border-default)", READER_CSS)
        self.assertIn("var(--accent)", READER_CSS)
        self.assertNotRegex(READER_CSS, r"#[0-9a-fA-F]{3,8}\b")
        reader_variables = set(re.findall(r"var\((--[\w-]+)", READER_CSS))
        app_variables = set(re.findall(r"(--[\w-]+)\s*:", APP_CSS))
        self.assertTrue(reader_variables)
        self.assertEqual(reader_variables - app_variables, set())

    def test_reader_styles_do_not_override_native_pdf_reader_settings(self) -> None:
        self.assertNotIn("pdf-reader-settings", READER_CSS)
        self.assertNotIn("desktop-pdf-settings", READER_CSS)
        self.assertNotIn("currentPdfOpenMode", READER_JS)

    def test_search_detail_exposes_reader_without_replacing_open_original(self) -> None:
        self.assertIn("查看结构化文本", APP_JS)
        self.assertIn("function openSelectedStructuredReader()", APP_JS)
        self.assertIn("reader.openForSearchResult(item)", APP_JS)
        self.assertIn("function openSource(sourceId, page)", APP_JS)


if __name__ == "__main__":
    unittest.main()

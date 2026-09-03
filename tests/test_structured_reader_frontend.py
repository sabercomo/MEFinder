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
def _read_split_source(subdir: str, suffix: str, fallback: str) -> str:
    """app.js / app.css 已按功能拆分到 static/js|css/，按文件名排序拼接还原。"""

    static_dir = ROOT / "src" / "me_finder" / "static"
    parts = sorted((static_dir / subdir).glob(f"*{suffix}"), key=lambda p: p.name)
    if parts:
        return "".join(path.read_text(encoding="utf-8") for path in parts)
    return (static_dir / fallback).read_text(encoding="utf-8")


APP_CSS = _read_split_source("css", ".css", "app.css")
APP_JS = _read_split_source("js", ".js", "app.js")


class StructuredReaderFrontendTests(unittest.TestCase):
    def test_reader_header_keeps_close_in_the_rightmost_column(self) -> None:
        # 两行式头部：书名行（含 ⋯ 与 ×）在上，模式轴 / 控件行在下。
        self.assertIn(".mef-reader-headrow", READER_CSS)
        self.assertIn(".mef-reader-toolrow", READER_CSS)
        # × 关闭在书名行右侧（headRow：heading + close，无用的 ⋯ 已删）。
        self.assertIn("headRow.appendChild(close)", READER_JS)
        self.assertNotIn("mef-reader-overflow", READER_JS)

    def test_reader_header_identifies_the_current_parsing_record(self) -> None:
        self.assertIn("eyebrow: eyebrow", READER_JS)
        self.assertIn("state.source.parser_label", READER_JS)
        # 解析记录（结构化文本 · MinerU）挪进 ⋯ 提示，不再占眉标；眉标固定「正在阅读」。
        self.assertIn("'结构化文本 · ' + state.source.parser_label", READER_JS)
        open_start = READER_JS.index("async function openReader(")
        open_end = READER_JS.index("function openForSearchResult(", open_start)
        self.assertIn("state.elements.eyebrow.textContent = '正在阅读'", READER_JS[open_start:open_end])

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
        self.assertIn("Math.abs(index - state.currentIndex)", READER_JS)
        self.assertIn("Math.abs(bestIndex - state.currentIndex)", READER_JS)
        self.assertIn("dataset.readerBoundary", READER_JS)
        self.assertIn("function scheduleScrollBoundaryCheck()", READER_JS)
        self.assertIn("viewport.scrollTop + viewport.clientHeight", READER_JS)
        self.assertNotIn("offsetTop", READER_JS)
        self.assertNotIn("getBoundingClientRect", READER_JS)
        self.assertNotIn("scroll-behavior: smooth", READER_CSS)
        render_start = READER_JS.index("function renderWindow(")
        render_end = READER_JS.index("function findAnchorNode(", render_start)
        render_body = READER_JS[render_start:render_end]
        self.assertLess(
            render_body.index("positionSourceTarget(target)"),
            render_body.index("state.boundaryObserver = createBoundaryObserver()"),
        )
        self.assertNotIn("scrollIntoView", render_body)

    def test_search_hit_is_centered_and_seeds_the_first_comparison(self) -> None:
        position_start = READER_JS.index("function positionSourceTarget(target)")
        position_end = READER_JS.index("function responseItems(", position_start)
        position_body = READER_JS[position_start:position_end]
        self.assertIn("target.querySelector('mark') || target", position_body)
        self.assertIn("viewport.scrollTop", position_body)
        self.assertIn("viewport.scrollLeft = 0", position_body)
        self.assertNotIn("scrollIntoView", position_body)

        self.assertIn("function visibleSourceHighlightRange()", READER_JS)
        self.assertIn("state.resolvedHighlights.get(itemAnchor(item, index))", READER_JS)
        locate_start = READER_JS.index("async function locateInAlignedVersion")
        locate_end = READER_JS.index("function toggleCitationMenu", locate_start)
        locate_body = READER_JS[locate_start:locate_end]
        self.assertIn(
            "visibleSourceHighlightRange() || sourceCenterRange()",
            locate_body,
        )
        comparison_start = READER_JS.index("function showComparison(")
        comparison_end = READER_JS.index("function closeComparison()", comparison_start)
        comparison_body = READER_JS[comparison_start:comparison_end]
        self.assertIn("var sourceHighlight = visibleSourceHighlightRange()", comparison_body)
        self.assertIn("positionSourceTarget(state.elements.content.querySelector", comparison_body)

    def test_home_and_end_jump_to_document_boundaries_without_blank_spacers(
        self,
    ) -> None:
        self.assertIn("function handleReaderNavigationKey(event)", READER_JS)
        self.assertIn("event.key === 'Home'", READER_JS)
        self.assertIn("goTo({targetIndex: 0})", READER_JS)
        self.assertIn("event.key === 'End'", READER_JS)
        self.assertIn("state.lastPosition !== null", READER_JS)
        self.assertIn("goTo({targetIndex: state.lastPosition})", READER_JS)
        self.assertNotIn("goTo({targetIndex: state.total - 1})", READER_JS)

    def test_deep_link_anchor_must_belong_to_the_loaded_source(self) -> None:
        self.assertIn("function responseContainsAnchor(", READER_JS)
        self.assertIn(
            "!responseContainsAnchor(items, responseStart, scrollAnchorId)",
            READER_JS,
        )
        self.assertIn("链接锚点不属于该文献或已失效", READER_JS)
        validation = READER_JS.index(
            "!responseContainsAnchor(items, responseStart, scrollAnchorId)"
        )
        mutation = READER_JS.index("state.total = clampInteger(", validation)
        self.assertLess(validation, mutation)

    def test_current_page_button_uses_backend_page_display_verbatim(self) -> None:
        self.assertIn("function backendPageDisplay(item)", READER_JS)
        self.assertIn("item.page_display.trim()", READER_JS)
        self.assertIn("var pageLabel = backendPageDisplay(item)", READER_JS)
        self.assertIn("state.elements.current.textContent = pageLabel", READER_JS)
        self.assertIn("dataset.readerAction = action", READER_JS)
        self.assertIn("'toggle-citation'", READER_JS)
        self.assertNotIn(
            "state.elements.current.textContent = 'PDF 第 '",
            READER_JS,
        )

    def test_page_citations_are_requested_from_backend_not_formatted_in_js(self) -> None:
        self.assertIn("citationEndpoint: '/api/document/citation'", READER_JS)
        self.assertIn("async function prefetchCitationRange(target)", READER_JS)
        self.assertIn("function copyCachedCitation(style)", READER_JS)
        self.assertIn("method: 'POST'", READER_JS)
        self.assertIn("'Content-Type': 'application/json'", READER_JS)
        for field in ("source_id:", "start_anchor_id:", "end_anchor_id:"):
            self.assertIn(field, READER_JS)
        request_start = READER_JS.index("async function prefetchCitationRange(target)")
        request_end = READER_JS.index("function copyCachedCitation(style)", request_start)
        request_body = READER_JS[request_start:request_end]
        self.assertNotIn("start_index:", request_body)
        self.assertNotIn("end_index:", request_body)
        self.assertNotIn("format:", request_body)
        self.assertIn("target.citationPayload = payload", request_body)
        self.assertIn("target.citationPayload.citation_formats", READER_JS)
        self.assertIn("formats[style]", READER_JS)
        self.assertIn("'chinese'", READER_JS)
        self.assertIn("'gb'", READER_JS)
        self.assertNotIn("function formatCitation", READER_JS)

    def test_clipboard_failure_has_a_local_fallback_and_clear_error(self) -> None:
        self.assertIn("global.navigator.clipboard.writeText", READER_JS)
        self.assertIn("document.createElement('textarea')", READER_JS)
        self.assertIn("document.execCommand('copy')", READER_JS)
        self.assertIn("无法写入剪贴板", READER_JS)
        self.assertIn(".mef-reader-clipboard-fallback", READER_CSS)

    def test_cross_item_selection_records_mounted_codepoint_boundaries(self) -> None:
        self.assertIn("function captureMountedSelection()", READER_JS)
        self.assertIn("selection.getRangeAt(0)", READER_JS)
        self.assertIn("document.createTreeWalker(", READER_JS)
        self.assertIn("NodeFilter.SHOW_TEXT", READER_JS)
        self.assertIn("closest('.mef-reader-item-text')", READER_JS)
        self.assertIn("state.elements.content.contains(startBody)", READER_JS)
        self.assertIn("state.elements.content.contains(endBody)", READER_JS)
        self.assertIn("utf16ToCodePointIndex(startItem.text_raw", READER_JS)
        self.assertIn("utf16ToCodePointIndex(endItem.text_raw", READER_JS)
        self.assertIn("startIndex: startIndex", READER_JS)
        self.assertIn("endIndex: endIndex", READER_JS)
        self.assertIn("state.citationRange = captured", READER_JS)
        self.assertIn("selectionBlocksWindowShift()", READER_JS)
        self.assertIn("选区端点必须都在当前已载入", READER_JS)
        self.assertIn(
            "if (state.open && state.selectionDragging) scheduleSelectionCapture()",
            READER_JS,
        )
        self.assertIn("state.citationRange = null", READER_JS)
        self.assertNotIn("load all pages for selection", READER_JS)

    def test_selection_can_locate_and_highlight_an_aligned_version(self) -> None:
        self.assertIn(
            "alignmentTargetsEndpoint: '/api/text-alignments/targets'",
            READER_JS,
        )
        self.assertIn(
            "alignmentLocateEndpoint: '/api/text-alignments/locate'",
            READER_JS,
        )
        self.assertIn("async function locateInAlignedVersion", READER_JS)
        for field in (
            "source_file_id:",
            "target_source_file_id:",
            "start_page_index:",
            "end_page_index:",
            "start_offset:",
            "end_offset:",
        ):
            self.assertIn(field, READER_JS)
        self.assertIn("pageMatchSpans: payload.page_match_spans", READER_JS)
        self.assertIn("'在' + alignmentTargetDisplayLabel(target) + '中定位'", READER_JS)
        self.assertIn("function alignmentTargetDisplayLabel(target)", READER_JS)
        self.assertIn(".mef-reader-alignment-action", READER_CSS)
        self.assertIn("function generateTextAlignmentAction", APP_JS)
        self.assertIn("'/api/text-alignments/generate'", APP_JS)
        self.assertIn("pivot_source_file_id: pivotSourceId", APP_JS)
        self.assertIn("generateSelectedTextAlignmentAction", APP_JS)
        self.assertIn("openVersionSelect", APP_JS)
        self.assertIn("pickPairVersion", APP_JS)
        self.assertIn("result.accepted_link_count", APP_JS)
        self.assertIn("result.rejected_link_count", APP_JS)
        self.assertIn("result.unmatched_link_count", APP_JS)
        comparison_start = READER_JS.index("function renderComparisonWindow()")
        comparison_end = READER_JS.index("async function loadComparisonWindow", comparison_start)
        comparison_body = READER_JS[comparison_start:comparison_end]
        self.assertIn("target.querySelector('mark')", comparison_body)
        self.assertIn("viewport.scrollTop", comparison_body)
        self.assertIn("viewport.scrollLeft = 0", comparison_body)
        self.assertNotIn("scrollIntoView", comparison_body)

    def test_group_alignment_accepts_pdf_and_epub_but_not_docx(self) -> None:
        helper = re.search(
            r"function documentSupportsTextAlignment\(source\) \{"
            r"(?P<body>.*?)\n\s*\}",
            APP_JS,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(helper)
        body = helper.group("body")
        self.assertIn("libraryFileFacet(source)", body)
        self.assertIn("facet === 'pdf' || facet === 'epub'", body)
        self.assertNotIn("'word'", body)
        self.assertIn("documentSupportsTextAlignment(src)", APP_JS)

    def test_group_manager_has_a_fixed_shell_and_one_vertical_scroll_region(self) -> None:
        self.assertIn("height: min(760px, calc(100dvh - 48px))", APP_CSS)
        self.assertIn(".group-manage-body { flex: 1 1 auto; min-height: 0; overflow-y: auto", APP_CSS)
        self.assertIn("scrollbar-gutter: stable", APP_CSS)
        self.assertIn(".grp-remove-btn { grid-column: 2; grid-row: 2; }", APP_CSS)
        self.assertNotIn(".grp-remove-btn:active { transform", APP_CSS)
        self.assertIn(".group-manage-foot { flex: 0 0 auto", APP_CSS)

    def test_aligned_pdf_versions_can_read_side_by_side_by_segment(self) -> None:
        self.assertIn("function sourceCenterRange()", READER_JS)
        self.assertIn("caretPositionFromPoint", READER_JS)
        self.assertIn("function showComparison(payload, targetDisplayName)", READER_JS)
        self.assertIn("function scheduleComparisonFollow()", READER_JS)
        self.assertIn("function loadComparisonWindow", READER_JS)
        self.assertIn("译本对照 · ", READER_JS)
        # 自动跟随从文字按钮改为开关（标签 + 轨道），状态用 is-active + aria-pressed 表达。
        self.assertIn("mef-reader-follow-switch", READER_JS)
        self.assertIn("自动跟随", READER_JS)
        self.assertIn("dataset.readerComparison", READER_JS)
        self.assertIn("payload.previous_start", READER_JS)
        self.assertIn("payload.next_start", READER_JS)
        self.assertIn(".mef-reader-body.is-comparing", READER_CSS)
        self.assertIn(
            "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);",
            READER_CSS,
        )
        self.assertNotIn("scrollTop / scrollHeight", READER_JS)

    def test_aligned_epub_uses_paragraph_spans_and_labels(self) -> None:
        highlight_start = READER_JS.index("function setComparisonHighlights(spans)")
        highlight_end = READER_JS.index("function comparisonItemAnchor(", highlight_start)
        highlight_body = READER_JS[highlight_start:highlight_end]
        for field in (
            "span.paragraph_id",
            "span.paragraph_char_start",
            "span.paragraph_char_end",
            "span.page_char_start",
            "span.page_char_end",
        ):
            self.assertIn(field, highlight_body)

        anchor_start = highlight_end
        anchor_end = READER_JS.index("function renderComparisonItem(", anchor_start)
        self.assertIn("item.paragraph_id", READER_JS[anchor_start:anchor_end])

        render_end = READER_JS.index("function updateComparisonControls(", anchor_end)
        render_body = READER_JS[anchor_end:render_end]
        self.assertIn("item.item_type === 'word_paragraph'", render_body)
        self.assertIn("'段落 ' + (absoluteIndex + 1)", render_body)
        self.assertIn("'本段无可显示文本'", render_body)

    def test_deep_link_uses_stable_anchors_and_validated_recovery_fields(self) -> None:
        self.assertIn("function parseReaderDeepLink(locationValue)", READER_JS)
        self.assertIn("pathname !== '/reader'", READER_JS)
        self.assertIn("var anchorId = String(params.get('page')", READER_JS)
        # Persisted Word anchors such as MEWJ-01-P000001 intentionally do not
        # share the source-01 prefix; source/anchor ownership is verified by
        # the backend rather than guessed from the string prefix.
        self.assertNotIn("anchorId.indexOf(sourceId + '-')", READER_JS)
        self.assertIn("(?:-PAGE-|-P)(\\d+)$", READER_JS)
        self.assertIn("/^[0-9a-f]{16}$/i", READER_JS)
        self.assertIn("codePointLength(rawQuote) > 50", READER_JS)
        self.assertIn("if (hashValue && !/^[0-9a-f]{16}$/i", READER_JS)
        self.assertIn("if (offsetValue && !offset)", READER_JS)
        self.assertIn("unknownParameter", READER_JS)
        self.assertIn(
            "preciseHighlightAvailable: spans.length ? true : undefined",
            READER_JS,
        )
        self.assertIn("params.getAll('source').length !== 1", READER_JS)
        self.assertIn("search.length > 1024", READER_JS)
        self.assertIn("page_text_hash: pageTextHash", READER_JS)
        self.assertIn("match_quote: matchQuote", READER_JS)
        self.assertIn("params.set('page', anchorId)", READER_JS)
        self.assertNotIn("params.set('page', String(index))", READER_JS)

    def test_each_citation_format_has_its_own_copy_gate(self) -> None:
        self.assertIn("function citationStyleCanCopy(target, style)", READER_JS)
        self.assertIn("formats[style + '_status'] === 'complete'", READER_JS)
        self.assertIn(
            "!citationStyleCanCopy(target, 'chinese')",
            READER_JS,
        )
        self.assertIn("!citationStyleCanCopy(target, 'gb')", READER_JS)
        self.assertIn("state.citationLoading = false;", READER_JS)

    def test_deep_link_history_updates_only_when_current_anchor_changes(self) -> None:
        self.assertIn("state.lastHistoryAnchor === anchorId", READER_JS)
        self.assertIn("function scheduleReaderDeepLink(item, index, anchorId)", READER_JS)
        self.assertIn("state.deepLinkTimer = global.setTimeout", READER_JS)
        self.assertIn("global.history.replaceState(", READER_JS)
        self.assertIn("state.lastHistoryAnchor = anchorId", READER_JS)
        self.assertIn("state.lastSession = {", READER_JS)
        self.assertIn("function restoreReaderLocation()", READER_JS)
        self.assertIn("parseReaderDeepLink(global.location) || state.lastSession", READER_JS)
        self.assertIn("restore: restoreReaderLocation", READER_JS)
        self.assertIn("state.originalUrl || '/'", READER_JS)

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
        # Every text slice is fed code-point→UTF-16 converted bounds, never raw
        # code-point offsets. The single slice in appendHighlightedText converts
        # both of its bounds.
        self.assertIn("codePointToUtf16Index(text, start)", READER_JS)
        self.assertIn("codePointToUtf16Index(text, end)", READER_JS)
        self.assertGreaterEqual(
            READER_JS.count("codePointToUtf16Index(text,"),
            2,
        )
        self.assertIn("offsetUnit === 'unicode_codepoint'", READER_JS)

    def test_highlighting_is_safe_and_does_not_inject_search_html(self) -> None:
        self.assertIn("document.createElement('mark')", READER_JS)
        # The slice is wrapped in a text node (never innerHTML), then the mark
        # adopts that node — HTML in the source text can never become markup.
        self.assertIn("var node = document.createTextNode(slice)", READER_JS)
        self.assertIn("mark.appendChild(node)", READER_JS)
        self.assertIn("document.createTextNode", READER_JS)
        self.assertNotIn(".innerHTML", READER_JS)
        self.assertIn("span.page_text_hash", READER_JS)
        self.assertIn("item.page_text_hash", READER_JS)
        self.assertIn("matchQuote: String(span.match_quote", READER_JS)
        self.assertIn("text.indexOf(quote, fromUtf16)", READER_JS)
        self.assertIn("utf16ToCodePointIndex", READER_JS)

    def test_hash_recovery_chooses_quote_nearest_the_saved_offset(self) -> None:
        self.assertIn(
            "function nearestQuoteRange(text, quote, savedCodePointStart)",
            READER_JS,
        )
        self.assertIn("text.indexOf(quote, fromUtf16)", READER_JS)
        self.assertIn("Math.abs(foundStart - savedCodePointStart)", READER_JS)
        self.assertIn("nearestQuoteRange(text, quote, range.start)", READER_JS)

    def test_cross_page_results_keep_every_page_span(self) -> None:
        self.assertIn("spans.forEach(function (span)", READER_JS)
        self.assertIn("state.highlights.get(anchorId).push", READER_JS)
        self.assertIn("pageMatchSpans: spans", READER_JS)
        self.assertNotIn("pageMatchSpans: [firstSpan]", READER_JS)

    def test_single_page_citation_is_prefetched_and_selection_caches_range(self) -> None:
        self.assertIn("citation_formats: item.citation_formats || {}", READER_JS)
        self.assertIn("page_range: {verified: item.page_verified === true}", READER_JS)
        self.assertIn("prefetchCitationRange(captured)", READER_JS)
        copy_start = READER_JS.index("function copyCachedCitation(style)")
        copy_end = READER_JS.index("function truncateCodePoints(", copy_start)
        self.assertNotIn("fetchFunction()", READER_JS[copy_start:copy_end])
        self.assertIn("writeClipboard(citation)", READER_JS[copy_start:copy_end])
        self.assertIn("formats.can_copy === true", READER_JS)

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

    def test_large_desktop_reader_increases_text_and_reading_measure(self) -> None:
        self.assertIn(
            "@media (min-width: 1500px) and (min-height: 800px)",
            READER_CSS,
        )
        self.assertIn("width: min(900px, calc(100% - 64px));", READER_CSS)
        self.assertIn("font-size: 18px; line-height: 1.92;", READER_CSS)

    def test_reader_panel_is_contained_by_the_application_viewport(self) -> None:
        self.assertRegex(
            READER_CSS,
            r"\.mef-structured-reader \{[^}]*padding: 24px;[^}]*overflow: hidden;",
        )
        self.assertRegex(
            READER_CSS,
            r"\.mef-reader-panel \{[^}]*max-width: 980px;[^}]*max-height: 900px;[^}]*margin: auto;",
        )
        self.assertRegex(
            READER_CSS,
            r"\.mef-reader-pane-header \{[^}]*flex-wrap: wrap;",
        )

    def test_search_detail_exposes_reader_without_replacing_open_original(self) -> None:
        self.assertIn("查看结构化文本", APP_JS)
        self.assertIn("function openSelectedStructuredReader()", APP_JS)
        self.assertIn("reader.openForSearchResult(item)", APP_JS)
        self.assertIn("function openSource(sourceId, page)", APP_JS)


if __name__ == "__main__":
    unittest.main()

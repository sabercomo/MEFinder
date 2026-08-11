from __future__ import annotations

import re
import unittest
from pathlib import Path

from src.me_finder.web import HTML


WEB_SOURCE = Path("src/me_finder/web.py").read_text(encoding="utf-8")
SEARCH_SERVICE_SOURCE = Path(
    "src/me_finder/application/search_service.py"
).read_text(encoding="utf-8")


class SearchControlsAndViewsTests(unittest.TestCase):
    def test_search_controls_share_one_row_without_native_source_or_limit_selects(self) -> None:
        self.assertIn('class="search-controls-row"', HTML)
        self.assertIn('id="query" class="search-box" placeholder="搜索引文"', HTML)
        self.assertNotIn('placeholder="输入中文引文…"', HTML)
        self.assertIn('id="source-type-control"', HTML)
        self.assertIn('id="document-select"', HTML)
        self.assertIn('id="limit-select"', HTML)
        self.assertNotIn('id="filter-source"', HTML)
        self.assertNotIn('id="filter-limit"', HTML)
        self.assertIn('data-value="200"', HTML)
        self.assertIn('data-value="all"', HTML)
        self.assertIn("searchLimit = limit === 'all'", HTML)

    def test_primary_search_and_library_dropdowns_use_application_menus(self) -> None:
        self.assertIn('id="library-sort-field-select"', HTML)
        # 排序方向合并成一个可点按钮（升/降切换），不再是独立下拉。
        self.assertIn('id="library-sort-dir"', HTML)
        self.assertIn('onclick="toggleLibrarySortDirection()"', HTML)
        self.assertIn("function toggleLibrarySortDirection()", HTML)
        self.assertIn("setLibrarySortOption(event,'field','title')", HTML)
        self.assertIn("setLibrarySortOption(event,'field','source_type')", HTML)
        self.assertIn("setLibrarySortOption(event,'field','status')", HTML)
        self.assertIn("function librarySortProjection(source)", HTML)
        self.assertIn("localStorage.setItem('meFinderLibrarySortField'", HTML)
        self.assertIn('id="citation-style-control"', HTML)
        self.assertIn('segment-style-select', HTML)

    def test_search_detail_context_is_compact_expandable_and_actions_are_docked(self) -> None:
        self.assertIn("const DETAIL_CONTEXT_PREVIEW_CHARS = 180", HTML)
        self.assertIn("const characters = Array.from(String(text || ''))", HTML)
        self.assertIn("characters.slice(-DETAIL_CONTEXT_PREVIEW_CHARS)", HTML)
        self.assertIn("characters.slice(0, DETAIL_CONTEXT_PREVIEW_CHARS)", HTML)
        self.assertIn("const label = isBefore ? '上文' : '下文'", HTML)
        self.assertIn('class="detail-context-toggle" type="button"', HTML)
        self.assertIn('aria-expanded="false" aria-controls="', HTML)
        self.assertIn('data-character-truncated="', HTML)
        self.assertIn("(characterTruncated ? '' : ' hidden')", HTML)
        self.assertIn("function refreshDetailContextToggles(panel)", HTML)
        self.assertIn("preview.scrollHeight > preview.clientHeight + 1", HTML)
        self.assertIn("function observeDetailContextLayout(panel)", HTML)
        self.assertIn("new ResizeObserver(function()", HTML)
        self.assertIn("detailContextResizeObserver.observe(detailScroll)", HTML)
        self.assertIn("function toggleDetailContext(button)", HTML)
        self.assertIn("preview.hidden = expanded", HTML)
        self.assertIn("full.hidden = !expanded", HTML)
        self.assertRegex(
            HTML,
            r"\.detail-context-preview\s*\{[^}]*-webkit-line-clamp:\s*4",
        )

        detail_start = HTML.index("function showDetail(item)")
        detail_end = HTML.index("function showEmptyDetail()", detail_start)
        detail_source = HTML[detail_start:detail_end]
        self.assertLess(
            detail_source.index('<div class="detail-scroll">'),
            detail_source.index('<div class="detail-actions"'),
        )
        self.assertIn("panel.querySelector('.detail-scroll')", detail_source)
        self.assertNotIn("document.querySelector('.results-detail-pane')", detail_source)
        self.assertRegex(HTML, r"\.detail-scroll\s*\{[^}]*overflow-y:\s*auto")
        self.assertRegex(HTML, r"\.detail-actions\s*\{[^}]*flex:\s*0 0 auto")
        self.assertRegex(
            HTML,
            r"\.detail-actions\s*\{[^}]*padding:\s*16px 28px 24px[^}]*gap:\s*8px",
        )
        self.assertNotRegex(HTML, r"\.detail-actions\s*\{[^}]*position:\s*sticky")
        self.assertRegex(HTML, r"\.detail-card\s*\{[^}]*overflow:\s*visible")
        primary_open_action = '<button class="action-btn primary" type="button" onclick="openSource('
        self.assertIn(primary_open_action, detail_source)
        self.assertIn('id="detail-format-control"', detail_source)
        self.assertIn('aria-label="选择出处格式"', detail_source)
        self.assertIn('aria-haspopup="menu"', detail_source)
        self.assertIn('role="menu" aria-label="出处格式"', detail_source)
        format_control = detail_source.index('id="detail-format-control"')
        copy_action = detail_source.index('onclick="copySelectedCitation()">复制出处</button>')
        structured_action = detail_source.index('onclick="openSelectedStructuredReader()">查看结构化文本</button>')
        open_action = detail_source.index(primary_open_action)
        self.assertLess(format_control, copy_action)
        self.assertLess(copy_action, structured_action)
        self.assertLess(structured_action, open_action)
        self.assertIn('id="citation-style-control"', detail_source)
        self.assertIn("citationStyleMenuMarkup()", detail_source)
        self.assertNotIn('id="detail-copy-control"', detail_source)
        self.assertNotIn("复制原文", detail_source)
        self.assertNotIn("复制原文与出处", detail_source)
        self.assertNotIn('id="detail-more-control"', detail_source)
        self.assertNotIn('aria-label="更多操作" title="更多操作"', detail_source)
        self.assertNotIn("补全书目信息", detail_source)
        self.assertIn("function logicalPageSideLabel(side, precision)", HTML)
        self.assertIn(
            "pdRow('双开位置', logicalPageSideLabel(item.logical_page_side, item.spread_hit_precision))",
            detail_source,
        )
        self.assertNotIn("function runSearchDetailAction(event, action)", HTML)
        self.assertNotIn("if (action === 'copy-original')", HTML)
        self.assertNotIn("if (action === 'copy-citation')", HTML)
        self.assertNotIn("if (action === 'copy-combined')", HTML)
        self.assertNotIn("function copySelectedOriginal()", HTML)
        self.assertNotIn("function copySelectedOriginalAndCitation()", HTML)
        self.assertIn("function copySelectedCitation()", HTML)
        self.assertIn("copyText(citationForItem(item));", HTML)
        self.assertIn("if (label) label.textContent = citationStyleDisplayLabel(citationStyle);", HTML)
        self.assertIn("function openSelectedStructuredReader()", HTML)
        self.assertIn("async function openSource(sourceId, page)", HTML)
        self.assertIn("async function openMetadataForSource(sourceId)", HTML)
        self.assertIn(".detail-format-trigger", HTML)
        self.assertIn(".detail-format-menu", HTML)
        actions_css = re.search(r"\.detail-actions\s*\{([^}]*)\}", HTML)
        self.assertIsNotNone(actions_css)
        self.assertNotIn("box-shadow", actions_css.group(1))

        keydown_start = HTML.index("document.addEventListener('keydown', function(e)")
        keydown_end = HTML.index("/* ═══ Helpers ═══ */", keydown_start)
        keydown_source = HTML[keydown_start:keydown_end]
        self.assertIn("isSearchShortcutInteractiveTarget(e.target)", keydown_source)
        self.assertIn("e.target.id !== 'query'", keydown_source)
        self.assertIn("'button, input, textarea, select, summary, a[href]", HTML)

    def test_narrow_search_layout_opens_one_detail_with_a_list_return(self) -> None:
        self.assertIn("selectResult(0, false)", HTML)
        self.assertIn("function selectResult(index, openNarrowDetail)", HTML)
        self.assertIn("if (openNarrowDetail !== false) showSearchResultDetail();", HTML)
        self.assertIn("function showSearchResultDetail()", HTML)
        self.assertIn("area.classList.add('is-detail-open')", HTML)
        self.assertIn("function showSearchResultsList()", HTML)
        self.assertIn("area.classList.remove('is-detail-open')", HTML)
        self.assertIn('onclick="showSearchResultsList()"', HTML)
        self.assertIn("返回结果列表", HTML)
        self.assertIn("@media (max-width: 959px)", HTML)
        self.assertIn(".results-area.is-detail-open > .results-list-pane { display: none; }", HTML)
        self.assertRegex(
            HTML,
            r"\.results-area\.is-detail-open\s*>\s*\.results-detail-pane\s*\{[^}]*display:\s*block",
        )
        self.assertRegex(HTML, r"\.detail-mobile-toolbar\s*\{\s*display:\s*none")

        row_start = HTML.index("function resultRowHTML(item, index)")
        row_end = HTML.index("function searchResultsArea()", row_start)
        row_source = HTML[row_start:row_end]
        self.assertLess(row_source.index("result-score"), row_source.index("result-match-type"))
        self.assertLess(row_source.index("result-match-type"), row_source.index("result-title"))

    def test_search_detail_warns_before_incomplete_citation_copy(self) -> None:
        detail_start = HTML.index("function showDetail(item)")
        detail_end = HTML.index("function showEmptyDetail()", detail_start)
        detail_source = HTML[detail_start:detail_end]
        self.assertIn("citationAvailabilityMarkup(item)", detail_source)
        self.assertLess(
            detail_source.index("citationAvailabilityMarkup(item)"),
            detail_source.index('<div class="detail-body">'),
        )

        availability_start = HTML.index("function citationAvailabilityMarkup(item)")
        availability_end = HTML.index("function updateDetailCitationAvailability()", availability_start)
        availability_source = HTML[availability_start:availability_end]
        self.assertIn("citationIsComplete(item)", availability_source)
        self.assertNotIn("_status']", availability_source)
        self.assertIn("出处信息不完整", availability_source)
        self.assertIn("暂不可生成完整引文；仍可查看正文和打开原文。", availability_source)
        self.assertIn("role=\"status\"", availability_source)
        self.assertIn("status.hidden = citationIsComplete(item)", HTML)
        self.assertIn("updateDetailCitationAvailability();", HTML)
        self.assertIn(".detail-citation-status[hidden] { display: none !important; }", HTML)

        self.assertIn("if (!citationIsComplete(item)) { showCitationMetadataError(item); return; }", HTML)
        self.assertEqual(
            HTML.count("if (!citationIsComplete(item)) { showCitationMetadataError(item); return; }"),
            1,
        )
        self.assertIn("return formats[citationStyle] || formats.chinese || formats.gb || item.copy_text || '';", HTML)

    def test_library_filters_by_language_alongside_file_type(self) -> None:
        # 语言与文件类型是筛选弹层里的两组分面（不再是平铺分段控件）。
        self.assertIn('id="filter-opts-lang"', HTML)
        self.assertIn('id="filter-opts-type"', HTML)
        self.assertIn("else if (kind === 'lang') libLangFilter = value;", HTML)
        self.assertIn("(s.language || 'chinese') === libLangFilter", HTML)

    def test_library_filters_by_document_type(self) -> None:
        self.assertIn('id="filter-opts-doctype"', HTML)
        self.assertIn("{v:'book', label:'著作'", HTML)
        self.assertIn("{v:'journal_article', label:'期刊论文'", HTML)
        self.assertIn("{v:'thesis', label:'学位论文'", HTML)
        self.assertLess(
            HTML.index("{v:'journal_article', label:'期刊论文'"),
            HTML.index("{v:'thesis', label:'学位论文'"),
        )
        self.assertIn("if (kind === 'doctype') libDocTypeFilter = value;", HTML)
        self.assertIn("function libraryDocType(source)", HTML)
        self.assertIn("libraryDocType(s) === libDocTypeFilter", HTML)

    def test_library_filter_lives_in_one_button_before_the_toolbar(self) -> None:
        # 三个筛选收进一个「筛选」按钮，位于视图切换/排序之前（Notion / Linear 范式）。
        filter_btn = HTML.index('id="library-filter"')
        view_switch = HTML.index('class="view-switch"', filter_btn)
        self.assertLess(filter_btn, view_switch)
        # 弹层分面按 文献类型 → 语言 → 文件 排列。
        pop = HTML[filter_btn:view_switch]
        self.assertLess(pop.index('id="filter-opts-doctype"'), pop.index('id="filter-opts-lang"'))
        self.assertLess(pop.index('id="filter-opts-lang"'), pop.index('id="filter-opts-type"'))

    def test_registered_pdf_can_be_resubmitted_to_mineru_from_the_drawer(self) -> None:
        self.assertIn('"/api/mineru-reparse"', WEB_SOURCE)
        self.assertNotIn("原生文本，本地解析即可，无需 MinerU OCR", WEB_SOURCE)
        self.assertIn("force_mineru=True,", WEB_SOURCE)
        self.assertIn(
            'display_file_name=str(record.get("file_name") or "")',
            WEB_SOURCE,
        )
        self.assertIn("job.get(\"source_file_id\") == sid", WEB_SOURCE)
        self.assertIn(
            'job.get("status") in {"processing", "cancelling"}',
            WEB_SOURCE,
        )
        self.assertIn("function submitMineruReparse(sourceId)", HTML)
        self.assertNotIn("function pollMineruReparse(sourceId, jobId)", HTML)
        self.assertIn("importQueue.push(queueItem)", HTML)
        self.assertIn("navigateTo('import')", HTML)
        self.assertIn("pollImportJob(queueItem.id)", HTML)
        self.assertIn("queue.scrollIntoView({behavior: 'smooth', block: 'start'})", HTML)
        self.assertIn("MinerU 在线解析", HTML)
        self.assertIn("重新 OCR", HTML)
        self.assertNotIn("src.pdf_profile.detected_pdf_type !== 'native_text'", HTML)
        self.assertIn("将把这份 PDF 上传到 MinerU 在线服务重新解析", HTML)

    def test_import_page_offers_three_parse_modes_with_auto_default(self) -> None:
        # 三种解析方式常驻可见，auto 默认选中；用户可主动强制 MinerU 或视觉 API。
        self.assertIn('name="pdf-parse-mode"', HTML)
        self.assertIn('value="auto" checked', HTML)
        self.assertIn('value="mineru"', HTML)
        self.assertIn('value="vision"', HTML)
        self.assertIn("function selectedPdfParseMode()", HTML)
        self.assertIn("parse_mode: q.parseMode || 'auto'", HTML)
        self.assertIn("parseMode: ext === '.pdf' ? pdfParseMode : null", HTML)
        self.assertIn("/api/import-upload/start", HTML)
        self.assertIn("q.file.slice(offset, end)", HTML)
        self.assertIn("pdf_parse_mode: selectedPdfParseMode()", HTML)
        self.assertIn("vision_provider_id: selectedVisionProviderId()", HTML)
        self.assertIn('self.headers.get("X-PDF-Parse-Mode", "auto")', WEB_SOURCE)
        self.assertIn('payload.get("pdf_parse_mode") or "auto"', WEB_SOURCE)
        self.assertIn('force_mineru = is_pdf and pdf_parse_mode == "mineru"', WEB_SOURCE)
        self.assertIn('"parse_route": parse_route', WEB_SOURCE)

    def test_directory_batch_import_is_bounded_and_isolates_pdf_index_writes(self) -> None:
        self.assertIn("ImportTaskQueue(worker_count=2)", WEB_SOURCE)
        self.assertIn("def start_native_import_batch(", WEB_SOURCE)
        self.assertIn("def start_remote_import_batch(", WEB_SOURCE)
        self.assertIn("def index_registered_pdf(", WEB_SOURCE)
        self.assertIn("replace_source_in_database(", WEB_SOURCE)
        self.assertIn("fail_import_at_index(job_id, exc, parsed=True)", WEB_SOURCE)
        self.assertIn(
            "native_pdf_job_ids = start_native_import_batch(",
            WEB_SOURCE,
        )
        self.assertIn(
            "word_job_ids = start_native_import_batch(word_items)",
            WEB_SOURCE,
        )
        self.assertIn("remote_job_ids = start_remote_import_batch(remote_items)", WEB_SOURCE)
        self.assertNotIn("for raw in raw_paths[:50]", WEB_SOURCE)
        self.assertIn("一次最多批量导入 50 个文件，请分批选择。", WEB_SOURCE)
        self.assertIn("const SCAN_IMPORT_BATCH_LIMIT = 50", HTML)
        self.assertIn("groups.ready.concat(groups.ocr)", HTML)
        self.assertIn("autoSelectable.slice(0, SCAN_IMPORT_BATCH_LIMIT)", HTML)
        self.assertIn("section('需 OCR 的新文件', groups.ocr, true, autoSelected)", HTML)
        self.assertIn("function handleScanCheckChange(input)", HTML)
        self.assertIn("checked > SCAN_IMPORT_BATCH_LIMIT", HTML)
        self.assertIn("function setupScanResultDragSelection()", HTML)
        self.assertIn("targetChecked: !input.checked", HTML)
        # marquee 建框收尾抽入共用 begin/endDragSelectionMarquee 后，类名经调用点传入。
        self.assertIn("beginDragSelectionMarquee(state, results, 'scan-selection-marquee', event)", HTML)
        self.assertIn("checkedCount < SCAN_IMPORT_BATCH_LIMIT", HTML)
        self.assertIn("setupScanResultDragSelection();", HTML)
        self.assertIn("下一批 ' + nextBatchCount + ' 个已自动勾选", HTML)
        self.assertIn("个未导入：", HTML)
        self.assertIn("if (submittedPaths.has(entry.path)) entry.status = 'processing'", HTML)
        # 扫描结果的拖动框选与文献库共用同一套内容坐标/边缘滚动实现，
        # 否则一次同样只能选中屏幕里放得下的那几行。
        self.assertIn("function scanScrollContainer()", HTML)
        self.assertIn("function updateScanDragSelection()", HTML)
        self.assertIn("}, dragSelectionAnchor(scroller, event));", HTML)
        self.assertIn("runDragSelectionAutoScroll(state, updateScanDragSelection);", HTML)
        self.assertIn("dragSelectionHits(item, box, state.scroller)", HTML)
        self.assertNotIn(
            "var hit = !!box && rect.right >= left && rect.left <= right "
            "&& rect.bottom >= top && rect.top <= bottom;",
            HTML,
        )
        self.assertNotIn("failed.forEach(function(err) { console.warn('import-local failed:', err.path, err.error); });\n    await runDirectoryScan();", HTML)

    def test_explicit_save_settings_show_unsaved_hint(self) -> None:
        """C-02：需显式保存的设置区块（MinerU / 视觉接口）改动即提示尚未保存。"""

        self.assertIn("function markSettingsSectionDirty(hintId)", HTML)
        self.assertIn("有未保存的修改，记得点保存", HTML)
        self.assertIn("mineruStatus.textContent = '有未保存的修改'", HTML)
        self.assertIn("t.closest('#vision-editor-card')) markSettingsSectionDirty('vision-save-hint')", HTML)

    def test_parse_modes_sit_above_dropzone_with_progressive_failure_recovery(self) -> None:
        """恢复 0.3.8 层级，并只在失败后渐进显示视觉 API 恢复区。"""

        self.assertNotIn('class="import-intro">', HTML)
        drop = HTML.index('id="drop-zone"')
        modes = HTML.index('class="pdf-parse-section"')
        scan = HTML.index('id="scan-section"')
        queue = HTML.index('id="import-queue"')
        self.assertLess(modes, drop)
        self.assertLess(drop, scan)
        self.assertLess(scan, queue)
        self.assertIn("padding: 36px 32px;", HTML)
        self.assertIn("height: 28px; padding: 0 8px", HTML)
        self.assertIn('id="import-recovery-panel" hidden', HTML)
        self.assertIn('id="import-recovery-provider"', HTML)
        self.assertIn("function importQueueNeedsRecoverySelector()", HTML)
        self.assertIn("panel.hidden = !shouldShow", HTML)
        self.assertIn("q.status === 'error'", HTML)
        self.assertNotIn('class="pdf-parse-details"', HTML)
        # I-02：扫描只列出文件、不直接导入。
        self.assertIn("扫描只列出文件、不直接导入", HTML)

    def test_optional_vision_api_and_mineru_fallback_are_wired(self) -> None:
        mineru_section = HTML.index('<span class="settings-section-title">MinerU API</span>')
        vision_section = HTML.index('<span class="settings-section-title">其他解析 API</span>')
        self.assertLess(mineru_section, vision_section)
        self.assertIn('id="import-vision-provider"', HTML)
        self.assertIn('id="import-vision-provider-options"', HTML)
        self.assertIn('id="vision-auto-fallback"', HTML)
        self.assertIn("function loadVisionProviders()", HTML)
        self.assertIn("function retryImportWithVision(id)", HTML)
        self.assertIn("function visionRetryProviderFor(q)", HTML)
        self.assertIn("var provider = visionRetryProviderFor(q);", HTML)
        self.assertIn("function filterImportVisionProviders(value)", HTML)
        self.assertIn("function importVisionSearchKeydown(event)", HTML)
        self.assertIn("function positionImportVisionMenu()", HTML)
        self.assertIn("providers.length > 8", HTML)
        self.assertIn("max-height: min(360px, calc(100vh - 24px))", HTML)
        self.assertIn("position: fixed", HTML)
        self.assertIn("q.mineruFailed = !!data.mineru_failed;", HTML)
        self.assertIn("mineruFailed: !!job.mineru_failed", HTML)
        render_start = HTML.index("function renderVisionProviders()")
        render_end = HTML.index("async function loadVisionProviders()", render_start)
        self.assertIn("renderImportQueue();", HTML[render_start:render_end])
        self.assertIn("fetch('/api/import-retry'", HTML)
        self.assertIn('"X-Vision-Provider-ID"', WEB_SOURCE)
        self.assertIn('"/api/vision-providers"', WEB_SOURCE)
        self.assertIn('summary.get("auto_fallback_from_mineru")', WEB_SOURCE)
        self.assertIn("can_retry_with_provider=bool(fallback)", WEB_SOURCE)
        self.assertIn("fallback = providers[0] if providers else None", WEB_SOURCE)
        self.assertNotIn(
            'default_id = str(summary.get("default_provider_id") or "")',
            WEB_SOURCE,
        )

    def test_mineru_settings_support_independent_token_accounts(self) -> None:
        self.assertIn('id="mineru-token"', HTML)
        self.assertIn('id="mineru-account-list"', HTML)
        self.assertIn('id="mineru-account-name"', HTML)
        self.assertIn('id="mineru-account-dialog"', HTML)
        self.assertIn('class="mineru-account-table"', HTML)
        self.assertIn('id="parser-provider-list"', HTML)
        self.assertIn('id="parser-stat-books"', HTML)
        self.assertIn('data-target="statistics-settings"', HTML)
        self.assertIn("showSettingsCategory('statistics-settings')", HTML)
        mineru_nav = HTML.index('data-target="mineru-api-settings"')
        vision_nav = HTML.index('data-target="vision-api-settings"')
        statistics_nav = HTML.index('data-target="statistics-settings"')
        citation_nav = HTML.index('data-target="citation-format-settings"')
        self.assertLess(mineru_nav, vision_nav)
        self.assertLess(vision_nav, statistics_nav)
        self.assertLess(statistics_nav, citation_nav)
        mineru_start = HTML.index('id="mineru-api-settings"')
        statistics_start = HTML.index('id="statistics-settings"', mineru_start)
        vision_start = HTML.index('id="vision-api-settings"', statistics_start)
        self.assertNotIn('id="parser-provider-list"', HTML[mineru_start:statistics_start])
        self.assertIn('id="parser-provider-list"', HTML[statistics_start:vision_start])
        self.assertNotIn('id="mineru-access-key-id"', HTML)
        self.assertNotIn('id="mineru-secret-access-key"', HTML)
        self.assertIn("fetch('/api/mineru-accounts'", HTML)
        self.assertIn("fetch('/api/mineru-accounts/service'", HTML)
        self.assertIn("fetch('/api/mineru-accounts/test'", HTML)
        self.assertIn("fetch('/api/parser-statistics'", HTML)
        self.assertIn("async function loadParserStatistics()", HTML)
        self.assertIn("不是官网用量或计费数据", HTML)
        self.assertIn("按解析服务", HTML)
        self.assertIn("def ensure_mineru_accounts()", WEB_SOURCE)
        self.assertNotIn("access_key_id: document.getElementById", HTML)
        self.assertNotIn("secret_access_key: document.getElementById", HTML)

    def test_api_settings_collapse_and_fallback_uses_one_auto_saving_switch(self) -> None:
        self.assertIn('id="mineru-api-settings"', HTML)
        self.assertIn('data-target="mineru-api-settings"', HTML)
        self.assertIn("showSettingsCategory('mineru-api-settings')", HTML)
        self.assertIn('id="vision-api-settings"', HTML)
        self.assertIn('data-target="vision-api-settings"', HTML)
        self.assertIn("showSettingsCategory('vision-api-settings')", HTML)
        self.assertIn("function showSettingsCategory(sectionId)", HTML)
        self.assertIn('class="vision-fallback-toggle"', HTML)
        self.assertIn('id="vision-fallback-summary"', HTML)
        self.assertIn('onchange="setVisionAutoFallback(this.checked)"', HTML)
        self.assertIn("function setVisionAutoFallback(enabled)", HTML)
        self.assertNotIn('id="vision-default-provider"', HTML)
        self.assertNotIn('class="vision-default-select-wrap"', HTML)
        self.assertNotIn("function saveVisionPolicy()", HTML)
        self.assertNotIn("保存切换设置", HTML)
        policy_start = HTML.index("async function setVisionAutoFallback(enabled)")
        policy_end = HTML.index("/* ═══ Import ═══ */", policy_start)
        policy_script = HTML[policy_start:policy_end]
        self.assertNotIn("default_provider_id", policy_script)
        self.assertIn("toggle.disabled = true", policy_script)
        self.assertIn("toggle.checked = previous", policy_script)

    def test_vision_models_can_be_discovered_or_entered_manually(self) -> None:
        self.assertIn('id="vision-model"', HTML)
        self.assertIn('id="vision-model-pop"', HTML)
        self.assertIn('id="vision-model-refresh"', HTML)
        self.assertIn("function renderVisionModelPop()", HTML)
        self.assertIn("function fetchVisionModels(options)", HTML)
        self.assertIn("fetch('/api/vision-providers/models'", HTML)
        self.assertIn('"/api/vision-providers/models"', WEB_SOURCE)
        self.assertIn("manual_entry_allowed", WEB_SOURCE)
        self.assertIn("{key: 'ocr', label: 'OCR 专用 · 优先'}", HTML)
        self.assertIn("{key: 'vision', label: '支持图片'}", HTML)
        self.assertIn("{key: 'text', label: '不支持图片'}", HTML)
        self.assertNotIn("{key: 'omni', label: '全模态'}", HTML)
        self.assertNotIn("{key: 'vision', label: '通用视觉'}", HTML)
        self.assertIn("function visionModelPriority(item)", HTML)
        self.assertIn("capability-unsupported", HTML)
        self.assertNotIn("可能支持图片", HTML)

        brand_start = HTML.index("var VISION_BRAND_RULES = [")
        brand_end = HTML.index("];", brand_start)
        brand_rules = HTML[brand_start:brand_end]
        self.assertGreater(
            brand_rules.index("{re: /deepseek/i"),
            brand_rules.index("{re: /together/i"),
        )
        self.assertIn("base: 'https://api.deepseek.com', unsupported: true", brand_rules)
        self.assertIn(
            'vision-model-badge capability-unsupported">不支持图片',
            HTML,
        )
        self.assertNotIn("通义千问、DeepSeek 等视觉模型", HTML)

    def test_backup_export_import_is_wired(self) -> None:
        self.assertIn('id="backup-settings"', HTML)
        self.assertIn('data-target="backup-settings"', HTML)
        self.assertIn(
            "showSettingsCategory('backup-settings')",
            HTML,
        )
        self.assertIn('onclick="exportBackup()"', HTML)
        self.assertIn('id="backup-import-path"', HTML)
        self.assertIn("function exportBackup()", HTML)
        self.assertIn("function importBackup()", HTML)
        self.assertIn("fetch('/api/backup/export'", HTML)
        self.assertIn("fetch('/api/backup/import'", HTML)
        self.assertIn('"/api/backup/export"', WEB_SOURCE)
        self.assertIn('"/api/backup/import"', WEB_SOURCE)
        self.assertIn("from .backup_service import restore_backup, write_backup", WEB_SOURCE)

    def test_every_top_level_settings_section_is_a_switchable_panel(self) -> None:
        settings_start = HTML.index('<div class="settings-page-content">')
        settings_end = HTML.index('</div><!-- /main-area -->', settings_start)
        settings_html = HTML[settings_start:settings_end]
        sections = re.findall(
            r'<section class="([^"]+)" id="([^"]+)" role="tabpanel">',
            settings_html,
        )
        expected_ids = {
            "appearance-card",
            "pdf-reader-settings",
            "software-update-settings",
            "macos-update-settings",
            "data-location-settings",
            "mineru-api-settings",
            "statistics-settings",
            "vision-api-settings",
            "citation-format-settings",
            "bib-completion-settings",
            "backup-settings",
        }
        self.assertEqual({section_id for _, section_id in sections}, expected_ids)
        for classes, section_id in sections:
            self.assertIn("settings-section", classes)
            self.assertIn(f'data-target="{section_id}"', settings_html)
            self.assertIn(f"showSettingsCategory('{section_id}')", settings_html)

    def test_batch_metadata_detection_is_wired(self) -> None:
        self.assertIn('id="batch-metadata-btn"', HTML)
        self.assertIn("function runBatchMetadataDetection()", HTML)
        self.assertIn("fetch('/api/bibliographic-metadata/batch-detect'", HTML)
        self.assertIn('"/api/bibliographic-metadata/batch-detect"', WEB_SOURCE)
        self.assertIn("def batch_metadata_candidates()", WEB_SOURCE)
        self.assertIn('if source == "manual":', WEB_SOURCE)
        self.assertIn("batchmeta-", WEB_SOURCE)

    def test_online_metadata_auto_match_threshold_defaults_to_90_percent(self) -> None:
        self.assertIn("const ONLINE_METADATA_AUTO_MATCH_THRESHOLD_DEFAULT = 0.90;", HTML)
        self.assertIn("function automaticBatchCandidateIndex(candidates)", HTML)
        self.assertIn("score >= onlineMetadataAutoMatchThreshold", HTML)
        self.assertIn("var automaticIndex = automaticBatchCandidateIndex(candidates);", HTML)
        self.assertNotIn(
            "candidates.length === 1 && candidates[0].match && candidates[0].match.level === 'high'",
            HTML,
        )

    def test_online_auto_match_threshold_has_its_own_settings_category(self) -> None:
        self.assertIn('data-target="bib-completion-settings"', HTML)
        self.assertIn('id="bib-completion-settings"', HTML)
        self.assertIn('id="online-auto-match-range"', HTML)
        self.assertIn("setOnlineAutoMatchThreshold(this.value)", HTML)
        self.assertIn("function loadOnlineAutoMatchThreshold()", HTML)
        # 匹配门槛属于“书目补全”，不能塞进“引文格式”一级菜单
        bib_at = HTML.index('data-target="bib-completion-settings"')
        citation_at = HTML.index('data-target="citation-format-settings"')
        self.assertNotEqual(bib_at, citation_at)

    def test_citation_format_preferences_filter_the_result_dropdown(self) -> None:
        vision_at = HTML.index('data-target="vision-api-settings"')
        citation_at = HTML.index('data-target="citation-format-settings"')
        appearance_at = HTML.index('data-target="appearance-card"')
        self.assertLess(vision_at, citation_at)
        self.assertLess(citation_at, appearance_at)
        self.assertIn("const DEFAULT_CITATION_STYLES = ['chinese', 'gb'];", HTML)
        self.assertIn("meFinderEnabledCitationStylesV1", HTML)
        self.assertIn("loadLocalCitationStyles() || DEFAULT_CITATION_STYLES.slice()", HTML)
        self.assertIn("saveLocalCitationStyles(enabledCitationStyles)", HTML)
        self.assertIn("body: JSON.stringify({citation_style: citationStyle})", HTML)
        self.assertIn("function citationStyleMenuMarkup()", HTML)
        self.assertIn("function setCitationStyleEnabled(style, checked)", HTML)
        for style in ("chinese", "gb", "chicago", "apa", "mla"):
            self.assertIn(f'name="citation-format" value="{style}"', HTML)
        self.assertIn("body: JSON.stringify({citation_styles: enabledCitationStyles})", HTML)

    def test_directory_scan_ui_and_endpoints_are_wired(self) -> None:
        self.assertIn('id="scan-section"', HTML)
        self.assertIn('id="scan-dir-list"', HTML)
        self.assertIn('id="scan-dir-input"', HTML)
        self.assertIn("function runDirectoryScan()", HTML)
        self.assertIn("function addScanDirectoryPaths(values)", HTML)
        self.assertIn("data.folders", HTML)
        self.assertIn("function importSelectedScanned()", HTML)
        self.assertIn("fetch('/api/scan-directories')", HTML)
        self.assertIn("fetch('/api/import-local'", HTML)
        self.assertIn("原始文件永远不会被移动或删除", HTML)
        self.assertIn("可一次选择多个文件夹", HTML)
        self.assertIn("目录数量上限", HTML)
        self.assertNotIn("消耗 MinerU 配额", HTML)
        self.assertIn('"/api/scan-directories"', WEB_SOURCE)
        self.assertIn('"/api/import-local"', WEB_SOURCE)
        self.assertIn("不在已配置的文献目录内", WEB_SOURCE)

    def test_drawer_file_info_collapses_and_editor_is_type_aware(self) -> None:
        self.assertIn('id="drawer-file-info"', HTML)
        self.assertIn("function toggleDrawerSection(event, sectionId)", HTML)
        self.assertIn('<div class="drawer-collapse-body" style="display:none">', HTML)
        self.assertIn('id="bib-doctype-control"', HTML)
        for label in ("著作", "期刊论文", "学位论文"):
            self.assertIn(label, HTML)
        self.assertNotIn("typeButton('book','图书')", HTML)
        self.assertNotIn("typeButton('translated_book','译著')", HTML)
        self.assertIn("function bibliographicEditorDocType(docType)", HTML)
        self.assertIn("docType === 'journal_article' || docType === 'thesis'", HTML)
        self.assertIn("function setBibliographicType(sourceId, docType)", HTML)
        self.assertIn("'出版刊物'", HTML)
        self.assertIn("'卷次'", HTML)
        self.assertIn("'期号'", HTML)
        self.assertIn("'页码（起止页）'", HTML)
        self.assertIn("var translator = value('translator', 'translator');", HTML)
        self.assertIn("editorDocType === 'journal_article' || editorDocType === 'thesis'", HTML)
        self.assertIn("['author','title','journal_name','publish_year','issue']", HTML)
        self.assertIn("['author','title','publisher','publish_year']", HTML)
        self.assertIn("field('publisher','publisher','学校'", HTML)
        self.assertIn("docType === 'thesis' && field === 'publisher' ? '学校'", HTML)

    def test_import_runs_bibliographic_recognition_and_missing_markers_ignore_isbn(self) -> None:
        self.assertIn('phase="metadata_recognition"', WEB_SOURCE)
        self.assertIn('persist_bibliographic_metadata(source_file_id, metadata)', WEB_SOURCE)
        self.assertIn("if (field === 'isbn'", HTML)
        self.assertIn('bibliographic-missing', HTML)

    def test_document_scope_is_searchable_and_sent_to_backend(self) -> None:
        self.assertIn('id="document-filter-query"', HTML)
        self.assertIn('function renderSearchDocumentOptions()', HTML)
        self.assertIn('function selectSearchDocument(event, sourceId)', HTML)
        self.assertIn("fetch('/api/library?view=summary')", HTML)
        self.assertIn("searchSourceFiles = data.items || []", HTML)
        self.assertNotIn("fetch('/api/sources')", HTML)
        self.assertIn("var bib = source.bibliographic || source.bibliographic_metadata || {}", HTML)
        self.assertIn('source_file_id: searchDocumentId || null', HTML)
        self.assertIn('payload.get("source_file_id")', SEARCH_SERVICE_SOURCE)

    def test_library_has_persistent_list_and_card_views(self) -> None:
        self.assertIn('aria-label="文献库显示方式"', HTML)
        self.assertIn('id="library-view-list"', HTML)
        self.assertIn('id="library-view-grid"', HTML)
        self.assertIn("localStorage.setItem('meFinderLibraryView', libViewMode)", HTML)
        self.assertIn('library-view-grid', HTML)
        self.assertIn('class="library-card library-entry', HTML)

    def test_calibration_lives_inside_library_drawer_not_a_page(self) -> None:
        self.assertNotIn('id="page-calibration"', HTML)
        self.assertNotIn('aria-label="页码校准显示方式"', HTML)
        self.assertNotIn("meFinderCalibrationView", HTML)
        self.assertIn('id="library-drawer-calibration"', HTML)
        self.assertIn("persistDisplayPreference('library_view', libViewMode)", HTML)


if __name__ == "__main__":
    unittest.main()

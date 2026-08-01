from __future__ import annotations

import re
import unittest
from pathlib import Path

from src.me_finder.web import HTML


WEB_SOURCE = Path("src/me_finder/web.py").read_text(encoding="utf-8")


class SearchControlsAndViewsTests(unittest.TestCase):
    def test_search_controls_share_one_row_without_native_source_or_limit_selects(self) -> None:
        self.assertIn('class="search-controls-row"', HTML)
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
        self.assertIn('id="library-sort-direction-select"', HTML)
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
        combined_copy_action = (
            '<button class="action-btn" onclick="copySelectedOriginalAndCitation()">'
            "复制原文与出处</button>"
        )
        self.assertIn(combined_copy_action, detail_source)
        self.assertNotIn('id="detail-more-control"', detail_source)
        self.assertNotIn('aria-haspopup="menu"', detail_source)
        self.assertNotIn('role="menuitem"', detail_source)
        structured_reader_action = (
            '<button class="action-btn" onclick="openSelectedStructuredReader()">'
            "查看结构化文本</button>"
        )
        self.assertIn(structured_reader_action, detail_source)
        self.assertNotIn(
            'role="menuitem" onclick="openSelectedStructuredReader()',
            detail_source,
        )
        self.assertLess(
            detail_source.index(combined_copy_action),
            detail_source.index(structured_reader_action),
        )
        self.assertLess(
            detail_source.index(structured_reader_action),
            detail_source.index('class="action-btn primary"'),
        )
        self.assertNotIn(".detail-more-control", HTML)
        self.assertNotIn(".detail-more-trigger", HTML)
        actions_css = re.search(r"\.detail-actions\s*\{([^}]*)\}", HTML)
        self.assertIsNotNone(actions_css)
        self.assertNotIn("box-shadow", actions_css.group(1))

        keydown_start = HTML.index("document.addEventListener('keydown', function(e)")
        keydown_end = HTML.index("/* ═══ Helpers ═══ */", keydown_start)
        keydown_source = HTML[keydown_start:keydown_end]
        self.assertIn("isSearchShortcutInteractiveTarget(e.target)", keydown_source)
        self.assertIn("e.target.id !== 'query'", keydown_source)
        self.assertIn("'button, input, textarea, select, summary, a[href]", HTML)

    def test_library_filters_by_language_alongside_file_type(self) -> None:
        self.assertIn('id="lib-lang-control"', HTML)
        self.assertIn('data-lang="chinese"', HTML)
        self.assertIn('data-lang="foreign"', HTML)
        self.assertIn("function setLibLangFilter(btn)", HTML)
        self.assertIn("(s.language || 'chinese') === libLangFilter", HTML)

    def test_library_filters_by_document_type(self) -> None:
        self.assertIn('id="lib-doctype-control"', HTML)
        self.assertIn('data-doctype="book"', HTML)
        self.assertIn('data-doctype="journal_article"', HTML)
        self.assertIn("function setLibDocTypeFilter(btn)", HTML)
        self.assertIn("function libraryDocType(source)", HTML)
        self.assertIn("libraryDocType(s) === libDocTypeFilter", HTML)

    def test_library_filter_groups_stay_together_before_toolbar(self) -> None:
        filters_start = HTML.index('class="library-filter-controls"')
        toolbar_start = HTML.index('class="view-switch"', filters_start)
        filters = HTML[filters_start:toolbar_start]
        self.assertLess(filters.index('id="lib-type-control"'), filters.index('id="lib-lang-control"'))
        self.assertLess(filters.index('id="lib-lang-control"'), filters.index('id="lib-doctype-control"'))

    def test_registered_pdf_can_be_resubmitted_to_mineru_from_the_drawer(self) -> None:
        self.assertIn('"/api/mineru-reparse"', WEB_SOURCE)
        self.assertNotIn("原生文本，本地解析即可，无需 MinerU OCR", WEB_SOURCE)
        self.assertIn("force_mineru=True,", WEB_SOURCE)
        self.assertIn(
            'display_file_name=str(record.get("file_name") or "")',
            WEB_SOURCE,
        )
        self.assertIn("job.get(\"source_file_id\") == sid and job.get(\"status\") == \"processing\"", WEB_SOURCE)
        self.assertIn("function submitMineruReparse(sourceId)", HTML)
        self.assertIn("function pollMineruReparse(sourceId, jobId)", HTML)
        self.assertIn("MinerU 在线解析", HTML)
        self.assertIn("重新 OCR", HTML)
        self.assertNotIn("src.pdf_profile.detected_pdf_type !== 'native_text'", HTML)
        self.assertIn("将把这份 PDF 上传到 MinerU 在线服务重新解析", HTML)

    def test_import_page_can_force_native_pdf_through_mineru(self) -> None:
        self.assertIn('name="pdf-parse-mode" value="auto" checked', HTML)
        self.assertIn('name="pdf-parse-mode" value="mineru"', HTML)
        self.assertIn("function selectedPdfParseMode()", HTML)
        self.assertIn("'X-PDF-Parse-Mode': q.parseMode || 'auto'", HTML)
        self.assertIn("pdf_parse_mode: selectedPdfParseMode()", HTML)
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
        self.assertIn("className = 'scan-selection-marquee'", HTML)
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

    def test_optional_vision_api_and_mineru_fallback_are_wired(self) -> None:
        mineru_section = HTML.index('<span class="settings-section-title">MinerU API</span>')
        vision_section = HTML.index('<span class="settings-section-title">其他解析 API</span>')
        self.assertLess(mineru_section, vision_section)
        self.assertIn('name="pdf-parse-mode" value="vision"', HTML)
        self.assertIn('id="import-vision-provider"', HTML)
        self.assertIn('id="vision-auto-fallback"', HTML)
        self.assertIn("function loadVisionProviders()", HTML)
        self.assertIn("function retryImportWithVision(id)", HTML)
        self.assertIn("function visionRetryProviderFor(q)", HTML)
        self.assertIn("var provider = visionRetryProviderFor(q);", HTML)
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

    def test_mineru_settings_require_api_token_instead_of_legacy_access_keys(self) -> None:
        self.assertIn('id="mineru-token"', HTML)
        self.assertNotIn('id="mineru-access-key-id"', HTML)
        self.assertNotIn('id="mineru-secret-access-key"', HTML)
        self.assertIn("data.has_legacy_access_keys", HTML)
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
            "vision-api-settings",
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
        for label in ("图书", "译著", "期刊论文"):
            self.assertIn(label, HTML)
        self.assertIn("function setBibliographicType(sourceId, docType)", HTML)
        self.assertIn("'出版刊物'", HTML)
        self.assertIn("'卷次'", HTML)
        self.assertIn("'期号'", HTML)
        self.assertIn("'页码（起止页）'", HTML)
        self.assertIn("document_type: typeButton ? typeButton.dataset.doctype : 'book'", HTML)
        self.assertIn("['author','title','journal_name','publish_year','issue']", HTML)

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
        self.assertIn('payload.get("source_file_id")', WEB_SOURCE)

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

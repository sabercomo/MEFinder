from __future__ import annotations

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

    def test_all_dropdowns_use_application_menus(self) -> None:
        self.assertNotIn("<select", HTML.lower())
        self.assertIn('id="library-sort-field-select"', HTML)
        self.assertIn('id="library-sort-direction-select"', HTML)
        self.assertIn("setLibrarySortOption(event,'field','title')", HTML)
        self.assertIn("setLibrarySortOption(event,'field','source_type')", HTML)
        self.assertIn("setLibrarySortOption(event,'field','status')", HTML)
        self.assertIn("function librarySortProjection(source)", HTML)
        self.assertIn("localStorage.setItem('meFinderLibrarySortField'", HTML)
        self.assertIn('id="citation-style-control"', HTML)
        self.assertIn('segment-style-select', HTML)

    def test_library_filters_by_language_alongside_file_type(self) -> None:
        self.assertIn('id="lib-lang-control"', HTML)
        self.assertIn('data-lang="chinese"', HTML)
        self.assertIn('data-lang="foreign"', HTML)
        self.assertIn("function setLibLangFilter(btn)", HTML)
        self.assertIn("(s.language || 'chinese') === libLangFilter", HTML)

    def test_registered_pdf_can_be_resubmitted_to_mineru_from_the_drawer(self) -> None:
        self.assertIn('"/api/mineru-reparse"', WEB_SOURCE)
        self.assertIn("原生文本，本地解析即可，无需 MinerU OCR", WEB_SOURCE)
        self.assertIn("job.get(\"source_file_id\") == sid and job.get(\"status\") == \"processing\"", WEB_SOURCE)
        self.assertIn("function submitMineruReparse(sourceId)", HTML)
        self.assertIn("function pollMineruReparse(sourceId, jobId)", HTML)
        self.assertIn("提交 MinerU 解析", HTML)
        self.assertIn("重新 OCR", HTML)
        self.assertIn("src.pdf_profile.detected_pdf_type !== 'native_text'", HTML)

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
        self.assertIn("function importSelectedScanned()", HTML)
        self.assertIn("fetch('/api/scan-directories')", HTML)
        self.assertIn("fetch('/api/import-local'", HTML)
        self.assertIn("原始文件永远不会被移动或删除", HTML)
        self.assertIn("消耗 MinerU 配额", HTML)
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
        self.assertIn("var response = await fetch('/api/library')", HTML)
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

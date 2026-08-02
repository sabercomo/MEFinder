from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder import document_deletion as document_deletion_module
from src.me_finder.calibration_library import build_calibration_library, build_library
from src.me_finder.database import build_database
from src.me_finder.document_deletion import DocumentDeletionService
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.pdf_extractors import load_pdf_import_config
from src.me_finder.search import SearchEngine
from src.me_finder.web import HTML


class CalibrationLibraryProjectionTests(unittest.TestCase):
    def test_real_mapping_states_are_grouped_without_guessing_from_row_presence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = root / "corpus" / "raw_pdf"
            raw.mkdir(parents=True)
            statuses = {
                "manual": "manual_mapped",
                "auto": "auto_mapped_high",
                "review": "auto_mapped_medium",
                "failed": "auto_mapping_failed",
                "pending": None,
                "active": None,
            }
            sources = []
            documents = []
            for source_id, status in statuses.items():
                path = raw / f"{source_id}.pdf"
                path.write_bytes(b"pdf")
                profile = {"pdf_page_count": 10, "mapping_status": status}
                sources.append(
                    {
                        "source_file_id": source_id,
                        "source_type": "pdf",
                        "file_name": path.name,
                        "relative_path": f"corpus/raw_pdf/{path.name}",
                        "display_title": "??????" if source_id == "pending" else source_id,
                        "pdf_profile": profile,
                    }
                )
                mapping = {"segments": []}
                if source_id == "manual":
                    mapping = {
                        "validated_by": "manual_ui",
                        "segments": [
                            {"pdf_page_start": 4, "pdf_page_end": 9, "citation_page_start": "1"}
                        ],
                    }
                documents.append(
                    {
                        "source_file_id": source_id,
                        "title": "批判理论" if source_id == "manual" else None,
                        "page_mapping": mapping,
                    }
                )
            result = build_calibration_library(
                root,
                sources,
                [],
                documents,
                active_source_ids={"active"},
            )
        by_id = {item["source_file_id"]: item for item in result["items"]}
        self.assertEqual(by_id["manual"]["status"], "manual_mapped")
        self.assertEqual(by_id["manual"]["mapping_summary"], "PDF 第 5 页 → 引用第 1 页")
        self.assertEqual(by_id["manual"]["mapping_segment_count"], 1)
        self.assertEqual(by_id["auto"]["status"], "auto_mapped_high")
        self.assertEqual(by_id["review"]["status"], "needs_review")
        self.assertEqual(by_id["failed"]["status"], "auto_mapping_failed")
        self.assertEqual(by_id["pending"]["status"], "unmapped")
        self.assertEqual(by_id["pending"]["title"], "pending")
        self.assertEqual(by_id["active"]["status"], "mapping")
        self.assertEqual(result["stats"], {"total": 6, "calibrated": 2, "pending": 1, "review": 1, "failed": 1, "mapping": 1})

    def test_build_library_includes_word_sources_and_keeps_pdf_only_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_pdf = root / "corpus" / "raw_pdf"
            raw_pdf.mkdir(parents=True)
            raw_docx = root / "corpus" / "raw_docx"
            raw_docx.mkdir(parents=True)
            (raw_pdf / "critique.pdf").write_bytes(b"pdf")
            (raw_docx / "volume1.docx").write_bytes(b"docx")
            sources = [
                {
                    "source_file_id": "pdf-critique",
                    "source_type": "pdf",
                    "file_name": "critique.pdf",
                    "relative_path": "corpus/raw_pdf/critique.pdf",
                    "size_bytes": 3,
                    "pdf_profile": {"pdf_page_count": 400, "mapping_status": "manual_mapped"},
                },
                {
                    "source_file_id": "word-vol1",
                    "source_type": "word",
                    "file_name": "volume1.docx",
                    "relative_path": "corpus/raw_docx/volume1.docx",
                    "size_bytes": 4,
                    "last_modified": "2026-07-01T08:00:00",
                },
            ]
            volumes = [
                {
                    "source_file_id": "word-vol1",
                    "volume_id": "vol-1",
                    "display_title": "马克思恩格斯文集 第1卷",
                    "corpus_title": "马克思恩格斯文集",
                    "primary_structure": "docx_sections",
                }
            ]
            works = [
                {"volume_id": "vol-1", "title": "导言", "author_label": "马克思"},
                {"volume_id": "vol-1", "title": "黑格尔法哲学批判", "author_label": "马克思"},
            ]
            documents = [
                {
                    "source_file_id": "pdf-critique",
                    "title": "Critique of Forms of Life",
                    "page_mapping": {
                        "validated_by": "manual_ui",
                        "segments": [
                            {"pdf_page_start": 21, "pdf_page_end": 405, "citation_page_start": "1"}
                        ],
                    },
                }
            ]
            result = build_library(root, sources, volumes, works, documents)
        by_id = {item["source_file_id"]: item for item in result["items"]}
        self.assertEqual(len(result["items"]), 2)

        word = by_id["word-vol1"]
        self.assertEqual(word["source_type"], "word")
        self.assertEqual(word["title"], "马克思恩格斯文集 第1卷")
        self.assertEqual(word["author"], "马克思")
        self.assertEqual(word["works_count"], 2)
        self.assertTrue(word["source_exists"])
        self.assertEqual(word["modified_at"], "2026-07-01T08:00:00")
        self.assertIsNone(word.get("status"))
        self.assertEqual(word["language"], "chinese")

        pdf = by_id["pdf-critique"]
        self.assertEqual(pdf["source_type"], "pdf")
        self.assertEqual(pdf["language"], "foreign")
        self.assertEqual(pdf["document_type"], "book")
        self.assertIsNone(word.get("document_type"))
        self.assertEqual(pdf["status"], "manual_mapped")
        self.assertEqual(pdf["status_group"], "calibrated")
        self.assertEqual(pdf["title"], "Critique of Forms of Life")
        self.assertEqual(pdf["mapping_summary"], "PDF 第 22 页 → 引用第 1 页")
        self.assertEqual(pdf["pdf_profile"]["pdf_page_count"], 400)

        self.assertEqual(
            result["stats"],
            {"total": 1, "calibrated": 1, "pending": 0, "review": 0, "failed": 0, "mapping": 0},
        )
        self.assertEqual(result["volumes"][0]["volume_id"], "vol-1")
        self.assertEqual(len(result["works"]), 2)

    def test_chinese_file_name_prevents_foreign_misclassification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = root / "corpus" / "raw_pdf"
            raw.mkdir(parents=True)
            path = raw / "马恩全集第50卷.pdf"
            path.write_bytes(b"pdf")
            result = build_library(
                root,
                [
                    {
                        "source_file_id": "marx-50",
                        "source_type": "pdf",
                        "file_name": path.name,
                        "relative_path": f"corpus/raw_pdf/{path.name}",
                        "pdf_profile": {"pdf_page_count": 10},
                    }
                ],
                [],
                [],
                [
                    {
                        "source_file_id": "marx-50",
                        "title": "K93.pdf",
                        "author": "kdc",
                        "page_mapping": {"segments": []},
                    }
                ],
            )

        self.assertEqual(result["items"][0]["language"], "chinese")

    def test_merged_library_keeps_pinyin_sort_and_safe_remove_copy(self) -> None:
        self.assertNotIn("cal-doc-select", HTML)
        self.assertNotIn("请选择 PDF 文献", HTML)
        self.assertNotIn('id="cal-card-grid"', HTML)
        self.assertNotIn('id="page-calibration"', HTML)
        self.assertIn("zh-CN-u-co-pinyin", HTML)
        self.assertIn("从文献库移除", HTML)
        self.assertIn("同时删除应用内保存的 PDF 副本", HTML)
        self.assertIn("重新导入相同文件时会复用", HTML)
        self.assertIn("/api/documents/remove", HTML)

    def test_library_stats_are_interactive_and_drive_status_filter(self) -> None:
        self.assertIn('id="library-stats"', HTML)
        self.assertIn("function renderLibraryStats()", HTML)
        self.assertIn("function applyLibStatusFilter(status)", HTML)
        self.assertIn("statusStatButton('pdf_all','PDF 总数'", HTML)
        self.assertIn("statusStatButton('failed','页码自动检测失败',current.failed,'danger','danger',libStatusFilter,'applyLibStatusFilter')", HTML)
        self.assertNotIn("statusStatButton('failed','检测失败'", HTML)
        self.assertIn("libStatusFilter = requested === libStatusFilter ? 'all' : requested", HTML)
        self.assertIn("if (libStatusFilter === 'pdf_all')", HTML)
        self.assertIn("sources = sources.filter(s => s.source_type === 'pdf')", HTML)
        self.assertIn("calibrationStatusGroup(s.status) === libStatusFilter", HTML)

    def test_semantic_status_stats_render_inline_icons_with_danger_tokens(self) -> None:
        self.assertIn('function statusStatButton(status, label, value, variant, icon, activeFilter, handlerName)', HTML)
        self.assertIn('class="status-stat status-stat--', HTML)
        self.assertIn('function statusStatIcon(icon)', HTML)
        self.assertIn('width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"', HTML)
        self.assertIn('.status-stat__icon { width: 16px; height: 16px; flex: 0 0 auto;', HTML)
        self.assertIn('.status-stat--danger .status-stat__icon { color: var(--danger-icon); opacity: 1; }', HTML)
        self.assertIn('overflow: visible;', HTML)
        self.assertIn('--danger: #D62C3A;', HTML)
        self.assertIn('--danger: #FF6673;', HTML)
        for token in ('--danger-soft:', '--danger-border:', '--danger-icon:', '--danger-contrast:'):
            self.assertEqual(HTML.count(token), 2)
        self.assertIn('.status-stat--success', HTML)
        self.assertIn('.status-stat--warning', HTML)
        self.assertIn('.status-stat--info', HTML)
        self.assertIn('.status-stat--neutral', HTML)
        self.assertIn('.status-chip--danger', HTML)
        danger_rule = HTML.split('.status-stat--danger .status-stat__icon', 1)[1].split('}', 1)[0]
        self.assertIn('var(--danger-icon)', danger_rule)
        self.assertNotIn('var(--accent)', danger_rule)
        self.assertIn('function statusChipIcon(group)', HTML)
        self.assertIn('class="status-chip__icon', HTML)

    def test_library_rows_and_cards_show_status_chip_for_pdf_only(self) -> None:
        self.assertIn("var statusChip = isPdf", HTML)
        self.assertIn("calTransientStatus[src.source_file_id] || src.status", HTML)
        self.assertIn("var wordStructure = !isPdf && vol && vol.primary_structure ? structureLabel(vol.primary_structure) : ''", HTML)
        self.assertIn("src.mapping_summary || '尚未建立引用页码映射'", HTML)
        self.assertIn("src.page_count ? src.page_count + ' 页' : '页数未知'", HTML)
        self.assertIn("(src.works_count || 1) + ' 篇'", HTML)

    def test_drawer_calibration_section_is_collapsible_and_pdf_scoped(self) -> None:
        self.assertIn('id="library-drawer-calibration"', HTML)
        self.assertIn('id="cal-collapse-toggle"', HTML)
        self.assertIn('id="cal-section-body"', HTML)
        self.assertIn('function toggleDrawerCalibration(forceOpen)', HTML)
        self.assertIn("host.style.display = isPdf ? 'block' : 'none'", HTML)
        self.assertIn('function renderDrawerCalibrationSummary(src)', HTML)
        drawer = HTML.split('id="library-drawer"', 1)[1].split('<!-- ── Import Page ── -->', 1)[0]
        for element_id in ('id="cal-editor"', 'id="cal-auto-preview"', 'id="cal-segments-body"', 'id="cal-preview-input"', 'id="cal-detail-actions"'):
            self.assertIn(element_id, drawer)

    def test_manual_calibration_supports_spread_layout_and_safe_single_default(self) -> None:
        self.assertIn("function segmentLayoutControl(layout, index)", HTML)
        self.assertIn("layout === 'spread' ? '双开页' : '单页'", HTML)
        self.assertIn("function setSegmentReadingDirection(index, value)", HTML)
        self.assertIn("setSegmentReadingDirection(' + index + ',\\'ltr\\')", HTML)
        self.assertIn("setSegmentReadingDirection(' + index + ',\\'rtl\\')", HTML)
        self.assertIn('aria-label="双开页阅读方向"', HTML)
        self.assertIn('>左→右</button>', HTML)
        self.assertIn('>右→左</button>', HTML)
        self.assertIn("function updateSegmentGutter(index, value)", HTML)
        self.assertIn("seg.layout_mode === 'spread' ? 2 : 1", HTML)
        self.assertIn("clean.layout_mode = seg.layout_mode === 'spread' ? 'spread' : 'single'", HTML)
        self.assertIn("clean.reading_direction = seg.reading_direction === 'rtl' ? 'rtl' : 'ltr'", HTML)
        self.assertIn("<th>页面布局</th>", HTML)
        self.assertIn("segment-col-layout", HTML)

    def test_auto_detection_reports_spread_layout_direction_and_evidence(self) -> None:
        self.assertIn("正在检测页码与页面布局", HTML)
        self.assertIn("页面布局：双开页", HTML)
        self.assertIn("layout.reading_direction === 'rtl' ? '右→左' : '左→右'", HTML)
        self.assertIn("layoutEvidence.paired_page_numbers", HTML)
        self.assertIn("layoutEvidence.stride_two_support", HTML)
        self.assertIn("spread_sequence_not_found:'识别到双开布局，但未找到可靠的双页页码序列'", HTML)

    def test_sidebar_has_no_calibration_entry_and_deep_links_stay_in_library(self) -> None:
        self.assertNotIn('data-page="calibration"', HTML)
        library = HTML.index('data-page="library"')
        importing = HTML.index('data-page="import"')
        self.assertLess(library, importing)
        self.assertNotIn("navigateTo('calibration')", HTML)
        self.assertIn("navigateTo('library');\n  if (!libLoaded) await loadLibrary();", HTML)

    def test_sidebar_can_collapse_to_an_icon_rail_and_persists(self) -> None:
        # Toggle control, handler, and persisted state.
        self.assertIn('class="sidebar-collapse-btn"', HTML)
        self.assertIn('onclick="toggleSidebar()"', HTML)
        self.assertIn('function toggleSidebar(force)', HTML)
        self.assertIn("localStorage.setItem('meFinderSidebarCollapsed'", HTML)
        # Early head script applies the class before paint to avoid a flash.
        self.assertIn(
            "localStorage.getItem('meFinderSidebarCollapsed') === '1'",
            HTML,
        )
        # Collapsed width is driven through the shared variable so the desktop
        # titlebar gradient (which reads --sidebar-width) stays in sync.
        self.assertIn('html.sidebar-collapsed { --sidebar-width: 64px; }', HTML)
        self.assertIn(
            '.sidebar-collapsed .sidebar-item > span:not(.sidebar-icon) { display: none; }',
            HTML,
        )

    def test_library_drawer_actions_wrap_without_compressing_labels(self) -> None:
        drawer_rule = HTML.split('.drawer-actions {', 1)[1].split('}', 1)[0]
        self.assertIn('flex-wrap: wrap;', drawer_rule)
        self.assertIn('.drawer-actions .action-btn { flex: 0 0 auto; white-space: nowrap; }', HTML)

    def test_library_header_and_controls_reflow_in_narrow_windows(self) -> None:
        controls_rule = HTML.split('.library-controls-row {', 1)[1].split('}', 1)[0]
        line_rule = HTML.split('.library-controls-line {', 1)[1].split('}', 1)[0]
        filters_rule = HTML.split('.library-filter-controls {', 1)[1].split('}', 1)[0]
        self.assertIn('flex-direction: column;', controls_rule)
        self.assertIn('align-items: stretch;', controls_rule)
        # Each line spreads its left/right groups to opposite edges and wraps.
        self.assertIn('justify-content: space-between;', line_rule)
        self.assertIn('flex-wrap: wrap;', line_rule)
        self.assertIn('width: 100%;', line_rule)
        # Filters grow and wrap internally so the view switch stays pinned right.
        self.assertIn('flex: 1 1 auto;', filters_rule)
        self.assertIn('@media (max-width: 1100px)', HTML)
        self.assertIn('#page-library .page-header-row { flex-wrap: wrap; }', HTML)
        self.assertIn('#page-library .calibration-header-stats { width: 100%; justify-content: flex-start; }', HTML)

    def test_library_supports_click_and_drag_multi_selection_for_pdf_and_word_removal(self) -> None:
        for element_id in (
            'id="library-selection-bar"',
            'id="library-selection-count"',
            'id="library-remove-selected-btn"',
            'id="library-select-visible-btn"',
        ):
            self.assertIn(element_id, HTML)
        # No persistent mode toggle: the action bar appears once items are picked.
        self.assertNotIn('id="library-delete-mode-btn"', HTML)
        self.assertIn('function clearLibrarySelection()', HTML)
        self.assertIn('function toggleLibraryDeleteSelection(sourceId, force)', HTML)
        self.assertIn('function toggleSelectVisibleLibraryDocuments()', HTML)
        self.assertIn('function setupLibraryDragSelection()', HTML)
        self.assertIn("state.marquee.className = 'library-selection-marquee'", HTML)
        self.assertIn(".library-entry[data-delete-selectable=\"1\"]", HTML)
        self.assertIn("setupLibraryDragSelection();", HTML)
        self.assertIn('function openRemoveSelectedDocumentsModal()', HTML)
        self.assertIn("fetch('/api/documents/remove-batch'", HTML)
        self.assertIn("source.source_type === 'pdf' || source.source_type === 'word'", HTML)
        self.assertIn('Word 文献的应用内语料副本会一并删除', HTML)
        self.assertNotIn('Word 文献由本地语料目录管理，目前只能在这里移除 PDF 文献', HTML)

    def test_drag_selection_reaches_documents_below_the_fold(self) -> None:
        """框选一次只能选一屏，是因为锚点和命中判定都用视口坐标、且不自动滚动。"""

        # 锚点与命中框都换算到滚动容器的内容坐标，滚出屏幕的行才留得住。
        self.assertIn("function dragSelectionAnchor(scroller, event)", HTML)
        self.assertIn("anchorX: event.clientX - viewport.left + scroller.scrollLeft,", HTML)
        self.assertIn("anchorY: event.clientY - viewport.top + scroller.scrollTop", HTML)
        self.assertIn("function dragSelectionHits(element, box, scroller)", HTML)
        self.assertIn("var top = rect.top - box.viewport.top + scroller.scrollTop;", HTML)
        # 指针压在上下边缘时持续滚动。
        self.assertIn("function runDragSelectionAutoScroll(state, apply)", HTML)
        self.assertIn("function stopDragSelectionAutoScroll(state)", HTML)
        self.assertIn("const DRAG_SELECT_EDGE_ZONE = 56;", HTML)
        self.assertIn("const DRAG_SELECT_MAX_SCROLL_SPEED = 26;", HTML)
        self.assertIn("state.pointerY < viewport.top + DRAG_SELECT_EDGE_ZONE", HTML)
        self.assertIn("state.pointerY > viewport.bottom - DRAG_SELECT_EDGE_ZONE", HTML)
        self.assertIn("if (state.autoScrollFrame) cancelAnimationFrame(state.autoScrollFrame);", HTML)
        # 文献库列表接到共用实现上。
        self.assertIn("function libraryScrollContainer()", HTML)
        self.assertIn("function updateLibraryDragSelection()", HTML)
        self.assertIn("runDragSelectionAutoScroll(state, updateLibraryDragSelection);", HTML)
        self.assertIn("stopDragSelectionAutoScroll(state);", HTML)
        # 旧的视口坐标命中判定不能留下。
        self.assertNotIn(
            "var hit = rect.right >= left && rect.left <= right "
            "&& rect.bottom >= top && rect.top <= bottom;",
            HTML,
        )


class DocumentDeletionServiceTests(unittest.TestCase):
    def _index(self, root: Path) -> tuple[Path, Path]:
        raw_dir = root / "corpus" / "raw_pdf"
        parsed_dir = root / "corpus" / "parsed" / "pdf"
        config_dir = root / "config"
        data_dir = root / "data"
        raw_dir.mkdir(parents=True)
        parsed_dir.mkdir(parents=True)
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        pdf_path = raw_dir / "消费社会.pdf"
        pdf_path.write_bytes(b"local pdf copy")
        (parsed_dir / "PDF_CONSUMER.json").write_text("{}", encoding="utf-8")
        config = {
            "documents": [
                {
                    "source_file_id": "pdf-consumer",
                    "document_id": "PDF_CONSUMER",
                    "file_name": pdf_path.name,
                    "title": "消费社会",
                    "page_mapping": {"segments": []},
                }
            ]
        }
        config_path = config_dir / "pdf_imports.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        pdf_text = "消费控制着整个生活。"
        word_text = "宗教是人民的鸦片。"
        index = {
            "metadata": {},
            "source_files": [
                {
                    "source_file_id": "pdf-consumer",
                    "source_type": "pdf",
                    "file_name": pdf_path.name,
                    "relative_path": "corpus/raw_pdf/消费社会.pdf",
                    "document_id": "PDF_CONSUMER",
                },
                {
                    "source_file_id": "word-one",
                    "source_type": "word",
                    "file_name": "word.docx",
                    "relative_path": "corpus/raw_docx/word.docx",
                },
            ],
            "volumes": [
                {"volume_id": "PDF_CONSUMER", "source_file_id": "pdf-consumer", "source_type": "pdf", "display_title": "消费社会"},
                {"volume_id": "WORD_1", "source_file_id": "word-one", "source_type": "word", "display_title": "马克思恩格斯文集第1卷"},
            ],
            "works": [
                {"work_id": "PDF-W1", "volume_id": "PDF_CONSUMER", "source_type": "pdf", "title": "消费社会"},
                {"work_id": "WORD-W1", "volume_id": "WORD_1", "source_type": "word", "title": "导言"},
            ],
            "paragraphs": [
                self._paragraph("PDF-P1", "pdf-consumer", "pdf", "PDF_CONSUMER", "PDF-W1", pdf_text),
                self._paragraph("WORD-P1", "word-one", "word", "WORD_1", "WORD-W1", word_text),
            ],
            "pdf_pages": [{"source_file_id": "pdf-consumer", "pdf_page_index": 0}],
            "pdf_page_mappings": [{"source_file_id": "pdf-consumer", "pdf_page_index": None}],
            "pdf_import_runs": [{"source_file_id": "pdf-consumer", "status": "success"}],
        }
        database_path = data_dir / "index.sqlite3"
        build_database(index, database_path)
        return database_path, pdf_path

    @staticmethod
    def _paragraph(paragraph_id: str, source_id: str, source_type: str, volume_id: str, work_id: str, text: str) -> dict[str, object]:
        return {
            "paragraph_id": paragraph_id,
            "volume_id": volume_id,
            "volume_number": 1 if source_type == "word" else None,
            "work_id": work_id,
            "source_file_id": source_id,
            "source_type": source_type,
            "paragraph_index": 1,
            "eligible_for_search": True,
            "text_raw": text,
            "normalized_text": normalize_text(text),
            "compact_text": compact_text(text),
            "plain_text": punctuationless_text(text),
            "document_title": "消费社会" if source_type == "pdf" else "马克思恩格斯文集",
            "work_title": "消费社会" if source_type == "pdf" else "导言",
            "volume_display": "消费社会" if source_type == "pdf" else "马克思恩格斯文集第1卷",
            "page_display": "1",
            "page_source_type": "manual_segment",
            "citation_page_start": "1",
            "original_file_name": "source.pdf" if source_type == "pdf" else "word.docx",
        }

    def test_default_removal_deletes_only_source_records_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path, pdf_path = self._index(root)
            result = DocumentDeletionService(root, database_path).remove("pdf-consumer")
            self.assertTrue(pdf_path.exists())
            self.assertTrue(result["original_pdf_preserved"])
            self.assertFalse((root / "corpus" / "parsed" / "pdf" / "PDF_CONSUMER.json").exists())
            config_path = root / "config" / "pdf_imports.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(len(config["documents"]), 1)
            retained = config["documents"][0]
            self.assertEqual(retained["source_file_id"], "pdf-consumer")
            self.assertEqual(retained["file_name"], pdf_path.name)
            self.assertFalse(retained["enabled"])
            self.assertTrue(retained["retained_after_removal"])
            self.assertNotIn("title", retained)
            self.assertEqual(load_pdf_import_config(config_path), [])
            connection = sqlite3.connect(str(database_path))
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_files WHERE source_file_id='pdf-consumer'").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM paragraphs WHERE source_file_id='pdf-consumer'").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_files WHERE source_file_id='word-one'").fetchone()[0], 1)
            finally:
                connection.close()
            engine = SearchEngine(database_path)
            try:
                self.assertEqual(engine.search("消费控制着整个生活", source_type="pdf")["total"], 0)
                self.assertEqual(engine.search("宗教是人民的鸦片。", source_type="word")["total"], 1)
            finally:
                engine.close()

    def test_internal_pdf_copy_requires_explicit_option(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path, pdf_path = self._index(root)
            result = DocumentDeletionService(root, database_path).remove(
                "pdf-consumer", delete_generated_artifacts=False, delete_internal_copy=True
            )
            self.assertFalse(pdf_path.exists())
            self.assertTrue(result["internal_copy_deleted"])
            config = json.loads(
                (root / "config" / "pdf_imports.json").read_text(encoding="utf-8")
            )
            self.assertEqual(config["documents"], [])

    def test_retained_pdf_keeps_parser_reference_when_artifacts_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path, pdf_path = self._index(root)
            config_path = root / "config" / "pdf_imports.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["documents"][0]["parser_results"] = {
                "manifest": "corpus/processed/mineru/manifests/kept.json"
            }
            config_path.write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )
            parsed_cache = (
                root / "corpus" / "parsed" / "pdf" / "PDF_CONSUMER.json"
            )

            DocumentDeletionService(root, database_path).remove(
                "pdf-consumer",
                delete_generated_artifacts=False,
            )

            self.assertTrue(pdf_path.exists())
            self.assertTrue(parsed_cache.exists())
            retained = json.loads(config_path.read_text(encoding="utf-8"))[
                "documents"
            ][0]
            self.assertFalse(retained["enabled"])
            self.assertEqual(
                retained["parser_results"]["manifest"],
                "corpus/processed/mineru/manifests/kept.json",
            )

    def test_removal_cleans_vision_result_and_work_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path, _pdf_path = self._index(root)
            result_dir = root / "corpus" / "processed" / "vision" / "results" / "task"
            work_manifest = (
                root
                / "corpus"
                / "processed"
                / "vision"
                / "manifests"
                / "work"
                / "vision-task.json"
            )
            final_manifest = work_manifest.parent.parent / "vision-pdf-consumer.json"
            result_dir.mkdir(parents=True)
            work_manifest.parent.mkdir(parents=True)
            (result_dir / "content_list.json").write_text("[]", encoding="utf-8")
            work_manifest.write_text('{"status":"completed"}', encoding="utf-8")
            final_manifest.write_text(
                json.dumps(
                    {
                        "work_manifest": str(work_manifest.relative_to(root)),
                        "segments": [
                            {
                                "result_dir": str(result_dir.relative_to(root)),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config" / "pdf_imports.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["documents"][0]["parser_results"] = {
                "manifest": str(final_manifest),
            }
            config_path.write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )

            DocumentDeletionService(root, database_path).remove("pdf-consumer")

            self.assertFalse(result_dir.exists())
            self.assertFalse(work_manifest.exists())
            self.assertFalse(final_manifest.exists())

    def test_removal_preserves_manifest_and_results_shared_by_another_document(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path, _pdf_path = self._index(root)
            processed = root / "corpus" / "processed" / "vision"
            shared_result = processed / "results" / "pdf-consumer" / "shared-task"
            shared_work = (
                processed
                / "manifests"
                / "work"
                / "vision-pdf-consumer-shared.json"
            )
            shared_manifest = processed / "manifests" / "vision-pdf-consumer.json"
            shared_result.mkdir(parents=True)
            shared_work.parent.mkdir(parents=True)
            (shared_result / "content_list.json").write_text("[]", encoding="utf-8")
            shared_work.write_text('{"status":"completed"}', encoding="utf-8")
            shared_manifest.write_text(
                json.dumps(
                    {
                        "work_manifest": str(shared_work.relative_to(root)),
                        "segments": [
                            {
                                "result_dir": str(shared_result.relative_to(root)),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config" / "pdf_imports.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["documents"][0]["parser_results"] = {
                "manifest": str(shared_manifest),
            }
            config["documents"].append(
                {
                    "source_file_id": "pdf-other",
                    "document_id": "PDF_OTHER",
                    "file_name": "other.pdf",
                    "parser_results": {"manifest": str(shared_manifest)},
                    "page_mapping": {"segments": []},
                }
            )
            config_path.write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )

            DocumentDeletionService(root, database_path).remove("pdf-consumer")

            self.assertTrue(shared_manifest.exists())
            self.assertTrue(shared_work.exists())
            self.assertTrue(shared_result.exists())
            remaining = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [
                    item["source_file_id"]
                    for item in remaining["documents"]
                    if item.get("enabled", True)
                ],
                ["pdf-other"],
            )
            retained = next(
                item
                for item in remaining["documents"]
                if item["source_file_id"] == "pdf-consumer"
            )
            self.assertFalse(retained["enabled"])
            self.assertNotIn("parser_results", retained)

    def test_removal_cleans_unattached_interrupted_parser_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path, _pdf_path = self._index(root)
            processed = root / "corpus" / "processed"
            mineru_manifest = (
                processed
                / "mineru"
                / "manifests"
                / "segments-pdf-consumer.json"
            )
            mineru_result = (
                processed
                / "mineru"
                / "results"
                / "pdf-consumer-p001-001"
            )
            mineru_state = processed / "mineru" / "tasks" / "batch-one.json"
            vision_work = (
                processed
                / "vision"
                / "manifests"
                / "work"
                / "vision-pdf-consumer-task.json"
            )
            vision_result = (
                processed / "vision" / "results" / "pdf-consumer" / "task-one"
            )
            import_job = processed / "import_jobs" / "import-one.json"
            for path in (
                mineru_manifest,
                mineru_state,
                vision_work,
                import_job,
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
            mineru_result.mkdir(parents=True)
            vision_result.mkdir(parents=True)
            mineru_manifest.write_text('{"segments":[]}', encoding="utf-8")
            mineru_state.write_text(
                '{"data_id":"pdf-consumer-p001-001"}',
                encoding="utf-8",
            )
            vision_work.write_text('{"status":"failed"}', encoding="utf-8")
            import_job.write_text(
                '{"source_file_id":"pdf-consumer","context":{}}',
                encoding="utf-8",
            )

            DocumentDeletionService(root, database_path).remove("pdf-consumer")

            for path in (
                mineru_manifest,
                mineru_result,
                mineru_state,
                vision_work,
                vision_result.parent,
                import_job,
            ):
                self.assertFalse(path.exists(), str(path))

    def test_failed_config_rollback_still_restores_staged_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path, _pdf_path = self._index(root)
            parsed_cache = root / "corpus" / "parsed" / "pdf" / "PDF_CONSUMER.json"
            real_save = document_deletion_module.save_import_config
            save_calls = 0

            def fail_only_rollback(path: Path, config: dict[str, object]) -> None:
                nonlocal save_calls
                save_calls += 1
                if save_calls == 2:
                    raise OSError("config restore blocked")
                real_save(path, config)

            with (
                patch.object(
                    document_deletion_module,
                    "save_import_config",
                    side_effect=fail_only_rollback,
                ),
                patch.object(
                    document_deletion_module,
                    "delete_sources_from_database",
                    side_effect=RuntimeError("database delete failed"),
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "恢复导入配置失败",
                ) as raised:
                    DocumentDeletionService(root, database_path).remove(
                        "pdf-consumer"
                    )

            self.assertEqual(save_calls, 2)
            self.assertTrue(parsed_cache.exists())
            self.assertIsInstance(raised.exception.__cause__, RuntimeError)
            self.assertIn("database delete failed", str(raised.exception.__cause__))

    def test_restore_staged_continues_after_one_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            staged: list[tuple[Path, Path]] = []
            DocumentDeletionService._stage(first, staged)
            DocumentDeletionService._stage(second, staged)
            failing_temporary = staged[1][1]
            real_replace = Path.replace

            def fail_one_replace(path: Path, target: Path) -> Path:
                if path == failing_temporary:
                    raise OSError("restore blocked")
                return real_replace(path, target)

            with patch.object(Path, "replace", new=fail_one_replace):
                errors = DocumentDeletionService._restore_staged(staged)

            self.assertTrue(first.exists())
            self.assertFalse(second.exists())
            self.assertTrue(failing_temporary.exists())
            self.assertEqual(len(errors), 1)
            self.assertIn("second.json", errors[0])
            self.assertIn("restore blocked", errors[0])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.calibration_library import build_calibration_library, build_library
from src.me_finder.database import build_database
from src.me_finder.document_deletion import DocumentDeletionService
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
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

        pdf = by_id["pdf-critique"]
        self.assertEqual(pdf["source_type"], "pdf")
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

    def test_calibration_html_has_card_library_pinyin_sort_and_safe_remove_copy(self) -> None:
        self.assertNotIn("cal-doc-select", HTML)
        self.assertNotIn("请选择 PDF 文献", HTML)
        self.assertIn("cal-card-grid", HTML)
        self.assertIn("zh-CN-u-co-pinyin", HTML)
        self.assertIn("全部</button>", HTML)
        self.assertIn('class="cal-status-tab__label">待确认</span>', HTML)
        self.assertIn('class="cal-status-tab__label">检测失败</span>', HTML)
        self.assertIn("从文献库移除", HTML)
        self.assertIn("同时删除应用内保存的 PDF 副本", HTML)
        self.assertIn("/api/documents/remove", HTML)

    def test_visual_polish_uses_complete_svg_refresh_and_interactive_stats(self) -> None:
        self.assertIn('id="cal-refresh-btn"', HTML)
        self.assertIn('title="刷新文献列表"', HTML)
        self.assertIn('aria-label="刷新文献列表"', HTML)
        self.assertIn('viewBox="0 0 24 24"', HTML)
        self.assertIn('M21 12a9 9 0 0 0-15.5-6.2L3 8', HTML)
        self.assertIn('M3 12a9 9 0 0 0 15.5 6.2L21 16', HTML)
        self.assertIn('.cal-refresh-btn.refreshing svg', HTML)
        self.assertIn('button.classList.add(\'refreshing\')', HTML)
        self.assertIn('button.disabled = true', HTML)
        self.assertIn('var scrollTop = pane ? pane.scrollTop : 0', HTML)
        self.assertIn('loadCalPdfs({showSkeleton:false})', HTML)
        self.assertIn("onclick=\"applyCalStatusFilter", HTML)

    def test_semantic_status_stats_render_inline_icons_with_danger_tokens(self) -> None:
        self.assertIn('class="status-stat status-stat--danger"', HTML)
        self.assertIn('class="status-stat__icon"', HTML)
        self.assertIn('class="status-stat__label">检测失败</span>', HTML)
        self.assertIn('class="status-stat__count">—</span>', HTML)
        self.assertIn('calibrationStatButton(\'failed\',\'检测失败\',current.failed,\'danger\',\'danger\')', HTML)
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

    def test_filter_tabs_keep_icon_and_label_separate(self) -> None:
        self.assertIn('class="cal-status-tab__label">全部</span>', HTML)
        self.assertIn('class="cal-status-tab__label">检测失败</span>', HTML)
        tab_rule = HTML.split('.cal-status-tab {', 1)[1].split('}', 1)[0]
        self.assertIn('display: inline-flex;', tab_rule)
        self.assertIn('align-items: center;', tab_rule)
        self.assertIn('gap: 7px;', tab_rule)
        self.assertIn('white-space: nowrap;', tab_rule)
        self.assertNotIn('.cal-status-badge.mapping::before', HTML)
        self.assertIn('background: var(--danger-soft); border: 1px solid var(--danger-border);', HTML)

    def test_card_hierarchy_and_more_menu_match_polished_interaction(self) -> None:
        self.assertIn("作者信息待完善", HTML)
        self.assertIn("mapping_segment_count", HTML)
        self.assertIn("自动映射 · 高可信", HTML)
        self.assertIn("人工映射", HTML)
        self.assertIn("未找到可靠页码序列", HTML)
        for label in ("打开原文", "查看映射", "自动检测页码", "编辑书目信息", "从文献库移除"):
            self.assertIn(label, HTML)
        self.assertIn("calibrationPrimaryAction", HTML)
        self.assertIn("updateCalibrationCard(sourceId)", HTML)

    def test_sidebar_keeps_library_and_calibration_adjacent(self) -> None:
        library = HTML.index('data-page="library"')
        calibration = HTML.index('data-page="calibration"')
        importing = HTML.index('data-page="import"')
        self.assertLess(library, calibration)
        self.assertLess(calibration, importing)

    def test_library_drawer_actions_wrap_without_compressing_labels(self) -> None:
        drawer_rule = HTML.split('.drawer-actions {', 1)[1].split('}', 1)[0]
        self.assertIn('flex-wrap: wrap;', drawer_rule)
        self.assertIn('.drawer-actions .action-btn { flex: 0 0 auto; white-space: nowrap; }', HTML)


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
            config = json.loads((root / "config" / "pdf_imports.json").read_text(encoding="utf-8"))
            self.assertEqual(config["documents"], [])
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


if __name__ == "__main__":
    unittest.main()

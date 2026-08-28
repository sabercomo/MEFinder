"""Guard the first-screen cost of opening the app on a large local corpus.

The library payload grew with the corpus until the first screen unconditionally
downloaded every document's PDF profile, mapping segments and recognition
evidence — around 1.3 MB of data no list row ever renders. These tests pin the
lazy-loading contract instead of the wall-clock timing, which varies by machine.
"""

from __future__ import annotations

import unittest

from src.me_finder.calibration_library import (
    SUMMARY_DROPPED_ITEM_FIELDS,
    build_library_detail,
    summarize_library,
)
from src.me_finder.web import HTML


def _payload() -> dict:
    return {
        "items": [
            {
                "source_file_id": "pdf-1",
                "source_type": "pdf",
                "title": "马克思恩格斯全集 第一卷",
                "author": "马克思、恩格斯",
                "status": "manual_mapped",
                "status_group": "calibrated",
                "language": "chinese",
                "document_type": "book",
                "mapping_summary": "PDF 第 3 页 → 引用第 1 页",
                "metadata_missing_fields": [],
                "page_count": 800,
                "pdf_profile": {"pdf_page_count": 800, "auto_page_mapping": {"candidates": list(range(500))}},
                "segments": [{"pdf_page_start": 2, "citation_page_start": 1}],
                "mapping_evidence": [{"evidence_text": "证据"}],
                "metadata_evidence": {"title": {"evidence_text": "版权页"}},
                "metadata_conflicts": [{"field": "author"}],
                "exception_pages": [1, 2, 3],
                "failure_reasons": ["reason"],
                "bibliographic_metadata": {
                    "title": "马克思恩格斯全集 第一卷",
                    "author": "马克思、恩格斯",
                    "publisher": "人民出版社",
                    "metadata_evidence": {"title": {"evidence_text": "版权页"}},
                    "metadata_conflicts": [{"field": "author"}],
                },
            },
            {
                "source_file_id": "word-1",
                "source_type": "word",
                "title": "手稿选编",
                "author": "马克思",
                "language": "chinese",
                "works_count": 3,
            },
        ],
        "stats": {"total": 1, "calibrated": 1},
        "volumes": [
            {"volume_id": "vol-1", "source_file_id": "pdf-1", "display_title": "第一卷"},
            {"volume_id": "vol-2", "source_file_id": "word-1", "display_title": "手稿选编"},
        ],
        "works": [
            {"volume_id": "vol-1", "title": "论犹太人问题"},
            {"volume_id": "vol-2", "title": "1844 年经济学哲学手稿"},
        ],
    }


class LibrarySummaryProjectionTests(unittest.TestCase):
    def test_summary_drops_only_detail_only_payload(self) -> None:
        summary = summarize_library(_payload())
        item = summary["items"][0]
        for field in SUMMARY_DROPPED_ITEM_FIELDS:
            self.assertNotIn(field, item)
        for field in (
            "source_file_id",
            "source_type",
            "title",
            "author",
            "status",
            "status_group",
            "language",
            "document_type",
            "mapping_summary",
            "metadata_missing_fields",
            "page_count",
        ):
            self.assertIn(field, item)

    def test_summary_strips_evidence_nested_in_bibliographic_metadata(self) -> None:
        metadata = summarize_library(_payload())["items"][0]["bibliographic_metadata"]
        self.assertNotIn("metadata_evidence", metadata)
        self.assertNotIn("metadata_conflicts", metadata)
        self.assertEqual(metadata["publisher"], "人民出版社")

    def test_summary_keeps_volumes_for_the_list_but_omits_works(self) -> None:
        summary = summarize_library(_payload())
        self.assertEqual(summary["view"], "summary")
        self.assertEqual(len(summary["volumes"]), 2)
        self.assertNotIn("works", summary)
        self.assertEqual(summary["stats"], {"total": 1, "calibrated": 1})

    def test_summary_does_not_mutate_the_full_payload(self) -> None:
        payload = _payload()
        summarize_library(payload)
        self.assertIn("pdf_profile", payload["items"][0])
        self.assertIn("metadata_evidence", payload["items"][0]["bibliographic_metadata"])


class LibraryDetailProjectionTests(unittest.TestCase):
    def test_detail_returns_full_record_with_its_volume_and_works(self) -> None:
        detail = build_library_detail(_payload(), "pdf-1")
        self.assertIsNotNone(detail)
        self.assertIn("pdf_profile", detail["item"])
        self.assertIn("mapping_evidence", detail["item"])
        self.assertEqual(detail["volume"]["volume_id"], "vol-1")
        self.assertEqual([work["title"] for work in detail["works"]], ["论犹太人问题"])

    def test_detail_is_none_for_unknown_or_empty_source(self) -> None:
        self.assertIsNone(build_library_detail(_payload(), "missing"))
        self.assertIsNone(build_library_detail(_payload(), ""))


class LibraryFirstScreenLoadingTests(unittest.TestCase):
    def test_startup_does_not_prefetch_the_whole_library(self) -> None:
        self.assertNotIn("ensureSearchDocuments().then(", HTML)
        # 文献下拉仍然在展开时懒加载。
        self.assertIn("if (shouldOpen && selectId === 'document-select')", HTML)
        self.assertIn("await ensureSearchDocuments();", HTML)

    def test_library_requests_are_shared_between_dropdown_and_library_page(self) -> None:
        self.assertIn("function fetchLibraryCatalog(force)", HTML)
        self.assertIn(
            "if (searchStore.libraryCatalogPromise) return searchStore.libraryCatalogPromise;",
            HTML,
        )
        self.assertIn(
            "if (searchStore.libraryCatalog) return Promise.resolve(searchStore.libraryCatalog);",
            HTML,
        )
        self.assertIn("function invalidateLibraryCatalog()", HTML)
        self.assertIn("searchStore.sourceFiles = libraryStore.sources;", HTML)

    def test_library_list_uses_the_summary_view_and_lazy_detail(self) -> None:
        self.assertIn("fetch('/api/library?view=summary')", HTML)
        self.assertIn("function ensureLibraryDetail(sourceId)", HTML)
        self.assertIn("'/api/library/document?source_id='", HTML)
        self.assertIn("await ensureLibraryDetail(sourceId);", HTML)

    def test_library_list_renders_in_batches_and_debounces_filtering(self) -> None:
        self.assertIn("const LIBRARY_RENDER_BATCH = 50;", HTML)
        self.assertIn("function appendLibraryEntries(sources, start, token)", HTML)
        self.assertIn("function libraryEntryHTML(src)", HTML)
        self.assertIn("if (token !== libraryStore.renderToken) return;", HTML)
        self.assertIn(
            "if (libraryStore.filterTimer) clearTimeout(libraryStore.filterTimer);",
            HTML,
        )

    def test_hidden_window_still_finishes_appending_the_list(self) -> None:
        # 隐藏文档不触发 requestAnimationFrame；没有定时器兜底就会只剩首批。
        self.assertIn("function scheduleLibraryChunk(callback)", HTML)
        self.assertIn("if (document.hidden || typeof requestAnimationFrame !== 'function') setTimeout(callback, 0);", HTML)
        self.assertIn("scheduleLibraryChunk(function()", HTML)

    def test_volume_lookup_uses_a_map_instead_of_scanning_per_row(self) -> None:
        self.assertIn("function buildVolumeIndex(volumes)", HTML)
        self.assertIn("function volumeForSource(sourceId)", HTML)
        self.assertNotIn("libraryStore.volumes.find(function(v)", HTML)
        self.assertNotIn("searchVolumes.find(function(item)", HTML)


class LibraryEndpointWiringTests(unittest.TestCase):
    def test_web_routes_expose_summary_and_detail(self) -> None:
        from pathlib import Path

        web_source = "\n".join(
            Path(f"src/me_finder/{name}").read_text(encoding="utf-8")
            for name in ("web.py", "web_runtime.py")
        )
        controller_source = Path(
            "src/me_finder/library_query_controller.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"/api/library": (', web_source)
        self.assertIn('"/api/library/document": (', web_source)
        self.assertIn('if requested_view == "summary":', controller_source)
        self.assertIn(
            "self._document_queries.library_summary(", controller_source
        )
        self.assertIn(
            "self._document_queries.library_detail(", controller_source
        )


if __name__ == "__main__":
    unittest.main()

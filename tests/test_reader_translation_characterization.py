"""Characterization snapshot for the reader and translation-work read paths.

Pins the full public output of ``structured_reader`` (window, citation,
outline, page-mapping overview) and ``translation_works`` (overview, link
window, review candidates, reading position, dismissals) before their SQL is
moved into ``persistence/``.  The refactor must keep every page anchor,
character range and citation string byte-identical; the golden file is
regenerated only when the product contract changes intentionally:

    .venv-macos312-arm64/bin/python -m tests.test_reader_translation_characterization --regen
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Dict

from src.me_finder import translation_works
from src.me_finder.database import build_database
from src.me_finder.document_outline import get_document_outline
from src.me_finder.page_mapping_overview import describe_page_mapping
from src.me_finder.structured_reader import (
    get_document_citation,
    get_document_window,
)
from src.me_finder.text_alignment import generate_alignment
from tests.test_translation_works import _ThreeVersionWork

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "reader_translation_characterization.json"

BOOK_METADATA = {
    "document_type": "book",
    "title": "快照样书",
    "author": "测试作者",
    "publish_place": "北京",
    "publisher": "测试出版社",
    "publish_year": "2026",
}


def _paragraph(
    source_id: str,
    index: int,
    text: str,
    *,
    page_source_type: str,
    original_page_start: str | None = None,
    page_display: str | None = None,
    work_id: str | None = None,
) -> dict[str, object]:
    return {
        "paragraph_id": f"{source_id}-P{index:06d}",
        "source_file_id": source_id,
        "source_type": "word",
        "paragraph_index": index,
        "eligible_for_search": bool(text),
        "text_raw": text,
        "page_source_type": page_source_type,
        "page_display": page_display or original_page_start,
        "original_page_start": original_page_start,
        "original_page_end": original_page_start,
        "work_id": work_id,
    }


def _reader_index() -> dict[str, object]:
    pdf_pages = [
        {
            "pdf_page_id": f"pdf-snap-PAGE-{index:06d}",
            "source_file_id": "pdf-snap",
            "pdf_page_index": index,
            "pdf_page_number_1based": index + 1,
            "text_raw": text,
            **fields,
        }
        for index, (text, fields) in enumerate(
            [
                ("第一页正文，跨页引文从这里开始。",
                 {"citation_page": "38", "page_mapping_method": "manual_segment",
                  "segment_id": "MAPSEG-000000-000001"}),
                ("第二页正文，跨页引文在这里结束。",
                 {"citation_page": "39", "page_mapping_method": "manual_segment",
                  "segment_id": "MAPSEG-000000-000001"}),
                ("标签页正文", {"pdf_page_label": "vii", "page_mapping_method": "uncalibrated"}),
                (" \n", {"page_mapping_method": "uncalibrated"}),
            ]
        )
    ]
    me_paragraphs = [
        _paragraph("word-me", 0, "第一篇首段。", page_source_type="section_break_verified",
                   original_page_start="3", work_id="MEWJ-01-W0001"),
        _paragraph("word-me", 1, "第一篇次段。", page_source_type="section_break_verified",
                   original_page_start="3", work_id="MEWJ-01-W0001"),
        _paragraph("word-me", 2, "第二篇首段。", page_source_type="section_break_verified",
                   original_page_start="4", work_id="MEWJ-01-W0002"),
    ]
    state_paragraphs = [
        _paragraph("word-states", 0, "分节推断正文", page_source_type="section_break_inferred",
                   original_page_start="39"),
        _paragraph("word-states", 1, "分节推断同页", page_source_type="section_break_inferred",
                   original_page_start="39"),
        _paragraph("word-states", 2, "目录范围正文", page_source_type="toc_range_bound",
                   page_display="40-45"),
        _paragraph("word-states", 3, "未知正文", page_source_type="unknown"),
    ]
    epub_list = [
        _paragraph("epub-list", index, text, page_source_type="epub_page_list",
                   original_page_start=page, work_id="EPUB-LIST-W0001")
        for index, (text, page) in enumerate(
            [("页码表第27页首段。", "27"), ("页码表第27页次段。", "27"), ("页码表第28页。", "28")]
        )
    ]
    epub_break = [
        _paragraph("epub-break", index, text, page_source_type="epub_pagebreak",
                   original_page_start=page)
        for index, (text, page) in enumerate([("分页标记第1页。", "1"), ("分页标记第2页。", "2")])
    ]
    epub_none = [
        _paragraph("epub-none", index, text, page_source_type="unknown")
        for index, text in enumerate(["没有出版方页码的正文。", "第二段。"])
    ]
    return {
        "metadata": {},
        "source_files": [
            {"source_file_id": "pdf-snap", "source_type": "pdf", "file_name": "snap.pdf",
             "file_format": "pdf", "display_title": "PDF 快照",
             "bibliographic_metadata": BOOK_METADATA},
            {"source_file_id": "word-me", "source_type": "word",
             "file_name": "马克思恩格斯文集第1卷.docx", "file_format": "docx",
             "volume_number": 1},
            {"source_file_id": "word-states", "source_type": "word", "file_name": "states.docx",
             "file_format": "docx", "bibliographic_metadata": BOOK_METADATA},
            {"source_file_id": "epub-list", "source_type": "word", "file_name": "list.epub",
             "file_format": "epub", "source_format": "epub",
             "bibliographic_metadata": BOOK_METADATA},
            {"source_file_id": "epub-break", "source_type": "word", "file_name": "break.epub",
             "file_format": "epub", "source_format": "epub",
             "bibliographic_metadata": BOOK_METADATA},
            {"source_file_id": "epub-none", "source_type": "word", "file_name": "none.epub",
             "file_format": "epub", "source_format": "epub",
             "bibliographic_metadata": BOOK_METADATA},
        ],
        "volumes": [
            {"volume_id": "MEWJ-01", "source_file_id": "word-me", "source_type": "word",
             "volume_number": 1, "display_title": "《马克思恩格斯文集》第1卷"},
            {"volume_id": "EPUB-LIST", "source_file_id": "epub-list", "source_type": "word",
             "display_title": "页码表样书"},
        ],
        "works": [
            {"work_id": "MEWJ-01-W0001", "volume_id": "MEWJ-01", "source_file_id": "word-me",
             "source_type": "word", "work_order": 1, "title": "第一篇"},
            {"work_id": "MEWJ-01-W0002", "volume_id": "MEWJ-01", "source_file_id": "word-me",
             "source_type": "word", "work_order": 2, "title": "第二篇"},
            {"work_id": "EPUB-LIST-W0001", "volume_id": "EPUB-LIST", "source_file_id": "epub-list",
             "source_type": "word", "work_order": 1, "title": "页码表正文"},
        ],
        "paragraphs": me_paragraphs + state_paragraphs + epub_list + epub_break + epub_none,
        "pdf_pages": pdf_pages,
    }


def _outcome(call: Callable[[], object]) -> object:
    """Return the payload, or the error type and message the caller would see."""
    try:
        return {"ok": call()}
    except Exception as error:  # the error contract is part of the snapshot
        return {"error": type(error).__name__, "message": str(error)}


def capture_reader(database_path: Path) -> Dict[str, object]:
    def window(source_id: str, start: object = 0, count: object = 10) -> object:
        return _outcome(lambda: get_document_window(database_path, source_id, start=start, count=count))

    def citation(source_id: str, start: str, end: str) -> object:
        return _outcome(lambda: get_document_citation(
            database_path, source_id, start_anchor_id=start, end_anchor_id=end))

    cases: Dict[str, object] = {}
    for source_id in ("pdf-snap", "word-me", "word-states", "epub-list", "epub-break", "epub-none"):
        cases[f"window/{source_id}"] = window(source_id)
        cases[f"outline/{source_id}"] = _outcome(lambda: get_document_outline(database_path, source_id))
        cases[f"page_mapping/{source_id}"] = _outcome(lambda: describe_page_mapping(database_path, source_id))
    cases["window/pdf-snap/page"] = window("pdf-snap", start=1, count=2)
    cases["window/word-states/middle"] = window("word-states", start=1, count=2)
    cases["window/epub-list/past-end"] = window("epub-list", start=9, count=2)
    cases["window/missing"] = window("no-such-source")
    cases["window/bad-count"] = window("pdf-snap", count=0)
    cases["citation/pdf/verified-cross-page"] = citation(
        "pdf-snap", "pdf-snap-PAGE-000000", "pdf-snap-PAGE-000001")
    cases["citation/pdf/single-page"] = citation(
        "pdf-snap", "pdf-snap-PAGE-000001", "pdf-snap-PAGE-000001")
    cases["citation/pdf/crosses-uncalibrated"] = citation(
        "pdf-snap", "pdf-snap-PAGE-000001", "pdf-snap-PAGE-000002")
    cases["citation/word-me/one-work"] = citation("word-me", "word-me-P000000", "word-me-P000001")
    cases["citation/word-me/cross-work"] = citation("word-me", "word-me-P000000", "word-me-P000002")
    cases["citation/word-states/inferred"] = citation(
        "word-states", "word-states-P000000", "word-states-P000000")
    cases["citation/epub-list/cross-page"] = citation("epub-list", "epub-list-P000000", "epub-list-P000002")
    cases["citation/epub-break/single"] = citation("epub-break", "epub-break-P000001", "epub-break-P000001")
    cases["citation/epub-break/no-work-membership"] = citation(
        "epub-break", "epub-break-P000000", "epub-break-P000001")
    cases["citation/epub-none/uncalibrated"] = citation("epub-none", "epub-none-P000000", "epub-none-P000000")
    cases["citation/reversed"] = citation("word-me", "word-me-P000001", "word-me-P000000")
    return cases


def capture_translation(db: Path, model_id: Callable[[], str]) -> Dict[str, object]:
    cases: Dict[str, object] = {}
    cases["overview/none"] = translation_works.alignment_overview(db)
    generate_alignment(db, "work-one", "pdf-de", "pdf-zh")
    cases["overview/direct"] = translation_works.alignment_overview(db)
    cases["overview/active-model"] = translation_works.alignment_overview(db, active_model_id=model_id())
    cases["overview/model-changed"] = translation_works.alignment_overview(db, active_model_id="another-model")
    cases["overview/light"] = translation_works.alignment_overview(db, include_statistics=False)
    generate_alignment(db, "work-one", "pdf-de", "pdf-en")
    cases["overview/indirect"] = translation_works.alignment_overview(db)
    cases["overview/scoped"] = translation_works.alignment_overview(db, source_id="pdf-zh", target_id="pdf-en")
    cases["overview/ungrouped"] = translation_works.alignment_overview(db, source_id="not-grouped")

    window = translation_works.alignment_link_window(db, "pdf-de", "pdf-zh", 0, 0)
    cases["link_window/direct"] = window
    cases["link_window/indirect"] = _outcome(
        lambda: translation_works.alignment_link_window(db, "pdf-zh", "pdf-en", 0, 0))
    cases["link_window/reversed"] = _outcome(
        lambda: translation_works.alignment_link_window(db, "pdf-de", "pdf-zh", 3, 1))
    link = window["links"][0]
    cases["review_candidates"] = translation_works.review_candidates(
        db, "pdf-de", "pdf-zh", link["source_segment_ids"], link["target_segment_ids"], 2)
    translation_works.defer_review(db, "pdf-de", "pdf-zh", link["source_segment_ids"])
    cases["link_window/deferred"] = translation_works.alignment_link_window(db, "pdf-de", "pdf-zh", 0, 0)
    cases["save_correction"] = translation_works.save_correction(
        db, "pdf-de", "pdf-zh", link["source_segment_ids"], [])
    cases["link_window/corrected"] = translation_works.alignment_link_window(db, "pdf-de", "pdf-zh", 0, 0)
    cases["overview/after-correction"] = translation_works.alignment_overview(db)

    cases["position/empty"] = translation_works.read_reading_position(db, "work-one")
    translation_works.save_reading_position(db, "work-one", "pdf-zh", "pdf-de", 3, 17)
    cases["position/saved"] = translation_works.read_reading_position(db, "work-one")
    translation_works.dismiss_suggestion(db, ["pdf-zh", "pdf-de"])
    cases["dismissals"] = translation_works.list_suggestion_dismissals(db)
    return cases


_ID_KEYS = ("alignment_run_id", "override_id", "confirmation_token", "correction_id")


def _normalize(value: object, ids: Dict[str, str]) -> object:
    """Replace wall-clock timestamps and generated ids with stable placeholders."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key.endswith("_at") and item is not None:
                result[key] = "<timestamp>"
            elif key in _ID_KEYS and isinstance(item, str):
                result[key] = ids.setdefault(item, f"<{key}-{len(ids)}>")
            else:
                result[key] = _normalize(item, ids)
        return result
    if isinstance(value, list):
        return [_normalize(item, ids) for item in value]
    if isinstance(value, str) and value in ids:
        return ids[value]
    return value


def capture() -> Dict[str, object]:
    with tempfile.TemporaryDirectory() as temp_dir:
        database_path = Path(temp_dir) / "index.sqlite3"
        build_database(_reader_index(), database_path)
        reader = capture_reader(database_path)

    class _Fixture(_ThreeVersionWork):
        def runTest(self) -> None:  # pragma: no cover - fixture holder only
            pass

    fixture = _Fixture()
    fixture.setUp()
    try:
        translation_works._DETECTED_BOUNDS_CACHE.clear()
        translation = capture_translation(fixture.db, fixture._model_id)
    finally:
        fixture.embedding_patch.stop()
        fixture.directory.cleanup()
    return json.loads(json.dumps(
        {"reader": reader, "translation": _normalize(translation, {})},
        ensure_ascii=False, sort_keys=True, default=str,
    ))


class ReaderTranslationCharacterizationTests(unittest.TestCase):
    maxDiff = None

    def test_outputs_match_golden(self) -> None:
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        captured = capture()
        for section in ("reader", "translation"):
            self.assertEqual(sorted(captured[section]), sorted(golden[section]), section)
            for case, expected in golden[section].items():
                with self.subTest(section=section, case=case):
                    self.assertEqual(captured[section][case], expected)

    def test_capture_is_deterministic(self) -> None:
        self.assertEqual(capture(), capture())


if __name__ == "__main__":
    if "--regen" in sys.argv:
        GOLDEN.write_text(
            json.dumps(capture(), ensure_ascii=False, sort_keys=True, indent=1) + "\n",
            encoding="utf-8",
        )
        print(f"regenerated {GOLDEN}")
    else:
        unittest.main()

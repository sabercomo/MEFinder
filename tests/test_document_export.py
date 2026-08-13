import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src.me_finder.document_export import (
    DOCUMENT_SCHEMA_VERSION,
    DocumentExportError,
    document_manifest,
    export_document_json,
    export_document_zip,
    iter_document_pages,
    read_document_export,
    stable_export_fields,
)


class DocumentExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = document_manifest(
            document={"document_id": "DOC-繁", "source_file_id": "pdf-1"},
            source_sha256=hashlib.sha256(b"book").hexdigest(),
            source_file={"file_name": "古籍《學》.pdf", "size_bytes": 123},
            bibliographic_metadata={"title": "學而時習之", "author": "孔子"},
            external_ids={"isbn": None},
            parser_provider="fake",
            parser_model="fake-v1",
            parser_options={"language": "zh"},
            parser_provenance={"request_id": "req-1"},
            parsed_at="2026-08-09T00:00:00+00:00",
            parser_version="1",
            warnings=["第二頁印刷模糊"],
            missing_ranges=[],
            page_count=2,
        )
        self.pages = [
            {
                "pdf_page_index": 0,
                "printed_page": "一",
                "text_raw": "學而時習之，不亦說乎？",
                "blocks": [{"text": "學而時習之", "bbox": [1, 2, 3, 4]}],
                "parser": "fake",
                "warnings": [],
            },
            {
                "physical_pdf_page": 2,
                "logical_page": "二",
                "text": "有朋自遠方來。",
                "parser_provenance": {"provider": "fake", "slice": "s1"},
                "warnings": ["印刷模糊"],
            },
        ]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_schema_version_is_fixed(self) -> None:
        self.assertEqual(self.manifest["schema_version"], "mefinder.document.v1")
        self.assertEqual(DOCUMENT_SCHEMA_VERSION, "mefinder.document.v1")

    def test_json_export_round_trips_unicode_order_warnings_and_provenance(self) -> None:
        path = export_document_json(self.root / "document.json", self.manifest, self.pages)
        exported = read_document_export(path)
        self.assertEqual([p["physical_pdf_page"] for p in exported.pages], [1, 2])
        self.assertEqual(exported.pages[0]["text"], "學而時習之，不亦說乎？")
        self.assertEqual(exported.pages[1]["warnings"], ["印刷模糊"])
        self.assertEqual(exported.pages[1]["parser_provenance"]["slice"], "s1")
        self.assertEqual(exported.manifest["warnings"], ["第二頁印刷模糊"])

    def test_large_zip_writes_pages_incrementally(self) -> None:
        path = self.root / "book.mefinder.zip"
        emitted = []

        def pages():
            for page in self.pages:
                emitted.append(page["physical_pdf_page"] if "physical_pdf_page" in page else 1)
                yield page

        original_dumps = json.dumps

        def reject_page_lists(value, *args, **kwargs):
            if isinstance(value, list):
                raise AssertionError("the exporter materialized the full page list")
            return original_dumps(value, *args, **kwargs)

        with mock.patch("src.me_finder.document_export.json.dumps", side_effect=reject_page_lists):
            export_document_zip(path, self.manifest, pages())
        self.assertEqual(emitted, [1, 2])
        self.assertEqual([p["physical_pdf_page"] for p in iter_document_pages(path)], [1, 2])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "pages.ndjson"})

    def test_failure_leaves_partial_and_never_publishes_final(self) -> None:
        path = self.root / "failed.mefinder.zip"

        def broken_pages():
            yield self.pages[0]
            raise RuntimeError("injected failure")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            export_document_zip(path, self.manifest, broken_pages())
        self.assertFalse(path.exists())
        self.assertTrue(path.with_name(path.name + ".partial").exists())

    def test_same_input_has_deterministic_stable_fields(self) -> None:
        first = export_document_json(self.root / "one.json", self.manifest, iter(self.pages))
        second = export_document_zip(self.root / "two.mefinder.zip", self.manifest, iter(self.pages))
        self.assertEqual(
            stable_export_fields(read_document_export(first)),
            stable_export_fields(read_document_export(second)),
        )

    def test_out_of_order_pages_are_rejected(self) -> None:
        with self.assertRaises(DocumentExportError):
            export_document_json(
                self.root / "bad.json",
                self.manifest,
                reversed(self.pages),
            )

    def test_unknown_layout_fields_remain_null_instead_of_being_invented(self) -> None:
        page = read_document_export(
            export_document_json(
                self.root / "optional.json",
                {**self.manifest, "page_count": 1},
                [{"physical_pdf_page": 1, "text": "無版面資訊"}],
            )
        ).pages[0]
        self.assertIsNone(page["bbox"])
        self.assertIsNone(page["blocks"])
        self.assertIsNone(page["reading_order"])

    def test_page_count_mismatch_does_not_publish(self) -> None:
        wrong = dict(self.manifest)
        wrong["page_count"] = 3
        path = self.root / "wrong.json"
        with self.assertRaises(DocumentExportError):
            export_document_json(path, wrong, self.pages)
        self.assertFalse(path.exists())
        self.assertTrue(path.with_name(path.name + ".partial").exists())


if __name__ == "__main__":
    unittest.main()

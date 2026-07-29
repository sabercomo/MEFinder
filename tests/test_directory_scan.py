from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder import pdf_import_service
from src.me_finder.pdf_import_service import (
    copy_local_document,
    scan_directories_for_documents,
)


class DirectoryScanTests(unittest.TestCase):
    def test_scan_groups_new_imported_and_conflicting_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / "sub").mkdir()
            (base / "新书.pdf").write_bytes(b"%PDF-1.4 new")
            (base / "sub" / "论文.docx").write_bytes(b"docx bytes")
            (base / "已导入.pdf").write_bytes(b"%PDF-1.4 same size!")
            (base / "同名不同容.pdf").write_bytes(b"%PDF-1.4 different bytes here")
            (base / "~$临时.docx").write_bytes(b"office lock file")
            (base / "跳过.txt").write_text("not a document", encoding="utf-8")
            imported = {
                "已导入.pdf": (base / "已导入.pdf").stat().st_size,
                "同名不同容.pdf": 5,
            }
            result = scan_directories_for_documents([str(base)], imported, detect_limit=0)
        by_name = {entry["name"]: entry for entry in result["entries"]}
        self.assertEqual(set(by_name), {"新书.pdf", "论文.docx", "已导入.pdf", "同名不同容.pdf"})
        self.assertEqual(by_name["新书.pdf"]["status"], "new")
        self.assertEqual(by_name["新书.pdf"]["needs_ocr"], None)
        self.assertEqual(by_name["论文.docx"]["status"], "new")
        self.assertEqual(by_name["论文.docx"]["file_type"], "docx")
        self.assertEqual(by_name["已导入.pdf"]["status"], "imported")
        self.assertEqual(by_name["同名不同容.pdf"]["status"], "name_conflict")
        self.assertFalse(result["errors"])
        self.assertFalse(result["limit_reached"])

    def test_detection_stops_on_a_time_budget_but_still_lists_every_file(self) -> None:
        # 预检测按墙钟预算收口：预算耗尽后剩余文件仍要出现在结果里，
        # 只是没有文本层判定，不能被悄悄丢掉。
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            for index in range(6):
                (base / f"第{index}篇.pdf").write_bytes(b"%PDF-1.4 payload")

            probed: list[str] = []

            def slow_probe(path):
                probed.append(Path(path).name)
                # 每个文件都比预算贵，因此只有第一个能被检测到。
                clock["now"] += 5.0
                return {"detected_pdf_type": "native_text"}

            clock = {"now": 0.0}
            with (
                mock.patch.object(pdf_import_service, "detect_pdf_type", slow_probe),
                mock.patch.object(
                    pdf_import_service.time,
                    "monotonic",
                    lambda: clock["now"],
                ),
            ):
                result = scan_directories_for_documents(
                    [str(base)],
                    {},
                    detect_time_budget=4.0,
                )

        self.assertEqual(len(result["entries"]), 6)
        self.assertEqual(len(probed), 1)
        detected = [
            entry for entry in result["entries"] if entry.get("needs_ocr") is not None
        ]
        self.assertEqual(len(detected), 1)
        undetected = [
            entry for entry in result["entries"] if entry.get("needs_ocr") is None
        ]
        self.assertEqual(len(undetected), 5)
        self.assertTrue(all(entry["status"] == "new" for entry in undetected))

    def test_missing_directory_is_reported_not_fatal(self) -> None:
        result = scan_directories_for_documents(["Z:\\不存在的目录\\xyz"], {})
        self.assertEqual(result["entries"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("目录不存在", result["errors"][0]["error"])

    def test_entry_limit_stops_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            for index in range(6):
                (base / f"doc{index}.docx").write_bytes(b"x")
            result = scan_directories_for_documents([str(base)], {}, max_entries=4)
        self.assertEqual(len(result["entries"]), 4)
        self.assertTrue(result["limit_reached"])

    def test_copy_local_document_preserves_original_and_dedupes_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            source_dir.mkdir(parents=True)
            source = source_dir / "某书.pdf"
            source.write_bytes(b"%PDF-1.4 original")

            first = copy_local_document(root, source)
            self.assertEqual(first, root / "corpus" / "raw_pdf" / "某书.pdf")
            self.assertEqual(first.read_bytes(), source.read_bytes())
            self.assertTrue(source.exists())

            second = copy_local_document(root, source)
            self.assertNotEqual(second, first)
            self.assertIn("(imported-", second.name)
            self.assertEqual(second.read_bytes(), source.read_bytes())
            leftovers = [p for p in first.parent.iterdir() if p.name.endswith(".copying")]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()

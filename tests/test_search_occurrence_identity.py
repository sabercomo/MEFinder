from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.search import SearchEngine


class SearchOccurrenceIdentityTests(unittest.TestCase):
    @staticmethod
    def _paragraph(paragraph_id: str, paragraph_index: int) -> dict[str, object]:
        raw = "同一句原文在书中确实出现了两次。"
        return {
            "paragraph_id": paragraph_id,
            "volume_id": "VOL-1",
            "volume_number": 1,
            "work_id": "WORK-1",
            "source_file_id": "word-1",
            "source_type": "word",
            "paragraph_index": paragraph_index,
            "eligible_for_search": True,
            "text_raw": raw,
            "normalized_text": normalize_text(raw),
            "compact_text": compact_text(raw),
            "plain_text": punctuationless_text(raw),
            "document_title": "重复原句测试",
            "work_title": "重复原句测试",
            "volume_display": "重复原句测试",
            "page_display": f"第 {paragraph_index + 1} 页",
        }

    def _assert_both_occurrences(self, index_path: Path) -> None:
        engine = SearchEngine(index_path)
        try:
            result = engine.search("同一句原文", mode="exact", source_type="word")
        finally:
            engine.close()

        self.assertEqual(result["total"], 2)
        self.assertEqual(
            {item["paragraph_id"] for item in result["results"]},
            {"P-1", "P-2"},
        )

    def test_equal_text_in_distinct_paragraphs_remains_two_occurrences(self) -> None:
        index = {
            "metadata": {},
            "source_files": [
                {
                    "source_file_id": "word-1",
                    "source_type": "word",
                    "file_name": "repeat.docx",
                }
            ],
            "volumes": [
                {
                    "volume_id": "VOL-1",
                    "source_file_id": "word-1",
                    "source_type": "word",
                    "display_title": "重复原句测试",
                }
            ],
            "works": [
                {
                    "work_id": "WORK-1",
                    "volume_id": "VOL-1",
                    "source_type": "word",
                    "title": "重复原句测试",
                }
            ],
            "paragraphs": [self._paragraph("P-1", 0), self._paragraph("P-2", 1)],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            json_path = root / "index.json"
            json_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
            self._assert_both_occurrences(json_path)

            database_path = root / "index.sqlite3"
            build_database(index, database_path)
            self._assert_both_occurrences(database_path)


if __name__ == "__main__":
    unittest.main()

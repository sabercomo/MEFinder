from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.database import (
    SCHEMA,
    build_database,
    delete_source_from_database,
    replace_source_in_database,
)
from src.me_finder.normalization import (
    compact_text,
    normalize_text,
    punctuationless_text,
)
from src.me_finder.search import SearchEngine


def _paragraph(
    number: int,
    text: str,
    *,
    source_id: str = "pdf-a",
    source_type: str = "pdf",
) -> dict[str, object]:
    volume_id = f"{source_id}-volume"
    work_id = f"{source_id}-work"
    return {
        "paragraph_id": f"{source_id}-p-{number}",
        "volume_id": volume_id,
        "volume_number": None,
        "work_id": work_id,
        "source_file_id": source_id,
        "source_type": source_type,
        "paragraph_index": number,
        "eligible_for_search": True,
        "text_raw": text,
        "normalized_text": normalize_text(text),
        "compact_text": compact_text(text),
        "plain_text": punctuationless_text(text),
        "document_title": "测试文献",
        "work_title": "测试文献",
        "volume_display": "测试文献",
        "page_display": "引用页码尚未校准",
        "page_source_type": "uncalibrated" if source_type == "pdf" else "unknown",
        "pdf_page_start_index": number if source_type == "pdf" else None,
        "pdf_page_end_index": number if source_type == "pdf" else None,
        "original_file_name": f"{source_id}.pdf",
        "sentences": [text],
    }


def _source_index(source_id: str, paragraphs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "metadata": {},
        "source_files": [
            {
                "source_file_id": source_id,
                "source_type": "pdf",
                "file_name": f"{source_id}.pdf",
            }
        ],
        "volumes": [
            {
                "volume_id": f"{source_id}-volume",
                "source_file_id": source_id,
                "source_type": "pdf",
                "display_title": "测试文献",
            }
        ],
        "works": [
            {
                "work_id": f"{source_id}-work",
                "volume_id": f"{source_id}-volume",
                "source_type": "pdf",
                "title": "测试文献",
            }
        ],
        "paragraphs": paragraphs,
    }


class FTS5SearchScalabilityTests(unittest.TestCase):
    def test_build_uses_sparse_payload_and_formats_only_visible_results(self) -> None:
        paragraphs = [
            _paragraph(index, f"共同检索词，第 {index} 条不同文本。")
            for index in range(205)
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            summary = build_database(_source_index("pdf-a", paragraphs), database)
            connection = sqlite3.connect(str(database))
            try:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM paragraphs LIMIT 1"
                    ).fetchone()[0]
                )
                fts_hits = connection.execute(
                    "SELECT COUNT(*) FROM paragraphs_fts "
                    "WHERE paragraphs_fts MATCH ?",
                    ('"共同检" AND "同检索" AND "检索词"',),
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO paragraphs_fts(paragraphs_fts, rank) "
                    "VALUES ('integrity-check', 1)"
                )
            finally:
                connection.close()

            engine = SearchEngine(database)
            try:
                with mock.patch.object(
                    engine, "_format_result", wraps=engine._format_result
                ) as formatter:
                    result = engine.search(
                        "共同检索词", source_type="pdf", limit=10
                    )
                all_result = engine.search(
                    "共同检索词", source_type="pdf", limit="all"
                )
            finally:
                engine.close()

        self.assertTrue(summary["fts5_search_index"])
        self.assertEqual(fts_hits, 205)
        self.assertTrue(
            {"text_raw", "normalized_text", "compact_text", "plain_text", "sentences"}.isdisjoint(payload)
        )
        self.assertEqual(formatter.call_count, 10)
        self.assertEqual(len(result["results"]), 10)
        self.assertTrue(result["has_more"])
        self.assertFalse(result["total_is_exact"])
        self.assertEqual(all_result["total"], 205)
        self.assertTrue(all_result["total_is_exact"])

    def test_fts_prefilter_preserves_modes_filters_and_short_query_fallback(self) -> None:
        first = _paragraph(1, "Alpha  Beta，马克思！", source_id="pdf-a")
        second = _paragraph(2, "另一份马克思材料。", source_id="pdf-b")
        index_a = _source_index("pdf-a", [first])
        index_b = _source_index("pdf-b", [second])
        combined = {
            "metadata": {},
            "source_files": index_a["source_files"] + index_b["source_files"],
            "volumes": index_a["volumes"] + index_b["volumes"],
            "works": index_a["works"] + index_b["works"],
            "paragraphs": [first, second],
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            build_database(combined, database)
            engine = SearchEngine(database)
            try:
                compact = engine.search(
                    "AlphaBeta，马克思", mode="compact", source_file_id="pdf-a"
                )
                punctuation = engine.search(
                    "Alpha Beta 马克思", mode="punctuation", source_file_id="pdf-a"
                )
                short = engine.search("马", mode="exact", source_file_id="pdf-b")
            finally:
                engine.close()

        self.assertEqual(compact["results"][0]["source_file_id"], "pdf-a")
        self.assertEqual(punctuation["results"][0]["source_file_id"], "pdf-a")
        self.assertEqual(short["results"][0]["source_file_id"], "pdf-b")

    def test_legacy_database_is_streamed_to_sparse_v2_on_first_search(self) -> None:
        paragraph = _paragraph(1, "旧库中的马克思原句。", source_type="word")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            connection = sqlite3.connect(str(database))
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT INTO source_files(source_file_id, source_type, payload_json) "
                "VALUES (?, ?, ?)",
                ("pdf-a", "word", json.dumps({"source_file_id": "pdf-a", "source_type": "word"})),
            )
            connection.execute(
                """
                INSERT INTO paragraphs(
                    paragraph_id, volume_id, work_id, source_file_id, source_type,
                    paragraph_index, eligible_for_search, text_raw, normalized_text,
                    compact_text, plain_text, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    paragraph["paragraph_id"],
                    paragraph["volume_id"],
                    paragraph["work_id"],
                    paragraph["source_file_id"],
                    paragraph["source_type"],
                    paragraph["paragraph_index"],
                    1,
                    paragraph["text_raw"],
                    paragraph["normalized_text"],
                    paragraph["compact_text"],
                    paragraph["plain_text"],
                    json.dumps(paragraph, ensure_ascii=False),
                ),
            )
            connection.commit()
            connection.close()

            engine = SearchEngine(database)
            try:
                self.assertFalse(engine._fts_ready)
                result = engine.search("马克思", mode="exact")
                self.assertTrue(engine._fts_ready)
            finally:
                engine.close()

            connection = sqlite3.connect(str(database))
            try:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM paragraphs"
                    ).fetchone()[0]
                )
                storage = json.loads(
                    connection.execute(
                        "SELECT value_json FROM metadata "
                        "WHERE key = 'paragraph_payload_storage'"
                    ).fetchone()[0]
                )
                fts_hits = connection.execute(
                    "SELECT COUNT(*) FROM paragraphs_fts "
                    "WHERE paragraphs_fts MATCH ?",
                    ('"马克思"',),
                ).fetchone()[0]
            finally:
                connection.close()

            backups = list((Path(directory) / "backups").glob("*.sqlite3"))

        self.assertEqual(result["total"], 1)
        self.assertEqual(storage, "sparse_text_v1")
        self.assertNotIn("text_raw", payload)
        self.assertEqual(fts_hits, 1)
        self.assertEqual(len(backups), 1)

    def test_replace_and_delete_keep_external_content_index_consistent(self) -> None:
        old = _paragraph(1, "旧文马克思。")
        replacement = _paragraph(1, "新文恩格斯。")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            build_database(_source_index("pdf-a", [old]), database)
            replace_source_in_database(
                _source_index("pdf-a", [replacement]),
                database,
                backup_existing=False,
            )
            connection = sqlite3.connect(str(database))
            try:
                marx = connection.execute(
                    "SELECT COUNT(*) FROM paragraphs_fts "
                    "WHERE paragraphs_fts MATCH ?",
                    ('"马克思"',),
                ).fetchone()[0]
                engels = connection.execute(
                    "SELECT COUNT(*) FROM paragraphs_fts "
                    "WHERE paragraphs_fts MATCH ?",
                    ('"恩格斯"',),
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO paragraphs_fts(paragraphs_fts, rank) "
                    "VALUES ('integrity-check', 1)"
                )
            finally:
                connection.close()
            delete_source_from_database(
                "pdf-a", database, backup_existing=False
            )
            connection = sqlite3.connect(str(database))
            try:
                after_delete = connection.execute(
                    "SELECT COUNT(*) FROM paragraphs_fts "
                    "WHERE paragraphs_fts MATCH ?",
                    ('"恩格斯"',),
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO paragraphs_fts(paragraphs_fts, rank) "
                    "VALUES ('integrity-check', 1)"
                )
            finally:
                connection.close()

        self.assertEqual(marx, 0)
        self.assertEqual(engels, 1)
        self.assertEqual(after_delete, 0)

    def test_missing_fts_runtime_falls_back_to_legacy_scan(self) -> None:
        paragraph = _paragraph(1, "降级路径仍能找到马克思。")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            build_database(_source_index("pdf-a", [paragraph]), database)
            connection = sqlite3.connect(str(database))
            connection.executescript(
                """
                DROP TRIGGER paragraphs_fts_ai;
                DROP TRIGGER paragraphs_fts_ad;
                DROP TRIGGER paragraphs_fts_au;
                DROP TABLE paragraphs_fts;
                DELETE FROM metadata WHERE key = 'paragraph_fts_version';
                """
            )
            connection.commit()
            connection.close()
            engine = SearchEngine(database)
            try:
                with mock.patch(
                    "src.me_finder.search.ensure_database_search_index",
                    return_value=False,
                ):
                    result = engine.search("马克思", mode="exact")
            finally:
                engine.close()

        self.assertEqual(result["total"], 1)
        self.assertFalse(engine._fts_ready)


if __name__ == "__main__":
    unittest.main()

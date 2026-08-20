from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.me_finder import database as dbmod
from src.me_finder.document_export_service import ensure_document_headings
from src.me_finder.document_heading import DOCUMENT_HEADING_VERSION


def _v2_title(text, level, bbox):
    return {
        "type": "title",
        "content": {"title_content": [{"type": "text", "content": text}], "level": level},
        "bbox": bbox,
    }


class LazyEnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "index.sqlite3"
        import sqlite3
        con = sqlite3.connect(str(self.db))
        try:
            con.executescript(dbmod.SCHEMA)
        finally:
            con.close()

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except OSError:
            pass  # Windows may briefly hold the sqlite file handle

    def _insert(self, sid, pages, relative_path="corpus/raw_pdf/__missing__.pdf"):
        import sqlite3
        con = sqlite3.connect(str(self.db))
        source = {
            "source_file_id": sid,
            "source_type": "pdf",
            "relative_path": relative_path,
            "bibliographic_metadata": {"title": "样书"},
        }
        con.execute(
            "INSERT INTO source_files(source_file_id,source_type,file_name,relative_path,volume_number,payload_json)"
            " VALUES(?,?,?,?,?,?)",
            (sid, "pdf", "x.pdf", relative_path, None, json.dumps(source, ensure_ascii=False)),
        )
        for pg in pages:
            con.execute(
                "INSERT INTO pdf_pages(source_file_id,pdf_page_index,payload_json) VALUES(?,?,?)",
                (sid, pg["pdf_page_index"], json.dumps(pg, ensure_ascii=False)),
            )
        con.commit(); con.close()

    def _blocks(self, sid):
        import sqlite3
        con = sqlite3.connect(str(self.db))
        rows = [json.loads(r[0]) for r in con.execute(
            "SELECT payload_json FROM pdf_pages WHERE source_file_id=? ORDER BY pdf_page_index", (sid,))]
        con.close()
        return [b for pg in rows for b in pg.get("blocks") or []]

    def _profile(self, sid):
        import sqlite3
        con = sqlite3.connect(str(self.db))
        r = con.execute("SELECT payload_json FROM source_files WHERE source_file_id=?", (sid,)).fetchone()
        con.close()
        return json.loads(r[0]).get("document_heading_profile")

    def _make_v2_dir(self, name, v2):
        rd = self.root / "rd" / name
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "x_content_list_v2.json").write_text(json.dumps(v2, ensure_ascii=False), encoding="utf-8")
        return rd

    def test_lazy_enrich_from_cached_v2_and_idempotent(self) -> None:
        rd = self._make_v2_dir("s1", [[_v2_title("标题甲", 1, [1, 1, 2, 2])]])
        sid = "s1"
        self._insert(sid, [{
            "pdf_page_index": 0,
            "text_raw": "标题甲\n正文内容",
            "blocks": [
                {"text": "标题甲", "bbox": [1, 1, 2, 2], "result_dir": str(rd),
                 "local_page_idx": 0, "pdf_page_index": 0, "mineru_type": "text"},
                {"text": "正文内容", "bbox": [1, 3, 9, 9], "result_dir": str(rd),
                 "local_page_idx": 0, "pdf_page_index": 0, "mineru_type": "text"},
            ],
        }])
        # before
        self.assertFalse(any(b.get("document_heading_level") for b in self._blocks(sid)))
        # first enrich
        prof = ensure_document_headings(database_path=self.db, runtime_root=self.root, source_file_id=sid)
        self.assertEqual(prof["version"], DOCUMENT_HEADING_VERSION)
        self.assertEqual(prof["status"], "complete")
        self.assertEqual(prof["sources"], ["mineru_v2"])
        blocks = self._blocks(sid)
        self.assertEqual(blocks[0]["document_heading_level"], 1)
        self.assertEqual(blocks[0]["document_heading_source"], "mineru_v2")
        # raw text untouched
        self.assertEqual(blocks[0]["text"], "标题甲")
        # idempotent: second call does not rewrite (enriched_at stable)
        prof2 = ensure_document_headings(database_path=self.db, runtime_root=self.root, source_file_id=sid)
        self.assertEqual(prof["enriched_at"], prof2["enriched_at"])

    def test_no_pdf_no_v2_is_unavailable_and_non_fatal(self) -> None:
        sid = "s2"
        self._insert(sid, [{
            "pdf_page_index": 0,
            "text_raw": "只有正文",
            "blocks": [{"text": "只有正文", "bbox": [1, 1, 2, 2],
                        "result_dir": str(self.root / "does-not-exist"),
                        "local_page_idx": 0, "pdf_page_index": 0, "mineru_type": "text"}],
        }])
        prof = ensure_document_headings(database_path=self.db, runtime_root=self.root, source_file_id=sid)
        self.assertEqual(prof["status"], "unavailable")
        self.assertFalse(any(b.get("document_heading_level") for b in self._blocks(sid)))

    def test_already_complete_is_skipped(self) -> None:
        rd = self._make_v2_dir("s3", [[_v2_title("甲", 1, [1, 1, 2, 2])]])
        sid = "s3"
        self._insert(sid, [{
            "pdf_page_index": 0, "text_raw": "甲",
            "blocks": [{"text": "甲", "bbox": [1, 1, 2, 2], "result_dir": str(rd),
                        "local_page_idx": 0, "pdf_page_index": 0, "mineru_type": "text"}],
        }])
        first = ensure_document_headings(database_path=self.db, runtime_root=self.root, source_file_id=sid)
        # tamper a block to prove the skip path does not re-run/rewrite
        import sqlite3
        con = sqlite3.connect(str(self.db))
        row = con.execute("SELECT payload_json FROM pdf_pages WHERE source_file_id=?", (sid,)).fetchone()
        pg = json.loads(row[0]); pg["blocks"][0]["sentinel"] = "kept"
        con.execute("UPDATE pdf_pages SET payload_json=? WHERE source_file_id=? AND pdf_page_index=0",
                    (json.dumps(pg, ensure_ascii=False), sid))
        con.commit(); con.close()
        again = ensure_document_headings(database_path=self.db, runtime_root=self.root, source_file_id=sid)
        self.assertEqual(first["enriched_at"], again["enriched_at"])  # skipped
        self.assertEqual(self._blocks(sid)[0].get("sentinel"), "kept")  # not overwritten

    def test_surrogate_headings_are_scrubbed_not_fatal(self) -> None:
        # PDF bookmark/outline strings arrive via ``surrogateescape`` and can
        # carry lone UTF-16 surrogates.  SQLite stores str as UTF-8 and rejects
        # them, so the enrichment write must scrub them instead of aborting the
        # export with "surrogates not allowed".  The taint enters through the
        # PDF outline, so patch ``enrich_pdf_headings`` to emit one directly.
        from unittest import mock

        rd = self._make_v2_dir("s4", [[_v2_title("标题甲", 1, [1, 1, 2, 2])]])
        sid = "s4"
        self._insert(sid, [{
            "pdf_page_index": 0, "text_raw": "标题甲",
            "blocks": [{"text": "标题甲", "bbox": [1, 1, 2, 2], "result_dir": str(rd),
                        "local_page_idx": 0, "pdf_page_index": 0, "mineru_type": "text"}],
        }])
        tainted_outline = {
            "classification": "semantic",
            "entries": [{"title": "Preface\udcc0\udc80\udcc0\udc80", "page": 9, "level": 1}],
        }
        with mock.patch(
            "src.me_finder.document_export_service.enrich_pdf_headings",
            return_value=tainted_outline,
        ):
            # Must not raise UnicodeEncodeError.
            prof = ensure_document_headings(
                database_path=self.db, runtime_root=self.root, source_file_id=sid
            )
        self.assertEqual(prof["version"], DOCUMENT_HEADING_VERSION)
        # Stored payload round-trips as valid UTF-8 with no surrogates left.
        import sqlite3
        con = sqlite3.connect(str(self.db))
        raw = con.execute(
            "SELECT payload_json FROM source_files WHERE source_file_id=?", (sid,)
        ).fetchone()[0]
        con.close()
        self.assertNotIn("\udcc0", raw)
        raw.encode("utf-8")  # would raise if a lone surrogate survived
        self.assertIn("�", raw)  # replaced, not silently dropped


if __name__ == "__main__":
    unittest.main()

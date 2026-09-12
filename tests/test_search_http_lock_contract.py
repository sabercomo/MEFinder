"""HTTP error contract for a search that loses the busy_timeout race.

A read that waits out the full ``busy_timeout`` on a concurrent writer's lock
raises ``sqlite3.OperationalError('database is locked')``. Over the real HTTP
boundary that must become a distinct, retriable 503 — not an unhandled 500, and
never a silent empty result that would read as "no hits". Non-lock operational
errors must stay real faults (500), not be masked as a transient.

The lock is injected at the engine seam so the test drives the whole real stack
(socket -> do_POST -> dispatch -> _post_search) without a 30-second wait; the
actual SQLite lock behaviour is covered by the alignment write-window tests.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from src.me_finder.app_context import AppContext
from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.web import make_handler

_FOLDING_SEAM = "src.me_finder.application.index_runtime.execute_with_script_folding"


def _index():
    text = "社会与个体。"
    paragraph = {
        "paragraph_id": "p0", "source_file_id": "book", "volume_id": "v", "work_id": "w",
        "source_type": "pdf", "paragraph_index": 0, "volume_number": 1,
        "document_title": "书", "work_title": "章", "volume_display": "书",
        "eligible_for_search": True, "text_raw": text,
        "normalized_text": normalize_text(text), "compact_text": compact_text(text),
        "plain_text": punctuationless_text(text),
        "pdf_page_start_index": 8, "pdf_page_end_index": 8,
        "citation_page_start": "1", "citation_page_end": "1", "page_display": "第 1 页",
    }
    return {
        "metadata": {}, "source_files": [
            {"source_file_id": "book", "source_type": "pdf", "file_name": "book.pdf",
             "display_title": "书"}],
        "volumes": [{"volume_id": "v", "source_file_id": "book", "source_type": "pdf"}],
        "works": [{"work_id": "w", "volume_id": "v", "source_type": "pdf", "title": "章"}],
        "paragraphs": [paragraph],
    }


class SearchHttpLockContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name)
        database = root / "data" / "index.sqlite3"
        build_database(_index(), database)
        self.handler = make_handler(
            database, app_context=AppContext.create(root, index_path=database)
        )
        self.handler.log_message = lambda *_: None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.opener = build_opener(ProxyHandler({}))

        def _cleanup() -> None:
            self.server.shutdown()
            self.server.server_close()
            self.handler.close_runtime()
            self.thread.join(timeout=2)

        self.addCleanup(_cleanup)

    def _search(self, query="社会"):
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}/api/search",
            data=json.dumps({"query": query, "mode": "exact"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_normal_search_returns_200(self) -> None:
        status, body = self._search()
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)

    def test_lock_timeout_maps_to_retriable_503_not_empty(self) -> None:
        with patch(_FOLDING_SEAM, side_effect=sqlite3.OperationalError("database is locked")):
            status, body = self._search()
        self.assertEqual(status, 503)
        self.assertTrue(body.get("retriable"))
        self.assertIn("error", body)
        # It must not have degraded into a fake "no hits" success.
        self.assertNotIn("results", body)

    def test_non_lock_operational_error_is_not_masked_as_transient(self) -> None:
        with patch(_FOLDING_SEAM, side_effect=sqlite3.OperationalError("no such table: paragraphs")):
            status, body = self._search()
        self.assertEqual(status, 500)
        self.assertNotIn("retriable", body)


if __name__ == "__main__":
    unittest.main()

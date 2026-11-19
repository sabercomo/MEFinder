"""Zotero sync through the real import queue, SQLite index and PDF reader."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from src.me_finder.app_context import AppContext
from src.me_finder.database import build_database
from src.me_finder.import_config_store import load_import_config, save_import_config
from src.me_finder.index_publisher import rebuild_local_index
from src.me_finder.pdf.import_service import register_pdf
from src.me_finder.persistence.zotero_sync_store import ZoteroSyncStore
from src.me_finder.preferences import resolve_preferences_path, save_preferences
from src.me_finder.search.engine import SearchEngine
from src.me_finder.web import make_handler
from src.me_finder.zotero_local_api import ZoteroLocalClient
from tests.test_long_filename_import import write_native_pdf
from tests.zotero_fake import FakeZotero


class ZoteroInPlaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runtime_root = self.root / "mefinder"
        self.database = self.runtime_root / "data" / "index.sqlite3"
        self.raw_pdf = self.runtime_root / "corpus" / "raw_pdf"
        self.raw_pdf.mkdir(parents=True)
        self.config_path = self.runtime_root / "config" / "pdf_imports.json"
        self.original = self.root / "外部文献.pdf"
        write_native_pdf(self.original)
        self.content = self.original.read_bytes()
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.source_id = f"pdf-import-{self.digest[:16]}"
        self.fake = FakeZotero(server_id="TestLibrary")
        base_url = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.fake.add_collection("TEST", "Test")
        self.fake.add_item("ITEM", "Test paper", ["TEST"])
        self.fake.add_attachment("ATT", "ITEM", self.original)
        self.enterContext(patch(
            "src.me_finder.zotero_sync.ZoteroLocalClient",
            side_effect=lambda **kwargs: ZoteroLocalClient(base_url, **kwargs),
        ))
        self.enterContext(patch(
            "src.me_finder.application.import_orchestrator.ZoteroLocalClient",
            side_effect=lambda **kwargs: ZoteroLocalClient(base_url, **kwargs),
        ))
        build_database({"metadata": {}}, self.database)
        save_preferences(
            {"zotero_sync_enabled": True, "zotero_sync_frequency": "manual",
             "zotero_sync_collections": ["TEST"]},
            resolve_preferences_path(self.runtime_root),
        )
        self.store = ZoteroSyncStore(self.database)

    def start_app(self) -> None:
        self.handler = make_handler(
            self.database,
            app_context=AppContext.create(self.runtime_root, index_path=self.database),
        )
        self.handler.log_message = lambda *_args: None
        self.addCleanup(self.handler.close_runtime)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method: str, path: str, *, headers=None, body=None) -> tuple[int, bytes]:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            payload = json.dumps(body or {}).encode() if method == "POST" else None
            connection.request(
                method, path, payload,
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def until(self, description, condition) -> None:
        deadline = time.monotonic() + 8
        while not condition():
            if time.monotonic() >= deadline:
                self.fail(description)
            time.sleep(0.02)

    def sync(self) -> dict:
        _status, prior = self.request("GET", "/api/zotero/status")
        previous_start = json.loads(prior).get("started_at")
        def start():
            status, body = self.request("POST", "/api/zotero/sync")
            self.assertEqual(status, 200)
            return json.loads(body)["started"]

        self.until("Zotero sync did not start", start)
        result = {}

        def complete():
            _status, response = self.request("GET", "/api/zotero/status")
            result.update(json.loads(response))
            return result.get("started_at") != previous_start and result["phase"] in {
                "done", "paused", "error",
            }

        self.until("Zotero sync did not finish", complete)
        self.assertEqual(result["phase"], "done", result)
        return result

    def wait_for_link(self) -> None:
        def linked():
            self.request("GET", "/api/zotero/status")
            return self.store.read().attachments["ATT"]["status"] == "linked"

        self.until("Zotero PDF never finished importing", linked)

    def test_new_pdf_is_indexed_and_streamed_without_a_copy(self) -> None:
        self.start_app()
        self.sync()
        self.wait_for_link()
        self.assertEqual(list(self.raw_pdf.glob("*.pdf")), [])
        engine = SearchEngine(self.database)
        self.addCleanup(engine.close)
        hits = engine.search("National science education standards")["results"]
        self.assertTrue(hits)
        self.assertEqual(hits[0]["source_file_id"], self.source_id)
        self.assertTrue(hits[0]["page_match_spans"])
        engine.close()
        self.assertEqual(
            self.request("GET", f"/source/{self.source_id}", headers={"Range": "bytes=0-7"}),
            (206, self.content[:8]),
        )

        moved = self.root / "renamed.pdf"
        self.original.rename(moved)
        self.fake.files["ATT"] = moved
        self.sync()
        self.assertEqual(self.handler.document_queries.source_path(self.source_id), moved.resolve())
        self.assertEqual(self.request("GET", f"/source/{self.source_id}"), (200, self.content))

        temporarily_missing = self.root / "missing.pdf"
        moved.rename(temporarily_missing)
        self.sync()
        self.assertIsNotNone(self.handler.index_runtime.source(self.source_id))
        self.assertEqual(self.request("GET", f"/source/{self.source_id}")[0], 404)
        temporarily_missing.rename(moved)

        self.fake.delete("ATT")
        self.sync()
        self.assertIsNone(self.handler.index_runtime.source(self.source_id))
        self.assertEqual(moved.read_bytes(), self.content)

    def test_legacy_sync_copy_switches_without_replacing_indexed_pages(self) -> None:
        copy = self.raw_pdf / "managed.pdf"
        shutil.copy2(self.original, copy)
        document = register_pdf(self.runtime_root, copy)
        self.assertEqual(document["source_file_id"], self.source_id)
        document["page_mapping"] = {
            "validated_by": "manual",
            "segments": [{"pdf_page_start": 0, "pdf_page_end": 1, "citation_page_start": "31"}],
        }
        save_import_config(self.config_path, {"documents": [document]})
        rebuild_local_index(self.runtime_root, database_path=self.database)
        self.store.write(attachments=[{
            "attachment_key": "ATT", "parent_item_key": "ITEM", "attachment_version": 3,
            "link_mode": "imported_file", "content_type": "application/pdf",
            "file_name": self.original.name, "file_signature": f"m1|1||{self.original.name}",
            "file_sha256": self.digest, "source_file_id": self.source_id,
            "origin": "imported", "status": "linked",
        }])
        with closing(sqlite3.connect(self.database)) as connection:
            pages = connection.execute("SELECT * FROM pdf_pages").fetchall()
        self.assertTrue(pages)

        self.start_app()
        self.original.write_bytes(self.content + b"\n")
        self.sync()
        self.assertEqual(self.handler.document_queries.source_path(self.source_id), copy.resolve())

        self.original.write_bytes(self.content)
        self.sync()
        self.assertEqual(self.handler.document_queries.source_path(self.source_id), self.original.resolve())
        self.assertEqual(self.request("GET", f"/source/{self.source_id}"), (200, self.content))
        self.assertEqual(copy.read_bytes(), self.content)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM pdf_pages").fetchall(), pages)
        with closing(SearchEngine(self.database)) as engine:
            hits = engine.search("National science education standards page 1")["results"]
        self.assertEqual(str(hits[0]["citation_page_start"]), "31")
        updated = load_import_config(self.config_path)["documents"][0]
        self.assertEqual(updated["page_mapping"], document["page_mapping"])


    def test_interrupted_mineru_task_resumes_after_rename_and_restart(self) -> None:
        save_preferences({"pdf_parse_mode": "mineru"}, resolve_preferences_path(self.runtime_root))
        calls = []

        def parse_mineru(_root, path, _source_id, **_options):
            calls.append(path)
            if len(calls) == 1:
                raise OSError("simulated interruption")

        self.enterContext(patch("src.me_finder.import_assembly.parse_pdf_with_mineru", parse_mineru))
        self.start_app()
        self.sync()
        job_id = self.store.read().attachments["ATT"]["import_job_id"]
        self.until(
            "MinerU task did not reach resumable failure",
            lambda: self.handler.import_orchestrator.job_status(job_id)["can_resume"],
        )
        self.assertEqual(calls, [self.original.resolve()])

        moved = self.root / "renamed.pdf"
        self.original.rename(moved)
        self.fake.files["ATT"] = moved
        self.server.shutdown()
        self.assertTrue(self.handler.close_runtime())
        self.start_app()
        status, response = self.request("POST", "/api/import-resume", body={"job_id": job_id})
        self.assertEqual(status, 200, response)
        self.wait_for_link()
        self.assertEqual(calls, [self.original.resolve(), moved.resolve()])
        self.assertEqual(self.handler.document_queries.source_path(self.source_id), moved.resolve())
        self.assertEqual(list(self.raw_pdf.glob("*.pdf")), [])

    def test_source_change_blocks_old_pages_and_indexes_a_new_version(self) -> None:
        self.start_app()
        self.sync()
        self.wait_for_link()

        self.original.unlink()
        write_native_pdf(self.original)
        self.assertNotEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.digest)
        self.assertEqual(self.request("GET", f"/source/{self.source_id}")[0], 404)
        self.sync()
        self.wait_for_link()
        source_id = self.store.read().attachments["ATT"]["source_file_id"]
        self.assertNotEqual(source_id, self.source_id)
        self.assertIsNone(self.handler.index_runtime.source(self.source_id))
        self.assertEqual(self.request("GET", f"/source/{source_id}"), (200, self.original.read_bytes()))

    def test_manual_copy_is_retained_when_matching_attachment_leaves(self) -> None:
        copy = self.raw_pdf / "manual.pdf"
        shutil.copy2(self.original, copy)
        register_pdf(self.runtime_root, copy)
        rebuild_local_index(self.runtime_root, database_path=self.database)
        self.start_app()
        self.sync()
        self.assertEqual(self.store.read().attachments["ATT"]["origin"], "linked_existing")
        self.assertEqual(self.handler.document_queries.source_path(self.source_id), copy.resolve())

        self.fake.delete("ATT")
        self.sync()
        self.assertEqual(self.handler.document_queries.source_path(self.source_id), copy.resolve())
        self.assertEqual(copy.read_bytes(), self.content)

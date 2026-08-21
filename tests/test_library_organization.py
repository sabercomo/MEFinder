from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.me_finder.app_context import AppContext
from src.me_finder.database import (
    assign_sources_to_document_group,
    build_database,
    create_document_group,
    create_folder,
    delete_document_group,
    delete_folder,
    load_database_index,
    migrate_database_schema,
    move_sources_to_folder,
    replace_source_in_database,
    update_source_version_metadata,
)
from src.me_finder.normalization import (
    compact_text,
    normalize_text,
    punctuationless_text,
)
from src.me_finder.search import SearchEngine
from src.me_finder.web import make_handler


def _paragraph(source_id: str, source_type: str, text: str) -> dict[str, object]:
    return {
        "paragraph_id": f"{source_id}-paragraph",
        "volume_id": f"{source_id}-volume",
        "volume_number": 1,
        "source_file_id": source_id,
        "source_type": source_type,
        "paragraph_index": 0,
        "eligible_for_search": True,
        "text_raw": text,
        "normalized_text": normalize_text(text),
        "compact_text": compact_text(text),
        "plain_text": punctuationless_text(text),
        "heading_path": ["第一章", "第一节"],
        "original_file_name": f"{source_id}.{'pdf' if source_type == 'pdf' else 'docx'}",
    }


def _index() -> dict[str, object]:
    sources = [
        {
            "source_file_id": "word-a",
            "source_type": "word",
            "file_name": "a.docx",
            "title": "版本甲",
        },
        {
            "source_file_id": "pdf-b",
            "source_type": "pdf",
            "file_name": "b.pdf",
            "title": "Version B",
        },
        {
            "source_file_id": "word-root",
            "source_type": "word",
            "file_name": "root.docx",
            "title": "根目录文献",
        },
    ]
    volumes = [
        {
            "volume_id": f"{source['source_file_id']}-volume",
            "source_file_id": source["source_file_id"],
            "source_type": source["source_type"],
            "display_title": source["title"],
        }
        for source in sources
    ]
    return {
        "metadata": {},
        "source_files": sources,
        "volumes": volumes,
        "works": [
            {
                "work_id": "word-a-work",
                "volume_id": "word-a-volume",
                "source_type": "word",
                "title": "第一章",
            }
        ],
        "paragraphs": [
            _paragraph("word-a", "word", "承认 共同关键词"),
            _paragraph("pdf-b", "pdf", "recognition 共同关键词"),
            _paragraph("word-root", "word", "承认 共同关键词"),
        ],
        "pdf_pages": [
            {
                "source_file_id": "pdf-b",
                "pdf_page_index": 0,
                "text_raw": "recognition 共同关键词",
                "page_text_hash": "page-hash",
                "ocr_used": True,
            }
        ],
    }


class LegacyLibraryMigrationTests(unittest.TestCase):
    def test_v2_migration_keeps_existing_sources_unfiled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "index.sqlite3"
            connection = sqlite3.connect(str(database))
            try:
                connection.executescript(
                    """
                    PRAGMA user_version = 2;
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                    CREATE TABLE source_files (
                        source_file_id TEXT PRIMARY KEY,
                        source_type TEXT NOT NULL,
                        file_name TEXT,
                        relative_path TEXT,
                        volume_number INTEGER,
                        payload_json TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO source_files VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "legacy-word",
                        "word",
                        "legacy.docx",
                        "corpus/legacy.docx",
                        1,
                        json.dumps({"source_file_id": "legacy-word"}),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertTrue(migrate_database_schema(database))

            connection = sqlite3.connect(str(database))
            try:
                row = connection.execute(
                    "SELECT source_file_id, folder_id, document_group_id "
                    "FROM source_files"
                ).fetchone()
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(version, 3)
        self.assertEqual(row, ("legacy-word", None, None))


class LibraryOrganizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "index.sqlite3"
        build_database(_index(), self.database)
        self.folder_a = create_folder("德国古典哲学", self.database)["folder_id"]
        self.folder_b = create_folder("课程资料", self.database)["folder_id"]
        self.group = create_document_group("精神现象学", self.database)[
            "document_group_id"
        ]
        move_sources_to_folder(["word-a"], self.folder_a, self.database)
        move_sources_to_folder(["pdf-b"], self.folder_b, self.database)
        assign_sources_to_document_group(
            ["word-a", "pdf-b"], self.group, self.database
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _source_ids(self, result: dict[str, object]) -> set[str]:
        return {
            str(item["source_file_id"])
            for item in result["results"]  # type: ignore[index]
        }

    def test_global_and_root_queries_keep_existing_filter_behavior(self) -> None:
        engine = SearchEngine(self.database)
        try:
            word_results = engine.search(
                "共同关键词", source_type="word", scope_type="all"
            )
            root_results = engine.search("共同关键词", scope_type="root")
        finally:
            engine.close()

        self.assertEqual(self._source_ids(word_results), {"word-a", "word-root"})
        self.assertEqual(self._source_ids(root_results), {"word-root"})

    def test_folder_scope_combines_with_filter_and_search(self) -> None:
        engine = SearchEngine(self.database)
        try:
            result = engine.search(
                "共同关键词",
                source_type="word",
                scope_type="folder",
                scope_id=str(self.folder_a),
            )
        finally:
            engine.close()

        self.assertEqual(self._source_ids(result), {"word-a"})

    def test_document_group_can_span_folders_and_scope_search(self) -> None:
        catalog = load_database_index(self.database)
        members = {
            str(item["source_file_id"]): item
            for item in catalog["source_files"]  # type: ignore[index]
            if item.get("document_group_id") == self.group
        }
        engine = SearchEngine(self.database)
        try:
            result = engine.search(
                "共同关键词",
                scope_type="document_group",
                scope_id=str(self.group),
            )
        finally:
            engine.close()

        self.assertEqual(
            {members["word-a"]["folder_id"], members["pdf-b"]["folder_id"]},
            {self.folder_a, self.folder_b},
        )
        self.assertEqual(self._source_ids(result), {"word-a", "pdf-b"})

    def test_deleting_folder_moves_documents_to_root_without_deleting_data(self) -> None:
        result = delete_folder(self.folder_a, self.database)
        connection = sqlite3.connect(str(self.database))
        try:
            source = connection.execute(
                "SELECT folder_id, document_group_id FROM source_files "
                "WHERE source_file_id = 'word-a'"
            ).fetchone()
            paragraph_count = connection.execute(
                "SELECT COUNT(*) FROM paragraphs WHERE source_file_id = 'word-a'"
            ).fetchone()[0]
            work_count = connection.execute(
                "SELECT COUNT(*) FROM works WHERE work_id = 'word-a-work'"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(result["moved_to_root_count"], 1)
        self.assertEqual(source, (None, self.group))
        self.assertEqual((paragraph_count, work_count), (1, 1))

    def test_deleting_document_group_unlinks_members_without_moving_or_deleting(self) -> None:
        result = delete_document_group(self.group, self.database)
        connection = sqlite3.connect(str(self.database))
        try:
            rows = connection.execute(
                "SELECT source_file_id, folder_id, document_group_id "
                "FROM source_files WHERE source_file_id IN ('word-a', 'pdf-b') "
                "ORDER BY source_file_id"
            ).fetchall()
            source_count = connection.execute(
                "SELECT COUNT(*) FROM source_files"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(result["unlinked_count"], 2)
        self.assertEqual(
            rows,
            [("pdf-b", self.folder_b, None), ("word-a", self.folder_a, None)],
        )
        self.assertEqual(source_count, 3)

    def test_moving_document_changes_only_organization_column(self) -> None:
        connection = sqlite3.connect(str(self.database))
        try:
            before = {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
                for table in ("volumes", "works", "paragraphs", "pdf_pages")
            }
        finally:
            connection.close()

        move_sources_to_folder(["word-a"], self.folder_b, self.database)

        connection = sqlite3.connect(str(self.database))
        try:
            after = {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
                for table in ("volumes", "works", "paragraphs", "pdf_pages")
            }
            folder_id = connection.execute(
                "SELECT folder_id FROM source_files WHERE source_file_id = 'word-a'"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(folder_id, self.folder_b)
        self.assertEqual(after, before)

    def test_version_label_changes_only_source_payload(self) -> None:
        connection = sqlite3.connect(str(self.database))
        try:
            before = {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
                for table in ("volumes", "works", "paragraphs", "pdf_pages")
            }
        finally:
            connection.close()

        result = update_source_version_metadata(
            "word-a", {"version_label": "贺麟译本"}, self.database
        )
        catalog = load_database_index(self.database)
        source = next(
            item
            for item in catalog["source_files"]  # type: ignore[index]
            if item["source_file_id"] == "word-a"
        )
        connection = sqlite3.connect(str(self.database))
        try:
            after = {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
                for table in ("volumes", "works", "paragraphs", "pdf_pages")
            }
        finally:
            connection.close()

        self.assertEqual(result["version_metadata"], {"version_label": "贺麟译本"})
        self.assertEqual(source["version_metadata"], {"version_label": "贺麟译本"})
        self.assertEqual(source["folder_id"], self.folder_a)
        self.assertEqual(source["document_group_id"], self.group)
        self.assertEqual(after, before)

    def test_full_rebuild_preserves_folders_groups_and_memberships(self) -> None:
        update_source_version_metadata(
            "word-a", {"version_label": "贺麟译本"}, self.database
        )
        build_database(_index(), self.database)
        catalog = load_database_index(self.database)
        sources = {
            str(item["source_file_id"]): item
            for item in catalog["source_files"]  # type: ignore[index]
        }

        self.assertEqual(sources["word-a"]["folder_id"], self.folder_a)
        self.assertEqual(sources["pdf-b"]["folder_id"], self.folder_b)
        self.assertEqual(sources["word-a"]["document_group_id"], self.group)
        self.assertEqual(sources["pdf-b"]["document_group_id"], self.group)
        self.assertEqual(
            sources["word-a"]["version_metadata"],
            {"version_label": "贺麟译本"},
        )
        self.assertEqual(len(catalog["folders"]), 2)  # type: ignore[arg-type]
        self.assertEqual(len(catalog["document_groups"]), 1)  # type: ignore[arg-type]

    def test_targeted_reimport_preserves_folder_and_group_membership(self) -> None:
        update_source_version_metadata(
            "word-a", {"version_label": "贺麟译本"}, self.database
        )
        replacement = {
            "metadata": {},
            "source_files": [
                {
                    "source_file_id": "word-a",
                    "source_type": "word",
                    "file_name": "a.docx",
                    "title": "版本甲（更新）",
                }
            ],
            "volumes": [
                {
                    "volume_id": "word-a-volume",
                    "source_file_id": "word-a",
                    "source_type": "word",
                    "display_title": "版本甲（更新）",
                }
            ],
            "works": [],
            "paragraphs": [
                _paragraph("word-a", "word", "更新后的共同关键词")
            ],
        }

        replace_source_in_database(
            replacement,
            self.database,
            backup_existing=False,
        )
        source = next(
            item
            for item in load_database_index(self.database)["source_files"]  # type: ignore[index]
            if item["source_file_id"] == "word-a"
        )

        self.assertEqual(source["folder_id"], self.folder_a)
        self.assertEqual(source["document_group_id"], self.group)
        self.assertEqual(source["version_metadata"], {"version_label": "贺麟译本"})
        self.assertEqual(source["title"], "版本甲（更新）")


class LibraryOrganizationWebTests(unittest.TestCase):
    @contextmanager
    def _server(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            database = root / "data" / "index.sqlite3"
            build_database(_index(), database)
            handler = make_handler(
                database,
                app_context=AppContext.create(root, index_path=database),
            )
            handler.log_message = lambda *_args: None
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server.server_port
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                thread.join(timeout=2)

    @staticmethod
    def _post(port: int, path: str, payload: dict[str, object]):
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps(payload)
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        status = response.status
        connection.close()
        return status, data

    def test_folder_routes_publish_membership_and_safe_delete(self) -> None:
        with self._server() as port:
            status, created = self._post(
                port, "/api/folders/create", {"name": "课程资料"}
            )
            folder_id = created["result"]["folder_id"]
            move_status, _moved = self._post(
                port,
                "/api/documents/move",
                {"source_file_ids": ["word-a"], "folder_id": folder_id},
            )

            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/library?view=summary")
            response = connection.getresponse()
            library = json.loads(response.read().decode("utf-8"))
            connection.close()

            delete_status, deleted = self._post(
                port, "/api/folders/delete", {"folder_id": folder_id}
            )
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/sources")
            response = connection.getresponse()
            catalog = json.loads(response.read().decode("utf-8"))
            connection.close()

        source_in_library = next(
            item for item in library["items"] if item["source_file_id"] == "word-a"
        )
        source_after_delete = next(
            item
            for item in catalog["source_files"]
            if item["source_file_id"] == "word-a"
        )
        self.assertEqual((status, move_status, delete_status), (200, 200, 200))
        self.assertEqual(source_in_library["folder_id"], folder_id)
        self.assertEqual(deleted["result"]["moved_to_root_count"], 1)
        self.assertIsNone(source_after_delete["folder_id"])

    def test_version_label_route_updates_payload_without_reindex(self) -> None:
        with self._server() as port:
            status, created = self._post(
                port, "/api/document-groups/create", {"title": "精神现象学"}
            )
            group_id = created["result"]["document_group_id"]
            assign_status, _assigned = self._post(
                port,
                "/api/document-groups/assign",
                {"source_file_ids": ["pdf-b"], "document_group_id": group_id},
            )
            label_status, _updated = self._post(
                port,
                "/api/document-groups/version-label",
                {"source_file_id": "pdf-b", "version_label": "Miller 英译本"},
            )
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/sources")
            response = connection.getresponse()
            catalog = json.loads(response.read().decode("utf-8"))
            connection.close()

        source = next(
            item
            for item in catalog["source_files"]
            if item["source_file_id"] == "pdf-b"
        )
        self.assertEqual((status, assign_status, label_status), (200, 200, 200))
        self.assertEqual(
            source["version_metadata"], {"version_label": "Miller 英译本"}
        )


if __name__ == "__main__":
    unittest.main()

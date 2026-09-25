"""Zotero 来源同步：本机只读客户端、差异计算、防误删与执行管线。

所有用例都跑在本地 fake server（tests/zotero_fake.py）或内存端口上，不依赖真实
Zotero。删除、导入、题录写入都经由注入的端口记录，验证同步只驱动既有管线。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

from src.me_finder.persistence.zotero_sync_store import (
    ZoteroSyncStore,
    read_zotero_sync_snapshot,
    restore_zotero_sync_snapshot,
)
from src.me_finder.preferences import read_preferences, save_preferences
from src.me_finder.zotero_local_api import ZoteroIncomplete, ZoteroLocalClient
from src.me_finder.zotero_sync import (
    ZoteroSyncPorts,
    ZoteroSyncService,
    effective_collections,
    removal_targets,
    zotero_metadata,
)
from tests.zotero_fake import FakeZotero


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeLibrary:
    """Stands in for the import queue, the index catalog and deletion."""

    def __init__(self) -> None:
        self.sources: Dict[str, Dict] = {}
        self.jobs: Dict[str, Dict] = {}
        self.imported: List[Path] = []
        self.removed: List[List[str]] = []
        self.metadata: List[tuple] = []
        self.pending_files: Dict[str, Path] = {}
        self.capacity = None
        self.resumed: List[str] = []
        self._counter = 0

    def add_source(self, path: Path, *, metadata_source: str = "manual") -> str:
        source_id = f"pdf-import-{_sha(path)[:16]}"
        self.sources[source_id] = {"source_file_id": source_id, "sha256": _sha(path), "metadata_source": metadata_source}
        return source_id

    def import_files(self, paths):
        jobs = []
        for path in paths:
            self._counter += 1
            job_id = f"job-{self._counter}"
            if self.capacity is not None:
                self.capacity -= 1
            self.imported.append(Path(path))
            self.jobs[job_id] = {"job_id": job_id, "status": "processing", "message": "正在解析"}
            self.pending_files[job_id] = Path(path)
            jobs.append({"path": str(path), "job_id": job_id, "source_file_id": "x"})
        return {"ok": True, "jobs": jobs, "errors": []}

    def resume_job(self, job_id):
        if self.capacity is not None:
            if self.capacity < 1:
                raise RuntimeError("导入任务暂时无法进入处理队列")
            self.capacity -= 1
        self.resumed.append(job_id)
        self.jobs[job_id].update(status="processing", phase="parsing", failure_stage=None)
        self.pending_files[job_id] = self.imported[int(job_id.split("-")[1]) - 1]
        return self.jobs[job_id]

    def finish_jobs(self) -> None:
        for job_id, path in list(self.pending_files.items()):
            prefix = "epub" if path.suffix == ".epub" else "pdf-import"
            source_id = f"{prefix}-{_sha(path)[:16]}"
            self.sources[source_id] = {"source_file_id": source_id, "sha256": _sha(path), "metadata_source": "automatic_recognition"}
            self.jobs[job_id]["status"] = "completed"
            del self.pending_files[job_id]

    def remove_documents(self, source_ids):
        self.removed.append(list(source_ids))
        for source_id in source_ids:
            self.sources.pop(source_id, None)
        return {"removed_source_ids": list(source_ids)}

    def apply_metadata(self, source_id, metadata):
        self.metadata.append((source_id, dict(metadata)))
        self.sources[source_id]["metadata_source"] = metadata["metadata_source"]


class ZoteroSyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.files = self.root / "zotero-storage"
        self.files.mkdir()
        self.fake = FakeZotero(server_id=None)
        self.base_url = self.fake.start()
        self.library = FakeLibrary()
        self.prefs_path = self.root / "config" / "preferences.json"
        self.store = ZoteroSyncStore(self.root / "index.sqlite3")
        self.service = self._service()

    def tearDown(self) -> None:
        self.service.stop()
        self.fake.stop()
        self.temp.cleanup()

    def _service(self, *, resumable: bool = True) -> ZoteroSyncService:
        return ZoteroSyncService(
            self.store,
            ZoteroSyncPorts(
                read_preferences=lambda: read_preferences(self.prefs_path),
                sources=lambda: list(self.library.sources.values()),
                import_files=self.library.import_files,
                job_status=lambda job_id: self.library.jobs.get(job_id),
                remove_documents=self.library.remove_documents,
                apply_metadata=self.library.apply_metadata,
                hash_file=_sha,
                import_capacity=lambda: self.library.capacity,
                resume_job=self.library.resume_job if resumable else None,
            ),
            client_factory=lambda server_id: ZoteroLocalClient(self.base_url, timeout=3, server_id=server_id),
        )

    def file(self, name: str, content: str) -> Path:
        path = self.files / name
        path.write_text(content, encoding="utf-8")
        return path

    def select(self, *keys: str, enabled: bool = True) -> None:
        save_preferences({"zotero_sync_enabled": enabled, "zotero_sync_collections": list(keys)}, self.prefs_path)

    def basic_library(self) -> None:
        self.fake.add_collection("MARX", "马克思主义哲学")
        self.fake.add_collection("CAP", "资本论研究", parent="MARX")
        self.fake.add_collection("HEGEL", "黑格尔")
        self.fake.add_item("ITEM1", "资本论", ["CAP"], creators=[{"creatorType": "author", "lastName": "马克思", "firstName": ""}], date="2004")
        self.fake.add_attachment("ATT1", "ITEM1", self.file("capital.pdf", "capital"))
        self.fake.add_item("ITEM2", "精神现象学", ["HEGEL", "CAP"])
        self.fake.add_attachment("ATT2", "ITEM2", self.file("phen.pdf", "phenomenology"))

    def rows(self) -> Dict[str, Dict]:
        return self.store.read().attachments


class LocalClientTests(ZoteroSyncTestCase):
    def test_probe_reports_each_connection_state(self) -> None:
        client = ZoteroLocalClient(self.base_url, timeout=3)
        self.assertEqual(client.probe().state, "connected")
        self.fake.enabled = False
        self.assertEqual(client.probe().state, "api_disabled")
        self.fake.stop()
        self.assertEqual(ZoteroLocalClient(self.base_url, timeout=3).probe().state, "not_running")

    def test_overview_reports_the_real_zotero_version(self) -> None:
        self.assertEqual(self.service.overview()["connection"]["label"], "已连接 Zotero 9.0.6")

    def test_zotero_10_server_id_enables_local_versions(self) -> None:
        self.fake.server_id = "srv1"
        probe = ZoteroLocalClient(self.base_url, timeout=3).probe()
        self.assertTrue(probe.local_versions)
        self.fake.server_id = None
        self.assertFalse(ZoteroLocalClient(self.base_url, timeout=3).probe().local_versions)

    def test_listing_pages_through_every_result(self) -> None:
        self.fake.add_collection("BIG", "大分类")
        for index in range(230):
            self.fake.add_item(f"K{index:05d}", f"书 {index}", ["BIG"])
        items = ZoteroLocalClient(self.base_url, timeout=3).collection_top_items("BIG")
        self.assertEqual(len(items), 230)
        self.assertTrue(any("start=200" in path for path in self.fake.requests))

    def test_total_results_mismatch_is_incomplete(self) -> None:
        self.fake.add_collection("C1", "分类")
        self.fake.add_item("A1", "书", ["C1"])
        self.fake.lie_total_by = 3
        with self.assertRaises(ZoteroIncomplete):
            ZoteroLocalClient(self.base_url, timeout=3).collection_top_items("C1")

    def test_file_url_resolves_to_local_path_and_404_means_none(self) -> None:
        path = self.file("a.pdf", "x")
        self.fake.add_item("P1", "书", [])
        self.fake.add_attachment("F1", "P1", path)
        client = ZoteroLocalClient(self.base_url, timeout=3)
        self.assertEqual(client.attachment_file_path("F1"), path.resolve())
        self.assertIsNone(client.attachment_file_path("NOPE"))

    def test_only_loopback_addresses_are_accepted(self) -> None:
        with self.assertRaises(ValueError):
            ZoteroLocalClient("http://example.org:23119/api")
        with self.assertRaises(ValueError):
            ZoteroLocalClient("https://127.0.0.1:23119/api")

    def test_requests_carry_zotero_headers(self) -> None:
        seen = {}

        def transport(url, headers, timeout):
            seen.update(headers)
            return 200, {"Total-Results": "0"}, b"[]"

        ZoteroLocalClient(transport=transport).collections()
        self.assertEqual(seen["zotero-allowed-request"], "1")
        self.assertEqual(seen["Zotero-API-Version"], "3")
        self.assertFalse(seen["User-Agent"].startswith("Mozilla/"))


class SelectionTests(unittest.TestCase):
    def test_parent_selection_covers_descendants_and_reports_missing(self) -> None:
        collections = [
            {"key": "A", "data": {"name": "A", "parentCollection": False}},
            {"key": "B", "data": {"name": "B", "parentCollection": "A"}},
            {"key": "C", "data": {"name": "C", "parentCollection": "B"}},
            {"key": "D", "data": {"name": "D", "parentCollection": False}},
        ]
        effective, missing = effective_collections(collections, ["A", "GONE"])
        self.assertEqual(effective, {"A", "B", "C"})
        self.assertEqual(missing, ["GONE"])

    def test_removal_never_deletes_documents_that_predate_the_sync(self) -> None:
        rows = {
            "X": {"source_file_id": "s1", "origin": "imported", "status": "linked"},
            "Y": {"source_file_id": "s2", "origin": "linked_existing", "status": "linked"},
            "Z1": {"source_file_id": "s3", "origin": "imported", "status": "linked"},
            "Z2": {"source_file_id": "s3", "origin": "imported", "status": "linked"},
        }
        delete, unlink = removal_targets(rows, ["X", "Y", "Z1"])
        self.assertEqual(delete, ["s1"])
        self.assertEqual(sorted(unlink), ["s2", "s3"])

    def test_zotero_metadata_mapping(self) -> None:
        metadata = zotero_metadata(
            {
                "itemType": "book",
                "title": "Time, Labor, and Social Domination",
                "creators": [
                    {"creatorType": "author", "firstName": "Moishe", "lastName": "Postone"},
                    {"creatorType": "translator", "lastName": "康", "firstName": "敏"},
                ],
                "date": "1993-05",
                "publisher": "Cambridge University Press",
                "place": "Cambridge",
                "ISBN": "9780521391573 0521391571",
            }
        )
        self.assertEqual(metadata["author"], "Moishe Postone")
        self.assertEqual(metadata["translator"], "康敏")
        self.assertEqual(metadata["document_type"], "translated_book")
        self.assertEqual(metadata["publish_year"], "1993")
        self.assertEqual(metadata["isbn"], "9780521391573")
        self.assertEqual(metadata["metadata_source"], "zotero")
        thesis = zotero_metadata({"itemType": "thesis", "title": "论文", "university": "复旦大学"})
        self.assertEqual((thesis["document_type"], thesis["publisher"]), ("thesis", "复旦大学"))


class JasminumMetadataTests(unittest.TestCase):
    """茉莉花插件抓取的知网题录写在 Zotero 标准字段里，同步以它为准。"""

    JOURNAL = {
        "itemType": "journalArticle",
        "title": "论马克思的物化批判",
        "creators": [
            {"creatorType": "author", "lastName": "张", "firstName": "三"},
            {"creatorType": "author", "lastName": "李", "firstName": "四"},
        ],
        "publicationTitle": "哲学研究",
        "volume": "",
        "issue": "3",
        "pages": "66-81+125",
        "date": "2021-03-15",
        "ISSN": "1000-0216",
        "libraryCatalog": "CNKI",
        "extra": "CNKICite: 12",
    }

    def test_jasminum_journal_fields_become_mefinder_metadata(self) -> None:
        from src.me_finder.zotero_sync import trim_item

        data = trim_item({"data": {**self.JOURNAL, "collections": ["C"]}})
        self.assertTrue(data["jasminum"])
        self.assertNotIn("extra", data, "引用次数这类易变字段不进指纹")
        metadata = zotero_metadata(data)
        self.assertEqual(metadata["metadata_source"], "zotero_jasminum")
        self.assertEqual(metadata["document_type"], "journal_article")
        self.assertEqual(metadata["author"], "张三、李四")
        self.assertEqual(metadata["journal_name"], "哲学研究")
        self.assertEqual((metadata["issue"], metadata["page_range"], metadata["publish_year"]), ("3", "66-81+125", "2021"))
        self.assertEqual(metadata["issn"], "1000-0216")

    def test_zotero_values_override_but_empty_fields_do_not_blank(self) -> None:
        from src.me_finder.zotero_sync import merge_metadata

        existing = {
            "title": "自动识别的错题名", "journal_name": "哲学研究", "volume": "7",
            "country": "中", "metadata_source": "automatic_recognition",
            "metadata_evidence": {"title": {"source": "pdf", "value": "自动识别的错题名"}},
        }
        merged = merge_metadata(existing, zotero_metadata(self.JOURNAL))
        self.assertEqual(merged["title"], "论马克思的物化批判")
        self.assertEqual(merged["volume"], "7", "Zotero 留空的字段保留原值")
        self.assertEqual(merged["country"], "中")
        self.assertEqual(merged["metadata_source"], "zotero_jasminum")
        self.assertNotIn("metadata_evidence", merged)

    def test_plain_zotero_item_is_not_labelled_jasminum(self) -> None:
        from src.me_finder.zotero_sync import trim_item

        data = trim_item({"data": {"itemType": "book", "title": "资本论", "libraryCatalog": "Library of Congress", "collections": []}})
        self.assertNotIn("jasminum", data)
        self.assertEqual(zotero_metadata(data)["metadata_source"], "zotero")


class SyncFlowTests(ZoteroSyncTestCase):
    def test_disabled_sync_does_nothing(self) -> None:
        self.basic_library()
        self.select("MARX", enabled=False)
        status = self.service.run_sync()
        self.assertEqual(status["phase"], "idle")
        self.assertEqual(self.library.imported, [])

    def test_new_items_import_once_even_in_two_selected_collections(self) -> None:
        self.basic_library()
        self.select("MARX", "HEGEL")
        status = self.service.run_sync()
        self.assertEqual(status["phase"], "done", status)
        self.assertEqual(sorted(path.name for path in self.library.imported), ["capital.pdf", "phen.pdf"])
        self.assertEqual({row["status"] for row in self.rows().values()}, {"pending"})
        # Parse finishes → the next resolve links both and writes Zotero metadata.
        self.library.finish_jobs()
        self.assertEqual(self.service.resolve_pending(), 2)
        rows = self.rows()
        self.assertEqual({row["status"] for row in rows.values()}, {"linked"})
        self.assertEqual(rows["ATT1"]["parent_item_key"], "ITEM1")
        self.assertEqual({entry[1]["metadata_source"] for entry in self.library.metadata}, {"zotero"})
        self.assertIn("资本论", {entry[1]["title"] for entry in self.library.metadata})
        # A second run with no change imports nothing and removes nothing.
        before = len(self.library.imported)
        status = self.service.run_sync()
        self.assertEqual(status["message"], "没有变化")
        self.assertEqual(len(self.library.imported), before)
        self.assertEqual(self.library.removed, [])

    def test_each_pdf_and_epub_attachment_is_its_own_document(self) -> None:
        self.fake.add_collection("C", "分类")
        self.fake.add_item("BOOK", "双附件", ["C"])
        self.fake.add_attachment("PDF1", "BOOK", self.file("vol1.pdf", "one"))
        self.fake.add_attachment("PDF2", "BOOK", self.file("vol2.pdf", "two"))
        self.fake.add_attachment("EP", "BOOK", self.file("book.epub", "epub"), content_type="application/epub+zip")
        self.fake.add_attachment("SNAP", "BOOK", self.file("page.html", "html"), content_type="text/html")
        self.select("C")
        self.service.run_sync()
        self.assertEqual(sorted(path.name for path in self.library.imported), ["book.epub", "vol1.pdf", "vol2.pdf"])
        rows = self.rows()
        self.assertEqual({rows[key]["parent_item_key"] for key in ("PDF1", "PDF2", "EP")}, {"BOOK"})
        self.assertNotIn("SNAP", rows)

    def test_same_file_already_in_library_is_linked_not_parsed(self) -> None:
        self.basic_library()
        existing = self.library.add_source(self.files / "capital.pdf")
        self.select("CAP")
        self.service.run_sync()
        rows = self.rows()
        self.assertEqual(rows["ATT1"]["status"], "linked")
        self.assertEqual(rows["ATT1"]["source_file_id"], existing)
        self.assertEqual(rows["ATT1"]["origin"], "linked_existing")
        self.assertNotIn("capital.pdf", [path.name for path in self.library.imported])
        # Zotero metadata still wins for a linked document.
        self.assertIn(existing, [entry[0] for entry in self.library.metadata])
        # Leaving the selection only unlinks a document that predates the sync.
        self.select("HEGEL")
        self.service.run_sync()
        self.assertIn(existing, self.library.sources)
        self.assertEqual(self.library.removed, [])
        self.assertNotIn("ATT1", self.rows())

    def test_metadata_change_updates_without_reparsing(self) -> None:
        self.basic_library()
        self.select("HEGEL")
        self.service.run_sync()
        self.library.finish_jobs()
        self.service.resolve_pending()
        imported = len(self.library.imported)
        self.fake.update("ITEM2", title="精神现象学（修订版）", publisher="商务印书馆")
        status = self.service.run_sync()
        self.assertEqual(len(self.library.imported), imported)
        self.assertEqual(self.library.metadata[-1][1]["title"], "精神现象学（修订版）")
        self.assertIn("题录更新 1", status["message"])

    def test_replaced_pdf_is_reparsed_and_old_document_retired(self) -> None:
        self.basic_library()
        self.select("HEGEL")
        self.service.run_sync()
        self.library.finish_jobs()
        self.service.resolve_pending()
        old_source = self.rows()["ATT2"]["source_file_id"]
        (self.files / "phen.pdf").write_text("phenomenology second edition", encoding="utf-8")
        self.fake.update("ATT2", md5="m2")
        status = self.service.run_sync()
        self.assertIn("重新解析 1", status["message"])
        self.assertIn(old_source, self.library.sources, "旧版本在新解析完成前仍可检索")
        self.library.finish_jobs()
        self.service.resolve_pending()
        self.assertNotIn(old_source, self.library.sources)
        self.assertNotEqual(self.rows()["ATT2"]["source_file_id"], old_source)

    def test_signature_change_with_same_content_does_not_reparse(self) -> None:
        self.basic_library()
        self.select("HEGEL")
        self.service.run_sync()
        self.library.finish_jobs()
        self.service.resolve_pending()
        imported = len(self.library.imported)
        self.fake.update("ATT2", mtime=99)
        self.service.run_sync()
        self.assertEqual(len(self.library.imported), imported)

    def test_deleted_or_moved_out_items_are_removed_with_their_documents(self) -> None:
        self.basic_library()
        self.select("MARX")
        self.service.run_sync()
        self.library.finish_jobs()
        self.service.resolve_pending()
        rows = self.rows()
        self.fake.delete("ITEM1")
        self.fake.delete("ATT1")
        self.fake.update("ITEM2", collections=["HEGEL"])
        status = self.service.run_sync()
        self.assertEqual(status["phase"], "done")
        self.assertEqual(sorted(self.library.removed[0]), sorted([rows["ATT1"]["source_file_id"], rows["ATT2"]["source_file_id"]]))
        self.assertEqual(self.rows(), {})

    def test_unchecking_a_collection_removes_after_preview(self) -> None:
        self.basic_library()
        self.select("MARX", "HEGEL")
        self.service.run_sync()
        self.library.finish_jobs()
        self.service.resolve_pending()
        preview = self.service.preview(["HEGEL"])
        self.assertEqual((preview["remove_count"], preview["unlink_count"]), (1, 0))
        self.select("HEGEL")
        self.service.run_sync()
        self.assertEqual(len(self.library.removed[0]), 1)
        self.assertEqual(set(self.rows()), {"ATT2"})

    def test_missing_file_is_unavailable_never_a_deletion(self) -> None:
        self.basic_library()
        self.select("MARX")
        self.service.run_sync()
        self.library.finish_jobs()
        self.service.resolve_pending()
        (self.files / "capital.pdf").unlink()
        self.fake.update("ATT1", md5="gone")
        status = self.service.run_sync()
        self.assertEqual(self.library.removed, [])
        self.assertEqual(self.rows()["ATT1"]["status"], "linked")
        self.assertIn("附件不可用", status["message"])
        # A new attachment whose file is missing is recorded, not imported.
        self.fake.add_item("ITEM3", "缺文件", ["CAP"])
        self.fake.add_attachment("ATT3", "ITEM3", self.files / "never-there.pdf")
        self.service.run_sync()
        self.assertEqual(self.rows()["ATT3"]["status"], "unavailable")

    def test_failed_parse_with_resumable_job_is_not_reimported(self) -> None:
        """失败任务仍在导入队列（可续传）时，手动同步不重导，免得重复消耗 MinerU 额度。"""

        self.basic_library()
        self.select("HEGEL")
        self.service.run_sync()
        job_id = self.rows()["ATT2"]["import_job_id"]
        self.library.jobs[job_id].update(status="failed", message="MinerU 解析失败")
        del self.library.pending_files[job_id]
        self.service.resolve_pending()
        self.assertEqual(self.rows()["ATT2"]["status"], "failed")
        imported = len(self.library.imported)
        self.service.run_sync("manual")
        self.assertEqual(len(self.library.imported), imported)
        # The user dismissed the job in the import page: a manual sync retries.
        del self.library.jobs[job_id]
        self.service.run_sync("manual")
        self.assertEqual(len(self.library.imported), imported + 1)
        # Automatic syncs never retry failures.
        self.library.jobs.clear()
        self.service.run_sync("interval")
        self.assertEqual(len(self.library.imported), imported + 1)

    def four_pdf_collection(self) -> None:
        self.fake.add_collection("BIG", "耶吉")
        for index in range(4):
            self.fake.add_item(f"I{index}", f"论文 {index}", ["BIG"])
            self.fake.add_attachment(f"A{index}", f"I{index}", self.file(f"paper{index}.pdf", f"body {index}"))
        self.select("BIG")

    def test_submission_is_throttled_to_the_import_queue_capacity(self) -> None:
        """队列只剩 2 个位置时只提交 2 篇；其余留待下次同步，不算解析失败。"""

        self.four_pdf_collection()
        self.library.capacity = 2
        self.service.run_sync()
        self.assertEqual(len(self.library.imported), 2)
        rows = self.rows()
        deferred = sorted(
            key for key, row in rows.items()
            if row.get("status") == "pending" and not row.get("import_job_id")
        )
        self.assertEqual(len(deferred), 2)
        for key in deferred:
            self.assertIn("排队已满", str(rows[key].get("status_message")))
        self.assertFalse([key for key, row in rows.items() if row.get("status") == "failed"])

        # The queue drains; the next sync picks the rest up instead of losing them.
        self.library.capacity = None
        self.service.run_sync("manual")
        self.assertEqual(len(self.library.imported), 4)
        self.library.finish_jobs()
        self.service.resolve_pending()
        self.assertCountEqual(
            {row.get("status") for row in self.rows().values()}, {"linked"}
        )

    def test_collection_with_deferred_attachments_is_not_labelled_synced(self) -> None:
        """被节流挡在队列外的条目必须露在"待同步 N 篇"里，不然整栏看着像已完成。"""

        self.four_pdf_collection()
        self.library.capacity = 2
        self.service.run_sync()
        big = next(row for row in self.service.overview()["collections"] if row["key"] == "BIG")
        self.assertEqual(big["unsynced_count"], 2)

        self.library.capacity = None
        self.service.run_sync("manual")
        self.library.finish_jobs()
        self.service.resolve_pending()
        big = next(row for row in self.service.overview()["collections"] if row["key"] == "BIG")
        self.assertEqual(big["unsynced_count"], 0)

    def reject_at_queue(self, key: str) -> str:
        job_id = self.rows()[key]["import_job_id"]
        self.library.jobs[job_id].update(
            status="failed",
            phase="queue_failed",
            failure_stage="queue",
            message="导入任务暂时无法进入处理队列。",
        )
        del self.library.pending_files[job_id]
        self.service.resolve_pending()
        self.assertEqual(self.rows()[key]["status"], "failed")
        return job_id

    def test_queue_rejected_job_is_resumed_in_place_once_the_queue_has_room(self) -> None:
        """排队被拒的任务由后台续跑原任务（导入页不留重复失败项），真解析失败不动。"""

        self.basic_library()
        self.select("CAP")
        self.service.run_sync()
        queued = self.reject_at_queue("ATT1")
        parse_failed = self.rows()["ATT2"]["import_job_id"]
        self.library.jobs[parse_failed].update(status="failed", failure_stage="parse", message="MinerU 解析失败")
        del self.library.pending_files[parse_failed]
        self.service.resolve_pending()
        imported = len(self.library.imported)

        self.library.capacity = 0
        self.assertEqual(self.service.resume_queue_rejected(), 0)
        self.assertEqual(self.rows()["ATT1"]["status"], "failed")

        self.library.capacity = 5
        self.assertEqual(self.service.resume_queue_rejected(), 1)
        self.assertEqual(self.library.resumed, [queued])
        self.assertEqual(self.rows()["ATT1"]["status"], "pending")
        self.assertEqual(self.rows()["ATT2"]["status"], "failed")

        # Even a manual sync does not import the resumed file a second time.
        self.service.run_sync("manual")
        self.assertEqual(len(self.library.imported), imported)
        self.library.finish_jobs()
        self.service.resolve_pending()
        self.assertEqual(self.rows()["ATT1"]["status"], "linked")

    def test_backfill_is_due_only_for_throttled_attachments_with_room(self) -> None:
        """节流留下的条目在队列腾位后由调度器补交，手动同步档也不会永远停在"排队已满"。"""

        now = [1000.0]
        self.service = ZoteroSyncService(
            self.store, self.service._ports, client_factory=self.service._client_factory, clock=lambda: now[0]
        )
        self.four_pdf_collection()
        self.library.capacity = 2
        self.service.run_sync()
        self.assertFalse(self.service._backfill_due())  # just ran
        now[0] += 10 * 60
        self.library.capacity = 0
        self.assertFalse(self.service._backfill_due())  # still no room
        self.library.capacity = 3
        self.assertTrue(self.service._backfill_due())
        self.service.run_sync("backfill")
        self.assertEqual(len(self.library.imported), 4)
        now[0] += 10 * 60
        self.assertFalse(self.service._backfill_due())  # nothing left behind

    def test_queue_failed_attachment_is_reimported_and_labelled_apart(self) -> None:
        """没有续跑端口时，排队被拒的任务由手动同步重导，并说清是排队问题。"""

        self.service.stop()
        self.service = self._service(resumable=False)
        self.basic_library()
        self.select("HEGEL")
        self.service.run_sync()
        job_id = self.rows()["ATT2"]["import_job_id"]
        self.library.jobs[job_id].update(
            status="failed",
            phase="queue_failed",
            failure_stage="queue",
            message="导入任务暂时无法进入处理队列。",
        )
        del self.library.pending_files[job_id]
        self.service.resolve_pending()
        self.assertEqual(self.rows()["ATT2"]["status"], "failed")

        labelled = next(
            row for row in self.service.status()["rows"]
            if "精神现象学" in str(row.get("title"))
        )
        self.assertIn("排队已满", str(labelled["status_text"]))
        self.assertNotIn("解析失败", str(labelled["status_text"]))

        imported = len(self.library.imported)
        self.service.run_sync("manual")
        self.assertEqual(len(self.library.imported), imported + 1)

    def test_linked_file_attachments_are_supported(self) -> None:
        self.fake.add_collection("C", "分类")
        self.fake.add_item("L", "链接文件", ["C"])
        self.fake.add_attachment("LF", "L", self.file("linked.pdf", "linked"), link_mode="linked_file")
        self.fake.add_attachment("URL", "L", self.file("x.pdf", "x"), link_mode="linked_url")
        self.select("C")
        self.service.run_sync()
        self.assertEqual([path.name for path in self.library.imported], ["linked.pdf"])
        self.assertEqual(self.rows()["LF"]["link_mode"], "linked_file")


class RemovalGuardTests(ZoteroSyncTestCase):
    """防误删：任何一次不完整的读取都不能产生移除。"""

    def synced(self) -> None:
        self.basic_library()
        self.select("MARX", "HEGEL")
        self.service.run_sync()
        self.library.finish_jobs()
        self.service.resolve_pending()
        self.assertEqual(len(self.rows()), 2)

    def assert_paused_without_removal(self, status) -> None:
        self.assertEqual(status["phase"], "paused", status)
        self.assertEqual(self.library.removed, [])
        self.assertEqual(len(self.rows()), 2)

    def test_zotero_not_running(self) -> None:
        self.synced()
        self.fake.stop()
        self.assert_paused_without_removal(self.service.run_sync())

    def test_local_api_disabled(self) -> None:
        self.synced()
        self.fake.enabled = False
        self.assert_paused_without_removal(self.service.run_sync())

    def test_request_failure_mid_read(self) -> None:
        self.synced()
        self.fake.fail_paths = ["/collections/HEGEL/items"]
        self.assert_paused_without_removal(self.service.run_sync())
        self.fake.fail_paths = ["/items"]
        self.assert_paused_without_removal(self.service.run_sync())

    def test_incomplete_paging(self) -> None:
        self.synced()
        self.fake.lie_total_by = 1
        self.assert_paused_without_removal(self.service.run_sync())

    def test_abnormal_empty_listing(self) -> None:
        self.synced()
        self.fake.hide_items_in = {"CAP", "HEGEL"}
        self.assert_paused_without_removal(self.service.run_sync())

    def test_empty_collection_list(self) -> None:
        self.synced()
        self.fake.collections.clear()
        self.assert_paused_without_removal(self.service.run_sync())

    def test_selected_collection_vanished(self) -> None:
        self.synced()
        del self.fake.collections["HEGEL"]
        status = self.service.run_sync()
        self.assert_paused_without_removal(status)
        self.assertIn("找不到", status["message"])

    def test_legitimately_emptied_collection_still_removes(self) -> None:
        self.synced()
        self.fake.update("ITEM2", collections=[])
        self.fake.delete("ITEM1")
        self.fake.delete("ATT1")
        status = self.service.run_sync()
        self.assertEqual(status["phase"], "done", status)
        self.assertEqual(len(self.library.removed[0]), 2)

    def test_pending_import_is_not_removed_until_it_finishes(self) -> None:
        self.basic_library()
        self.fake.update("ITEM2", collections=["HEGEL"])
        self.select("HEGEL")
        self.service.run_sync()
        self.select("MARX")
        self.service.run_sync()
        self.assertEqual(self.rows()["ATT2"]["status"], "pending")
        self.library.finish_jobs()
        self.service.run_sync()
        self.assertNotIn("ATT2", self.rows())
        self.assertEqual(len(self.library.removed), 1)


class IncrementalReadTests(ZoteroSyncTestCase):
    def test_zotero_10_versions_fetch_only_changed_items(self) -> None:
        self.fake.server_id = "srv-1"
        self.basic_library()
        self.select("MARX", "HEGEL")
        self.service.run_sync()
        self.library.finish_jobs()
        self.service.resolve_pending()
        self.fake.requests.clear()
        self.fake.update("ITEM2", title="新题名")
        status = self.service.run_sync()
        self.assertEqual(status["phase"], "done")
        full_reads = [path for path in self.fake.requests if "format=json" in path and "itemKey" not in path and "/collections?" not in path]
        self.assertEqual(full_reads, [], "Zotero 10 增量读取不应再整表拉 JSON")
        self.assertTrue(any("itemKey=ITEM2" in path for path in self.fake.requests))
        self.assertEqual(self.library.metadata[-1][1]["title"], "新题名")

    def test_zotero_7_always_reads_in_full(self) -> None:
        self.basic_library()
        self.select("HEGEL")
        self.service.run_sync()
        self.fake.requests.clear()
        self.service.run_sync()
        self.assertTrue(any("/collections/HEGEL/items/top" in path and "format=json" in path for path in self.fake.requests))

    def test_new_server_id_discards_stored_versions(self) -> None:
        self.fake.server_id = "srv-1"
        self.basic_library()
        self.select("HEGEL")
        self.service.run_sync()
        self.fake.server_id = "srv-2"
        self.fake.requests.clear()
        self.service.run_sync()
        self.assertTrue(any("format=json" in path and "/collections/HEGEL/items/top" in path for path in self.fake.requests))


class PersistenceTests(unittest.TestCase):
    def test_rows_survive_a_full_index_rebuild_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "old.sqlite3"
            store = ZoteroSyncStore(old)
            store.write(
                items=[{"item_key": "I", "item_version": 3, "fingerprint": "f", "data_json": "{}", "collections_json": "[\"C\"]"}],
                attachments=[{"attachment_key": "A", "parent_item_key": "I", "link_mode": "imported_file", "origin": "imported", "status": "linked", "source_file_id": "s"}],
                state={"server_id": "srv", "synced_collections_json": json.dumps(["C"])},
            )
            snapshot = read_zotero_sync_snapshot(old)
            fresh = Path(directory) / "fresh.sqlite3"
            connection = sqlite3.connect(str(fresh))
            restore_zotero_sync_snapshot(connection, snapshot)
            connection.commit()
            connection.close()
            state = ZoteroSyncStore(fresh).read()
            self.assertEqual(state.attachments["A"]["source_file_id"], "s")
            self.assertEqual(state.synced_collections, ["C"])
            self.assertEqual(state.server_id, "srv")

    def test_preferences_validate_zotero_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            saved = save_preferences({"zotero_sync_enabled": True, "zotero_sync_collections": ["ABCD1234", "ABCD1234"], "zotero_sync_frequency": "interval"}, path)
            self.assertEqual(saved["zotero_sync_collections"], ["ABCD1234"])
            self.assertEqual(saved["zotero_sync_frequency"], "interval")
            with self.assertRaises(ValueError):
                save_preferences({"zotero_sync_frequency": "hourly"}, path)
            with self.assertRaises(ValueError):
                save_preferences({"zotero_sync_collections": ["../x"]}, path)
            with self.assertRaises(ValueError):
                save_preferences({"zotero_sync_enabled": "yes"}, path)


if __name__ == "__main__":
    unittest.main()

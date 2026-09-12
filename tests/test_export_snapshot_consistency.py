"""Export must not write to the library and must read one consistent snapshot.

Reproduction tests for the export write-back hazards:

* the lazy heading enrichment inside export blindly rewrote full stale
  payloads, reverting concurrent bibliographic edits;
* export read source rows and page payloads on separate connections, so a
  concurrent metadata save or deletion could produce a mixed-version file or
  an empty shell that still reported success.

The fixed contract: enrichment is a separate, coordinated application
operation; the export service functions are strictly read-only and take every
DB read from one explicit read transaction.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

from src.me_finder.application.document_heading_enrichment import (
    DocumentHeadingEnrichment,
    ensure_document_headings,
)
from src.me_finder.bibliographic_metadata import update_metadata_in_database
from src.me_finder.document_export_service import (
    export_indexed_pdf,
    export_indexed_pdf_markdown,
)
from src.me_finder.database import build_database


def _snapshot_fixture(root: Path) -> tuple[Path, str]:
    """One indexed two-page PDF whose payloads carry an explicit version."""

    source_id = "source-snapshot-1"
    source_path = root / "corpus" / "raw_pdf" / "一致性导出.pdf"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"source bytes")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    pages = [
        {
            "source_file_id": source_id,
            "pdf_page_index": index,
            "physical_pdf_page": index + 1,
            "pdf_page_number_1based": index + 1,
            "text_raw": f"版本甲正文第{index + 1}页",
            "blocks": [{"text_level": None, "text": f"版本甲正文第{index + 1}页"}],
            "parser": "mineru",
        }
        for index in range(2)
    ]
    database = root / "data" / "index.sqlite3"
    build_database(
        {
            "metadata": {},
            "source_files": [
                {
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "document_id": "DOCUMENT_SNAPSHOT_1",
                    "file_name": source_path.name,
                    "relative_path": "corpus/raw_pdf/一致性导出.pdf",
                    "file_format": "pdf",
                    "size_bytes": source_path.stat().st_size,
                    "sha256": digest,
                    "display_title": "一致性导出",
                    "bibliographic_metadata": {
                        "title": "一致性导出",
                        "author": "某作者",
                    },
                    "pdf_profile": {"pdf_page_count": 2, "parser": "mineru"},
                }
            ],
            "volumes": [
                {
                    "volume_id": "VOLUME_SNAPSHOT_1",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "volume_number": 1,
                    "display_title": "一致性导出",
                }
            ],
            "pdf_pages": pages,
        },
        database,
    )
    return database, source_id


def _source_payload(database: Path, source_id: str) -> dict:
    import sqlite3

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM source_files WHERE source_file_id = ?",
            (source_id,),
        ).fetchone()
    return json.loads(row[0])


def _page_payloads(database: Path, source_id: str) -> list[dict]:
    import sqlite3

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM pdf_pages WHERE source_file_id = ? "
            "ORDER BY pdf_page_index",
            (source_id,),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _concurrent_rewriter(database: Path, source_id: str):
    """A writer that plays the role of a metadata save plus re-parsed pages."""

    def rewrite() -> None:
        import sqlite3

        source = _source_payload(database, source_id)
        source["bibliographic_metadata"]["title"] = "并发修改后的标题"
        pages = _page_payloads(database, source_id)
        # A logical document version must be published in one transaction.
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE source_files SET payload_json=? WHERE source_file_id=?",
                (json.dumps(source, ensure_ascii=False), source_id),
            )
            for page in pages:
                page["text_raw"] = page["text_raw"].replace("版本甲", "版本乙")
                page["blocks"][0]["text"] = page["text_raw"]
                connection.execute(
                    "UPDATE pdf_pages SET payload_json = ? "
                    "WHERE source_file_id = ? AND pdf_page_index = ?",
                    (json.dumps(page, ensure_ascii=False), source_id, int(page["pdf_page_index"])),
                )

    return rewrite


class ExportWriteSeparationTests(unittest.TestCase):
    """The export service functions must never write to the database."""

    def test_markdown_export_does_not_backfill_heading_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = _snapshot_fixture(root)
            with mock.patch(
                "src.me_finder.application.document_heading_enrichment.enrich_pdf_headings",
                return_value={"classification": "none"},
            ) as fake_enrichment:
                export_indexed_pdf_markdown(
                    database_path=database,
                    source_file_id=source_id,
                    output_dir=root / "exports",
                    runtime_root=root,
                )
                fake_enrichment.assert_not_called()
            self.assertNotIn(
                "document_heading_profile",
                _source_payload(database, source_id),
                "export wrote heading enrichment into the library",
            )


class EnrichmentNeverOverwritesNewDataTests(unittest.TestCase):
    """A concurrent bibliographic save must survive heading enrichment."""

    def test_enrichment_write_keeps_concurrent_metadata(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_enrichment(pages, pdf_path, segments, root=None):
            started.set()
            self.assertTrue(release.wait(10), "enrichment compute was not released")
            return {"classification": "none"}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = _snapshot_fixture(root)
            with mock.patch(
                "src.me_finder.application.document_heading_enrichment.enrich_pdf_headings",
                side_effect=slow_enrichment,
            ):
                outcome: dict = {}

                def run_enrichment() -> None:
                    outcome["profile"] = ensure_document_headings(
                        database_path=database,
                        runtime_root=root,
                        source_file_id=source_id,
                    )

                worker = threading.Thread(target=run_enrichment)
                worker.start()
                self.assertTrue(started.wait(10), "enrichment never reached compute")
                update_metadata_in_database(
                    database,
                    source_id,
                    {"title": "并发修改后的标题", "author": "某作者"},
                )
                release.set()
                worker.join(10)

            payload = _source_payload(database, source_id)
            self.assertEqual(
                payload["bibliographic_metadata"]["title"],
                "并发修改后的标题",
                "stale enrichment payload reverted a concurrent metadata save",
            )


class ExportSnapshotConsistencyTests(unittest.TestCase):
    """Export output must never mix two database versions."""

    def test_markdown_export_reports_one_version_during_metadata_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            database, source_id = _snapshot_fixture(temp_dir)
            rewrite = _concurrent_rewriter(database, source_id)
            page_read_started = threading.Event()
            original = document_export_service_module()._text_export_pages

            def hooked(connection, source_id_arg, runtime_root):
                page_read_started.set()
                return original(connection, source_id_arg, runtime_root)

            writer = threading.Thread(target=rewrite)
            writer.start()
            with mock.patch.object(
                document_export_service_module(), "_text_export_pages", hooked
            ):
                result = export_indexed_pdf_markdown(
                    database_path=database,
                    source_file_id=source_id,
                    output_dir=Path(temp_dir) / "exports",
                    runtime_root=temp_dir,
                )
            self.assertTrue(page_read_started.wait(5))
            writer.join(10)
            self.assertFalse(writer.is_alive(), "concurrent writer deadlocked")
            content = Path(result["path"]).read_text(encoding="utf-8")
            exported_new_pages = "版本乙" in content
            exported_new_title = "并发修改后的标题" in content
            self.assertFalse(
                exported_new_pages and not exported_new_title,
                "mixed-version export: new page payloads with the old title",
            )
            self.assertFalse(
                exported_new_title and not exported_new_pages,
                "mixed-version export: new title with old page payloads",
            )
            # The concurrent writer must never be lost.
            payload = _source_payload(database, source_id)
            pages = _page_payloads(database, source_id)
            self.assertEqual(payload["bibliographic_metadata"]["title"], "并发修改后的标题")
            self.assertTrue(all("版本乙" in page["text_raw"] for page in pages))

    def test_markdown_export_reports_one_version_during_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            database, source_id = _snapshot_fixture(temp_dir)
            page_read_started = threading.Event()
            original = document_export_service_module()._text_export_pages

            def delete_document() -> None:
                import sqlite3

                if not page_read_started.wait(5):
                    return
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "DELETE FROM pdf_pages WHERE source_file_id = ?", (source_id,)
                    )
                    connection.execute(
                        "DELETE FROM source_files WHERE source_file_id = ?", (source_id,)
                    )

            def hooked(connection, source_id_arg, runtime_root):
                page_read_started.set()
                return original(connection, source_id_arg, runtime_root)

            writer = threading.Thread(target=delete_document)
            writer.start()
            with mock.patch.object(
                document_export_service_module(), "_text_export_pages", hooked
            ):
                result = export_indexed_pdf_markdown(
                    database_path=database,
                    source_file_id=source_id,
                    output_dir=Path(temp_dir) / "exports",
                    runtime_root=temp_dir,
                )
            self.assertTrue(page_read_started.wait(5))
            writer.join(10)
            self.assertFalse(writer.is_alive(), "deletion deadlocked against the export")
            content = Path(result["path"]).read_text(encoding="utf-8")
            # Either the full pre-deletion document, never an empty shell.
            self.assertIn("版本甲正文第1页", content)
            self.assertIn("版本甲正文第2页", content)

    def test_zip_export_manifest_and_pages_share_one_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            database, source_id = _snapshot_fixture(temp_dir)
            rewrite = _concurrent_rewriter(database, source_id)
            module = document_export_service_module()
            original = module.iter_indexed_pdf_pages
            page_stream_started = threading.Event()

            def hooked(*args, **kwargs):
                page_stream_started.set()
                yield from original(*args, **kwargs)

            writer = threading.Thread(target=rewrite)
            writer.start()
            with mock.patch.object(module, "iter_indexed_pdf_pages", hooked):
                result = export_indexed_pdf(
                    database_path=database,
                    runtime_root=temp_dir,
                    source_file_id=source_id,
                    output_dir=Path(temp_dir) / "exports",
                )
            self.assertTrue(page_stream_started.wait(5))
            writer.join(10)
            self.assertFalse(writer.is_alive(), "concurrent writer deadlocked")
            with zipfile.ZipFile(result["path"]) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                page_blobs = archive.read("pages.ndjson")
            title_in_manifest = manifest["document"]["title"]
            exported_new_pages = "版本乙".encode() in page_blobs
            exported_new_title = title_in_manifest == "并发修改后的标题"
            self.assertFalse(
                exported_new_pages and not exported_new_title,
                "mixed-version zip export: new pages with the old manifest title",
            )


def document_export_service_module():
    import src.me_finder.document_export_service as module

    return module


if __name__ == "__main__":
    unittest.main()


class _RecordingCoordinationPort:
    """Records enter/exit of one coordination region."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def _region(self):
        import contextlib

        @contextlib.contextmanager
        def region():
            self.entered += 1
            try:
                yield
            finally:
                self.exited += 1
        return region()

    def operation(self):
        return self._region()

    def mutation(self):
        return self._region()


class CoordinatedEnrichmentOperationTests(unittest.TestCase):
    """Enrichment runs as its own operation inside the existing write gates."""

    def test_completed_heading_preparation_does_not_wait_for_alignment_mutation(self):
        from src.me_finder.document_heading import DOCUMENT_HEADING_VERSION
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, source_id = _snapshot_fixture(root)
            with sqlite3.connect(database) as db:
                source = json.loads(db.execute('SELECT payload_json FROM source_files').fetchone()[0])
                source['document_heading_profile'] = {'version': DOCUMENT_HEADING_VERSION, 'status': 'complete'}
                db.execute('UPDATE source_files SET payload_json=?', (json.dumps(source),))
            port = mock.Mock()
            port.mutation.side_effect = AssertionError('unnecessary wait on alignment mutation')
            operation = DocumentHeadingEnrichment(database_path=database, runtime_root=root, index_runtime=port)
            self.assertEqual(operation.enrich(source_id)['status'], 'complete')
            port.mutation.assert_not_called()

    def test_epub_export_does_not_enter_alignment_mutation(self):
        from scripts.performance_fixture import create_fixture
        from src.me_finder.archive_transfer_controller import ArchiveTransferController
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_fixture(root, documents=2, paragraphs=12, alignment_paragraphs=8)
            database = root / 'data/index.sqlite3'
            port = mock.Mock()
            port.mutation.side_effect = AssertionError('EPUB export must not wait for alignment')
            enrichment = DocumentHeadingEnrichment(database_path=database, runtime_root=root, index_runtime=port)
            controller = ArchiveTransferController(
                mock.Mock(), database_path=database, runtime_root=root,
                document_output_dir=root / 'exports', prepare_document_export=enrichment.enrich)
            status, body = controller.export_document_markdown({'source_id': 'bench-002'})
            self.assertEqual(status, 200, body)
            self.assertTrue(list((root / 'exports').glob('*.md')))
            port.mutation.assert_not_called()

    def test_heading_computation_runs_before_write_coordination(self):
        from src.me_finder.application import document_heading_enrichment as module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, source_id = _snapshot_fixture(root)
            port = _RecordingCoordinationPort()
            original = module.enrich_pdf_headings
            observed = []
            def compute(*args, **kwargs):
                observed.append(port.entered)
                return original(*args, **kwargs)
            with mock.patch.object(module, 'enrich_pdf_headings', side_effect=compute):
                DocumentHeadingEnrichment(database_path=database, runtime_root=root, index_runtime=port).enrich(source_id)
            self.assertEqual(port.entered, 1)
            self.assertEqual(observed, [0])

    def test_enrichment_operation_enters_write_coordination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = _snapshot_fixture(root)
            durable = _RecordingCoordinationPort()
            index_runtime = _RecordingCoordinationPort()
            operation = DocumentHeadingEnrichment(
                database_path=database,
                runtime_root=root,
                durable_operations=durable,
                index_runtime=index_runtime,
            )
            profile = operation.enrich(source_id)
            self.assertIn(profile["status"], {"complete", "unavailable", "partial"})
            self.assertEqual((durable.entered, durable.exited), (1, 1))
            self.assertEqual((index_runtime.entered, index_runtime.exited), (1, 1))

    def test_controller_prepares_export_and_survives_preparation_failure(self) -> None:
        from src.me_finder.archive_transfer_controller import ArchiveTransferController

        class _NoBackup:
            def export(self, *, output_dir=None):
                return {}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = _snapshot_fixture(root)
            prepared: list[str] = []

            def prepare(source_id: str) -> None:
                prepared.append(source_id)

            def failing_prepare(source_id: str) -> None:
                raise RuntimeError("enrichment backend unavailable")

            controller = ArchiveTransferController(
                _NoBackup(),
                database_path=database,
                runtime_root=root,
                document_output_dir=root / "exports",
                prepare_document_export=prepare,
            )
            status, body = controller.export_document_markdown({"source_id": source_id})
            self.assertEqual(status, 200)
            self.assertEqual(prepared, [source_id])

            failing_controller = ArchiveTransferController(
                _NoBackup(),
                database_path=database,
                runtime_root=root,
                document_output_dir=root / "exports",
                prepare_document_export=failing_prepare,
            )
            status, body = failing_controller.export_document_markdown(
                {"source_id": source_id}
            )
            self.assertEqual(status, 200, "a failed preparation must not block export")

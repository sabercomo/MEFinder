from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from src.me_finder.app_context import AppPaths
from src.me_finder.application.document_import_coordinator import (
    DocumentImportCoordinator,
)
from src.me_finder.chunked_upload import ChunkedUploadError, ChunkedUploadStore
from src.me_finder.import_resume import sha256_file
from src.me_finder.mineru_api import MinerUError, resolve_mineru_config_path
from src.me_finder.mineru_local_settings import save_mineru_local_config


class FakeImportJobs:
    def __init__(self) -> None:
        self.import_calls = []
        self.native_batches = []
        self.remote_batches = []
        self.released = []
        self.released_batches = []
        self.cleanup_calls = []
        self.processing_sources = set()
        self.referenced_targets = set()
        self.next_job_number = 0

    def register_pdf_for_import(self, target, *, original_file_name=None):
        source_file_id = f"pdf-import-{sha256_file(Path(target))[:16]}"
        return ({"source_file_id": source_file_id}, source_file_id, Path(target))

    def release_import_reservation(self, source_file_id):
        self.released.append(source_file_id)

    def release_item_reservations(self, items):
        self.released_batches.append(
            [
                str(item["source_file_id"])
                for item in items
                if item.get("source_reserved")
            ]
        )

    def cleanup_unreferenced_import_target(self, candidate):
        self.cleanup_calls.append(candidate)
        if candidate is None:
            return False
        target = Path(candidate)
        if target in self.referenced_targets:
            return False
        target.unlink(missing_ok=True)
        return True

    def start_import_job(
        self,
        target,
        profile,
        source_file_id,
        is_pdf,
        force_mineru=False,
        vision_provider_id=None,
        consume_reservation=False,
        display_file_name=None,
    ):
        call = {
            "target": Path(target),
            "profile": dict(profile),
            "source_file_id": source_file_id,
            "is_pdf": is_pdf,
            "force_mineru": force_mineru,
            "vision_provider_id": vision_provider_id,
            "consume_reservation": consume_reservation,
            "display_file_name": display_file_name,
        }
        self.import_calls.append(call)
        self.referenced_targets.add(Path(target))
        return "import-one"

    def job_for_source(self, source_file_id, *, statuses=()):
        if source_file_id in self.processing_sources:
            return {"status": "processing"}
        return None

    def start_native_import_batch(self, items):
        snapshot = [dict(item) for item in items]
        self.native_batches.append(snapshot)
        return self._batch_job_ids(items, "native")

    def start_remote_import_batch(self, items):
        snapshot = [dict(item) for item in items]
        self.remote_batches.append(snapshot)
        return self._batch_job_ids(items, "remote")

    def _batch_job_ids(self, items, prefix):
        job_ids = []
        for item in items:
            self.next_job_number += 1
            self.referenced_targets.add(Path(item["target"]))
            job_ids.append(f"{prefix}-{self.next_job_number}")
        return job_ids


class DocumentImportCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = AppPaths.create(self.root)
        self.jobs = FakeImportJobs()
        self.admission_events = []

        @contextmanager
        def admission():
            self.admission_events.append("enter")
            try:
                yield
            finally:
                self.admission_events.append("exit")

        self.coordinator = DocumentImportCoordinator(
            self.paths,
            self.jobs,
            admission=admission,
            detect_pdf=lambda _path: {
                "detected_pdf_type": "scanned",
                "pdf_page_count": 1,
            },
        )

    def tearDown(self) -> None:
        self.coordinator.close()
        self.temp_dir.cleanup()

    def enable_local_mineru(self) -> None:
        save_mineru_local_config(
            {
                "enabled": True,
                "endpoint": "http://127.0.0.1:8000",
                "backend": "pipeline",
            },
            resolve_mineru_config_path(self.root),
        )

    def test_stream_upload_is_stored_then_queued_with_existing_response(self) -> None:
        payload = b"%PDF-1.4\nbody\n%%EOF\n"

        result = self.coordinator.import_stream(
            "folder/通典.pdf",
            len(payload),
            io.BytesIO(payload),
            pdf_parse_mode="auto",
        )

        self.assertEqual(
            result,
            {
                "ok": True,
                "job_id": "import-one",
                "file_name": "通典.pdf",
                "source_file_id": (
                    "pdf-import-"
                    f"{sha256_file(self.jobs.import_calls[0]['target'])[:16]}"
                ),
                "detected_pdf_type": "scanned",
                "parse_route": "mineru",
                "provider_id": None,
            },
        )
        stored = list((self.root / "corpus" / "raw_pdf").glob("*.pdf"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].read_bytes(), payload)
        self.assertEqual(
            list((self.root / "corpus" / "raw_pdf").glob(".*.tmp")),
            [],
        )
        self.assertEqual(
            self.jobs.import_calls[0]["display_file_name"],
            "folder/通典.pdf",
        )
        self.assertEqual(self.admission_events, ["enter", "exit"])

    def test_auto_stream_reports_local_ocr_route_when_component_is_available(self) -> None:
        payload = b"%PDF-1.4\nbody\n%%EOF\n"

        with mock.patch(
            "src.me_finder.application.document_import_coordinator.local_ocr_available",
            return_value=True,
        ):
            result = self.coordinator.import_stream(
                "scan.pdf",
                len(payload),
                io.BytesIO(payload),
                pdf_parse_mode="auto",
            )

        self.assertEqual(result["parse_route"], "local_ocr")
        self.assertFalse(self.jobs.import_calls[0]["force_mineru"])
        self.assertIsNone(self.jobs.import_calls[0]["vision_provider_id"])

    def test_incomplete_stream_removes_temp_file_and_reservation(self) -> None:
        with self.assertRaisesRegex(MinerUError, "上传数据不完整"):
            self.coordinator.import_stream(
                "paper.pdf",
                5,
                io.BytesIO(b"abc"),
            )

        directory = self.root / "corpus" / "raw_pdf"
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(self.jobs.import_calls, [])

    def test_stream_validates_type_options_and_size_with_original_messages(
        self,
    ) -> None:
        with self.assertRaisesRegex(MinerUError, "只支持 PDF 或 DOCX"):
            self.coordinator.import_stream("paper.txt", 1, io.BytesIO(b"x"))
        with self.assertRaisesRegex(MinerUError, "PDF 解析方式无效"):
            self.coordinator.import_stream(
                "paper.pdf",
                1,
                io.BytesIO(b"x"),
                pdf_parse_mode="invalid",
            )
        with self.assertRaisesRegex(
            MinerUError,
            "请选择一个其他解析 API",
        ):
            self.coordinator.import_stream(
                "paper.pdf",
                1,
                io.BytesIO(b"x"),
                pdf_parse_mode="vision",
            )
        with self.assertRaisesRegex(MinerUError, "请先在设置中启用"):
            self.coordinator.import_stream(
                "paper.pdf",
                1,
                io.BytesIO(b"x"),
                pdf_parse_mode="mineru-local",
            )
        with self.assertRaisesRegex(MinerUError, "文件为空"):
            self.coordinator.import_stream("paper.pdf", 0, io.BytesIO())

    def test_stream_can_start_directly_with_enabled_local_mineru(self) -> None:
        self.enable_local_mineru()
        payload = b"%PDF-1.4\nlocal\n%%EOF\n"

        result = self.coordinator.import_stream(
            "local.pdf",
            len(payload),
            io.BytesIO(payload),
            pdf_parse_mode="mineru-local",
        )

        self.assertEqual(result["parse_route"], "mineru")
        self.assertEqual(result["provider_id"], "mineru-local")
        call = self.jobs.import_calls[0]
        self.assertTrue(call["force_mineru"])
        self.assertIsNone(call["vision_provider_id"])
        self.assertTrue(call["profile"]["mineru_local"])

    def test_chunked_upload_lifecycle_preserves_payload_and_metadata(self) -> None:
        payload = b"%PDF-1.4\nchunked\n%%EOF\n"
        started = self.coordinator.start_chunked(
            "folder/通典.pdf",
            len(payload),
            pdf_parse_mode="vision",
            vision_provider_id="provider-one",
        )
        upload_id = str(started["upload_id"])
        first = self.coordinator.append_chunk(
            upload_id,
            0,
            8,
            io.BytesIO(payload[:8]),
        )
        second = self.coordinator.append_chunk(
            upload_id,
            8,
            len(payload) - 8,
            io.BytesIO(payload[8:]),
        )
        finished = self.coordinator.finish_chunked(upload_id)

        self.assertTrue(started["ok"])
        self.assertEqual(started["file_name"], "通典.pdf")
        self.assertFalse(first["complete"])
        self.assertTrue(second["complete"])
        self.assertEqual(finished["parse_route"], "vision")
        self.assertEqual(finished["provider_id"], "provider-one")
        self.assertEqual(
            list((self.paths.corpus_root / ".upload-staging").glob("*.part")),
            [],
        )
        stored = list((self.paths.corpus_root / "raw_pdf").glob("*.pdf"))
        self.assertEqual(stored[0].read_bytes(), payload)
        self.assertEqual(
            self.admission_events,
            ["enter", "exit"] * 4,
        )

    def test_chunked_cancel_and_close_remove_staged_files(self) -> None:
        first = self.coordinator.start_chunked("one.pdf", 2)
        second = self.coordinator.start_chunked("two.pdf", 2)
        staging = self.paths.corpus_root / ".upload-staging"
        self.assertEqual(len(list(staging.glob("*.part"))), 2)
        self.assertEqual(self.coordinator.active_session_count(), 2)
        self.assertTrue(self.coordinator.has_active_uploads())

        cancelled = self.coordinator.cancel_chunked(str(first["upload_id"]))
        self.assertEqual(cancelled, {"ok": True, "cancelled": True})
        self.assertEqual(len(list(staging.glob("*.part"))), 1)
        self.assertEqual(self.coordinator.active_session_count(), 1)

        self.coordinator.close()
        self.assertEqual(list(staging.glob("*.part")), [])
        self.assertEqual(self.coordinator.active_session_count(), 0)
        self.assertFalse(self.coordinator.has_active_uploads())
        self.assertIn("upload_id", second)

    def test_chunked_start_allows_files_larger_than_3_5_gib(self) -> None:
        total_size = 7 * 512 * 1024 * 1024
        started = self.coordinator.start_chunked(
            "paper.pdf",
            total_size,
        )

        self.assertTrue(started["ok"])
        self.assertEqual(started["total_size"], total_size)

    def test_finished_upload_is_cleaned_if_type_detection_fails(self) -> None:
        coordinator = DocumentImportCoordinator(
            self.paths,
            self.jobs,
            detect_pdf=lambda _path: (_ for _ in ()).throw(
                MinerUError("detection failed")
            ),
        )
        try:
            payload = b"pdf-body"
            started = coordinator.start_chunked("paper.pdf", len(payload))
            upload_id = str(started["upload_id"])
            coordinator.append_chunk(
                upload_id,
                0,
                len(payload),
                io.BytesIO(payload),
            )

            with self.assertRaisesRegex(MinerUError, "detection failed"):
                coordinator.finish_chunked(upload_id)

            self.assertEqual(
                list((self.paths.corpus_root / ".upload-staging").glob("*.part")),
                [],
            )
            self.assertEqual(
                list((self.paths.corpus_root / "raw_pdf").glob("*.pdf")),
                [],
            )
        finally:
            coordinator.close()

    def test_finished_staging_file_is_removed_when_metadata_is_invalid(self) -> None:
        store = ChunkedUploadStore(
            self.paths.corpus_root / ".invalid-upload-staging"
        )
        started = store.start(
            "paper.pdf",
            3,
            metadata={
                "is_pdf": "1",
                "parse_mode": "invalid",
                "provider_id": "",
            },
        )
        upload_id = str(started["upload_id"])
        store.append(upload_id, 0, 3, io.BytesIO(b"pdf"))
        coordinator = DocumentImportCoordinator(
            self.paths,
            self.jobs,
            chunked_uploads=store,
        )
        try:
            with self.assertRaisesRegex(MinerUError, "PDF 解析方式无效"):
                coordinator.finish_chunked(upload_id)

            self.assertEqual(list(store.directory.glob("*.part")), [])
            self.assertEqual(
                list((self.paths.corpus_root / "raw_pdf").glob("*.pdf")),
                [],
            )
        finally:
            coordinator.close()

    def test_admission_rejection_happens_before_stream_storage(self) -> None:
        @contextmanager
        def reject_admission():
            raise MinerUError("imports unavailable")
            yield

        coordinator = DocumentImportCoordinator(
            self.paths,
            self.jobs,
            admission=reject_admission,
        )
        try:
            with self.assertRaisesRegex(MinerUError, "imports unavailable"):
                coordinator.import_stream(
                    "paper.pdf",
                    3,
                    io.BytesIO(b"pdf"),
                )

            self.assertFalse(self.paths.corpus_root.exists())
            self.assertEqual(self.jobs.import_calls, [])
        finally:
            coordinator.close()

    def test_local_import_groups_work_but_preserves_response_order(self) -> None:
        scan_root = self.root / "library"
        scan_root.mkdir()
        scanned = scan_root / "scanned.pdf"
        word = scan_root / "notes.docx"
        native = scan_root / "native.pdf"
        outside = self.root / "outside.pdf"
        scanned.write_bytes(b"scanned")
        word.write_bytes(b"word")
        native.write_bytes(b"native")
        outside.write_bytes(b"outside")

        coordinator = DocumentImportCoordinator(
            self.paths,
            self.jobs,
            detect_pdf=lambda path: {
                "detected_pdf_type": (
                    "native_text" if Path(path).name == "native.pdf" else "scanned"
                )
            },
        )
        try:
            result = coordinator.import_local(
                [scanned, word, native, outside],
                [scan_root],
            )
        finally:
            coordinator.close()

        self.assertEqual(
            [item["file_name"] for item in result["jobs"]],
            ["scanned.pdf", "notes.docx", "native.pdf"],
        )
        self.assertEqual(
            [item["parse_route"] for item in result["jobs"]],
            ["mineru", None, "native"],
        )
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["path"], str(outside))
        self.assertIn("不在已配置", result["errors"][0]["error"])
        self.assertEqual(
            [item["display_file_name"] for item in self.jobs.native_batches[0]],
            ["native.pdf"],
        )
        self.assertEqual(
            [item["display_file_name"] for item in self.jobs.native_batches[1]],
            ["notes.docx"],
        )
        self.assertEqual(
            [item["display_file_name"] for item in self.jobs.remote_batches[0]],
            ["scanned.pdf"],
        )

    def test_local_duplicate_pdf_is_reported_and_second_copy_is_cleaned(self) -> None:
        scan_root = self.root / "library"
        scan_root.mkdir()
        first = scan_root / "first.pdf"
        second = scan_root / "second.pdf"
        first.write_bytes(b"same")
        second.write_bytes(b"same")

        result = self.coordinator.import_local(
            [first, second],
            [scan_root],
        )

        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("同一批次中已有内容相同", result["errors"][0]["error"])
        stored = list((self.paths.corpus_root / "raw_pdf").glob("*.pdf"))
        self.assertEqual(len(stored), 1)

    def test_local_batch_can_start_directly_with_enabled_local_mineru(self) -> None:
        self.enable_local_mineru()
        scan_root = self.root / "local-library"
        scan_root.mkdir()
        source = scan_root / "local.pdf"
        source.write_bytes(b"local-mineru")

        result = self.coordinator.import_local(
            [source],
            [scan_root],
            pdf_parse_mode="mineru-local",
        )

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["jobs"][0]["provider_id"], "mineru-local")
        item = self.jobs.remote_batches[0][0]
        self.assertTrue(item["force_mineru"])
        self.assertTrue(item["profile"]["mineru_local"])

    def test_local_processing_conflict_releases_pdf_and_keeps_other_jobs(self) -> None:
        scan_root = self.root / "library"
        scan_root.mkdir()
        blocked = scan_root / "blocked.pdf"
        word = scan_root / "notes.docx"
        blocked.write_bytes(b"blocked")
        word.write_bytes(b"word")
        blocked_source_id = f"pdf-import-{sha256_file(blocked)[:16]}"
        self.jobs.processing_sources.add(blocked_source_id)

        result = self.coordinator.import_local(
            [blocked, word],
            [scan_root],
        )

        self.assertEqual(
            [item["file_name"] for item in result["jobs"]],
            ["notes.docx"],
        )
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("已有解析任务正在运行", result["errors"][0]["error"])
        self.assertEqual(self.jobs.released, [blocked_source_id])
        self.assertEqual(
            list((self.paths.corpus_root / "raw_pdf").glob("*.pdf")),
            [],
        )
        self.assertEqual(
            len(list((self.paths.corpus_root / "raw_docx").glob("*.docx"))),
            1,
        )


if __name__ == "__main__":
    unittest.main()

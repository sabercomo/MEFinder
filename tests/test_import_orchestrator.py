from __future__ import annotations

import json
import tempfile
import threading
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Sequence, Tuple
from unittest import mock

from src.me_finder.app_context import AppPaths
from src.me_finder.application.backup_coordinator import BackupCoordinator
from src.me_finder.application.import_job_store import (
    ImportJobCancelled as StoreImportJobCancelled,
    ImportJobStore,
)
from src.me_finder.application.import_job_lifecycle import (
    ImportJobCleanupFailed,
    ImportJobRetrySwapFailed,
)
from src.me_finder.application.import_orchestrator import (
    ImportJobCancelled as OrchestratorImportJobCancelled,
    ImportOrchestrator,
)
from src.me_finder.import_job_journal import ImportJobJournal
from src.me_finder.import_queue import ImportQueueFullError
from src.me_finder.import_resume import sha256_file
from src.me_finder.lifecycle import DurableOperationGate
from src.me_finder.mineru_api import MinerUError
from src.me_finder.mineru_local_settings import save_mineru_local_config


class _FakeIndexRuntime:
    def __init__(self) -> None:
        self.rebuild_calls: List[Tuple[Callable[..., object], Sequence[str]]] = []
        self.replace_calls: List[Tuple[Mapping[str, object], str, bool]] = []
        self.mutation_entries = 0
        self.missing_source_ids: set[str] = set()
        self.replace_failure: Exception | None = None

    @contextmanager
    def mutation(self) -> Iterator[None]:
        self.mutation_entries += 1
        yield

    def rebuild(
        self,
        on_progress: Callable[[Dict[str, object]], None],
        expected_source_ids: Sequence[str] = (),
    ) -> set[str]:
        self.rebuild_calls.append((on_progress, tuple(expected_source_ids)))
        on_progress({"phase": "rebuilding_index"})
        return set(self.missing_source_ids)

    def replace_source(
        self,
        extracted: Mapping[str, object],
        expected_source_id: str,
        *,
        backup_existing: bool = False,
    ) -> None:
        self.replace_calls.append(
            (extracted, expected_source_id, bool(backup_existing))
        )
        if self.replace_failure is not None:
            raise self.replace_failure


class _FakeQueue:
    def __init__(self) -> None:
        self.tasks: List[Tuple[Callable[..., None], Tuple[object, ...]]] = []
        self.failure: Exception | None = None

    def submit(self, task: Callable[..., None], *args: object) -> None:
        if self.failure is not None:
            raise self.failure
        self.tasks.append((task, args))

    def run_next(self) -> None:
        task, args = self.tasks.pop(0)
        task(*args)


class ImportOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "data").mkdir()
        self.paths = AppPaths.create(
            self.root,
            index_path=self.root / "data" / "index.sqlite3",
        )
        self.runtime = _FakeIndexRuntime()
        self.queue = _FakeQueue()
        self.journal = ImportJobJournal(
            self.root / "corpus" / "processed" / "import_jobs"
        )
        self.mineru_calls = 0
        self.provider_calls = 0

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _parse_mineru(self, *_args: object, **_kwargs: object) -> None:
        self.mineru_calls += 1

    def _parse_provider(self, *_args: object, **_kwargs: object) -> None:
        self.provider_calls += 1

    @staticmethod
    def _unused_extract(*_args: object, **_kwargs: object) -> Dict[str, object]:
        raise AssertionError("PDF extraction should not run")

    @staticmethod
    def _unused_metadata(_source_id: str) -> Dict[str, object]:
        raise AssertionError("metadata detection should not run")

    @staticmethod
    def _unused_persist(
        _source_id: str,
        _payload: Dict[str, object],
    ) -> Dict[str, object]:
        raise AssertionError("metadata persistence should not run")

    def _orchestrator(
        self,
        *,
        parse_mineru: Callable[..., object] | None = None,
        extract_pdf: Callable[..., Dict[str, object]] | None = None,
        job_store: ImportJobStore | None = None,
    ) -> ImportOrchestrator:
        return ImportOrchestrator(
            self.paths,
            self.runtime,
            DurableOperationGate(),
            self.queue,
            self.journal,
            parse_with_mineru=parse_mineru or self._parse_mineru,
            parse_with_provider=self._parse_provider,
            extract_pdf=extract_pdf or self._unused_extract,
            detect_metadata=self._unused_metadata,
            persist_metadata=self._unused_persist,
            job_store=job_store,
        )

    def _target(self, name: str = "sample.docx") -> Path:
        target = self.root / name
        if target.suffix.lower() == ".docx":
            document_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>document body</w:t></w:r></w:p></w:body>'
                '</w:document>'
            )
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("word/document.xml", document_xml)
        else:
            target.write_bytes(b"document-body")
        return target

    def test_startup_restores_without_submitting_work(self) -> None:
        target = self._target()
        self.journal.save_job(
            {
                "job_id": "import-restored",
                "status": "processing",
                "phase": "text_parsing",
                "message": "正在解析",
            },
            target=target,
            source_file_id="word-restored",
            profile={"detected_pdf_type": "docx"},
            is_pdf=False,
        )

        orchestrator = self._orchestrator()

        self.assertEqual(self.queue.tasks, [])
        restored = orchestrator.job_status("import-restored")
        self.assertIsNotNone(restored)
        self.assertEqual(restored["status"], "paused")
        self.assertTrue(restored["can_resume"])
        self.assertEqual(
            [item["job_id"] for item in orchestrator.resumable_import_jobs()],
            ["import-restored"],
        )

    def test_scanned_auto_job_records_local_ocr_route_when_available(self) -> None:
        target = self._target("scan.pdf")
        orchestrator = self._orchestrator()

        with mock.patch(
            "src.me_finder.application.import_orchestrator.local_ocr_available",
            return_value=True,
        ):
            job, context = orchestrator._build_import_job(
                target,
                {"detected_pdf_type": "scanned"},
                "pdf-scan",
                True,
            )

        self.assertEqual(job["parse_route"], "local_ocr")
        self.assertFalse(context["force_mineru"])
        self.assertIsNone(context["vision_provider_id"])

    def test_online_mineru_failure_exposes_local_retry_only_after_opt_in(self) -> None:
        orchestrator = self._orchestrator()
        job = {
            "job_id": "failed-mineru",
            "status": "failed",
            "phase": "failed",
            "parse_route": "mineru",
            "mineru_failed": True,
        }

        self.assertFalse(
            orchestrator.public_import_job(job)["can_retry_with_local_mineru"]
        )
        save_mineru_local_config(
            {
                "enabled": True,
                "endpoint": "http://127.0.0.1:8000",
                "backend": "pipeline",
            },
            self.root / "config" / "mineru_api.local.json",
        )

        self.assertTrue(
            orchestrator.public_import_job(job)["can_retry_with_local_mineru"]
        )
        local_failure = dict(job, provider_id="mineru-local")
        self.assertFalse(
            orchestrator.public_import_job(local_failure)[
                "can_retry_with_local_mineru"
            ]
        )

    def test_word_import_completes_through_injected_runtime(self) -> None:
        orchestrator = self._orchestrator()
        target = self._target()

        job_id = orchestrator.start_import_job(
            target,
            {"detected_pdf_type": "docx"},
            "word-one",
            False,
        )

        self.assertEqual(len(self.queue.tasks), 1)
        self.queue.run_next()
        status = orchestrator.job_status(job_id)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["phase"], "completed")
        self.assertEqual(status["message"], "导入完成，已自动更新索引")
        self.assertEqual(len(self.runtime.replace_calls), 1)
        self.assertEqual(self.runtime.rebuild_calls, [])
        self.assertIsNone(self.journal.get_job(job_id))
        self.assertEqual(self.mineru_calls, 0)
        self.assertEqual(self.provider_calls, 0)

    def test_queue_failure_is_durable_and_resumable(self) -> None:
        self.queue.failure = ImportQueueFullError("queue full")
        orchestrator = self._orchestrator()
        target = self._target()

        job_id = orchestrator.start_import_job(
            target,
            {"detected_pdf_type": "docx"},
            "word-queue-full",
            False,
        )

        status = orchestrator.job_status(job_id)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["phase"], "queue_failed")
        self.assertEqual(status["failure_stage"], "queue")
        self.assertTrue(status["can_resume"])
        self.assertIsNotNone(self.journal.get_job(job_id))
        self.assertEqual(
            [item["job_id"] for item in orchestrator.resumable_import_jobs()],
            [job_id],
        )

    def test_backup_restore_and_pdf_cleanup_follow_config_then_store_order(
        self,
    ) -> None:
        orchestrator = self._orchestrator()
        raw_pdf = self.root / "corpus" / "raw_pdf"
        raw_pdf.mkdir(parents=True)
        target = raw_pdf / "cleanup.pdf"
        target.write_bytes(b"%PDF-cleanup")
        backup_path = self.root / "backup.zip"
        backup_path.write_bytes(b"backup")
        config_guard = threading.RLock()
        backup_in_restore = threading.Event()
        cleanup_attempted_config = threading.Event()
        config = {"documents": []}
        cleanup_results: list[bool] = []

        @contextmanager
        def config_lock() -> Iterator[None]:
            if threading.current_thread().name == "cleanup-order":
                cleanup_attempted_config.set()
            if not config_guard.acquire(timeout=1):
                raise RuntimeError("config lock timed out")
            try:
                yield
            finally:
                config_guard.release()

        @contextmanager
        def locked_config(_path: Path) -> Iterator[Dict[str, object]]:
            with config_lock():
                yield config

        def restore(*_args: object, **_kwargs: object) -> Dict[str, object]:
            config["documents"] = [{"file_name": target.name}]
            backup_in_restore.set()
            if not cleanup_attempted_config.wait(timeout=1):
                raise RuntimeError("cleanup never attempted config lock")
            return {"count": 1}

        backup = BackupCoordinator(
            self.paths,
            self.runtime,
            DurableOperationGate(),
            orchestrator,
            app_data_root=lambda: self.root / "app-data",
            restore=restore,
            config_lock=config_lock,
        )
        job_id = "restore-cleanup-order"
        orchestrator.register_background_job(
            {"job_id": job_id, "status": "processing"}
        )
        backup_thread = threading.Thread(
            target=backup._run_restore_job,
            args=(job_id, backup_path),
            name="backup-order",
            daemon=True,
        )

        def cleanup() -> None:
            cleanup_results.append(
                orchestrator.cleanup_unreferenced_import_target(target)
            )

        with mock.patch(
            "src.me_finder.application.import_orchestrator.locked_import_config",
            side_effect=locked_config,
        ):
            backup_thread.start()
            self.assertTrue(backup_in_restore.wait(timeout=1))
            cleanup_thread = threading.Thread(
                target=cleanup,
                name="cleanup-order",
                daemon=True,
            )
            cleanup_thread.start()
            backup_thread.join(timeout=2)
            cleanup_thread.join(timeout=2)

        self.assertFalse(backup_thread.is_alive())
        self.assertFalse(cleanup_thread.is_alive())
        self.assertEqual(cleanup_results, [False])
        self.assertTrue(target.is_file())
        self.assertEqual(orchestrator.job_status(job_id)["status"], "completed")

    def test_backup_restore_and_pdf_registration_follow_config_then_store_order(
        self,
    ) -> None:
        store = ImportJobStore()
        orchestrator = self._orchestrator(job_store=store)
        raw_pdf = self.root / "corpus" / "raw_pdf"
        raw_pdf.mkdir(parents=True)
        target = raw_pdf / "register.pdf"
        target.write_bytes(b"%PDF-register")
        backup_path = self.root / "backup.zip"
        backup_path.write_bytes(b"backup")
        config_guard = threading.RLock()
        backup_in_restore = threading.Event()
        register_attempted_config = threading.Event()
        registration_results: list[Tuple[Dict[str, object], str, Path]] = []

        @contextmanager
        def config_lock() -> Iterator[None]:
            if threading.current_thread().name == "register-order":
                register_attempted_config.set()
            if not config_guard.acquire(timeout=1):
                raise RuntimeError("config lock timed out")
            try:
                yield
            finally:
                config_guard.release()

        def restore(*_args: object, **_kwargs: object) -> Dict[str, object]:
            backup_in_restore.set()
            if not register_attempted_config.wait(timeout=1):
                raise RuntimeError("registration never attempted config lock")
            return {"count": 1}

        def register(*_args: object, **_kwargs: object) -> Dict[str, object]:
            with config_lock():
                return {
                    "source_file_id": "pdf-registered-order",
                    "file_name": target.name,
                }

        backup = BackupCoordinator(
            self.paths,
            self.runtime,
            DurableOperationGate(),
            orchestrator,
            app_data_root=lambda: self.root / "app-data",
            restore=restore,
            config_lock=config_lock,
        )
        job_id = "restore-register-order"
        orchestrator.register_background_job(
            {"job_id": job_id, "status": "processing"}
        )
        backup_thread = threading.Thread(
            target=backup._run_restore_job,
            args=(job_id, backup_path),
            name="backup-register-order",
            daemon=True,
        )

        def register_target() -> None:
            registration_results.append(
                orchestrator.register_pdf_for_import(target)
            )

        with mock.patch(
            "src.me_finder.application.import_orchestrator.import_config_lock",
            side_effect=config_lock,
        ), mock.patch(
            "src.me_finder.application.import_orchestrator.register_pdf",
            side_effect=register,
        ), mock.patch(
            "src.me_finder.application.import_orchestrator.reuse_registered_pdf_copy",
            return_value=target,
        ):
            backup_thread.start()
            self.assertTrue(backup_in_restore.wait(timeout=1))
            register_thread = threading.Thread(
                target=register_target,
                name="register-order",
                daemon=True,
            )
            register_thread.start()
            backup_thread.join(timeout=2)
            register_thread.join(timeout=2)

        self.assertFalse(backup_thread.is_alive())
        self.assertFalse(register_thread.is_alive())
        self.assertEqual(registration_results[0][1], "pdf-registered-order")
        self.assertEqual(orchestrator.job_status(job_id)["status"], "completed")
        with self.assertRaisesRegex(MinerUError, "正在准备导入"):
            store.reserve_source("pdf-registered-order")
        predicted_source_id = f"pdf-import-{sha256_file(target)[:16]}"
        store.reserve_source(predicted_source_id)
        store.release_reservation(predicted_source_id)

    def test_retry_new_journal_failure_keeps_old_job_and_never_enqueues(
        self,
    ) -> None:
        self.queue.failure = ImportQueueFullError("queue full")
        orchestrator = self._orchestrator()
        target = self._target("retry.pdf")
        profile = {"detected_pdf_type": "scanned"}
        old_job_id = orchestrator.start_import_job(
            target,
            profile,
            "pdf-retry",
            True,
            force_mineru=True,
        )
        self.queue.failure = None

        with mock.patch.object(
            self.journal,
            "save_job",
            side_effect=OSError("new journal failed"),
        ):
            with self.assertRaisesRegex(OSError, "new journal failed"):
                orchestrator.start_retry_import_job(
                    old_job_id,
                    target,
                    profile,
                    "pdf-retry",
                    True,
                    previous_statuses=("failed",),
                    force_mineru=True,
                )

        self.assertEqual(self.queue.tasks, [])
        self.assertEqual(orchestrator.job_status(old_job_id)["status"], "failed")
        self.assertIsNotNone(self.journal.get_job(old_job_id))
        self.assertEqual(
            [job["job_id"] for job in self.journal.list_jobs()],
            [old_job_id],
        )

    def test_retry_old_delete_failure_rolls_back_new_job_without_enqueue(
        self,
    ) -> None:
        self.queue.failure = ImportQueueFullError("queue full")
        orchestrator = self._orchestrator()
        target = self._target("retry-delete.pdf")
        profile = {"detected_pdf_type": "scanned"}
        old_job_id = orchestrator.start_import_job(
            target,
            profile,
            "pdf-retry-delete",
            True,
            force_mineru=True,
        )
        self.queue.failure = None
        original_delete = self.journal.delete_job

        def fail_old_delete(job_id: str) -> bool:
            if job_id == old_job_id:
                raise OSError("old delete failed")
            return original_delete(job_id)

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=fail_old_delete,
        ):
            with self.assertRaisesRegex(OSError, "old delete failed"):
                orchestrator.start_retry_import_job(
                    old_job_id,
                    target,
                    profile,
                    "pdf-retry-delete",
                    True,
                    previous_statuses=("failed",),
                    force_mineru=True,
                )

        self.assertEqual(self.queue.tasks, [])
        self.assertEqual(orchestrator.job_status(old_job_id)["status"], "failed")
        self.assertEqual(
            [job["job_id"] for job in self.journal.list_jobs()],
            [old_job_id],
        )

    def test_retry_rollback_failure_is_reported_and_never_enqueues(
        self,
    ) -> None:
        self.queue.failure = ImportQueueFullError("queue full")
        orchestrator = self._orchestrator()
        target = self._target("retry-rollback.pdf")
        profile = {"detected_pdf_type": "scanned"}
        old_job_id = orchestrator.start_import_job(
            target,
            profile,
            "pdf-retry-rollback",
            True,
            force_mineru=True,
        )
        self.queue.failure = None

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=OSError("filesystem read-only"),
        ), self.assertLogs(level="ERROR") as logs:
            with self.assertRaisesRegex(
                ImportJobRetrySwapFailed,
                "filesystem read-only",
            ):
                orchestrator.start_retry_import_job(
                    old_job_id,
                    target,
                    profile,
                    "pdf-retry-rollback",
                    True,
                    previous_statuses=("failed",),
                    force_mineru=True,
                )

        self.assertEqual(self.queue.tasks, [])
        self.assertIn("roll back retry replacement", "\n".join(logs.output))
        journal_jobs = self.journal.list_jobs()
        self.assertEqual(len(journal_jobs), 2)
        new_job_id = next(
            str(job["job_id"])
            for job in journal_jobs
            if str(job["job_id"]) != old_job_id
        )
        self.assertEqual(orchestrator.job_status(old_job_id)["status"], "failed")
        self.assertIsNone(orchestrator.job_status(new_job_id))

    def test_retry_lineage_restores_only_committed_third_generation(
        self,
    ) -> None:
        self.queue.failure = ImportQueueFullError("queue full")
        orchestrator = self._orchestrator()
        target = self._target("retry-lineage.pdf")
        profile = {"detected_pdf_type": "scanned"}
        old_job_id = orchestrator.start_import_job(
            target,
            profile,
            "pdf-retry-lineage",
            True,
            force_mineru=True,
        )
        self.queue.failure = None

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=OSError("first attempt cleanup failed"),
        ), self.assertLogs(level="ERROR"):
            with self.assertRaises(ImportJobRetrySwapFailed):
                orchestrator.start_retry_import_job(
                    old_job_id,
                    target,
                    profile,
                    "pdf-retry-lineage",
                    True,
                    previous_statuses=("failed",),
                    force_mineru=True,
                )

        orphan_job_id = next(
            str(job["job_id"])
            for job in self.journal.list_jobs()
            if str(job["job_id"]) != old_job_id
        )
        committed_job_id = orchestrator.start_retry_import_job(
            old_job_id,
            target,
            profile,
            "pdf-retry-lineage",
            True,
            previous_statuses=("failed",),
            force_mineru=True,
        )
        self.assertEqual(len(self.queue.tasks), 1)
        self.assertIsNone(self.journal.get_job(old_job_id))
        self.assertIsNone(self.journal.get_job(orphan_job_id))
        self.assertEqual(
            self.journal.get_job(committed_job_id)["replacement_generation"],
            2,
        )
        orchestrator.update_import_job(
            committed_job_id,
            status="failed",
            phase="failed",
            failure_stage="parse",
            can_resume=True,
        )
        third_job_id = orchestrator.start_retry_import_job(
            committed_job_id,
            target,
            profile,
            "pdf-retry-lineage",
            True,
            previous_statuses=("failed",),
            force_mineru=True,
        )
        third_record = self.journal.get_job(third_job_id)
        self.assertEqual(third_record["replacement_lineage_id"], old_job_id)
        self.assertEqual(third_record["replacement_generation"], 3)
        self.assertEqual(len(self.queue.tasks), 2)
        self.assertIsNone(self.journal.get_job(committed_job_id))

        self.queue = _FakeQueue()
        restarted = self._orchestrator()

        self.assertEqual(self.queue.tasks, [])
        self.assertIsNone(restarted.job_status(old_job_id))
        self.assertIsNone(restarted.job_status(orphan_job_id))
        self.assertIsNone(restarted.job_status(committed_job_id))
        self.assertEqual(restarted.job_status(third_job_id)["status"], "paused")
        self.assertIsNone(self.journal.get_job(orphan_job_id))
        self.assertIsNone(self.journal.get_job(committed_job_id))
        self.assertIsNotNone(self.journal.get_job(third_job_id))

    def test_retry_cannot_commit_while_orphan_cleanup_still_fails(
        self,
    ) -> None:
        self.queue.failure = ImportQueueFullError("queue full")
        orchestrator = self._orchestrator()
        target = self._target("retry-orphan-cleanup.pdf")
        profile = {"detected_pdf_type": "scanned"}
        old_job_id = orchestrator.start_import_job(
            target,
            profile,
            "pdf-retry-orphan-cleanup",
            True,
            force_mineru=True,
        )
        self.queue.failure = None
        original_delete = self.journal.delete_job

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=OSError("initial rollback failed"),
        ), self.assertLogs(level="ERROR"):
            with self.assertRaises(ImportJobRetrySwapFailed):
                orchestrator.start_retry_import_job(
                    old_job_id,
                    target,
                    profile,
                    "pdf-retry-orphan-cleanup",
                    True,
                    previous_statuses=("failed",),
                    force_mineru=True,
                )
        orphan_job_id = next(
            str(job["job_id"])
            for job in self.journal.list_jobs()
            if str(job["job_id"]) != old_job_id
        )

        def fail_orphan_delete(job_id: str) -> bool:
            if job_id == orphan_job_id:
                raise OSError("orphan cleanup failed")
            return original_delete(job_id)

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=fail_orphan_delete,
        ):
            with self.assertRaisesRegex(OSError, "orphan cleanup failed"):
                orchestrator.start_retry_import_job(
                    old_job_id,
                    target,
                    profile,
                    "pdf-retry-orphan-cleanup",
                    True,
                    previous_statuses=("failed",),
                    force_mineru=True,
                )

        self.assertEqual(self.queue.tasks, [])
        self.assertIsNotNone(self.journal.get_job(old_job_id))
        self.assertIsNotNone(self.journal.get_job(orphan_job_id))
        self.assertEqual(orchestrator.job_status(old_job_id)["status"], "failed")

    def test_completed_replacement_leaves_no_orphan_to_restore(self) -> None:
        self.queue.failure = ImportQueueFullError("queue full")
        orchestrator = self._orchestrator()
        target = self._target("retry-completed-lineage.pdf")
        profile = {"detected_pdf_type": "scanned"}
        old_job_id = orchestrator.start_import_job(
            target,
            profile,
            "pdf-retry-completed-lineage",
            True,
            force_mineru=True,
        )
        self.queue.failure = None

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=OSError("initial rollback failed"),
        ), self.assertLogs(level="ERROR"):
            with self.assertRaises(ImportJobRetrySwapFailed):
                orchestrator.start_retry_import_job(
                    old_job_id,
                    target,
                    profile,
                    "pdf-retry-completed-lineage",
                    True,
                    previous_statuses=("failed",),
                    force_mineru=True,
                )
        orphan_job_id = next(
            str(job["job_id"])
            for job in self.journal.list_jobs()
            if str(job["job_id"]) != old_job_id
        )
        completed_job_id = orchestrator.start_retry_import_job(
            old_job_id,
            target,
            profile,
            "pdf-retry-completed-lineage",
            True,
            previous_statuses=("failed",),
            force_mineru=True,
        )
        self.assertIsNone(self.journal.get_job(orphan_job_id))
        orchestrator.update_import_job(
            completed_job_id,
            status="completed",
            phase="completed",
        )
        self.assertEqual(self.journal.list_jobs(), [])

        self.queue = _FakeQueue()
        restarted = self._orchestrator()

        self.assertEqual(self.queue.tasks, [])
        self.assertIsNone(restarted.job_status(orphan_job_id))
        self.assertIsNone(restarted.job_status(completed_job_id))

    def test_index_only_resume_does_not_call_a_parser(self) -> None:
        self.queue.failure = ImportQueueFullError("queue full")
        orchestrator = self._orchestrator()
        target = self._target()
        job_id = orchestrator.start_import_job(
            target,
            {"detected_pdf_type": "docx"},
            "word-index-retry",
            False,
        )
        orchestrator.update_import_job(
            job_id,
            status="failed",
            phase="index_failed",
            failure_stage="index",
            can_resume=True,
            message="索引更新失败",
        )
        self.queue.failure = None

        resumed = orchestrator.resume_import_job(job_id)
        self.assertEqual(resumed["phase"], "rebuilding_index")
        self.assertEqual(len(self.queue.tasks), 1)
        self.queue.run_next()

        self.assertEqual(orchestrator.job_status(job_id)["status"], "completed")
        self.assertEqual(len(self.runtime.rebuild_calls), 1)
        self.assertEqual(self.mineru_calls, 0)
        self.assertEqual(self.provider_calls, 0)

    def test_resume_rejects_changed_source_content(self) -> None:
        self.queue.failure = ImportQueueFullError("queue full")
        orchestrator = self._orchestrator()
        target = self._target()
        job_id = orchestrator.start_import_job(
            target,
            {"detected_pdf_type": "docx"},
            "word-changed",
            False,
        )
        target.write_bytes(b"replacement-body")
        self.queue.failure = None

        with self.assertRaisesRegex(MinerUError, "内容已经变化"):
            orchestrator.resume_import_job(job_id)

        status = orchestrator.job_status(job_id)
        self.assertEqual(status["status"], "failed")
        self.assertFalse(status["can_resume"])
        self.assertEqual(self.queue.tasks, [])

    def test_active_dismiss_is_completed_by_the_worker(self) -> None:
        orchestrator = self._orchestrator()
        target = self._target()
        job_id = orchestrator.start_import_job(
            target,
            {"detected_pdf_type": "docx"},
            "word-cancelled",
            False,
        )

        self.assertEqual(orchestrator.dismiss_import_job(job_id), "cancelling")
        self.assertEqual(orchestrator.job_status(job_id)["status"], "cancelling")
        self.queue.run_next()

        self.assertIsNone(orchestrator.job_status(job_id))
        self.assertIsNone(self.journal.get_job(job_id))
        self.assertEqual(self.runtime.rebuild_calls, [])

    def test_failure_transition_cannot_overwrite_worker_cancellation(self) -> None:
        orchestrator = self._orchestrator()
        target = self._target()
        job_id = orchestrator.start_import_job(
            target,
            {"detected_pdf_type": "docx"},
            "word-cancelled-before-failure",
            False,
        )

        def cancel_then_fail(current_job_id: str, _target: Path) -> str:
            self.assertEqual(
                orchestrator.dismiss_import_job(current_job_id),
                "cancelling",
            )
            raise RuntimeError("rebuild failed after cancellation")

        with mock.patch.object(
            orchestrator,
            "index_text_document",
            side_effect=cancel_then_fail,
        ):
            self.queue.run_next()

        self.assertIsNone(orchestrator.job_status(job_id))
        self.assertIsNone(self.journal.get_job(job_id))

    def test_source_deletion_coordination_is_atomic_and_purges_old_jobs(
        self,
    ) -> None:
        self.queue.failure = ImportQueueFullError("queue full")
        orchestrator = self._orchestrator()
        target = self._target()
        job_id = orchestrator.start_import_job(
            target,
            {"detected_pdf_type": "docx"},
            "word-delete",
            False,
        )

        orchestrator.begin_source_deletion("word-delete")
        with self.assertRaisesRegex(MinerUError, "正在删除"):
            orchestrator.start_import_job(
                target,
                {"detected_pdf_type": "docx"},
                "word-delete",
                False,
            )
        self.assertEqual(orchestrator.purge_source_jobs(["word-delete"]), [])
        self.assertIsNone(orchestrator.job_status(job_id))
        self.assertIsNone(self.journal.get_job(job_id))
        orchestrator.end_source_deletion("word-delete")

    def test_active_job_blocks_source_deletion(self) -> None:
        orchestrator = self._orchestrator()
        target = self._target()
        orchestrator.start_import_job(
            target,
            {"detected_pdf_type": "docx"},
            "word-active",
            False,
        )

        with self.assertRaisesRegex(MinerUError, "仍在解析中"):
            orchestrator.begin_source_deletion("word-active")

    def test_index_missing_source_keeps_display_name_in_error(self) -> None:
        raw_pdf = self.root / "corpus" / "raw_pdf"
        raw_pdf.mkdir(parents=True)
        (self.root / "config").mkdir()
        (raw_pdf / "stored.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (self.root / "config" / "pdf_imports.json").write_text(
            json.dumps(
                {
                    "documents": [
                        {
                            "source_file_id": "pdf-one",
                            "document_id": "pdf-one-doc",
                            "file_name": "stored.pdf",
                            "original_file_name": "论文原名.pdf",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        def extract_pdf(*_args: object, **_kwargs: object) -> Dict[str, object]:
            return {"source_files": [{"source_file_id": "pdf-one"}]}

        self.runtime.replace_failure = RuntimeError(
            "写入后未找到文献记录：pdf-one"
        )
        orchestrator = self._orchestrator(extract_pdf=extract_pdf)

        with self.assertRaisesRegex(
            MinerUError,
            "论文原名.pdf 未能进入索引：写入后未找到文献记录",
        ):
            orchestrator.index_registered_pdf("job-one", "pdf-one")

    def test_narrow_job_queries_return_public_snapshots(self) -> None:
        orchestrator = self._orchestrator()
        background_job = {
            "job_id": "batchmeta-one",
            "source_file_id": "source-one",
            "status": "processing",
            "phase": "metadata_recognition",
            "progress": {"resume": {"completed_pages": [1]}},
        }
        orchestrator.register_background_job(background_job)
        background_job["progress"]["resume"]["completed_pages"].append(2)
        orchestrator.register_background_job(
            {
                "job_id": "failed-one",
                "source_file_id": "source-one",
                "status": "failed",
                "phase": "failed",
            }
        )

        processing = orchestrator.job_for_source(
            "source-one", statuses=("processing",)
        )
        failed = orchestrator.job_for_source(
            "source-one", statuses=("failed",)
        )
        prefixed = orchestrator.processing_job_with_prefix("batchmeta-")

        self.assertEqual(processing["job_id"], "batchmeta-one")
        self.assertEqual(failed["job_id"], "failed-one")
        self.assertEqual(prefixed["job_id"], "batchmeta-one")
        processing["status"] = "changed"
        processing["progress"]["resume"]["completed_pages"].append(3)
        self.assertEqual(
            orchestrator.job_status("batchmeta-one")["status"],
            "processing",
        )
        self.assertEqual(
            orchestrator.job_status("batchmeta-one")["progress"]["resume"][
                "completed_pages"
            ],
            [1],
        )

    def test_injected_store_owns_state_and_cancel_exception_is_reexported(
        self,
    ) -> None:
        store = ImportJobStore()
        orchestrator = self._orchestrator(job_store=store)

        orchestrator.register_background_job(
            {
                "job_id": "background-one",
                "source_file_id": "source-one",
                "status": "processing",
            }
        )

        self.assertEqual(
            store.job_snapshot("background-one")["status"],
            "processing",
        )
        self.assertIs(OrchestratorImportJobCancelled, StoreImportJobCancelled)

    def test_journal_failures_preserve_state_transition_order(self) -> None:
        store = ImportJobStore()
        orchestrator = self._orchestrator(job_store=store)
        store.restore_job(
            "inactive",
            self._stored_job("inactive", "failed"),
            self._stored_context("inactive"),
        )

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=OSError("delete failed"),
        ):
            with self.assertRaisesRegex(OSError, "delete failed"):
                orchestrator.dismiss_import_job("inactive")
        self.assertIsNotNone(store.job_snapshot("inactive"))

        store.restore_job(
            "route",
            {
                **self._stored_job("route", "failed"),
                "parse_route": "mineru",
            },
            {
                **self._stored_context("route"),
                "force_mineru": True,
            },
        )
        with mock.patch.object(
            self.journal,
            "switch_parser_route",
            side_effect=OSError("switch failed"),
        ):
            with self.assertRaisesRegex(OSError, "switch failed"):
                orchestrator.switch_import_job_route(
                    "route",
                    parse_route="vision",
                    force_mineru=False,
                    vision_provider_id="provider-one",
                    provider_name="Provider One",
                )
        route_job, route_context = store.job_and_context_snapshot("route")
        self.assertEqual(route_job["parse_route"], "mineru")
        self.assertTrue(route_context["force_mineru"])

        store.restore_job(
            "active",
            self._stored_job("active", "processing"),
            self._stored_context("active"),
        )
        with mock.patch.object(
            self.journal,
            "update_job",
            side_effect=OSError("update failed"),
        ):
            with self.assertRaisesRegex(OSError, "update failed"):
                orchestrator.dismiss_import_job("active")
        self.assertEqual(store.job_snapshot("active")["status"], "cancelling")
        with self.assertRaises(StoreImportJobCancelled):
            store.ensure_not_cancelled("active")

        store.restore_job(
            "completed",
            self._stored_job("completed", "processing"),
            self._stored_context("completed"),
        )
        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=OSError("complete delete failed"),
        ):
            with self.assertRaisesRegex(OSError, "complete delete failed"):
                orchestrator.update_import_job(
                    "completed",
                    status="completed",
                    phase="completed",
                )
        self.assertEqual(
            store.job_snapshot("completed")["status"],
            "processing",
        )
        self.assertTrue(store.has_active_jobs())

        store.restore_job(
            "cancelled",
            self._stored_job("cancelled", "processing"),
            self._stored_context("cancelled"),
        )
        self.assertEqual(
            store.request_dismissal("cancelled"),
            "cancelling",
        )
        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=OSError("cancel delete failed"),
        ):
            with self.assertRaisesRegex(
                ImportJobCleanupFailed,
                "cancel delete failed",
            ):
                orchestrator.finish_cancelled_import_job("cancelled")
        self.assertEqual(
            store.job_snapshot("cancelled")["status"],
            "failed",
        )
        self.assertNotIn("source-cancelled", store.active_source_ids())
        store.ensure_not_cancelled("cancelled")

    def test_failed_transition_cannot_expose_inactive_state_before_journal(
        self,
    ) -> None:
        store = ImportJobStore()
        orchestrator = self._orchestrator(job_store=store)
        store.restore_job(
            "terminal",
            self._stored_job("terminal", "processing"),
            self._stored_context("terminal"),
        )
        journal_entered = threading.Event()
        release_journal = threading.Event()
        observation_finished = threading.Event()
        transition_errors: list[Exception] = []
        observed_active: list[bool] = []

        def fail_journal_update(
            _job_id: str,
            **_updates: object,
        ) -> dict[str, object]:
            journal_entered.set()
            if not release_journal.wait(timeout=1):
                raise AssertionError("journal release timed out")
            raise OSError("journal write failed")

        def transition() -> None:
            try:
                orchestrator.update_import_job(
                    "terminal",
                    status="failed",
                    phase="failed",
                )
            except Exception as exc:
                transition_errors.append(exc)

        def observe_active_jobs() -> None:
            observed_active.append(orchestrator.has_active_jobs())
            observation_finished.set()

        with mock.patch.object(
            self.journal,
            "update_job",
            side_effect=fail_journal_update,
        ):
            transition_thread = threading.Thread(target=transition)
            transition_thread.start()
            self.assertTrue(journal_entered.wait(timeout=1))
            observer_thread = threading.Thread(target=observe_active_jobs)
            observer_thread.start()
            self.assertFalse(observation_finished.wait(timeout=0.05))
            release_journal.set()
            transition_thread.join(timeout=1)
            observer_thread.join(timeout=1)

        self.assertFalse(transition_thread.is_alive())
        self.assertFalse(observer_thread.is_alive())
        self.assertEqual(len(transition_errors), 1)
        self.assertIsInstance(transition_errors[0], OSError)
        self.assertEqual(str(transition_errors[0]), "journal write failed")
        self.assertEqual(observed_active, [True])
        self.assertEqual(
            store.job_snapshot("terminal")["status"],
            "processing",
        )

    @staticmethod
    def _stored_job(job_id: str, status: str) -> dict[str, object]:
        return {
            "job_id": job_id,
            "source_file_id": f"source-{job_id}",
            "status": status,
            "can_resume": status == "failed",
        }

    @staticmethod
    def _stored_context(job_id: str) -> dict[str, object]:
        return {
            "target": Path(f"{job_id}.docx"),
            "source_file_id": f"source-{job_id}",
            "profile": {},
            "is_pdf": False,
        }

    def test_native_word_batch_replaces_each_document_without_full_rebuild(self) -> None:
        orchestrator = self._orchestrator()
        first = self._target("first.docx")
        second = self._target("second.docx")
        items = [
            {
                "target": first,
                "profile": {"detected_pdf_type": "docx"},
                "source_file_id": "word-first",
                "is_pdf": False,
                "display_file_name": first.name,
            },
            {
                "target": second,
                "profile": {"detected_pdf_type": "docx"},
                "source_file_id": "word-second",
                "is_pdf": False,
                "display_file_name": second.name,
            },
        ]

        job_ids = orchestrator.start_native_import_batch(items)
        self.assertEqual(len(job_ids), 2)
        self.assertEqual(len(self.queue.tasks), 1)
        self.queue.run_next()

        self.assertEqual(len(self.runtime.replace_calls), 2)
        self.assertEqual(self.runtime.rebuild_calls, [])
        self.assertEqual(
            [orchestrator.job_status(job_id)["status"] for job_id in job_ids],
            ["completed", "completed"],
        )

    def test_native_word_batch_cancelled_first_job_does_not_block_second(
        self,
    ) -> None:
        orchestrator = self._orchestrator()
        first = self._target("first-cancelled.docx")
        second = self._target("second-active.docx")
        job_ids = orchestrator.start_native_import_batch(
            [
                {
                    "target": first,
                    "profile": {"detected_pdf_type": "docx"},
                    "source_file_id": "word-first-cancelled",
                    "is_pdf": False,
                    "display_file_name": first.name,
                },
                {
                    "target": second,
                    "profile": {"detected_pdf_type": "docx"},
                    "source_file_id": "word-second-active",
                    "is_pdf": False,
                    "display_file_name": second.name,
                },
            ]
        )

        self.assertEqual(orchestrator.dismiss_import_job(job_ids[0]), "cancelling")
        self.queue.run_next()

        self.assertIsNone(orchestrator.job_status(job_ids[0]))
        self.assertIsNone(self.journal.get_job(job_ids[0]))
        self.assertEqual(orchestrator.job_status(job_ids[1])["status"], "completed")
        self.assertEqual(len(self.runtime.replace_calls), 1)
        self.assertEqual(self.runtime.rebuild_calls, [])

    def test_native_batch_finalization_cannot_complete_a_cancelled_job(self) -> None:
        orchestrator = self._orchestrator()
        target = self._target("cancel-before-finalize.docx")
        job_id = orchestrator.start_native_import_batch(
            [
                {
                    "target": target,
                    "profile": {"detected_pdf_type": "docx"},
                    "source_file_id": "word-cancel-before-finalize",
                    "is_pdf": False,
                    "display_file_name": target.name,
                }
            ]
        )[0]
        original_finalize = orchestrator.finalize_import_job

        def cancel_then_finalize(
            current_job_id: str,
            source_file_id: str,
            is_pdf: bool,
        ) -> None:
            self.assertEqual(
                orchestrator.dismiss_import_job(current_job_id),
                "cancelling",
            )
            original_finalize(current_job_id, source_file_id, is_pdf)

        with mock.patch.object(
            orchestrator,
            "finalize_import_job",
            side_effect=cancel_then_finalize,
        ):
            self.queue.run_next()

        self.assertIsNone(orchestrator.job_status(job_id))
        self.assertIsNone(self.journal.get_job(job_id))

    def test_permanent_mineru_failure_keeps_public_retry_fields(self) -> None:
        def fail_mineru(*_args: object, **_kwargs: object) -> None:
            raise MinerUError("账号失效", allow_parser_fallback=True)

        orchestrator = self._orchestrator(parse_mineru=fail_mineru)
        target = self._target("scan.pdf")
        job_id = orchestrator.start_import_job(
            target,
            {"detected_pdf_type": "scanned", "pdf_page_count": 1},
            "pdf-scan",
            True,
        )
        self.queue.run_next()

        status = orchestrator.job_status(job_id)
        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["mineru_failed"])
        self.assertFalse(status["can_retry_with_provider"])
        self.assertTrue(status["needs_provider_config"])
        self.assertIn("可在设置中配置其他解析 API", status["message"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.application.import_job_lifecycle import (
    ImportJobCleanupFailed,
    ImportJobLifecycle,
)
from src.me_finder.application.import_job_store import (
    ImportJobCancelled,
    ImportJobStore,
)
from src.me_finder.import_job_journal import ImportJobJournal


class ImportJobLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.target = self.root / "sample.docx"
        self.target.write_bytes(b"document")
        self.journal = ImportJobJournal(self.root / "jobs")
        self.store = ImportJobStore()
        self.lifecycle = ImportJobLifecycle(self.journal, self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed(
        self,
        job_id: str,
        status: str,
        *,
        target: Path | None = None,
        replaces_job_id: str | None = None,
    ) -> None:
        job_target = target or self.target
        record = self.journal.save_job(
            {
                "job_id": job_id,
                "source_file_id": f"source-{job_id}",
                "status": status,
                "phase": status,
            },
            target=job_target,
            source_file_id=f"source-{job_id}",
            profile={},
            is_pdf=False,
            replaces_job_id=replaces_job_id,
        )
        self.store.restore_job(
            job_id,
            {
                "job_id": job_id,
                "source_file_id": f"source-{job_id}",
                "status": status,
                "phase": status,
                "can_resume": status in {"failed", "paused"},
            },
            {
                "target": job_target,
                "source_file_id": f"source-{job_id}",
                "profile": {},
                "is_pdf": False,
                "force_mineru": False,
                "vision_provider_id": None,
                "file_hash": record["file_hash"],
            },
        )

    @staticmethod
    def _no_index_failure(
        _job: object,
        *,
        is_pdf: bool,
    ) -> None:
        del is_pdf
        return None

    def test_resume_journal_failure_keeps_memory_resumable(self) -> None:
        self._seed("resume-failed", "failed")

        with mock.patch.object(
            self.journal,
            "update_job",
            side_effect=OSError("journal unavailable"),
        ):
            with self.assertRaisesRegex(OSError, "journal unavailable"):
                self.lifecycle.begin_resume(
                    "resume-failed",
                    infer_failure_stage=self._no_index_failure,
                    validate_target=lambda _job_id, _context: self.target,
                )

        snapshot = self.store.job_snapshot("resume-failed")
        self.assertEqual(snapshot["status"], "failed")
        self.assertTrue(snapshot["can_resume"])
        self.assertFalse(self.store.has_active_jobs())

    def test_resume_hides_inactive_state_until_journal_is_durable(self) -> None:
        self._seed("resume-order", "failed")
        journal_entered = threading.Event()
        release_journal = threading.Event()
        observation_finished = threading.Event()
        observed_active: list[bool] = []
        transitions = []
        original_update = self.journal.update_job

        def blocked_update(job_id: str, **updates: object):
            journal_entered.set()
            if not release_journal.wait(timeout=1):
                raise AssertionError("journal release timed out")
            return original_update(job_id, **updates)

        def resume() -> None:
            transitions.append(
                self.lifecycle.begin_resume(
                    "resume-order",
                    infer_failure_stage=self._no_index_failure,
                    validate_target=lambda _job_id, _context: self.target,
                )
            )

        def observe() -> None:
            observed_active.append(self.store.has_active_jobs())
            observation_finished.set()

        with mock.patch.object(
            self.journal,
            "update_job",
            side_effect=blocked_update,
        ):
            transition_thread = threading.Thread(target=resume)
            transition_thread.start()
            self.assertTrue(journal_entered.wait(timeout=1))
            observer_thread = threading.Thread(target=observe)
            observer_thread.start()
            self.assertFalse(observation_finished.wait(timeout=0.05))
            release_journal.set()
            transition_thread.join(timeout=1)
            observer_thread.join(timeout=1)

        self.assertFalse(transition_thread.is_alive())
        self.assertFalse(observer_thread.is_alive())
        self.assertEqual(observed_active, [True])
        self.assertEqual(transitions[0].job["status"], "processing")

    def test_terminal_journal_failure_keeps_job_active(self) -> None:
        self._seed("terminal", "processing")

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=OSError("delete failed"),
        ):
            with self.assertRaisesRegex(OSError, "delete failed"):
                self.lifecycle.update_job(
                    "terminal",
                    status="completed",
                    phase="completed",
                )

        self.assertEqual(self.store.job_snapshot("terminal")["status"], "processing")
        self.assertTrue(self.store.has_active_jobs())

    def test_cancellation_wins_atomically_over_terminal_update(self) -> None:
        self._seed("terminal-race", "processing")
        cancellation_entered_journal = threading.Event()
        release_cancellation = threading.Event()
        terminal_finished = threading.Event()
        terminal_errors: list[Exception] = []
        original_update = self.journal.update_job

        def block_cancellation(job_id: str, **updates: object):
            if updates.get("status") == "cancelling":
                cancellation_entered_journal.set()
                if not release_cancellation.wait(timeout=1):
                    raise AssertionError("cancellation release timed out")
            return original_update(job_id, **updates)

        def cancel() -> None:
            self.lifecycle.dismiss_job("terminal-race")

        def complete() -> None:
            try:
                self.lifecycle.update_job(
                    "terminal-race",
                    status="completed",
                    phase="completed",
                )
            except Exception as exc:
                terminal_errors.append(exc)
            finally:
                terminal_finished.set()

        with mock.patch.object(
            self.journal,
            "update_job",
            side_effect=block_cancellation,
        ):
            cancellation_thread = threading.Thread(target=cancel)
            cancellation_thread.start()
            self.assertTrue(cancellation_entered_journal.wait(timeout=1))
            terminal_thread = threading.Thread(target=complete)
            terminal_thread.start()
            self.assertFalse(terminal_finished.wait(timeout=0.05))
            release_cancellation.set()
            cancellation_thread.join(timeout=1)
            terminal_thread.join(timeout=1)

        self.assertFalse(cancellation_thread.is_alive())
        self.assertFalse(terminal_thread.is_alive())
        self.assertEqual(len(terminal_errors), 1)
        self.assertIsInstance(terminal_errors[0], ImportJobCancelled)
        self.assertEqual(
            self.store.job_snapshot("terminal-race")["status"],
            "cancelling",
        )
        self.assertEqual(
            self.journal.get_job("terminal-race")["status"],
            "cancelling",
        )

    def test_active_dismiss_failure_remains_visible_as_cancelling(self) -> None:
        self._seed("active", "processing")

        with mock.patch.object(
            self.journal,
            "update_job",
            side_effect=OSError("update failed"),
        ):
            with self.assertRaisesRegex(OSError, "update failed"):
                self.lifecycle.dismiss_job("active")

        self.assertEqual(self.store.job_snapshot("active")["status"], "cancelling")
        self.assertTrue(self.store.has_active_jobs())

    def test_cancel_delete_failure_becomes_durable_failed_journal_first(
        self,
    ) -> None:
        self._seed("cleanup-failed", "processing")
        self.assertEqual(
            self.lifecycle.dismiss_job("cleanup-failed"),
            "cancelling",
        )
        store_status_during_journal_update: list[str] = []
        original_update = self.journal.update_job

        def record_update(job_id: str, **updates: object):
            store_status_during_journal_update.append(
                str(self.store.job_snapshot(job_id)["status"])
            )
            return original_update(job_id, **updates)

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=OSError("disk busy"),
        ), mock.patch.object(
            self.journal,
            "update_job",
            side_effect=record_update,
        ):
            with self.assertRaisesRegex(
                ImportJobCleanupFailed,
                "disk busy",
            ):
                self.lifecycle.finish_cancelled_job("cleanup-failed")

        self.assertEqual(store_status_during_journal_update, ["cancelling"])
        durable = self.journal.get_job("cleanup-failed")
        snapshot = self.store.job_snapshot("cleanup-failed")
        self.assertEqual(durable["status"], "failed")
        self.assertEqual(durable["phase"], "cancellation_cleanup_failed")
        self.assertFalse(durable["can_resume"])
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["phase"], "cancellation_cleanup_failed")
        self.assertFalse(self.store.has_active_jobs())
        self.store.ensure_not_cancelled("cleanup-failed")

    def test_cancel_delete_and_state_write_failure_still_releases_worker(
        self,
    ) -> None:
        self._seed("cleanup-double-failed", "processing")
        self.lifecycle.dismiss_job("cleanup-double-failed")

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=OSError("delete blocked"),
        ), mock.patch.object(
            self.journal,
            "update_job",
            side_effect=OSError("write blocked"),
        ):
            with self.assertRaisesRegex(
                ImportJobCleanupFailed,
                "delete blocked.*write blocked",
            ):
                self.lifecycle.finish_cancelled_job(
                    "cleanup-double-failed"
                )

        snapshot = self.store.job_snapshot("cleanup-double-failed")
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["phase"], "cancellation_cleanup_failed")
        self.assertFalse(self.store.has_active_jobs())
        self.store.ensure_not_cancelled("cleanup-double-failed")
        self.assertEqual(
            self.journal.get_job("cleanup-double-failed")["status"],
            "cancelling",
        )

        restarted_store = ImportJobStore()
        ImportJobLifecycle(
            self.journal,
            restarted_store,
        ).restore_startup_jobs(self._no_index_failure)
        self.assertIsNone(self.journal.get_job("cleanup-double-failed"))
        self.assertIsNone(
            restarted_store.job_snapshot("cleanup-double-failed")
        )

    def test_startup_isolates_a_persistently_undeletable_cancelled_job(
        self,
    ) -> None:
        self._seed("stale-cancelling", "cancelling")
        self._seed("resumable-neighbor", "failed")
        restarted_store = ImportJobStore()
        original_delete = self.journal.delete_job

        def delete_job(job_id: str) -> bool:
            if job_id == "stale-cancelling":
                raise OSError("journal directory is read-only")
            return original_delete(job_id)

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=delete_job,
        ), self.assertLogs(level="WARNING") as logs:
            ImportJobLifecycle(
                self.journal,
                restarted_store,
            ).restore_startup_jobs(self._no_index_failure)

        self.assertIn("stale-cancelling", "\n".join(logs.output))
        self.assertIsNone(restarted_store.job_snapshot("stale-cancelling"))
        self.assertEqual(
            restarted_store.job_snapshot("resumable-neighbor")["status"],
            "failed",
        )
        self.assertIsNotNone(self.journal.get_job("stale-cancelling"))

    def test_startup_rolls_back_retry_replacement_before_commit(self) -> None:
        self._seed("retry-old", "failed")
        self._seed(
            "retry-new",
            "processing",
            replaces_job_id="retry-old",
        )
        restarted_store = ImportJobStore()

        ImportJobLifecycle(
            self.journal,
            restarted_store,
        ).restore_startup_jobs(self._no_index_failure)

        self.assertEqual(
            restarted_store.job_snapshot("retry-old")["status"],
            "failed",
        )
        self.assertIsNone(restarted_store.job_snapshot("retry-new"))
        self.assertIsNotNone(self.journal.get_job("retry-old"))
        self.assertIsNone(self.journal.get_job("retry-new"))

    def test_startup_restores_retry_replacement_after_commit(self) -> None:
        self._seed(
            "retry-committed",
            "processing",
            replaces_job_id="retry-predecessor",
        )
        restarted_store = ImportJobStore()

        ImportJobLifecycle(
            self.journal,
            restarted_store,
        ).restore_startup_jobs(self._no_index_failure)

        restored = restarted_store.job_snapshot("retry-committed")
        self.assertEqual(restored["status"], "paused")
        self.assertNotIn("replaces_job_id", restored)
        self.assertEqual(
            self.journal.get_job("retry-committed")["replaces_job_id"],
            "retry-predecessor",
        )

    def test_startup_retries_stale_replacement_cleanup_and_restores_neighbors(
        self,
    ) -> None:
        missing_target = self.root / "missing-replacement.docx"
        missing_target.write_bytes(b"replacement")
        self._seed("retry-old-stale", "failed")
        self._seed(
            "retry-new-stale",
            "processing",
            target=missing_target,
            replaces_job_id="retry-old-stale",
        )
        self._seed("retry-neighbor", "failed")
        self._seed(
            "retry-other-lineage",
            "failed",
            replaces_job_id="retry-other-root",
        )
        missing_target.unlink()
        delete_attempts: list[str] = []
        original_delete = self.journal.delete_job

        def fail_replacement_delete(job_id: str) -> bool:
            delete_attempts.append(job_id)
            if job_id == "retry-new-stale":
                raise OSError("replacement journal is read-only")
            return original_delete(job_id)

        blocked_store = ImportJobStore()
        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=fail_replacement_delete,
        ), mock.patch.object(
            self.journal,
            "update_job",
            side_effect=OSError("normalization must not run"),
        ) as update, self.assertLogs(level="WARNING") as logs:
            ImportJobLifecycle(
                self.journal,
                blocked_store,
            ).restore_startup_jobs(self._no_index_failure)

        update.assert_not_called()
        self.assertIn("retry-new-stale", "\n".join(logs.output))
        self.assertIn("skipping retry lineage", "\n".join(logs.output))
        self.assertIsNone(blocked_store.job_snapshot("retry-new-stale"))
        self.assertIsNone(blocked_store.job_snapshot("retry-old-stale"))
        self.assertEqual(
            blocked_store.job_snapshot("retry-neighbor")["status"],
            "failed",
        )
        self.assertEqual(
            blocked_store.job_snapshot("retry-other-lineage")["status"],
            "failed",
        )
        self.assertEqual(delete_attempts.count("retry-new-stale"), 1)
        self.assertIsNotNone(self.journal.get_job("retry-new-stale"))

        recovered_store = ImportJobStore()
        ImportJobLifecycle(
            self.journal,
            recovered_store,
        ).restore_startup_jobs(self._no_index_failure)

        self.assertIsNone(recovered_store.job_snapshot("retry-new-stale"))
        self.assertEqual(
            recovered_store.job_snapshot("retry-old-stale")["status"],
            "failed",
        )
        self.assertEqual(
            recovered_store.job_snapshot("retry-neighbor")["status"],
            "failed",
        )
        self.assertEqual(
            recovered_store.job_snapshot("retry-other-lineage")["status"],
            "failed",
        )
        self.assertIsNone(self.journal.get_job("retry-new-stale"))

    def test_startup_skips_stale_cancelling_before_target_normalization(
        self,
    ) -> None:
        missing_target = self.root / "missing-cancelled.docx"
        missing_target.write_bytes(b"cancelled")
        self._seed(
            "cancelled-missing-target",
            "cancelling",
            target=missing_target,
        )
        self._seed("cancelled-neighbor", "failed")
        missing_target.unlink()
        restarted_store = ImportJobStore()

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=OSError("cancel journal is read-only"),
        ), mock.patch.object(
            self.journal,
            "update_job",
            side_effect=OSError("normalization must not run"),
        ) as update, self.assertLogs(level="WARNING"):
            ImportJobLifecycle(
                self.journal,
                restarted_store,
            ).restore_startup_jobs(self._no_index_failure)

        update.assert_not_called()
        self.assertIsNone(
            restarted_store.job_snapshot("cancelled-missing-target")
        )
        self.assertEqual(
            restarted_store.job_snapshot("cancelled-neighbor")["status"],
            "failed",
        )
        self.assertIsNotNone(
            self.journal.get_job("cancelled-missing-target")
        )

    def test_startup_keeps_latest_committed_replacement_and_skips_orphan(
        self,
    ) -> None:
        missing_target = self.root / "superseded.docx"
        missing_target.write_bytes(b"superseded")
        self._seed(
            "retry-generation-one",
            "processing",
            target=missing_target,
            replaces_job_id="retry-old-committed",
        )
        self._seed(
            "retry-generation-two",
            "processing",
            replaces_job_id="retry-old-committed",
        )
        self._seed("retry-generation-neighbor", "failed")
        self._seed(
            "retry-generation-other-lineage",
            "failed",
            replaces_job_id="retry-generation-other-root",
        )
        missing_target.unlink()
        restarted_store = ImportJobStore()
        original_delete = self.journal.delete_job
        normalized_job_ids: list[str] = []
        original_update = self.journal.update_job

        def fail_orphan_delete(job_id: str) -> bool:
            if job_id == "retry-generation-one":
                raise OSError("superseded journal is read-only")
            return original_delete(job_id)

        def track_normalization(job_id: str, **updates: object):
            normalized_job_ids.append(job_id)
            return original_update(job_id, **updates)

        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=fail_orphan_delete,
        ), mock.patch.object(
            self.journal,
            "update_job",
            side_effect=track_normalization,
        ), self.assertLogs(level="WARNING") as logs:
            ImportJobLifecycle(
                self.journal,
                restarted_store,
            ).restore_startup_jobs(self._no_index_failure)

        self.assertIn("superseded retry replacement", "\n".join(logs.output))
        self.assertNotIn("retry-generation-one", normalized_job_ids)
        self.assertNotIn("retry-generation-two", normalized_job_ids)
        self.assertIsNone(
            restarted_store.job_snapshot("retry-generation-one")
        )
        self.assertIsNone(
            restarted_store.job_snapshot("retry-generation-two")
        )
        self.assertEqual(
            restarted_store.job_snapshot("retry-generation-neighbor")["status"],
            "failed",
        )
        self.assertEqual(
            restarted_store.job_snapshot(
                "retry-generation-other-lineage"
            )["status"],
            "failed",
        )
        self.assertIsNotNone(
            self.journal.get_job("retry-generation-one")
        )

        recovered_store = ImportJobStore()
        ImportJobLifecycle(
            self.journal,
            recovered_store,
        ).restore_startup_jobs(self._no_index_failure)

        self.assertIsNone(
            recovered_store.job_snapshot("retry-generation-one")
        )
        self.assertEqual(
            recovered_store.job_snapshot("retry-generation-two")["status"],
            "paused",
        )
        self.assertEqual(
            recovered_store.job_snapshot("retry-generation-neighbor")["status"],
            "failed",
        )
        self.assertEqual(
            recovered_store.job_snapshot(
                "retry-generation-other-lineage"
            )["status"],
            "failed",
        )
        self.assertIsNone(
            self.journal.get_job("retry-generation-one")
        )

    def test_startup_restores_predecessor_when_newest_child_is_uncommitted(
        self,
    ) -> None:
        self._seed("retry-root-chain", "failed")
        self._seed(
            "retry-middle-chain",
            "processing",
            replaces_job_id="retry-root-chain",
        )
        self.journal.delete_job("retry-root-chain")
        self._seed(
            "retry-child-chain",
            "processing",
            replaces_job_id="retry-middle-chain",
        )
        restarted_store = ImportJobStore()

        ImportJobLifecycle(
            self.journal,
            restarted_store,
        ).restore_startup_jobs(self._no_index_failure)

        self.assertEqual(
            restarted_store.job_snapshot("retry-middle-chain")["status"],
            "paused",
        )
        self.assertIsNone(
            restarted_store.job_snapshot("retry-child-chain")
        )
        self.assertIsNotNone(self.journal.get_job("retry-middle-chain"))
        self.assertIsNone(self.journal.get_job("retry-child-chain"))

    def test_startup_keeps_cancelling_anchor_when_orphan_cleanup_fails(
        self,
    ) -> None:
        self._seed(
            "retry-cancel-orphan",
            "processing",
            replaces_job_id="retry-cancel-root",
        )
        self._seed(
            "retry-cancel-head",
            "cancelling",
            replaces_job_id="retry-cancel-root",
        )
        original_delete = self.journal.delete_job
        failed_delete_order: list[str] = []

        def fail_orphan_delete(job_id: str) -> bool:
            failed_delete_order.append(job_id)
            if job_id == "retry-cancel-orphan":
                raise OSError("orphan is read-only")
            return original_delete(job_id)

        blocked_store = ImportJobStore()
        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=fail_orphan_delete,
        ), self.assertLogs(level="WARNING") as logs:
            ImportJobLifecycle(
                self.journal,
                blocked_store,
            ).restore_startup_jobs(self._no_index_failure)

        self.assertEqual(failed_delete_order, ["retry-cancel-orphan"])
        self.assertIn("skipping retry lineage", "\n".join(logs.output))
        self.assertIsNone(
            blocked_store.job_snapshot("retry-cancel-orphan")
        )
        self.assertIsNone(blocked_store.job_snapshot("retry-cancel-head"))
        self.assertIsNotNone(self.journal.get_job("retry-cancel-orphan"))
        self.assertIsNotNone(self.journal.get_job("retry-cancel-head"))

        recovered_delete_order: list[str] = []

        def record_delete(job_id: str) -> bool:
            recovered_delete_order.append(job_id)
            return original_delete(job_id)

        recovered_store = ImportJobStore()
        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=record_delete,
        ):
            ImportJobLifecycle(
                self.journal,
                recovered_store,
            ).restore_startup_jobs(self._no_index_failure)

        self.assertEqual(
            recovered_delete_order,
            ["retry-cancel-orphan", "retry-cancel-head"],
        )
        self.assertIsNone(
            recovered_store.job_snapshot("retry-cancel-orphan")
        )
        self.assertIsNone(
            recovered_store.job_snapshot("retry-cancel-head")
        )
        self.assertIsNone(self.journal.get_job("retry-cancel-orphan"))
        self.assertIsNone(self.journal.get_job("retry-cancel-head"))

    def test_startup_deletes_uncommitted_child_before_cancelling_root(
        self,
    ) -> None:
        self._seed("retry-cancel-root-live", "cancelling")
        self._seed(
            "retry-cancel-child",
            "processing",
            replaces_job_id="retry-cancel-root-live",
        )
        delete_order: list[str] = []
        original_delete = self.journal.delete_job

        def record_delete(job_id: str) -> bool:
            delete_order.append(job_id)
            return original_delete(job_id)

        restarted_store = ImportJobStore()
        with mock.patch.object(
            self.journal,
            "delete_job",
            side_effect=record_delete,
        ):
            ImportJobLifecycle(
                self.journal,
                restarted_store,
            ).restore_startup_jobs(self._no_index_failure)

        self.assertEqual(
            delete_order,
            ["retry-cancel-child", "retry-cancel-root-live"],
        )
        self.assertIsNone(
            restarted_store.job_snapshot("retry-cancel-root-live")
        )
        self.assertIsNone(
            restarted_store.job_snapshot("retry-cancel-child")
        )


if __name__ == "__main__":
    unittest.main()

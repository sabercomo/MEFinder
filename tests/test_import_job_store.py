from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.me_finder.application.import_job_store import (
    ImportJobCancelled,
    ImportJobStore,
)
from src.me_finder.mineru_api import MinerUError


class ImportJobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ImportJobStore()

    @staticmethod
    def job(
        job_id: str,
        source_file_id: str,
        status: str = "processing",
        **fields: object,
    ) -> dict[str, object]:
        return {
            "job_id": job_id,
            "source_file_id": source_file_id,
            "status": status,
            **fields,
        }

    @staticmethod
    def context(
        source_file_id: str,
        target: Path | None = None,
        **fields: object,
    ) -> dict[str, object]:
        return {
            "source_file_id": source_file_id,
            "target": target or Path(f"{source_file_id}.docx"),
            "is_pdf": False,
            **fields,
        }

    def add(self, job_id: str, source_file_id: str, **fields: object) -> None:
        self.store.add_import_job(
            self.job(job_id, source_file_id, **fields),
            self.context(source_file_id),
        )

    def test_restore_and_queries_return_copies(self) -> None:
        job = self.job(
            "restored",
            "source-one",
            status="paused",
            progress={"resume": {"completed_pages": [1]}},
        )
        context = self.context(
            "source-one",
            force_mineru=True,
            profile={"parser": {"options": ["layout"]}},
        )
        self.store.restore_job("restored", job, context)

        job["status"] = "changed-outside"
        job["progress"]["resume"]["completed_pages"].append(2)
        context["force_mineru"] = False
        context["profile"]["parser"]["options"].append("ocr")
        snapshot, context_snapshot = self.store.job_and_context_snapshot("restored")
        snapshot["status"] = "changed-copy"
        snapshot["progress"]["resume"]["completed_pages"].append(3)
        context_snapshot["force_mineru"] = False
        context_snapshot["profile"]["parser"]["options"].append("table")

        stored_job, stored_context = self.store.job_and_context_snapshot("restored")
        self.assertEqual(stored_job["status"], "paused")
        self.assertEqual(
            stored_job["progress"]["resume"]["completed_pages"],
            [1],
        )
        self.assertTrue(stored_context["force_mineru"])
        self.assertEqual(
            stored_context["profile"]["parser"]["options"],
            ["layout"],
        )

    def test_update_reports_durability_and_context_update_fails_fast(self) -> None:
        self.store.register_background_job(
            self.job("background", "source-bg")
        )
        self.add("durable", "source-durable")

        self.assertFalse(self.store.has_recovery_context("background"))
        self.assertTrue(self.store.has_recovery_context("durable"))
        self.assertFalse(
            self.store.update_job("background", {"phase": "running"})
        )
        self.assertTrue(
            self.store.update_job(
                "durable",
                {
                    "phase": "running",
                    "progress": {"resume": {"completed_pages": [1]}},
                },
            )
        )
        update = {"profile": {"parser": {"options": ["layout"]}}}
        self.store.update_context("durable", update)
        update["profile"]["parser"]["options"].append("ocr")
        self.store.update_context("durable", {"file_hash": "abc"})

        self.assertEqual(
            self.store.job_snapshot("background")["phase"],
            "running",
        )
        self.assertEqual(
            self.store.job_and_context_snapshot("durable")[1]["file_hash"],
            "abc",
        )
        durable_job, durable_context = self.store.job_and_context_snapshot(
            "durable"
        )
        durable_job["progress"]["resume"]["completed_pages"].append(2)
        self.assertEqual(
            self.store.job_snapshot("durable")["progress"]["resume"][
                "completed_pages"
            ],
            [1],
        )
        self.assertEqual(
            durable_context["profile"]["parser"]["options"],
            ["layout"],
        )
        with self.assertRaises(KeyError):
            self.store.update_context("missing", {"file_hash": "abc"})

    def test_switch_route_updates_job_and_context_as_one_transition(self) -> None:
        self.add("route", "source-route")

        self.store.switch_job_route(
            "route",
            parse_route="vision",
            force_mineru=False,
            vision_provider_id="provider-one",
            provider_name="Provider One",
        )

        job, context = self.store.job_and_context_snapshot("route")
        self.assertEqual(job["parse_route"], "vision")
        self.assertEqual(job["provider_id"], "provider-one")
        self.assertEqual(job["provider_name"], "Provider One")
        self.assertFalse(context["force_mineru"])
        self.assertEqual(context["vision_provider_id"], "provider-one")
        with self.assertRaisesRegex(
            MinerUError,
            "\u6062\u590d\u4fe1\u606f\u4e0d\u5b58\u5728",
        ):
            self.store.switch_job_route(
                "missing",
                parse_route="native",
                force_mineru=False,
                vision_provider_id=None,
                provider_name=None,
            )

    def test_source_availability_checks_keep_existing_messages(self) -> None:
        self.store.reserve_source("pending")
        with self.assertRaisesRegex(
            MinerUError,
            "\u6b63\u5728\u51c6\u5907\u5bfc\u5165",
        ):
            self.store.add_import_job(
                self.job("pending-job", "pending"),
                self.context("pending"),
            )

        self.store.begin_source_deletion("deleting")
        with self.assertRaisesRegex(MinerUError, "\u6b63\u5728\u5220\u9664"):
            self.store.reserve_source("deleting")

        self.add("active", "running")
        with self.assertRaisesRegex(
            MinerUError,
            "\u5df2\u6709\u89e3\u6790\u4efb\u52a1",
        ):
            self.store.reserve_source("running")

    def test_consumed_reservation_allows_job_registration(self) -> None:
        self.store.reserve_source("source-one")
        self.store.add_import_job(
            self.job("job-one", "source-one"),
            self.context("source-one"),
            consume_reservation=True,
        )
        self.store.release_reservation("source-one")

        self.assertEqual(
            self.store.job_snapshot("job-one")["status"],
            "processing",
        )

    def test_atomic_scope_keeps_reservation_swap_indivisible(self) -> None:
        entered = threading.Event()
        attempted = threading.Event()
        finished = threading.Event()
        errors: list[MinerUError] = []

        def competing_delete() -> None:
            entered.wait()
            attempted.set()
            try:
                self.store.begin_source_deletion("actual")
            except MinerUError as exc:
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=competing_delete)
        thread.start()
        with self.store.atomic():
            self.store.reserve_source("predicted")
            entered.set()
            self.assertTrue(attempted.wait(timeout=1))
            self.assertFalse(finished.wait(timeout=0.05))
            self.store.replace_reservation("predicted", "actual")
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], MinerUError)
        self.assertIn("正在准备导入", str(errors[0]))
        self.store.release_reservation("actual")
        self.store.begin_source_deletion("actual")

    def test_cancellation_transition_is_cooperative(self) -> None:
        self.add("cancel", "source-cancel")

        self.assertEqual(self.store.request_dismissal("cancel"), "cancelling")
        status = self.store.job_snapshot("cancel")
        self.assertEqual(status["status"], "cancelling")
        self.assertEqual(status["phase"], "cancelling")
        self.assertFalse(status["can_resume"])
        with self.assertRaisesRegex(
            ImportJobCancelled,
            "\u7528\u6237\u5df2\u505c\u6b62",
        ):
            self.store.ensure_not_cancelled("cancel")

        self.store.finish_cancelled_job("cancel")
        self.store.ensure_not_cancelled("cancel")
        self.assertIsNone(self.store.job_snapshot("cancel"))

    def test_inactive_dismiss_waits_for_durable_cleanup(self) -> None:
        self.add("failed", "source-failed", status="failed")

        self.assertEqual(self.store.request_dismissal("failed"), "dismissed")
        self.assertIsNotNone(self.store.job_and_context_snapshot("failed"))
        self.store.remove_job("failed")
        self.assertIsNone(self.store.job_and_context_snapshot("failed"))
        self.assertEqual(self.store.request_dismissal("failed"), "dismissed")

    def test_begin_resume_rechecks_conflicts_before_atomic_update(self) -> None:
        self.add(
            "resumable",
            "source-resume",
            status="failed",
            can_resume=True,
        )
        updates = {
            "status": "processing",
            "phase": "stored",
            "can_resume": False,
        }

        restored, context = self.store.begin_resume("resumable", updates)

        self.assertEqual(restored["status"], "processing")
        self.assertEqual(context["source_file_id"], "source-resume")
        with self.assertRaisesRegex(
            MinerUError,
            "\u5f53\u524d\u4e0d\u80fd\u7ee7\u7eed",
        ):
            self.store.begin_resume("resumable", updates)

    def test_resume_is_blocked_by_deletion_reservation_and_other_job(self) -> None:
        cases = (
            ("deleting", "\u6b63\u5728\u5220\u9664"),
            ("pending", "\u6b63\u5728\u51c6\u5907导入"),
            ("active", "\u5df2\u6709\u89e3\u6790\u4efb\u52a1"),
        )
        for mode, message in cases:
            with self.subTest(mode=mode):
                store = ImportJobStore()
                store.add_import_job(
                    self.job(
                        "resume",
                        "source-one",
                        status="failed",
                        can_resume=True,
                    ),
                    self.context("source-one"),
                )
                if mode == "deleting":
                    store.begin_source_deletion("source-one")
                elif mode == "pending":
                    store.reserve_source("source-one")
                else:
                    store.add_import_job(
                        self.job("other", "source-one"),
                        self.context("source-one"),
                    )
                with self.assertRaisesRegex(MinerUError, message):
                    store.begin_resume("resume", {"status": "processing"})

    def test_deletion_coordination_and_cleanup_queries_are_narrow(self) -> None:
        self.add("processing", "source-active")
        with self.assertRaises(MinerUError) as raised:
            self.store.begin_source_deletion("source-active")
        self.assertEqual(
            str(raised.exception),
            "该文献仍在解析中，请等待任务结束后再删除。",
        )

        self.store.update_job(
            "processing",
            {"status": "failed", "can_resume": True},
        )
        self.store.begin_source_deletion("source-active")
        self.assertEqual(
            self.store.source_job_ids(["source-active"]),
            ["processing"],
        )
        self.store.remove_jobs(["processing"])
        self.assertIsNone(self.store.job_snapshot("processing"))
        self.store.end_source_deletion("source-active")

    def test_job_collections_are_snapshots(self) -> None:
        self.add("active", "source-active")
        self.add(
            "paused",
            "source-paused",
            status="paused",
            can_resume=True,
        )
        self.store.register_background_job(
            self.job("batchmeta-one", "source-meta")
        )

        resumable = self.store.resumable_snapshots()
        resumable[0][0]["status"] = "changed"

        self.assertEqual(
            self.store.active_source_ids(),
            {"source-active", "source-meta"},
        )
        self.assertTrue(self.store.has_active_jobs())
        self.assertEqual(
            self.store.job_for_source(
                "source-active",
                statuses=("processing",),
            )["job_id"],
            "active",
        )
        self.assertEqual(
            self.store.processing_job_with_prefix("batchmeta-")["job_id"],
            "batchmeta-one",
        )
        self.assertEqual(
            self.store.resumable_snapshots()[0][0]["status"],
            "paused",
        )

    def test_background_prefix_registration_is_atomic(self) -> None:
        barrier = threading.Barrier(2)
        results = []

        def register(job_id: str) -> None:
            barrier.wait()
            results.append(
                self.store.register_background_job_unless_processing(
                    "batchmeta-",
                    self.job(job_id, ""),
                )
            )

        threads = [
            threading.Thread(target=register, args=(job_id,))
            for job_id in ("batchmeta-one", "batchmeta-two")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertIn(
            self.store.processing_job_with_prefix("batchmeta-")["job_id"],
            {"batchmeta-one", "batchmeta-two"},
        )

    def test_target_reference_query_tracks_context_lifetime(self) -> None:
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "document.docx"
            target.write_bytes(b"body")
            self.store.add_import_job(
                self.job("target", "source-target", status="failed"),
                self.context("source-target", target),
            )

            self.assertTrue(self.store.target_is_referenced(target))
            self.store.remove_job("target")
            self.assertFalse(self.store.target_is_referenced(target))


if __name__ == "__main__":
    unittest.main()

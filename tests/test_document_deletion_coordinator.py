from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from src.me_finder.app_context import AppPaths
from src.me_finder.application.document_deletion_coordinator import (
    BatchDeletionConflict,
    DocumentDeletionCoordinator,
    DocumentDeletionFailed,
    DocumentDeletionRejected,
)
from src.me_finder.mineru_api import MinerUError


class FakeIndex:
    def __init__(self, events):
        self.events = events
        self.reopen_error = None

    @contextmanager
    def mutation(self):
        self.events.append("mutation-enter")
        try:
            yield
        finally:
            self.events.append("mutation-exit")

    def suspend(self):
        self.events.append("suspend")

    def reopen(self, *, attempts=1):
        self.events.append("reopen")
        if self.reopen_error:
            raise self.reopen_error
        return True


class FakeDurable:
    def __init__(self, events):
        self.events = events

    @contextmanager
    def operation(self):
        self.events.append("durable-enter")
        try:
            yield
        finally:
            self.events.append("durable-exit")


class FakeJobs:
    def __init__(self, events):
        self.events = events
        self.conflicts = {}
        self.purge_warnings = []
        self.purged = []

    def begin_source_deletion(self, source_file_id):
        self.events.append(f"begin:{source_file_id}")
        if source_file_id in self.conflicts:
            raise MinerUError(self.conflicts[source_file_id])

    def end_source_deletion(self, source_file_id):
        self.events.append(f"end:{source_file_id}")

    def purge_source_jobs(self, source_file_ids):
        self.purged.append(list(source_file_ids))
        return list(self.purge_warnings)


class FakeService:
    def __init__(self, events):
        self.events = events
        self.single_result = {
            "source_file_id": "pdf-one",
            "cleanup_warnings": ["artifact warning"],
        }
        self.batch_result = {
            "removed_source_ids": ["pdf-one"],
            "failures": [{"source_id": "missing", "error": "not found"}],
        }
        self.single_error = None
        self.batch_error = None
        self.single_args = None
        self.batch_args = None

    def remove(self, source_file_id, **options):
        self.events.append("remove")
        self.single_args = (source_file_id, options)
        if self.single_error:
            raise self.single_error
        return dict(self.single_result)

    def remove_many(self, source_file_ids, **options):
        self.events.append("remove-many")
        self.batch_args = (list(source_file_ids), options)
        if self.batch_error:
            raise self.batch_error
        return {
            **self.batch_result,
            "failures": list(self.batch_result.get("failures") or []),
        }


class DocumentDeletionCoordinatorTests(unittest.TestCase):
    def _fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        events = []
        index = FakeIndex(events)
        jobs = FakeJobs(events)
        service = FakeService(events)
        coordinator = DocumentDeletionCoordinator(
            AppPaths.create(root),
            index,
            FakeDurable(events),
            jobs,
            service_factory=lambda _root, _index: service,
        )
        return coordinator, index, jobs, service, events

    def test_single_removal_preserves_transaction_order_and_cleanup(self) -> None:
        coordinator, _index, jobs, service, events = self._fixture()
        jobs.purge_warnings = ["journal warning"]

        result = coordinator.remove(
            "pdf-one",
            delete_generated_artifacts=False,
            delete_internal_copy=True,
        )

        self.assertEqual(
            events,
            [
                "begin:pdf-one",
                "mutation-enter",
                "suspend",
                "durable-enter",
                "remove",
                "durable-exit",
                "reopen",
                "mutation-exit",
                "end:pdf-one",
            ],
        )
        self.assertEqual(jobs.purged, [["pdf-one"]])
        self.assertEqual(
            result["cleanup_warnings"],
            ["artifact warning", "journal warning"],
        )
        self.assertEqual(
            service.single_args,
            (
                "pdf-one",
                {
                    "delete_generated_artifacts": False,
                    "delete_internal_copy": True,
                },
            ),
        )

    def test_validation_failure_reopens_and_releases_reservation(self) -> None:
        coordinator, _index, jobs, service, events = self._fixture()
        service.single_error = ValueError("not found")

        with self.assertRaisesRegex(DocumentDeletionRejected, "not found"):
            coordinator.remove("pdf-one")

        self.assertIn("reopen", events)
        self.assertEqual(events[-1], "end:pdf-one")
        self.assertEqual(jobs.purged, [])

    def test_reopen_failure_after_commit_purges_jobs_then_reports_failure(self) -> None:
        coordinator, index, jobs, _service, events = self._fixture()
        index.reopen_error = RuntimeError("open failed")

        with self.assertRaisesRegex(
            DocumentDeletionFailed,
            "文献已删除，但索引重新载入失败",
        ):
            coordinator.remove("pdf-one")

        self.assertEqual(jobs.purged, [["pdf-one"]])
        self.assertEqual(events[-1], "end:pdf-one")

    def test_batch_merges_reservation_and_service_failures(self) -> None:
        coordinator, _index, jobs, service, events = self._fixture()
        jobs.conflicts["busy"] = "still parsing"

        result = coordinator.remove_many(
            ["pdf-one", "busy", "pdf-one", ""],
            internal_copy_source_ids=["pdf-one", "busy"],
        )

        self.assertEqual(service.batch_args[0], ["pdf-one"])
        self.assertEqual(
            service.batch_args[1]["internal_copy_ids"],
            ["pdf-one"],
        )
        self.assertEqual(
            result["failures"],
            [
                {"source_id": "busy", "error": "still parsing"},
                {"source_id": "missing", "error": "not found"},
            ],
        )
        self.assertIn("end:pdf-one", events)
        self.assertNotIn("end:busy", events)

    def test_batch_with_no_accepted_sources_exposes_conflicts(self) -> None:
        coordinator, _index, jobs, _service, events = self._fixture()
        jobs.conflicts["busy"] = "still parsing"

        with self.assertRaises(BatchDeletionConflict) as raised:
            coordinator.remove_many(["busy"])

        self.assertEqual(str(raised.exception), "still parsing")
        self.assertEqual(
            raised.exception.failures,
            [{"source_id": "busy", "error": "still parsing"}],
        )
        self.assertNotIn("end:busy", events)

    def test_batch_failure_carries_reservation_failures(self) -> None:
        coordinator, _index, jobs, service, _events = self._fixture()
        jobs.conflicts["busy"] = "still parsing"
        service.batch_error = RuntimeError("rollback incomplete")

        with self.assertRaises(DocumentDeletionFailed) as raised:
            coordinator.remove_many(["pdf-one", "busy"])

        self.assertEqual(
            raised.exception.failures,
            [{"source_id": "busy", "error": "still parsing"}],
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from src.me_finder.app_context import AppPaths
from src.me_finder.application.bibliographic_metadata_coordinator import (
    BibliographicMetadataCoordinator,
    BibliographicMetadataError,
    BibliographicMetadataQueueError,
)
from src.me_finder.import_queue import (
    ImportQueueClosedError,
    ImportQueueFullError,
)


class FakeQueries:
    def __init__(self) -> None:
        self.configured = {}
        self.candidates = []
        self.detected = {}
        self.batch_active = None
        self.batch_calls = 0

    def configured_document(self, source_file_id):
        document = self.configured[source_file_id]
        return Path("config.json"), {"documents": [document]}, document

    def detect_bibliographic_metadata(self, source_file_id, *, force=False):
        result = self.detected[source_file_id]
        if isinstance(result, Exception):
            raise result
        return dict(result)

    def batch_metadata_candidates(
        self, *, additional_active_source_ids=()
    ):
        self.batch_calls += 1
        self.batch_active = set(additional_active_source_ids)
        return copy.deepcopy(self.candidates)


class FakeIndexRuntime:
    def __init__(self, events) -> None:
        self.events = events
        self.suspend_count = 0
        self.reopen_count = 0
        self.reopen_errors = []

    @contextmanager
    def mutation(self):
        self.events.append("mutation_enter")
        try:
            yield
        finally:
            self.events.append("mutation_exit")

    def suspend(self):
        self.suspend_count += 1
        self.events.append("suspend")

    def reopen(self, *, attempts=1):
        self.reopen_count += 1
        self.events.append("reopen")
        if self.reopen_errors:
            raise self.reopen_errors.pop(0)
        return True


class FakeDurableOperations:
    def __init__(self, events) -> None:
        self.events = events

    @contextmanager
    def operation(self):
        self.events.append("durable_enter")
        try:
            yield
        finally:
            self.events.append("durable_exit")


class FakeJobs:
    def __init__(self) -> None:
        self.running = None
        self.registration_race_winner = None
        self.registered = []
        self.submitted = None
        self.submit_error = None
        self.updates = []

    def processing_job_with_prefix(self, job_id_prefix):
        return self.running

    def register_background_job(self, job):
        self.registered.append(dict(job))

    def register_background_job_unless_processing(self, job_id_prefix, job):
        if self.registration_race_winner is not None:
            return dict(self.registration_race_winner)
        self.register_background_job(job)
        return None

    def submit_background_task(self, task, *args):
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted = (task, args)

    def update_import_job(self, job_id, **updates):
        self.updates.append((job_id, dict(updates)))

    def run_submitted(self):
        task, args = self.submitted
        task(*args)


class BibliographicMetadataCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = AppPaths.create(
            self.root,
            index_path=self.root / "data" / "index.sqlite3",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _fixture(self, documents):
        events = []
        config = {"documents": copy.deepcopy(documents)}
        saves = []
        database_updates = []
        queries = FakeQueries()
        index_runtime = FakeIndexRuntime(events)
        jobs = FakeJobs()

        @contextmanager
        def lock_config(path):
            self.assertEqual(path, self.paths.config_root / "pdf_imports.json")
            events.append("config_enter")
            try:
                yield config
            finally:
                events.append("config_exit")

        def save_config(path, payload):
            self.assertEqual(path, self.paths.config_root / "pdf_imports.json")
            events.append("save_config")
            saves.append(copy.deepcopy(payload))

        def update_database(path, source_file_id, metadata):
            self.assertEqual(path, self.paths.index_path)
            events.append("update_database")
            database_updates.append((source_file_id, copy.deepcopy(metadata)))
            return {"source_files": 1}

        coordinator = BibliographicMetadataCoordinator(
            self.paths,
            queries,
            index_runtime,
            FakeDurableOperations(events),
            jobs,
            lock_config=lock_config,
            save_config=save_config,
            update_database=update_database,
            canonicalize=lambda payload: dict(payload),
            missing_fields=lambda _metadata: ["author"],
            build_manual_metadata=lambda payload, previous: {
                **dict(previous or {}),
                **dict(payload),
                "metadata_source": "manual",
            },
            metadata_fields=("title", "author", "publish_year"),
        )
        return {
            "coordinator": coordinator,
            "queries": queries,
            "index": index_runtime,
            "jobs": jobs,
            "events": events,
            "config": config,
            "saves": saves,
            "database_updates": database_updates,
            "update_database": update_database,
        }

    def test_persist_detected_preserves_transaction_order_and_projection(self) -> None:
        fixture = self._fixture(
            [{"source_file_id": "pdf-one", "title": "旧标题"}]
        )

        result = fixture["coordinator"].persist_detected(
            "pdf-one",
            {
                "title": "新标题",
                "publish_year": "2026",
                "document_type": "book",
                "metadata_status": "complete",
                "metadata_source": "front_matter",
            },
        )

        self.assertEqual(result["metadata_missing_fields"], ["author"])
        saved_document = fixture["saves"][0]["documents"][0]
        self.assertEqual(saved_document["title"], "新标题")
        self.assertEqual(saved_document["publication_year"], "2026")
        self.assertEqual(saved_document["bibliographic_metadata"], result)
        self.assertEqual(
            fixture["database_updates"], [("pdf-one", result)]
        )
        self.assertEqual(
            fixture["events"],
            [
                "durable_enter",
                "mutation_enter",
                "config_enter",
                "save_config",
                "suspend",
                "update_database",
                "reopen",
                "config_exit",
                "mutation_exit",
                "durable_exit",
            ],
        )

    def test_database_failure_restores_config_and_reopens_runtime(self) -> None:
        fixture = self._fixture(
            [{"source_file_id": "pdf-one", "title": "旧标题"}]
        )

        def fail_update(_path, _source_file_id, _metadata):
            fixture["events"].append("update_database")
            raise RuntimeError("database failed")

        fixture["coordinator"]._update_database = fail_update

        with self.assertRaisesRegex(RuntimeError, "database failed"):
            fixture["coordinator"].persist_detected(
                "pdf-one", {"title": "新标题"}
            )

        self.assertEqual(len(fixture["saves"]), 2)
        self.assertEqual(
            fixture["saves"][-1]["documents"][0]["title"], "旧标题"
        )
        self.assertEqual(fixture["index"].reopen_count, 1)

    def test_reload_failure_after_database_commit_does_not_rollback_config(self) -> None:
        fixture = self._fixture(
            [{"source_file_id": "pdf-one", "title": "旧标题"}]
        )
        fixture["index"].reopen_errors.append(RuntimeError("reload failed"))

        with self.assertRaisesRegex(RuntimeError, "reload failed"):
            fixture["coordinator"].persist_detected(
                "pdf-one", {"title": "新标题"}
            )

        self.assertEqual(len(fixture["saves"]), 1)
        self.assertEqual(
            fixture["saves"][0]["documents"][0]["title"], "新标题"
        )
        self.assertEqual(fixture["index"].reopen_count, 2)

    def test_missing_document_fails_before_index_is_suspended(self) -> None:
        fixture = self._fixture([])

        with self.assertRaisesRegex(
            BibliographicMetadataError, "PDF 配置中找不到"
        ):
            fixture["coordinator"].persist_detected(
                "missing", {"title": "标题"}
            )

        self.assertEqual(fixture["saves"], [])
        self.assertEqual(fixture["index"].suspend_count, 0)

    def test_save_manual_builds_from_current_configured_document(self) -> None:
        fixture = self._fixture(
            [
                {
                    "source_file_id": "pdf-one",
                    "title": "旧标题",
                    "author": "旧作者",
                }
            ]
        )
        fixture["queries"].configured["pdf-one"] = {
            "source_file_id": "pdf-one",
            "title": "旧标题",
            "author": "旧作者",
        }

        result = fixture["coordinator"].save_manual(
            "pdf-one", {"title": "人工标题"}
        )

        self.assertEqual(result["title"], "人工标题")
        self.assertEqual(result["author"], "旧作者")
        self.assertEqual(result["metadata_source"], "manual")
        self.assertEqual(fixture["database_updates"][0][0], "pdf-one")

    def test_metadata_lock_serializes_concurrent_writes(self) -> None:
        fixture = self._fixture(
            [
                {"source_file_id": "pdf-one"},
                {"source_file_id": "pdf-two"},
            ]
        )
        active = 0
        max_active = 0
        counter_lock = threading.Lock()
        start = threading.Barrier(3)

        def slow_update(_path, _source_file_id, _metadata):
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with counter_lock:
                active -= 1
            return {"source_files": 1}

        fixture["coordinator"]._update_database = slow_update

        def persist(source_file_id):
            start.wait()
            fixture["coordinator"].persist_detected(
                source_file_id, {"title": source_file_id}
            )

        threads = [
            threading.Thread(target=persist, args=(source_file_id,))
            for source_file_id in ("pdf-one", "pdf-two")
        ]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(max_active, 1)

    def test_start_batch_reuses_running_job_and_handles_empty_candidates(self) -> None:
        fixture = self._fixture([])
        fixture["jobs"].running = {"job_id": "batchmeta-running"}

        running = fixture["coordinator"].start_batch()

        self.assertEqual(
            running,
            {"job_id": "batchmeta-running", "already_running": True},
        )
        self.assertEqual(fixture["queries"].batch_calls, 0)

        fixture["jobs"].running = None
        empty = fixture["coordinator"].start_batch(
            additional_active_source_ids={"calibrating"}
        )
        self.assertEqual(
            empty,
            {"job_id": None, "candidates": 0},
        )
        self.assertEqual(fixture["queries"].batch_active, {"calibrating"})

    def test_start_batch_reuses_job_that_wins_registration_race(self) -> None:
        fixture = self._fixture([])
        fixture["queries"].candidates = [
            {"source_file_id": "pdf-one", "title": "标题"}
        ]
        fixture["jobs"].registration_race_winner = {
            "job_id": "batchmeta-winner"
        }

        result = fixture["coordinator"].start_batch()

        self.assertEqual(
            result,
            {"job_id": "batchmeta-winner", "already_running": True},
        )
        self.assertEqual(fixture["jobs"].registered, [])
        self.assertIsNone(fixture["jobs"].submitted)

    def test_batch_records_updated_unchanged_and_failed_items(self) -> None:
        fixture = self._fixture([{"source_file_id": "updated"}])
        fixture["queries"].candidates = [
            {
                "source_file_id": "updated",
                "title": "待更新",
                "bibliographic_metadata": {"title": "旧标题"},
            },
            {
                "source_file_id": "unchanged",
                "title": "无变化",
                "bibliographic_metadata": {"title": "相同标题"},
            },
            {
                "source_file_id": "failed",
                "title": "识别失败",
                "bibliographic_metadata": {"title": "旧标题"},
            },
        ]
        fixture["queries"].detected = {
            "updated": {"title": "新标题"},
            "unchanged": {"title": "相同标题"},
            "failed": RuntimeError("识别失败原因"),
        }

        started = fixture["coordinator"].start_batch()
        fixture["jobs"].run_submitted()

        self.assertTrue(started["job_id"].startswith("batchmeta-"))
        self.assertEqual(started["candidates"], 3)
        self.assertFalse(started["already_running"])
        self.assertEqual(
            fixture["jobs"].registered[0]["message"],
            "准备识别 3 部文献…",
        )
        final = fixture["jobs"].updates[-1][1]
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["batch_updated"], 1)
        self.assertEqual(final["batch_unchanged"], 1)
        self.assertEqual(
            final["message"],
            "批量识别完成：更新 1 部，无变化 1 部，失败 1 部",
        )
        self.assertEqual(
            final["batch_failures"],
            [
                {
                    "source_file_id": "failed",
                    "title": "识别失败",
                    "error": "识别失败原因",
                }
            ],
        )
        progress = [update for _job_id, update in fixture["jobs"].updates[:-1]]
        self.assertEqual(
            [item["message"] for item in progress],
            [
                "正在识别 1/3：待更新",
                "正在识别 2/3：无变化",
                "正在识别 3/3：识别失败",
            ],
        )

    def test_queue_errors_mark_registered_batch_failed_and_propagate(self) -> None:
        for error in (
            ImportQueueFullError("queue full"),
            ImportQueueClosedError("queue closed"),
        ):
            with self.subTest(error=type(error).__name__):
                fixture = self._fixture([])
                fixture["queries"].candidates = [
                    {"source_file_id": "pdf-one", "title": "标题"}
                ]
                fixture["jobs"].submit_error = error

                with self.assertRaises(
                    BibliographicMetadataQueueError
                ) as raised:
                    fixture["coordinator"].start_batch()

                self.assertEqual(
                    raised.exception.job_id,
                    fixture["jobs"].registered[-1]["job_id"],
                )
                self.assertEqual(str(raised.exception), str(error))
                failed = fixture["jobs"].updates[-1][1]
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["phase"], "queue_failed")
                self.assertEqual(
                    failed["message"], "批量识别任务未能进入处理队列。"
                )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, Iterator, Mapping, Optional

from src.me_finder.app_context import AppPaths
from src.me_finder.application.document_query_service import DocumentQueryError
from src.me_finder.application.page_mapping_coordinator import (
    PageMappingCoordinator,
)
from src.me_finder.mineru_api import MinerUError


class RecordingGate:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    @contextmanager
    def operation(self) -> Iterator[None]:
        self.events.append("enter:durable")
        try:
            yield
        finally:
            self.events.append("exit:durable")


class RecordingIndex:
    def __init__(
        self,
        events: list[object],
        *,
        source: Optional[Dict[str, object]] = None,
    ) -> None:
        self.events = events
        self.source_record = source
        self.reopen_results: list[object] = []

    @contextmanager
    def mutation(self) -> Iterator[None]:
        self.events.append("enter:mutation")
        try:
            yield
        finally:
            self.events.append("exit:mutation")

    def source(self, source_file_id: str) -> Optional[Dict[str, object]]:
        self.events.append(("source", source_file_id))
        return copy.deepcopy(self.source_record)

    def suspend(self) -> None:
        self.events.append("suspend")

    def reopen(self, *, attempts: int = 1) -> bool:
        self.events.append(("reopen", attempts))
        result = self.reopen_results.pop(0) if self.reopen_results else True
        if isinstance(result, Exception):
            raise result
        return bool(result)


class RecordingJobs:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.registered: list[Dict[str, object]] = []
        self.updates: list[tuple[str, Dict[str, object]]] = []
        self.rebuild_error: Optional[Exception] = None

    def register_background_job(self, job: Mapping[str, object]) -> None:
        snapshot = copy.deepcopy(dict(job))
        self.registered.append(snapshot)
        self.events.append(("register", snapshot))

    def rebuild_runtime_index(
        self,
        job_id: str,
        expected_source_ids: Optional[list[str]] = None,
    ) -> set[str]:
        self.events.append(("rebuild", job_id))
        if self.rebuild_error is not None:
            raise self.rebuild_error
        return set()

    def update_import_job(self, job_id: str, **updates: object) -> None:
        snapshot = copy.deepcopy(updates)
        self.updates.append((job_id, snapshot))
        self.events.append(("update", job_id, snapshot))


class RecordingQueries:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.error: Optional[DocumentQueryError] = None

    def source_path(self, source_file_id: str) -> Path:
        if self.error is not None:
            raise self.error
        return self.path


class ConfigHarness:
    def __init__(
        self,
        events: list[object],
        config: Dict[str, object],
    ) -> None:
        self.events = events
        self.config = copy.deepcopy(config)
        self.saved: list[Dict[str, object]] = []

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.events.append("enter:config")
        try:
            yield
        finally:
            self.events.append("exit:config")

    def load(self, path: Path) -> Dict[str, object]:
        self.events.append(("load", path))
        return copy.deepcopy(self.config)

    def save(self, path: Path, config: Dict[str, object]) -> None:
        self.events.append(("save", path))
        self.config = copy.deepcopy(config)
        self.saved.append(copy.deepcopy(config))


class PageMappingCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "config").mkdir()
        (self.root / "data").mkdir()
        self.config_path = self.root / "config" / "pdf_imports.json"
        self.config_path.write_text("{}", encoding="utf-8")
        self.pdf_path = self.root / "document.pdf"
        self.pdf_path.touch()
        self.paths = AppPaths.create(
            self.root,
            index_path=self.root / "data" / "index.sqlite3",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _config(
        mapping: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        return {
            "documents": [
                {
                    "source_file_id": "pdf-one",
                    "file_name": "document.pdf",
                    "page_mapping": mapping or {"segments": []},
                }
            ]
        }

    def _coordinator(
        self,
        *,
        events: list[object],
        config: ConfigHarness,
        index: Optional[RecordingIndex] = None,
        jobs: Optional[RecordingJobs] = None,
        queries: Optional[RecordingQueries] = None,
        extractor=None,
        apply_mapping=None,
    ) -> tuple[
        PageMappingCoordinator,
        RecordingIndex,
        RecordingJobs,
        RecordingQueries,
    ]:
        resolved_index = index or RecordingIndex(events)
        resolved_jobs = jobs or RecordingJobs(events)
        resolved_queries = queries or RecordingQueries(self.pdf_path)
        coordinator = PageMappingCoordinator(
            self.paths,
            resolved_index,
            RecordingGate(events),
            resolved_queries,
            resolved_jobs,
            extract_pdf=extractor or (lambda *args, **kwargs: {}),
            config_lock=config.lock,
            load_config=config.load,
            save_config=config.save,
            apply_mapping=apply_mapping or (lambda *args, **kwargs: {}),
        )
        return coordinator, resolved_index, resolved_jobs, resolved_queries

    def test_manual_mapping_persists_then_rebuilds_with_original_lock_order(
        self,
    ) -> None:
        events: list[object] = []
        config = ConfigHarness(events, self._config())
        coordinator, _index, jobs, _queries = self._coordinator(
            events=events,
            config=config,
        )

        coordinator.apply_manual_page_mapping(
            "pdf-one",
            [
                {
                    "pdf_page_start": 1,
                    "pdf_page_end": 3,
                    "citation_page_start": "7",
                    "number_style": "arabic",
                }
            ],
        )

        mapping = config.config["documents"][0]["page_mapping"]
        self.assertEqual(mapping["segments"][0]["citation_page_start"], "7")
        self.assertEqual(mapping["validated_by"], "manual_ui")
        self.assertEqual(mapping["mapping_origin"], "manual")
        self.assertEqual(mapping["mapping_status"], "manual_mapped")
        job = jobs.registered[0]
        self.assertTrue(str(job["job_id"]).startswith("calibration-"))
        self.assertEqual(job["message"], "正在应用页码校准并重建索引…")
        self.assertEqual(
            jobs.updates,
            [
                (
                    job["job_id"],
                    {
                        "status": "completed",
                        "phase": "completed",
                        "message": "页码校准已生效",
                    },
                )
            ],
        )
        self.assertLess(events.index("enter:durable"), events.index("enter:mutation"))
        self.assertLess(events.index("enter:mutation"), events.index("enter:config"))
        self.assertLess(
            events.index("exit:config"), self._event_index(events, "rebuild")
        )
        self.assertLess(
            self._event_index(events, "rebuild"), events.index("exit:mutation")
        )

    def test_manual_rebuild_failure_keeps_config_and_marks_job_failed(self) -> None:
        events: list[object] = []
        config = ConfigHarness(events, self._config())
        jobs = RecordingJobs(events)
        jobs.rebuild_error = RuntimeError("rebuild broke")
        coordinator, _index, _jobs, _queries = self._coordinator(
            events=events,
            config=config,
            jobs=jobs,
        )

        with self.assertRaisesRegex(RuntimeError, "rebuild broke"):
            coordinator.apply_manual_page_mapping("pdf-one", [])

        self.assertEqual(
            config.config["documents"][0]["page_mapping"]["mapping_status"],
            "unmapped",
        )
        self.assertEqual(jobs.updates[-1][1]["status"], "failed")
        self.assertEqual(jobs.updates[-1][1]["message"], "rebuild broke")

    def test_detection_exposes_a_safe_active_snapshot_and_clears_it(self) -> None:
        events: list[object] = []
        original_mapping = {
            "segments": [
                {
                    "pdf_page_start": 0,
                    "pdf_page_end": 2,
                    "citation_page_start": "1",
                }
            ],
            "validated_by": "manual_ui",
        }
        config = ConfigHarness(events, self._config(original_mapping))
        extractor_entered = threading.Event()
        release_extractor = threading.Event()
        received_config: list[Dict[str, object]] = []

        def extract_pdf(
            path: Path,
            root: Path,
            document: Dict[str, object],
            *,
            parsed_dir: object,
        ) -> Dict[str, object]:
            events.append("extract")
            received_config.append(copy.deepcopy(document))
            extractor_entered.set()
            release_extractor.wait(timeout=5)
            return {
                "source_files": [
                    {
                        "pdf_profile": {
                            "auto_page_mapping": {
                                "mapping_status": "auto_mapped_high",
                                "applied_segments": [{"pdf_page_start": 0}],
                            }
                        }
                    }
                ]
            }

        coordinator, _index, _jobs, _queries = self._coordinator(
            events=events,
            config=config,
            extractor=extract_pdf,
        )
        result: list[Dict[str, object]] = []
        failures: list[BaseException] = []

        def detect() -> None:
            try:
                result.append(coordinator.detect_auto_page_mapping("pdf-one"))
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=detect)
        worker.start()
        self.assertTrue(extractor_entered.wait(timeout=5))
        active = coordinator.active_source_ids()
        self.assertEqual(active, {"pdf-one"})
        active.clear()
        self.assertEqual(coordinator.active_source_ids(), {"pdf-one"})
        release_extractor.set()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(coordinator.active_source_ids(), set())
        self.assertEqual(received_config[0]["page_mapping"]["segments"], [])
        self.assertIsNone(received_config[0]["page_mapping"]["validated_by"])
        self.assertEqual(
            config.config["documents"][0]["page_mapping"],
            original_mapping,
        )
        self.assertTrue(result[0]["manual_mapping_present"])
        self.assertTrue(result[0]["dry_run"])
        self.assertEqual(result[0]["source_file"], "document.pdf")
        self.assertLess(events.index("enter:mutation"), events.index("extract"))
        self.assertLess(events.index("extract"), events.index("exit:mutation"))

    def test_detection_returns_source_missing_result_and_clears_active(self) -> None:
        events: list[object] = []
        config = ConfigHarness(events, self._config())
        queries = RecordingQueries(self.pdf_path)
        queries.error = DocumentQueryError("原始文件不存在。")
        coordinator, _index, _jobs, _queries = self._coordinator(
            events=events,
            config=config,
            queries=queries,
        )

        result = coordinator.detect_auto_page_mapping("pdf-one")

        self.assertEqual(result["mapping_status"], "source_missing")
        self.assertEqual(result["failure_reasons"], ["source_missing"])
        self.assertEqual(result["selected_segments"], [])
        self.assertEqual(coordinator.active_source_ids(), set())

    def test_live_auto_mapping_updates_database_inside_existing_lock_order(
        self,
    ) -> None:
        events: list[object] = []
        config = ConfigHarness(events, self._config())
        applied_calls: list[tuple[object, ...]] = []

        def apply_mapping(
            index_path: Path,
            source_file_id: str,
            segments: list[Dict[str, object]],
            *,
            auto_mapping: Dict[str, object],
            mapping_status: str,
        ) -> Dict[str, int]:
            events.append("apply")
            applied_calls.append(
                (
                    index_path,
                    source_file_id,
                    copy.deepcopy(segments),
                    copy.deepcopy(auto_mapping),
                    mapping_status,
                )
            )
            return {"pages": 4, "paragraphs": 12}

        coordinator, _index, _jobs, _queries = self._coordinator(
            events=events,
            config=config,
            apply_mapping=apply_mapping,
        )

        updated = coordinator.apply_live_auto_mapping(
            "pdf-one",
            [
                {
                    "pdf_page_start": 0,
                    "pdf_page_end": 3,
                    "citation_page_start": "1",
                    "mapping_confidence": 0.96,
                    "confidence_level": "high",
                }
            ],
            {"detection_method": "edge_sequence"},
            False,
        )

        self.assertEqual(updated, {"pages": 4, "paragraphs": 12})
        self.assertEqual(applied_calls[0][0], self.paths.index_path)
        self.assertEqual(applied_calls[0][1], "pdf-one")
        self.assertEqual(applied_calls[0][4], "auto_mapped_high")
        mapping = config.config["documents"][0]["page_mapping"]
        self.assertEqual(mapping["validated_by"], "auto_mapping_ui")
        self.assertEqual(mapping["mapping_origin"], "auto")
        self.assertEqual(mapping["mapping_status"], "auto_mapped_high")
        self.assertLess(events.index("enter:mutation"), events.index("enter:durable"))
        self.assertLess(events.index("enter:durable"), events.index("enter:config"))
        self.assertLess(events.index("suspend"), events.index("apply"))
        self.assertLess(events.index("apply"), self._event_index(events, "reopen"))
        self.assertLess(
            self._event_index(events, "reopen"), events.index("exit:config")
        )

    def test_live_auto_mapping_rolls_back_config_when_database_write_fails(
        self,
    ) -> None:
        events: list[object] = []
        initial = self._config()
        config = ConfigHarness(events, initial)

        def fail_apply(*args: object, **kwargs: object) -> Dict[str, int]:
            raise RuntimeError("database write failed")

        coordinator, _index, _jobs, _queries = self._coordinator(
            events=events,
            config=config,
            apply_mapping=fail_apply,
        )

        with self.assertRaisesRegex(RuntimeError, "database write failed"):
            coordinator.apply_live_auto_mapping(
                "pdf-one",
                [
                    {
                        "pdf_page_start": 0,
                        "pdf_page_end": 1,
                        "citation_page_start": "1",
                    }
                ],
                {},
                False,
            )

        self.assertEqual(len(config.saved), 2)
        self.assertEqual(config.config, initial)
        self.assertEqual(
            [event for event in events if self._event_name(event) == "reopen"],
            [("reopen", 1)],
        )

    def test_live_auto_mapping_keeps_config_after_database_commit(self) -> None:
        events: list[object] = []
        initial = self._config()
        config = ConfigHarness(events, initial)
        index = RecordingIndex(events)
        index.reopen_results = [RuntimeError("first reopen failed"), True]
        coordinator, _index, _jobs, _queries = self._coordinator(
            events=events,
            config=config,
            index=index,
            apply_mapping=lambda *args, **kwargs: {"pages": 1},
        )

        with self.assertRaisesRegex(RuntimeError, "first reopen failed"):
            coordinator.apply_live_auto_mapping(
                "pdf-one",
                [
                    {
                        "pdf_page_start": 0,
                        "pdf_page_end": 0,
                        "citation_page_start": "1",
                    }
                ],
                {},
                False,
            )

        self.assertEqual(len(config.saved), 1)
        self.assertNotEqual(config.config, initial)
        self.assertEqual(
            [event for event in events if self._event_name(event) == "reopen"],
            [("reopen", 1), ("reopen", 1)],
        )

    def test_live_auto_mapping_requires_confirmation_before_manual_replace(
        self,
    ) -> None:
        events: list[object] = []
        config = ConfigHarness(
            events,
            self._config(
                {
                    "segments": [
                        {
                            "pdf_page_start": 0,
                            "pdf_page_end": 1,
                            "citation_page_start": "1",
                        }
                    ],
                    "validated_by": "manual_ui",
                }
            ),
        )
        coordinator, _index, _jobs, _queries = self._coordinator(
            events=events,
            config=config,
        )

        with self.assertRaisesRegex(MinerUError, "必须明确确认"):
            coordinator.apply_live_auto_mapping("pdf-one", [], {}, False)

        self.assertEqual(config.saved, [])

    def test_accept_promotes_segments_and_rebuilds(self) -> None:
        events: list[object] = []
        config = ConfigHarness(events, self._config())
        index = RecordingIndex(
            events,
            source={
                "source_file_id": "pdf-one",
                "pdf_profile": {
                    "auto_page_mapping": {
                        "applied_segments": [
                            {
                                "pdf_page_start": 0,
                                "pdf_page_end": 2,
                                "citation_page_start": "i",
                                "number_style": "roman_lower",
                                "mapping_confidence": 0.97,
                                "mapping_evidence": {"kind": "label"},
                                "layout_mode": "spread",
                                "reading_direction": "rtl",
                                "gutter_x": 0.47,
                            },
                            "ignored",
                        ]
                    }
                },
            },
        )
        coordinator, _index, jobs, _queries = self._coordinator(
            events=events,
            config=config,
            index=index,
        )

        count = coordinator.accept_auto_page_mapping("pdf-one")

        self.assertEqual(count, 1)
        segment = config.config["documents"][0]["page_mapping"]["segments"][0]
        self.assertEqual(segment["method"], "manual_segment")
        self.assertEqual(segment["label"], "已接受自动页码映射")
        self.assertEqual(segment["reading_direction"], "rtl")
        self.assertEqual(segment["gutter_x"], 0.47)
        self.assertEqual(
            config.config["documents"][0]["page_mapping"]["validated_by"],
            "auto_mapping_accepted",
        )
        self.assertTrue(str(jobs.registered[0]["job_id"]).startswith("auto-map-"))
        self.assertEqual(jobs.updates[-1][1]["message"], "自动页码映射已接受")
        self.assertLess(events.index("enter:durable"), events.index("enter:mutation"))
        self.assertLess(events.index("enter:mutation"), events.index("enter:config"))

    def test_accept_rebuild_failure_marks_job_failed(self) -> None:
        events: list[object] = []
        config = ConfigHarness(events, self._config())
        index = RecordingIndex(
            events,
            source={
                "pdf_profile": {
                    "auto_page_mapping": {
                        "applied_segments": [
                            {
                                "pdf_page_start": 0,
                                "pdf_page_end": 0,
                                "citation_page_start": "1",
                            }
                        ]
                    }
                }
            },
        )
        jobs = RecordingJobs(events)
        jobs.rebuild_error = RuntimeError("accept rebuild failed")
        coordinator, _index, _jobs, _queries = self._coordinator(
            events=events,
            config=config,
            index=index,
            jobs=jobs,
        )

        with self.assertRaisesRegex(RuntimeError, "accept rebuild failed"):
            coordinator.accept_auto_page_mapping("pdf-one")

        self.assertEqual(jobs.updates[-1][1]["status"], "failed")
        self.assertEqual(jobs.updates[-1][1]["message"], "accept rebuild failed")

    def test_accept_rebuild_business_error_marks_job_failed(self) -> None:
        events: list[object] = []
        config = ConfigHarness(events, self._config())
        index = RecordingIndex(
            events,
            source={
                "pdf_profile": {
                    "auto_page_mapping": {
                        "applied_segments": [
                            {
                                "pdf_page_start": 0,
                                "pdf_page_end": 0,
                                "citation_page_start": "1",
                            }
                        ]
                    }
                }
            },
        )
        jobs = RecordingJobs(events)
        jobs.rebuild_error = MinerUError("accept rebuild rejected")
        coordinator, _index, _jobs, _queries = self._coordinator(
            events=events,
            config=config,
            index=index,
            jobs=jobs,
        )

        with self.assertRaisesRegex(MinerUError, "accept rebuild rejected"):
            coordinator.accept_auto_page_mapping("pdf-one")

        self.assertEqual(jobs.updates[-1][1]["status"], "failed")
        self.assertEqual(jobs.updates[-1][1]["message"], "accept rebuild rejected")

    def test_accept_validation_error_does_not_create_failed_job(self) -> None:
        events: list[object] = []
        config = ConfigHarness(events, self._config())
        index = RecordingIndex(
            events,
            source={"pdf_profile": {"auto_page_mapping": {}}},
        )
        coordinator, _index, jobs, _queries = self._coordinator(
            events=events,
            config=config,
            index=index,
        )

        with self.assertRaisesRegex(MinerUError, "没有可接受"):
            coordinator.accept_auto_page_mapping("pdf-one")

        self.assertEqual(jobs.registered, [])
        self.assertEqual(jobs.updates, [])

    @staticmethod
    def _event_name(event: object) -> object:
        return event[0] if isinstance(event, tuple) else event

    @classmethod
    def _event_index(cls, events: list[object], name: str) -> int:
        return next(
            index
            for index, event in enumerate(events)
            if cls._event_name(event) == name
        )


if __name__ == "__main__":
    unittest.main()

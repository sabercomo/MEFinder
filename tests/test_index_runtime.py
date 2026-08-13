from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.me_finder.app_context import AppPaths
from src.me_finder.application.index_runtime import IndexRuntime
from src.me_finder.application.search_service import SearchRequest


class FakeEngine:
    def __init__(self, name: str, source_ids: tuple[str, ...]) -> None:
        self.name = name
        self.close_count = 0
        self.index = {
            "metadata": {"generation": name},
            "source_files": [
                {
                    "source_file_id": source_id,
                    "display_title": f"{name}-{source_id}",
                }
                for source_id in source_ids
            ],
            "volumes": [{"volume_id": f"{name}-volume"}],
            "works": [{"work_id": f"{name}-work"}],
        }

    def close(self) -> None:
        self.close_count += 1

    def search(
        self,
        query: str,
        mode: str = "auto",
        limit: int | str | None = 10,
        source_type: str = "all",
        source_file_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "engine": self.name,
            "query": query,
            "mode": mode,
            "limit": limit,
            "source_type": source_type,
            "source_file_id": source_file_id,
        }


class EngineFactory:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.paths: list[Path] = []

    def __call__(self, path: Path) -> FakeEngine:
        self.paths.append(Path(path))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


class IndexRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name) / "runtime"
        self.index_path = self.root / "data" / "custom.sqlite3"
        self.paths = AppPaths.create(
            self.root,
            index_path=self.index_path,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def runtime(
        self,
        factory: EngineFactory,
        *,
        rebuild_index=None,
        replace_source=None,
    ) -> IndexRuntime:
        return IndexRuntime(
            self.paths,
            engine_factory=factory,
            rebuild_index=(
                rebuild_index
                if rebuild_index is not None
                else self._unexpected_rebuild
            ),
            replace_source=(
                replace_source
                if replace_source is not None
                else self._unexpected_replace
            ),
        )

    def test_initial_catalog_and_ready_reads_use_injected_engine(self) -> None:
        engine = FakeEngine("initial", ("source-one",))
        factory = EngineFactory(engine)
        runtime = self.runtime(factory)

        search = runtime.search(SearchRequest(query="原句"))
        seen_paths: list[Path] = []
        read_result = runtime.run_when_ready(
            lambda path: seen_paths.append(path) or "read-ok"
        )

        self.assertEqual(factory.paths, [self.paths.index_path])
        self.assertEqual(runtime.metadata(), {"generation": "initial"})
        self.assertEqual(
            runtime.source("source-one")["display_title"],  # type: ignore[index]
            "initial-source-one",
        )
        self.assertEqual(
            runtime.catalog()["volumes"],
            [{"volume_id": "initial-volume"}],
        )
        self.assertEqual(search["engine"], "initial")  # type: ignore[index]
        self.assertEqual(read_result, "read-ok")
        self.assertEqual(seen_paths, [self.paths.index_path])

    def test_suspend_blocks_reads_and_reopen_calls_dynamic_factory(self) -> None:
        initial = FakeEngine("initial", ("old",))
        replacement = FakeEngine("replacement", ("new",))
        active_builder = {"call": lambda _path: initial}

        def dynamic_factory(path: Path) -> FakeEngine:
            return active_builder["call"](path)

        runtime = IndexRuntime(
            self.paths,
            engine_factory=dynamic_factory,
            rebuild_index=self._unexpected_rebuild,
            replace_source=self._unexpected_replace,
        )
        runtime.suspend()

        self.assertTrue(runtime.rebuilding)
        self.assertEqual(initial.close_count, 1)
        self.assertIsNone(runtime.search(SearchRequest(query="blocked")))
        self.assertIsNone(runtime.run_when_ready(lambda _path: "blocked"))

        active_builder["call"] = lambda _path: replacement
        self.assertTrue(runtime.reopen())
        self.assertFalse(runtime.rebuilding)
        self.assertEqual(
            runtime.search(SearchRequest(query="ready"))["engine"],  # type: ignore[index]
            "replacement",
        )

    def test_rebuild_publishes_catalog_and_reports_missing_sources(self) -> None:
        initial = FakeEngine("initial", ("old",))
        rebuilt = FakeEngine("rebuilt", ("kept", "new"))
        factory = EngineFactory(initial, rebuilt)
        calls: list[tuple[Path, object, Path]] = []
        progress_updates: list[dict[str, object]] = []

        def rebuild_index(root, on_progress, *, database_path):
            calls.append((Path(root), on_progress, Path(database_path)))
            if on_progress is not None:
                on_progress({"phase": "rebuilding_index"})
            return {}

        runtime = self.runtime(factory, rebuild_index=rebuild_index)
        with runtime.mutation():
            missing = runtime.rebuild(
                progress_updates.append,
                expected_source_ids=("kept", "missing"),
            )

        self.assertEqual(
            calls,
            [
                (
                    self.paths.runtime_root,
                    progress_updates.append,
                    self.paths.index_path,
                )
            ],
        )
        self.assertEqual(progress_updates, [{"phase": "rebuilding_index"}])
        self.assertEqual(missing, {"missing"})
        self.assertEqual(initial.close_count, 1)
        self.assertFalse(runtime.rebuilding)
        self.assertIsNotNone(runtime.source("new"))

    def test_failed_rebuild_recovers_runtime_and_reraises_original_error(self) -> None:
        initial = FakeEngine("initial", ("old",))
        recovered = FakeEngine("recovered", ("old",))
        factory = EngineFactory(initial, recovered)
        failure = RuntimeError("rebuild failed")

        def rebuild_index(*_args, **_kwargs):
            raise failure

        runtime = self.runtime(factory, rebuild_index=rebuild_index)
        with self.assertRaises(RuntimeError) as raised:
            runtime.rebuild()

        self.assertIs(raised.exception, failure)
        self.assertEqual(initial.close_count, 1)
        self.assertFalse(runtime.rebuilding)
        self.assertEqual(
            runtime.search(SearchRequest(query="after recovery"))["engine"],  # type: ignore[index]
            "recovered",
        )

    def test_replace_source_retries_reopen_and_publishes_expected_source(self) -> None:
        initial = FakeEngine("initial", ("old",))
        published = FakeEngine("published", ("new",))
        factory = EngineFactory(
            initial,
            OSError("busy once"),
            OSError("busy twice"),
            published,
        )
        replace_calls: list[tuple[dict[str, object], Path, bool]] = []

        def replace_source(extracted, path, *, backup_existing):
            replace_calls.append((extracted, Path(path), backup_existing))
            return {}

        runtime = self.runtime(factory, replace_source=replace_source)
        extracted = {"source_files": [{"source_file_id": "new"}]}
        with patch(
            "src.me_finder.application.index_runtime.time.sleep"
        ) as sleep:
            runtime.replace_source(
                extracted,
                "new",
                backup_existing=True,
            )

        self.assertEqual(
            replace_calls,
            [(extracted, self.paths.index_path, True)],
        )
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.05, 0.1],
        )
        self.assertEqual(initial.close_count, 1)
        self.assertFalse(runtime.rebuilding)
        self.assertEqual(
            runtime.search(SearchRequest(query="new source"))["engine"],  # type: ignore[index]
            "published",
        )

    def test_failed_replace_recovers_runtime_and_reraises_original_error(self) -> None:
        initial = FakeEngine("initial", ("old",))
        recovered = FakeEngine("recovered", ("old",))
        factory = EngineFactory(initial, recovered)
        failure = RuntimeError("write failed")

        def replace_source(*_args, **_kwargs):
            raise failure

        runtime = self.runtime(factory, replace_source=replace_source)
        with self.assertRaises(RuntimeError) as raised:
            runtime.replace_source({}, "new")

        self.assertIs(raised.exception, failure)
        self.assertFalse(runtime.rebuilding)
        self.assertEqual(
            runtime.search(SearchRequest(query="after recovery"))["engine"],  # type: ignore[index]
            "recovered",
        )

    def test_failed_replace_keeps_write_error_when_reopen_also_fails(
        self,
    ) -> None:
        initial = FakeEngine("initial", ("old",))
        reopen_failures = [
            OSError(f"reopen failed {attempt}") for attempt in range(5)
        ]
        factory = EngineFactory(initial, *reopen_failures)
        write_failure = RuntimeError("write failed")

        def replace_source(*_args, **_kwargs):
            raise write_failure

        runtime = self.runtime(factory, replace_source=replace_source)
        with patch(
            "src.me_finder.application.index_runtime.time.sleep"
        ), patch(
            "src.me_finder.application.index_runtime.logging.exception"
        ) as log_exception:
            with self.assertRaises(RuntimeError) as raised:
                runtime.replace_source({}, "new")

        self.assertIs(raised.exception, write_failure)
        log_exception.assert_called_once()
        self.assertTrue(runtime.rebuilding)
        self.assertIsNone(
            runtime.search(SearchRequest(query="runtime unavailable"))
        )

    def test_failed_rebuild_keeps_recovery_error_when_reopen_fails(self) -> None:
        initial = FakeEngine("initial", ("old",))
        recovery_failure = OSError("recovery failed")
        factory = EngineFactory(initial, recovery_failure)
        rebuild_failure = RuntimeError("rebuild failed")

        def rebuild_index(*_args, **_kwargs):
            raise rebuild_failure

        runtime = self.runtime(factory, rebuild_index=rebuild_index)
        with self.assertRaises(OSError) as raised:
            runtime.rebuild()

        self.assertIs(raised.exception, recovery_failure)
        self.assertIs(raised.exception.__context__, rebuild_failure)
        self.assertTrue(runtime.rebuilding)
        self.assertIsNone(
            runtime.search(SearchRequest(query="runtime unavailable"))
        )

    def test_replace_fails_fast_when_published_catalog_omits_expected_source(
        self,
    ) -> None:
        initial = FakeEngine("initial", ("old",))
        published = FakeEngine("published", ("other",))
        factory = EngineFactory(initial, published)
        runtime = self.runtime(
            factory,
            replace_source=lambda *_args, **_kwargs: {},
        )

        with self.assertRaisesRegex(RuntimeError, "expected"):
            runtime.replace_source({}, "expected")

        self.assertFalse(runtime.rebuilding)
        self.assertIsNotNone(runtime.source("other"))

    def test_shutdown_rejects_engine_publish_but_keeps_committed_catalog(
        self,
    ) -> None:
        initial = FakeEngine("initial", ("old",))
        candidate = FakeEngine("candidate", ("committed",))
        runtime = self.runtime(EngineFactory(initial, candidate))

        runtime.suspend()
        runtime.begin_shutdown()
        published = runtime.reopen()

        self.assertFalse(published)
        self.assertTrue(runtime.closing)
        self.assertFalse(runtime.rebuilding)
        self.assertEqual(candidate.close_count, 1)
        self.assertIsNotNone(runtime.source("committed"))
        self.assertIsNone(runtime.search(SearchRequest(query="closed")))

    def test_reopen_exhaustion_leaves_runtime_unavailable(self) -> None:
        initial = FakeEngine("initial", ("old",))
        failure = OSError("still busy")
        runtime = self.runtime(
            EngineFactory(initial, OSError("busy"), failure)
        )
        runtime.suspend()

        with patch("src.me_finder.application.index_runtime.time.sleep"):
            with self.assertRaises(OSError) as raised:
                runtime.reopen(attempts=2)

        self.assertIs(raised.exception, failure)
        self.assertTrue(runtime.rebuilding)
        self.assertIsNone(runtime.search(SearchRequest(query="unavailable")))

    def test_close_is_terminal_and_releases_the_live_engine_once(self) -> None:
        initial = FakeEngine("initial", ("old",))
        runtime = self.runtime(EngineFactory(initial))

        runtime.close()
        runtime.close()

        self.assertTrue(runtime.closing)
        self.assertEqual(initial.close_count, 1)
        self.assertIsNone(runtime.search(SearchRequest(query="closed")))

    @staticmethod
    def _unexpected_rebuild(*_args, **_kwargs):
        raise AssertionError("rebuild_index should not be called")

    @staticmethod
    def _unexpected_replace(*_args, **_kwargs):
        raise AssertionError("replace_source should not be called")


if __name__ == "__main__":
    unittest.main()

"""Exercise the architecture scanner against small, isolated source trees."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_architecture_boundaries as boundaries


class SQLiteArchitectureGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        package = patch.object(boundaries, "PACKAGE", self.root)
        package.start()
        self.addCleanup(package.stop)

    def write_source(self, name: str, source: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def test_service_execute_calls_are_not_sql(self) -> None:
        for name, source in {
            "application/import_orchestrator.py": "self._parser_executor.execute(request)",
            "application/literature_verification_service.py": "SearchService.execute(engine, request)",
            "application/parallel_passage_service.py": "SearchService.execute(engine, request)",
            "application/script_search.py": "SearchService.execute(engine, request)",
        }.items():
            self.write_source(name, source)
        self.assertEqual(boundaries._sqlite_usage_outside_persistence(), ({}, {}))

    def test_service_module_still_counts_database_calls(self) -> None:
        self.write_source("application/script_search.py", '''
SearchService.execute(engine, request)
connection.execute("SELECT 1")
connection.executemany(sql, rows)
connection.executescript(schema)
''')
        self.assertEqual(boundaries._sqlite_usage_outside_persistence(),
                         ({}, {"application/script_search.py": 3}))

    def test_each_call_counts_including_cursor_and_dynamic_sql(self) -> None:
        self.write_source("search/recall.py", '''
connection.execute("SELECT 1")
self.db().execute(sql, args)
connection.cursor().execute(f"SELECT {columns} FROM paragraphs")
cursor.executemany(sql, rows)
connection.executescript(schema)
''')
        self.assertEqual(boundaries._sqlite_usage_outside_persistence(),
                         ({}, {"search/recall.py": 5}))

    def test_unknown_executor_requires_review(self) -> None:
        self.write_source("new_service.py", '''
worker.execute(request)
SearchService.execute(engine, request)
''')
        self.assertEqual(boundaries._sqlite_usage_outside_persistence(),
                         ({}, {"new_service.py": 2}))

    def test_persistence_is_excluded_and_connection_guard_is_preserved(self) -> None:
        self.write_source("persistence/store.py", '''
sqlite3.connect(path).execute("SELECT 1")
''')
        self.write_source("reader.py", "sqlite3.connect(path)")
        self.assertEqual(boundaries._sqlite_usage_outside_persistence(),
                         ({"reader.py": 1}, {}))

    def test_existing_module_addition_fails_the_repository_gate(self) -> None:
        # Use a real baseline entry: one existing query is allowed, a second is not.
        self.write_source("indexer.py", 'connection.execute("SELECT 1")')
        gate = boundaries.ArchitectureBoundaryTests(
            "test_sql_execute_calls_outside_persistence_only_shrink"
        )
        baseline = boundaries.SQL_EXECUTE_OUTSIDE_PERSISTENCE
        with patch.object(boundaries, "SQL_EXECUTE_OUTSIDE_PERSISTENCE",
                          {"indexer.py": baseline["indexer.py"]}):
            gate.test_sql_execute_calls_outside_persistence_only_shrink()
            self.write_source("indexer.py", '''
connection.execute("SELECT 1")
connection.execute("SELECT 2")
''')
            with self.assertRaises(AssertionError):
                gate.test_sql_execute_calls_outside_persistence_only_shrink()

    def test_new_sql_module_fails_the_repository_gate(self) -> None:
        self.write_source("reader.py", 'cursor.execute(sql)')
        gate = boundaries.ArchitectureBoundaryTests(
            "test_sql_execute_calls_outside_persistence_only_shrink"
        )
        with patch.object(boundaries, "SQL_EXECUTE_OUTSIDE_PERSISTENCE", {}):
            with self.assertRaises(AssertionError):
                gate.test_sql_execute_calls_outside_persistence_only_shrink()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "me_finder"


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_web_boundary_stays_split_by_responsibility(self) -> None:
        # web.py is now only the HTTP composition root + platform PDF openers;
        # the service/route assembly lives in web_runtime.py.  Caps only ratchet
        # down — when a file hits its cap, move a real responsibility out.
        limits = {
            "web.py": 700,
            "web_runtime.py": 950,
            "web_http.py": 800,
            "web_assets.py": 120,
            "database.py": 1650,
        }
        for relative, limit in limits.items():
            lines = (PACKAGE / relative).read_text(encoding="utf-8").splitlines()
            self.assertLessEqual(
                len(lines),
                limit,
                f"{relative} 已超过 {limit} 行，请先拆出新的明确边界。",
            )

    def test_http_transport_does_not_import_concrete_adapters(self) -> None:
        source = (PACKAGE / "web_http.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        internal_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
        }
        self.assertEqual(internal_modules, {"application", "http_range"})

    def test_document_query_application_service_contains_no_sql(self) -> None:
        source = (
            PACKAGE / "application" / "document_query_service.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertNotIn("sqlite3", imported_modules)
        self.assertNotIn("SELECT ", source)

    def test_application_layer_does_not_depend_on_web_transport(self) -> None:
        violations = []
        for path in sorted((PACKAGE / "application").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                if any(
                    module.endswith((".web", ".web_http"))
                    or module in {"web", "web_http"}
                    for module in modules
                ):
                    violations.append(path.name)
        self.assertEqual(violations, [])

    def test_application_layer_does_not_import_sqlite_adapters(self) -> None:
        violations = []
        for path in sorted((PACKAGE / "application").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and (node.module or "").endswith("persistence")
                ):
                    violations.append(path.name)
        self.assertEqual(violations, [])

    def test_frontend_core_state_is_grouped_by_domain(self) -> None:
        state = (PACKAGE / "static" / "js" / "00-state.js").read_text(
            encoding="utf-8"
        )
        for store in (
            "searchStore",
            "libraryStore",
            "parserStore",
            "settingsStore",
            "importStore",
        ):
            self.assertIn(f"const {store} =", state)


if __name__ == "__main__":
    unittest.main()

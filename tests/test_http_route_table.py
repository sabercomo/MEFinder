from __future__ import annotations

import unittest

from src.me_finder.http_route_table import RoutePolicy, RouteTable, route


class RouteTableTests(unittest.TestCase):
    def test_from_maps_unwraps_policy_and_adapts_parameterless_get(self) -> None:
        table = RouteTable.from_maps(
            get_maps=[{"/api/a": lambda params: (200, {"q": params})}],
            parameterless_get_maps=[{"/api/b": lambda: (200, {"b": True})}],
            post_maps=[
                {
                    "/api/c": route(
                        lambda payload: (201, payload),
                        mutates_data_root=True,
                        max_body_bytes=10,
                    ),
                    "/api/raw": route(lambda request: (200, {}), body="raw"),
                }
            ],
        )
        self.assertEqual(table.get("GET", "/api/a").handler({"x": 1}), (200, {"q": {"x": 1}}))
        self.assertEqual(table.get("GET", "/api/b").handler({"ignored": 1}), (200, {"b": True}))
        post = table.get("POST", "/api/c")
        self.assertEqual(post.handler({"k": 1}), (201, {"k": 1}))
        self.assertEqual(post.policy.max_body_bytes, 10)
        self.assertIsNone(table.get("POST", "/api/a"))
        self.assertEqual(table.paths("GET"), {"/api/a", "/api/b"})
        self.assertEqual(table.raw_body_post_paths, {"/api/raw"})
        self.assertEqual(table.data_root_mutating_post_paths, {"/api/c"})

    def test_declared_handler_stays_callable_like_the_bare_handler(self) -> None:
        declared = route(lambda payload: (200, payload), mutates_data_root=True)
        self.assertEqual(declared({"a": 1}), (200, {"a": 1}))
        self.assertEqual(declared.policy, RoutePolicy(mutates_data_root=True))

    def test_duplicate_routes_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate HTTP route: POST /api/x"):
            RouteTable.from_maps(
                post_maps=[{"/api/x": lambda _p: (200, {})}, {"/api/x": lambda _p: (200, {})}]
            )

    def test_get_route_cannot_declare_raw_body(self) -> None:
        with self.assertRaises(ValueError):
            RouteTable.from_maps(get_maps=[{"/api/x": route(lambda _p: (200, {}), body="raw")}])


# 2026-09-25 前 web_http.py 手写维护的两份集合，原样钉在这里：由路由声明推导出的
# 集合必须与之逐项相等（v0.5.7 B2b 行为不变的证据）。以后有意增删时同步改这里。
LEGACY_RAW_BODY_POST_PATHS = frozenset({"/api/import", "/api/import-upload/chunk"})
LEGACY_DATA_ROOT_MUTATING_POST_PATHS = frozenset(
    {
        "/api/preferences", "/api/mineru-accounts", "/api/mineru-accounts/service",
        "/api/mineru-config", "/api/mineru-local", "/api/mineru-local/component",
        "/api/local-ocr", "/api/local-ocr/component", "/api/text-alignment/models",
        "/api/vision-providers", "/api/general-model", "/api/import",
        "/api/import-upload/start", "/api/import-upload/chunk", "/api/import-upload/cancel",
        "/api/import-upload/finish", "/api/mineru-reparse", "/api/import-retry-mineru",
        "/api/import-retry-mineru-local", "/api/import-retry", "/api/import-resume",
        "/api/import-resume-dismiss", "/api/bibliographic-metadata/batch-detect",
        "/api/export-directory/choose", "/api/backup/export", "/api/document/export",
        "/api/backup/import", "/api/import-local", "/api/calibration",
        "/api/bibliographic-metadata/save", "/api/auto-page-mapping/apply",
        "/api/auto-page-mapping/accept", "/api/documents/remove", "/api/documents/remove-batch",
        "/api/document-groups/create", "/api/document-groups/rename",
        "/api/document-groups/delete", "/api/document-groups/add-member",
        "/api/document-groups/remove-member", "/api/document-groups/set-base",
        "/api/document-groups/version-label",
    }
)
LEGACY_BODY_LIMITS = {
    "/api/document/citation": (16 * 1024, False),
    "/api/bibliographic-metadata/parse-cnki-citation": (32 * 1024, True),
    "/api/bibliographic-metadata/lookup-cnki": (32 * 1024, False),
    "/api/bibliographic-metadata/cnki-candidate": (32 * 1024, False),
    "/api/bibliographic-metadata/open-cnki": (32 * 1024, False),
    "/api/import-upload/start": (64 * 1024, False),
    "/api/import-upload/finish": (64 * 1024, False),
    "/api/import-upload/cancel": (64 * 1024, False),
}


class AssembledRouteTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import json
        import tempfile
        from pathlib import Path

        from src.me_finder.app_context import AppContext
        from src.me_finder.database import build_database
        from src.me_finder.web import make_handler

        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name) / "runtime"
        index_path = root / "data" / "index.sqlite3"
        (root / "config").mkdir(parents=True)
        build_database({"metadata": {}}, index_path)
        (root / "config" / "preferences.json").write_text(json.dumps({}), encoding="utf-8")
        cls.handler = make_handler(index_path, app_context=AppContext.create(root, index_path=index_path))
        cls.table = cls.handler.route_table

    @classmethod
    def tearDownClass(cls) -> None:
        cls.handler.close_runtime()
        cls._tmp.cleanup()

    def test_derived_policy_sets_equal_the_legacy_hand_written_sets(self) -> None:
        self.assertEqual(self.table.raw_body_post_paths, LEGACY_RAW_BODY_POST_PATHS)
        self.assertEqual(
            self.table.data_root_mutating_post_paths, LEGACY_DATA_ROOT_MUTATING_POST_PATHS
        )

    def test_body_limits_are_declared_on_their_routes(self) -> None:
        declared = {}
        for path in self.table.paths("POST"):
            policy = self.table.get("POST", path).policy
            if policy.max_body_bytes is not None:
                declared[path] = (policy.max_body_bytes, policy.drain_oversize)
        self.assertEqual(declared, LEGACY_BODY_LIMITS)

    def test_assembled_routes_equal_the_current_http_contract(self) -> None:
        """由注册表导出的路由清单必须与 docs/contracts 当前版本契约逐条相等。"""

        import json
        from pathlib import Path

        contracts = sorted(
            (Path(__file__).resolve().parents[1] / "docs" / "contracts").glob("v*-http-api.json"),
            key=lambda item: tuple(int(part) for part in item.name[1:].split("-")[0].split(".")),
        )
        contract = json.loads(contracts[-1].read_text(encoding="utf-8"))
        self.assertEqual(sorted(self.table.paths("GET")), contract["get"])
        self.assertEqual(sorted(self.table.paths("POST")), contract["post"])

    def test_only_document_pages_keeps_blank_query_values(self) -> None:
        keep_blank = {
            path
            for path in self.table.paths("GET")
            if self.table.get("GET", path).policy.keep_blank_query
        }
        self.assertEqual(keep_blank, {"/api/document/pages"})


if __name__ == "__main__":
    unittest.main()

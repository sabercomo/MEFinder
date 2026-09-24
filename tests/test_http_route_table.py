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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from src.me_finder.http_contract import (
    GET_API_ROUTES,
    HIGH_RISK_WRITE_CONTRACTS,
    POST_API_ROUTES,
)


ROOT = Path(__file__).resolve().parents[1]
WEB_PATH = ROOT / "src" / "me_finder" / "web.py"
# Route literals live in domain assembly functions; web_runtime merges them.
ROUTES_PATH = ROOT / "src" / "me_finder" / "http_routes.py"
HTTP_PATH = ROOT / "src" / "me_finder" / "web_http.py"
CONTRACT_PATH = ROOT / "docs" / "contracts" / "v0.4.9-http-api.json"
WRITE_CONTRACT_PATH = (
    ROOT / "docs" / "contracts" / "v0.5.1-high-risk-write-api.json"
)


def _dictionary_keys(source_path: Path, names: set[str]) -> set[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    routes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in names:
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        routes.update(
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        )
    return routes


class HTTPAPIContractTests(unittest.TestCase):
    def test_json_contract_matches_python_contract(self) -> None:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["release"], "0.4.9")
        self.assertEqual(contract["get"], sorted(GET_API_ROUTES))
        self.assertEqual(contract["post"], sorted(POST_API_ROUTES))

    def test_route_tables_and_special_handlers_are_frozen(self) -> None:
        get_routes = _dictionary_keys(
            ROUTES_PATH, {"get_routes"}
        )
        post_routes = _dictionary_keys(
            ROUTES_PATH, {"post_routes"}
        )
        self.assertEqual(get_routes | {"/api/calibration"}, GET_API_ROUTES)
        self.assertEqual(
            post_routes
            | {
                "/api/import",
                "/api/import-local",
                "/api/import-upload/cancel",
                "/api/import-upload/chunk",
                "/api/import-upload/finish",
                "/api/import-upload/start",
                "/api/search",
            },
            POST_API_ROUTES,
        )

    def test_every_http_transport_api_literal_is_documented(self) -> None:
        source = HTTP_PATH.read_text(encoding="utf-8")
        literals = set(re.findall(r'"(/api/[^"?]+)"', source))
        self.assertLessEqual(literals, GET_API_ROUTES | POST_API_ROUTES)

    def test_high_risk_write_contract_matches_python_contract(self) -> None:
        contract = json.loads(WRITE_CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["release"], "0.5.1")
        self.assertEqual(contract["routes"], HIGH_RISK_WRITE_CONTRACTS)

    def test_high_risk_contract_is_selective_and_covers_priority_domains(self) -> None:
        routes = set(HIGH_RISK_WRITE_CONTRACTS)
        self.assertLess(routes, POST_API_ROUTES)
        self.assertTrue(any(path.startswith("/api/import") for path in routes))
        self.assertIn("/api/vision-providers", routes)
        self.assertIn("/api/mineru-accounts/service", routes)
        self.assertIn("/api/data-location/migrate", routes)

    def test_high_risk_contracts_define_typed_request_and_success_shapes(self) -> None:
        allowed_types = {
            "array",
            "boolean",
            "integer",
            "object",
            "string",
            "string|null",
        }
        for path, contract in HIGH_RISK_WRITE_CONTRACTS.items():
            with self.subTest(path=path):
                request = contract["request"]
                success = contract["success"]
                self.assertEqual(success["status"], [200])
                self.assertTrue(request["required"])
                self.assertTrue(success["required"])
                self.assertLessEqual(
                    set(request["required"].values()), allowed_types
                )
                self.assertLessEqual(
                    set(request["optional"].values()), allowed_types
                )
                self.assertLessEqual(
                    set(success["required"].values()), allowed_types
                )


if __name__ == "__main__":
    unittest.main()

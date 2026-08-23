from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from src.me_finder.component_catalog import (
    COMPONENT_CATALOG_CHECK_INTERVAL_SECONDS,
    ComponentCatalog,
    ComponentCatalogError,
    validate_component_catalog,
)
from src.me_finder.local_ocr_installer import LOCAL_OCR_MANIFEST_FILE


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ComponentCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundled = self.root / "bundled.json"
        self.payload = json.loads(
            LOCAL_OCR_MANIFEST_FILE.read_text(encoding="utf-8")
        )
        self.bundled.write_text(json.dumps(self.payload), encoding="utf-8")

    def test_successful_check_caches_catalog_and_waits_twenty_four_hours(self) -> None:
        now = [1000.0]
        requests = []
        remote = json.loads(json.dumps(self.payload))
        remote["mineru"]["version"] = "3.4.6"
        remote["mineru"]["profiles"]["pipeline"]["package"] = (
            "mineru[pipeline]==3.4.6"
        )
        remote["mineru"]["profiles"]["vlm"]["packages"] = {
            "darwin-arm64": "mineru[core,mlx]==3.4.6",
            "win32-x86_64": "mineru[core,lmdeploy]==3.4.6",
            "linux-x86_64": "mineru[core,vllm]==3.4.6",
        }

        def opener(request, timeout):
            requests.append((request.full_url, timeout))
            return _Response(json.dumps(remote).encode())

        catalog = ComponentCatalog(
            self.root,
            self.bundled,
            opener=opener,
            clock=lambda: now[0],
        )
        result = catalog.check_now()
        self.assertEqual(result["source"], "remote")
        self.assertEqual(len(requests), 1)
        restarted = ComponentCatalog(
            self.root,
            self.bundled,
            opener=opener,
            clock=lambda: now[0],
        )
        restarted.check_now()
        self.assertEqual(len(requests), 1)
        now[0] += COMPONENT_CATALOG_CHECK_INTERVAL_SECONDS
        restarted.check_now()
        self.assertEqual(len(requests), 2)

    def test_failed_check_keeps_bundled_catalog_and_is_also_throttled(self) -> None:
        now = [2000.0]
        requests = []

        def opener(request, timeout):
            requests.append(request.full_url)
            raise OSError("offline")

        catalog = ComponentCatalog(
            self.root,
            self.bundled,
            opener=opener,
            clock=lambda: now[0],
        )
        first = catalog.check_now()
        second = catalog.check_now()
        self.assertEqual(first["source"], "bundled")
        self.assertEqual(first["last_error"], "offline")
        self.assertEqual(second["last_error"], "offline")
        self.assertEqual(len(requests), 1)

    def test_invalid_remote_catalog_never_replaces_valid_cache(self) -> None:
        cached = self.root / "components/catalog/manifest.json"
        cached.parent.mkdir(parents=True)
        cached.write_text(json.dumps(self.payload), encoding="utf-8")

        catalog = ComponentCatalog(
            self.root,
            self.bundled,
            opener=lambda *_args, **_kwargs: _Response(b'{"schema_version": 2}'),
            clock=lambda: 3000.0,
        )
        result = catalog.check_now(force=True)
        self.assertEqual(result["source"], "remote")
        self.assertIn("版本", result["last_error"])
        self.assertEqual(json.loads(cached.read_text()), self.payload)

    def test_catalog_rejects_unpinned_dependencies_and_unsafe_paths(self) -> None:
        unpinned = json.loads(json.dumps(self.payload))
        unpinned["engines"]["ndlocr-lite"]["dependencies"] = ["pillow"]
        with self.assertRaises(ComponentCatalogError):
            validate_component_catalog(unpinned)

        unsafe = json.loads(json.dumps(self.payload))
        unsafe["engines"]["ndlocr-lite"]["script_path"] = "../ocr.py"
        with self.assertRaises(ComponentCatalogError):
            validate_component_catalog(unsafe)


if __name__ == "__main__":
    unittest.main()

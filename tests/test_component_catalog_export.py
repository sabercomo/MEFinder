from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.export_component_catalog import export_catalog


class ComponentCatalogExportTests(unittest.TestCase):
    def test_export_is_valid_and_checksum_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog, checksum = export_catalog(Path(directory))
            payload = json.loads(catalog.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["mineru"]["version"], "3.4.5")
            self.assertTrue(
                checksum.read_text(encoding="utf-8").startswith(
                    hashlib.sha256(catalog.read_bytes()).hexdigest()
                )
            )


if __name__ == "__main__":
    unittest.main()

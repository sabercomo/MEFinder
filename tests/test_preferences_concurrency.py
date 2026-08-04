from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.me_finder.preferences import read_preferences, save_preferences


class PreferenceConcurrencyTests(unittest.TestCase):
    def test_concurrent_updates_preserve_every_field_and_valid_json(self) -> None:
        updates = (
            {"theme": "midnight"},
            {"library_view": "grid"},
            {"calibration_view": "list"},
            {"scan_directories": ["D:/Papers", "E:/Notes"]},
            {"pdf_open_mode": "system"},
            {"auto_update": True},
            {"citation_styles": ["chinese", "gb", "apa"]},
        )
        expected = {
            "theme": "midnight",
            "library_view": "grid",
            "calibration_view": "list",
            "scan_directories": [str(Path("D:/Papers")), str(Path("E:/Notes"))],
            "pdf_open_mode": "system",
            "auto_update": True,
            "citation_styles": ["chinese", "gb", "apa"],
            "citation_style": "chinese",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            start = threading.Barrier(len(updates))

            def write_repeatedly(update: dict[str, object]) -> None:
                start.wait(timeout=5)
                for _ in range(40):
                    save_preferences(update, path)

            with ThreadPoolExecutor(max_workers=len(updates)) as executor:
                futures = [executor.submit(write_repeatedly, update) for update in updates]
                for future in futures:
                    future.result(timeout=20)

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload, expected)
            self.assertEqual(read_preferences(path), expected)
            self.assertFalse(path.with_suffix(path.suffix + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("update_site_release", ROOT / "scripts" / "update_site_release.py")
update_site_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update_site_release)


RELEASE = {
    "tag_name": "v0.6.0",
    "published_at": "2026-10-15T08:00:00Z",
    "assets": [
        {"name": "MEFinder-v0.6.0-windows-setup.exe", "size": 78541957},
        {"name": "MEFinder-v0.6.0-windows-setup.exe.sha256.txt", "size": 101},
        {"name": "MEFinder-v0.6.0-macos-arm64.dmg", "size": 100229379},
        {"name": "MEFinder-v0.5.5-windows-setup.exe", "size": 1},
        {"name": "notes.txt", "size": 5},
    ],
}


class UpdateSiteReleaseTests(unittest.TestCase):
    def test_release_fields_keeps_only_current_version_binaries(self):
        version, date, sizes = update_site_release.release_fields(RELEASE)
        self.assertEqual(version, "0.6.0")
        self.assertEqual(date, "2026-10-15")
        self.assertEqual(sizes, {"windows-setup.exe": "74.9 MB", "macos-arm64.dmg": "95.6 MB"})

    def test_rewrite_updates_checked_in_release_js_and_keeps_dev_fields(self):
        source = (ROOT / "site" / "release.js").read_text(encoding="utf-8")
        out = update_site_release.rewrite(source, "0.6.0", "2026-10-15", {"windows-setup.exe": "80.1 MB"})
        self.assertIn('released: "0.6.0"', out)
        self.assertIn('date: "2026-10-15"', out)
        self.assertIn('"windows-setup.exe": "80.1 MB"', out)
        self.assertNotIn('"windows-portable.zip"', out)
        self.assertIn("dev:", out)
        self.assertIn("devTopic:", out)

    def test_rewrite_refuses_unexpected_shape(self):
        with self.assertRaises(ValueError):
            update_site_release.rewrite("window.MEF_RELEASE = {};", "0.6.0", "2026-10-15", {})


if __name__ == "__main__":
    unittest.main()

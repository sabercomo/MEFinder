"""Optional native GUI gate; regular CI keeps using portable unit/HTTP tests."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(os.environ.get("MEFINDER_NATIVE_READER_TEST") == "1",
                     "set MEFINDER_NATIVE_READER_TEST=1 on a desktop session")
class NativeReaderWindowTests(unittest.TestCase):
    def test_reader_close_and_main_shutdown_exit_without_stranded_bridge_threads(self):
        result = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).with_name("native_reader_window_probe.py"))],
            capture_output=True, text=True, timeout=40,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS: reopen and native close", result.stdout)
        self.assertIn("PASS: GUI and backend cleanup", result.stdout)

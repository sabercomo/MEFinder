"""Regression gates for false-positive native prototype evidence (no GUI here)."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from prototypes.native_host_appkit import NativeHost, REPORT_STEPS, check_reader, main


class NativeHostAcceptanceTests(unittest.TestCase):
    def test_reader_must_have_content_source_anchor_and_highlight(self):
        options = {'sourceId': 'book-a', 'targetIndex': 3, 'anchorId': 'page-3'}
        payload = {'ready': True, 'state': {'open': True, 'sourceId': 'book-a', 'currentIndex': 3},
                   'anchors': ['page-3'], 'marks': ['目标原句']}
        check_reader(payload, options, '目标原句')
        for field, value in [('ready', False), ('anchors', []), ('marks', [])]:
            with self.subTest(field=field), self.assertRaises(AssertionError):
                check_reader({**payload, field: value}, options, '目标原句')
        for field, value in [('sourceId', 'book-b'), ('currentIndex', 2), ('open', False)]:
            broken = deepcopy(payload)
            broken['state'][field] = value
            with self.subTest(field=field), self.assertRaises(AssertionError):
                check_reader(broken, options, '目标原句')

    def test_shutdown_requires_actual_zero_backend_exit(self):
        for code in (0, 1, -9):
            with self.subTest(code=code):
                host = NativeHost.__new__(NativeHost)
                host.windows = []
                host.report = {'exit': {'ok': False}}
                host.backend = Mock(returncode=code)
                host.shutdown()
                host.backend.communicate.assert_called_once_with('stop\n', timeout=20)
                self.assertEqual(host.report['exit']['ok'], code == 0)
                self.assertEqual(host.report['exit']['backend_exit_code'], code)

    def test_timeout_cleanup_must_not_pass(self):
        host = NativeHost.__new__(NativeHost)
        host.windows = []
        host.report = {'exit': {'ok': False}}
        host.backend = Mock()
        host.backend.communicate.side_effect = [subprocess.TimeoutExpired('backend', 20), ('', '')]
        host.shutdown()
        self.assertFalse(host.report['exit']['ok'])
        host.backend.kill.assert_called_once()

    def test_zero_host_exit_without_completed_child_evidence_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            process = Mock(returncode=0)
            process.communicate.return_value = ('', '')
            with patch('sys.argv', ['native', '--report', str(path)]), patch(
                    'prototypes.native_host_appkit.subprocess.Popen', return_value=process):
                self.assertEqual(main(), 1)
            report = json.loads(path.read_text())
            self.assertTrue(all(not report[step]['ok'] for step in REPORT_STEPS))

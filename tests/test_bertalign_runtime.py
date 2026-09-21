"""Exercise optional component lifecycle with fake provisioning and real locks."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.bertalign_backend import BERTALIGN_MODEL_REVISION
from src.me_finder.bertalign_runtime import ManagedBertalignRuntime, resolve_bertalign_launch
from src.me_finder.alignment_runtime_lock import compute_admission, ComputeUnavailable
from src.me_finder.preferences import read_preferences, save_preferences
from tests import test_managed_alignment_runtime as fixtures


class BertalignRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagedAlignmentRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        self.manifest = fixture._manifest()
        (fixture.worker_root / 'fakeworker.py').write_text('''import sys,json
from pathlib import Path
args=[a for a in sys.argv[1:] if not a.startswith('--')]
if '--verify' in sys.argv:
    message={'type':'hello','protocol':1,'capabilities':{n:True for n in ('numpy','torch','sentence_transformers','faiss','numba')}}
else:
    model=Path(args[0]);model.mkdir(parents=True,exist_ok=True)
    (model/'mefinder-revision.txt').write_text(''' + repr(BERTALIGN_MODEL_REVISION) + ''')
    message={'type':'result'}
Path(args[-1]).write_text(json.dumps(message)+'\\n')
''', encoding='utf-8')
        self.manager = ManagedBertalignRuntime(fixture.runtime, manifest_path=self.manifest,
            platform_key='test-platform', worker_context=fixture._worker_context,
            process_launcher=fixtures._process_launcher)
        self.addCleanup(self.manager.close)

    def test_install_validate_uninstall_isolated_from_default(self):
        default_dir = self.fixture.runtime / 'components/text-alignment'
        default_dir.mkdir(parents=True)
        (default_dir / 'sentinel').write_text('existing default model')
        manager = self.manager
        manager.perform({'action':'install'})
        summary = self.fixture._wait(manager)
        self.assertTrue(summary['installed'], summary)
        self.assertTrue(summary['has_models'])
        launch = resolve_bertalign_launch(self.fixture.runtime, worker_context=self.fixture._worker_context)
        self.assertIn('--bertalign', launch.command)
        manager.perform({'action':'validate'})
        self.assertEqual(self.fixture._wait(manager)['error'], '')
        manager.perform({'action':'uninstall'})
        self.assertFalse(self.fixture._wait(manager)['installed'])
        self.assertEqual((default_dir/'sentinel').read_text(), 'existing default model')
        self.assertIsNone(resolve_bertalign_launch(self.fixture.runtime))

    def test_uninstall_waits_for_active_bertalign_and_can_be_cancelled(self):
        import time
        self.manager.perform({'action': 'install'})
        self.assertTrue(self.fixture._wait(self.manager)['installed'])
        with compute_admission(self.fixture.runtime, component_directory='text-alignment-bertalign'):
            self.manager.perform({'action': 'uninstall'})
            marker = self.manager.component_root / '.maintenance'
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            self.assertTrue(self.manager.summary()['installed'])
            self.manager.perform({'action': 'cancel'})
            self.assertTrue(self.fixture._wait(self.manager)['installed'])
        self.assertFalse(marker.exists())

    def test_model_validation_failure_does_not_publish(self):
        with mock.patch.object(self.manager, '_validate', side_effect=ValueError('broken model')):
            self.manager.perform({'action':'install'})
            summary = self.fixture._wait(self.manager)
        self.assertFalse(summary['installed'])
        self.assertIn('broken model', summary['error'])
        self.assertFalse(list(self.manager.component_root.glob('.staging-*')))

    def test_bertalign_maintenance_does_not_block_default(self):
        self.manager.component_root.mkdir(parents=True)
        (self.manager.component_root / '.maintenance').write_text('install')
        with compute_admission(self.fixture.runtime):
            with self.assertRaises(ComputeUnavailable):
                with compute_admission(self.fixture.runtime, component_directory='text-alignment-bertalign'):
                    self.fail('maintenance lease must reject computation')

    def test_backend_preference_validates_and_persists(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'prefs.json'
            self.assertEqual(read_preferences(path)['alignment_backend'], 'default')
            save_preferences({'alignment_backend':'bertalign'}, path)
            self.assertEqual(read_preferences(path)['alignment_backend'], 'bertalign')
            with self.assertRaises(ValueError):
                save_preferences({'alignment_backend':'unknown'}, path)
            self.assertEqual(json.loads(path.read_text())['alignment_backend'], 'bertalign')

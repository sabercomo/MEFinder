"""Regression cases from the 2B/2C runtime audit; isolated temporary roots."""
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from tests import test_managed_alignment_runtime as fixtures
from src.me_finder import managed_alignment_runtime as runtime
from src.me_finder.managed_embedding_models import ManagedEmbeddingModels
from src.me_finder.managed_component_assembly import assemble_managed_components
from src.me_finder.local_ocr_installer import LOCAL_OCR_MANIFEST_FILE


class RuntimeReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagedAlignmentRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def install(self, **kwargs):
        manager = self.fixture._manager(**kwargs)
        manager.perform({'action': 'install'})
        self.assertTrue(self.fixture._wait(manager)['installed'])
        return manager

    def test_restart_recovers_runtime_and_clears_abandoned_maintenance(self):
        manager = self.install()
        marker = manager.component_root / '.maintenance'
        marker.write_text('pid=99999999')
        manager.runtime_dir.replace(manager.component_root / '.previous-interrupted')
        recovered = self.fixture._manager()
        self.assertTrue(recovered.summary()['installed'])
        with runtime.compute_admission(self.fixture.runtime):
            self.assertFalse(marker.exists())

    def test_restart_does_not_clear_live_maintenance(self):
        manager = self.install()
        marker = manager.component_root / '.maintenance'
        with runtime._CrossProcessOperationLock(manager.component_root / '.operation.lock'):
            marker.write_text('pid=active')
            self.fixture._manager()
            self.assertTrue(marker.exists())
        marker.unlink()

    def test_old_cached_catalog_uses_bundled_alignment_definition(self):
        root = self.fixture.root / 'app'
        cache = root / 'components/catalog/manifest.json'
        cache.parent.mkdir(parents=True)
        old = json.loads(LOCAL_OCR_MANIFEST_FILE.read_text())
        del old['alignment']
        cache.write_text(json.dumps(old))
        components = assemble_managed_components(root)
        self.addCleanup(components.mineru.close)
        self.addCleanup(components.alignment_runtime.close)
        self.assertTrue(components.alignment_runtime.summary()['supported'])
        self.assertEqual(components.catalog.manifest_path(), cache.resolve())

    def test_uninstall_waits_for_download_receipt_before_deleting(self):
        started, release = threading.Event(), threading.Event()
        def download(*args):
            started.set()
            release.wait(5)
        models = ManagedEmbeddingModels(self.fixture.runtime, downloader=download)
        manager = self.install(models=models)
        models.perform({'action': 'download', 'model_id': 'minilm-l12-v2'})
        self.assertTrue(started.wait(3))
        try:
            manager.perform({'action': 'uninstall'})
            time.sleep(.2)
            self.assertTrue(manager.runtime_dir.exists())
            self.assertEqual(manager.summary()['state'], 'uninstall_pending')
        finally:
            release.set()
            models.wait_for_idle('minilm-l12-v2')
        result = self.fixture._wait(manager)
        self.assertEqual(result['error'], '')
        self.assertFalse(manager.models_dir.exists())

    def test_cross_process_lease_blocks_uninstall_and_new_tasks(self):
        manager = self.install()
        ready = self.fixture.root / 'ready'
        script = '''import sys,time
from pathlib import Path
from src.me_finder.managed_alignment_runtime import compute_admission
with compute_admission(Path(sys.argv[1])):
 Path(sys.argv[2]).write_text('ready')
 time.sleep(30)
'''
        process = subprocess.Popen([sys.executable, '-c', script, str(self.fixture.runtime), str(ready)])
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            manager.perform({'action': 'uninstall'})
            time.sleep(.2)
            self.assertTrue(manager.runtime_dir.exists())
            with self.assertRaises(runtime.ComputeUnavailable):
                with runtime.compute_admission(self.fixture.runtime):
                    pass
        finally:
            process.terminate()
            process.wait(timeout=5)
        self.assertFalse(self.fixture._wait(manager)['installed'])

    def test_main_close_reaps_model_download_and_rejects_new_downloads(self):
        from scripts.performance_fixture import create_fixture
        from src.me_finder.app_context import AppContext
        from src.me_finder.web_runtime import build_application_runtime
        root = self.fixture.root / 'app'
        create_fixture(root, documents=2, paragraphs=20, alignment_paragraphs=8)
        app = build_application_runtime(
            AppContext.create(root, index_path=root / 'data/index.sqlite3'),
            open_pdf_with_platform=lambda *a: None, open_path_with_default_app=lambda *a: None,
            open_external_cnki_url=lambda *a: None, open_mineru_token_page=lambda *a: None)
        self.addCleanup(app.close_runtime, timeout=5)
        controller = app.controller_post_routes['/api/text-alignment/runtime'].__self__
        models = controller._managed_components['text-alignment-models']
        processes = []
        def spawn(*args, **kwargs):
            child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
            processes.append(child)
            return child
        launch = runtime.RuntimeLaunch(command=(sys.executable,), env={}, cwd=str(root))
        # Replace the downloader with the same production factory, injecting only the child.
        models._downloader = runtime.make_model_downloader(
            root, process_launcher=spawn,
            cancel_check=lambda: models.download_cancel_requested())
        with mock.patch.object(runtime, 'resolve_installed_runtime_launch', return_value=launch):
            models.perform({'action': 'download', 'model_id': 'minilm-l12-v2'})
            deadline = time.monotonic() + 5
            while not processes and time.monotonic() < deadline:
                time.sleep(.01)
            try:
                self.assertTrue(processes)
                self.assertTrue(app.close_runtime(timeout=5))
                self.assertIsNotNone(processes[0].poll())
                self.assertFalse(models._states['minilm-l12-v2'].thread.is_alive())
                with self.assertRaises(RuntimeError):
                    models.perform({'action': 'download', 'model_id': 'multilingual-e5-large'})
            finally:
                for child in processes:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
                models.wait_for_idle('minilm-l12-v2')

    def test_intel_runtime_uses_available_onnx_wheel_without_changing_arm(self):
        intel = runtime.load_alignment_runtime_manifest(LOCAL_OCR_MANIFEST_FILE, platform_key='darwin-x86_64')
        arm = runtime.load_alignment_runtime_manifest(LOCAL_OCR_MANIFEST_FILE, platform_key='darwin-arm64')
        self.assertIn('onnxruntime==1.23.2', intel.packages)
        self.assertIn('onnxruntime==1.29.0', arm.packages)
        self.assertNotEqual(intel.identity(), arm.identity())

    def test_catalog_rejects_unpinned_platform_override(self):
        from src.me_finder.component_catalog import validate_component_catalog, ComponentCatalogError
        payload = json.loads(LOCAL_OCR_MANIFEST_FILE.read_text())
        payload['alignment']['onnxruntime_by_platform'] = {'darwin-x86_64': 'onnxruntime>=1.23.2'}
        with self.assertRaises(ComponentCatalogError):
            validate_component_catalog(payload)

    def test_parallel_runtime_readers_share_the_lease(self):
        with runtime.compute_admission(self.fixture.runtime):
            with runtime.compute_admission(self.fixture.runtime):
                self.assertTrue(True)

    def test_bundled_numpy_pin_supports_selected_python(self):
        alignment = json.loads(LOCAL_OCR_MANIFEST_FILE.read_text())['alignment']
        # numpy 2.5.2 published Requires-Python >=3.12 (PyPI metadata).
        self.assertGreaterEqual(tuple(map(int, alignment['python'].split('.'))), (3, 12))

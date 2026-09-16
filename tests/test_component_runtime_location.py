"""Desktop components must survive switching to a synced library."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from src.me_finder.application import text_alignment_coordinator as coordinator

from src.me_finder import runtime_location
from src.me_finder.local_ocr_settings import resolve_local_ocr_config_path
from src.me_finder.local_ocr_installer import LocalOCRInstaller
from src.me_finder.managed_embedding_models import ManagedEmbeddingModels
from src.me_finder.text_alignment import _default_alignment_model_cache


class ComponentRuntimeLocationTests(unittest.TestCase):
    def test_switched_library_reuses_machine_components_for_all_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            library = home / 'OneDrive' / 'MEFinder'
            local = home / 'Library/Application Support/MEFinder/runtime'
            with patch.object(runtime_location, 'sys', SimpleNamespace(platform='darwin')), \
                 patch.object(runtime_location.Path, 'home', return_value=home), \
                 patch.object(runtime_location, 'local_app_data_root', return_value=library):
                root = library / 'runtime'
                self.assertEqual(resolve_local_ocr_config_path(root), local / 'config/local_ocr.json')
                installer = LocalOCRInstaller(root, resolve_local_ocr_config_path(root))
                self.assertEqual(installer.component_root, local.resolve() / 'components/local-ocr')
                expected = local / 'components/text-alignment/models'
                self.assertEqual(Path(ManagedEmbeddingModels(root).summary()['cache_dir']), expected)
                self.assertEqual(_default_alignment_model_cache(root / 'data/index.sqlite3'), expected)

    def test_explicit_development_runtime_stays_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(resolve_local_ocr_config_path(root), root / 'config/local_ocr.json')
            self.assertEqual(Path(ManagedEmbeddingModels(root).summary()['cache_dir']), root / 'components/text-alignment/models')

    def test_windows_existing_component_location_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            library = base / 'OneDrive/MEFinder'
            with patch.object(runtime_location, 'sys', SimpleNamespace(platform='win32')), \
                 patch.dict(runtime_location.os.environ, {'LOCALAPPDATA': str(base / 'Local')}), \
                 patch.object(runtime_location, 'local_app_data_root', return_value=library):
                self.assertEqual(runtime_location.component_runtime_root(library / 'runtime'),
                                 library / 'runtime')

    def test_generation_preflight_and_inference_use_the_same_local_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            library = home / "OneDrive/MEFinder"
            root = library / "runtime"
            expected = home / "Library/Application Support/MEFinder/runtime/components/text-alignment/models"
            paths = SimpleNamespace(runtime_root=root, index_path=root / "data/index.sqlite3")
            with patch.object(runtime_location, "sys", SimpleNamespace(platform="darwin")), \
                 patch.object(runtime_location.Path, "home", return_value=home), \
                 patch.object(runtime_location, "local_app_data_root", return_value=library), \
                 patch.object(coordinator, "model_component_installed", return_value=True) as preflight, \
                 patch.object(coordinator, "generate_alignment") as generate:
                capable = SimpleNamespace(
                    probe=lambda: {"numpy": True, "fastembed": True, "onnxruntime": True}
                )
                coordinator.TextAlignmentCoordinator(
                    paths, MagicMock(), MagicMock(),
                    compute_runner_factory=lambda **kwargs: capable,
                ).generate("group", "a", "b")
                self.assertEqual(preflight.call_args.args[0], expected)
                self.assertEqual(generate.call_args.kwargs["model_cache_dir"], expected)

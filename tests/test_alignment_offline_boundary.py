"""Model presence and explicit download versus offline generation."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.me_finder.embedding_models import model_component_dir, model_component_installed
from src.me_finder.semantic_alignment import FastEmbedEmbeddingProvider, embedding_model_config


class AlignmentOfflineBoundaryTests(unittest.TestCase):
    def test_incomplete_directory_is_not_an_installed_model(self):
        with TemporaryDirectory() as directory:
            cache = Path(directory)
            model = model_component_dir(cache, 'minilm-l12-v2')
            (model / 'blobs').mkdir(parents=True)
            (model / 'blobs/partial.incomplete').write_bytes(b'partial')
            self.assertFalse(model_component_installed(cache, 'minilm-l12-v2'))

    def test_generation_loads_only_local_files(self):
        constructor = Mock(return_value=SimpleNamespace(embed=lambda *a, **kw: iter([[1., 0.]])))
        with TemporaryDirectory() as directory, patch.dict('sys.modules', {'fastembed': SimpleNamespace(TextEmbedding=constructor), 'onnxruntime': SimpleNamespace(disable_telemetry_events=lambda: None)}):
            FastEmbedEmbeddingProvider(embedding_model_config('minilm-l12-v2'))(['test'], Path(directory))
        self.assertTrue(constructor.call_args.kwargs.get('local_files_only'))

    def test_stale_receipt_allows_repair_and_required_files_control_presence(self):
        from src.me_finder.managed_embedding_models import ManagedEmbeddingModels

        def download(model_id, cache):
            config = embedding_model_config(model_id)
            base = model_component_dir(cache, model_id)
            (base / 'refs').mkdir(parents=True)
            (base / 'refs/main').write_text('a' * 40)
            snapshot = base / 'snapshots' / ('a' * 40)
            snapshot.mkdir(parents=True)
            for name in config.required_files:
                (snapshot / name).write_bytes(b'fixture')

        with TemporaryDirectory() as directory:
            component = ManagedEmbeddingModels(Path(directory), downloader=download)
            receipt = component._receipt_path('multilingual-e5-large')
            receipt.parent.mkdir(parents=True)
            receipt.write_text('{}')
            before = component.summary()['models'][1]
            self.assertFalse(before['installed'])
            component.perform({'model_id': 'multilingual-e5-large', 'action': 'download'})
            component.wait_for_idle('multilingual-e5-large')
            self.assertTrue(component.summary()['models'][1]['installed'])
            snapshot = model_component_dir(component._cache_dir, 'multilingual-e5-large') / 'snapshots' / ('a' * 40)
            (snapshot / 'model.onnx_data').unlink()
            after = component.summary()['models'][1]
            self.assertFalse(after['installed'])
            self.assertEqual(after['state'], 'not_installed')

    def test_only_explicit_download_enables_network(self):
        from src.me_finder.managed_embedding_models import download_embedding_model
        with TemporaryDirectory() as directory, patch('src.me_finder.semantic_alignment.embed_texts') as embed:
            download_embedding_model('minilm-l12-v2', Path(directory))
        self.assertIs(embed.call_args.kwargs['local_files_only'], False)

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from src.me_finder.embedding_models import embedding_model_config
from src.me_finder.managed_embedding_models import ManagedEmbeddingModels


class ManagedEmbeddingModelsTests(unittest.TestCase):
    def test_download_uses_managed_cache_and_marks_model_installed(self) -> None:
        calls: list[tuple[str, Path]] = []
        release = threading.Event()

        def download(model_id: str, cache_dir: Path) -> None:
            calls.append((model_id, cache_dir))
            release.wait(timeout=5)
            config = embedding_model_config(model_id)
            base = cache_dir / config.fastembed_cache_dirname
            (base / "refs").mkdir(parents=True)
            (base / "refs/main").write_text("a" * 40)
            snapshot = base / "snapshots" / ("a" * 40)
            snapshot.mkdir(parents=True)
            for name in config.required_files:
                (snapshot / name).write_bytes(b"test fixture")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            component = ManagedEmbeddingModels(root, downloader=download)
            started = component.perform(
                {"model_id": "multilingual-e5-large", "action": "download"}
            )
            self.assertEqual(
                next(
                    model
                    for model in started["models"]
                    if model["id"] == "multilingual-e5-large"
                )["state"],
                "downloading",
            )
            release.set()
            component.wait_for_idle("multilingual-e5-large")
            summary = component.summary()

            model = next(
                item
                for item in summary["models"]
                if item["id"] == "multilingual-e5-large"
            )
            self.assertTrue(model["installed"])
            self.assertEqual(model["dimension"], 1024)
            self.assertEqual(model["size"], "约 2.24 GB")
            self.assertEqual(model["prefix_mode"], "query")
            self.assertEqual(model["downloaded_bytes"], 2_240_000_000)
            self.assertEqual(model["total_bytes"], 2_240_000_000)
            self.assertTrue(model["total_is_estimate"])
            self.assertEqual(model["progress"], 1.0)
            self.assertEqual(
                calls,
                [
                    (
                        "multilingual-e5-large",
                        root / "components/text-alignment/models",
                    )
                ],
            )

    def test_downloading_summary_reports_cached_blob_progress(self) -> None:
        ready = threading.Event()
        release = threading.Event()

        def download(_model_id: str, cache_dir: Path) -> None:
            blobs = (
                cache_dir
                / "models--qdrant--multilingual-e5-large-onnx"
                / "blobs"
            )
            blobs.mkdir(parents=True)
            (blobs / "model.onnx_data.incomplete").write_bytes(b"x" * 25)
            ready.set()
            release.wait(timeout=5)

        with tempfile.TemporaryDirectory() as temp_dir:
            component = ManagedEmbeddingModels(Path(temp_dir), downloader=download)
            component.perform(
                {"model_id": "multilingual-e5-large", "action": "download"}
            )
            self.assertTrue(ready.wait(timeout=5))

            model = next(
                item
                for item in component.summary()["models"]
                if item["id"] == "multilingual-e5-large"
            )
            self.assertEqual(model["state"], "downloading")
            self.assertEqual(model["downloaded_bytes"], 25)
            self.assertEqual(model["total_bytes"], 2_240_000_000)
            self.assertAlmostEqual(model["progress"], 25 / 2_240_000_000)

            release.set()
            component.wait_for_idle("multilingual-e5-large")

    def test_failed_summary_preserves_partial_download_progress(self) -> None:
        def download(_model_id: str, cache_dir: Path) -> None:
            archive = cache_dir / "fast-multilingual-e5-large.tar.gz"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"partial")
            raise OSError("network interrupted")

        with tempfile.TemporaryDirectory() as temp_dir:
            component = ManagedEmbeddingModels(Path(temp_dir), downloader=download)
            component.perform(
                {"model_id": "multilingual-e5-large", "action": "download"}
            )
            component.wait_for_idle("multilingual-e5-large")

            model = next(
                item
                for item in component.summary()["models"]
                if item["id"] == "multilingual-e5-large"
            )
            self.assertEqual(model["state"], "failed")
            self.assertEqual(model["error"], "network interrupted")
            self.assertEqual(model["downloaded_bytes"], len(b"partial"))
            self.assertGreater(model["progress"], 0)


if __name__ == "__main__":
    unittest.main()

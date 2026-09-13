from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.embedding_models import (
    embedding_model_config,
    embedding_model_summaries,
)
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
            self.assertEqual(model["size"], "约 2.25 GB")
            self.assertEqual(model["prefix_mode"], "query")
            self.assertEqual(model["downloaded_bytes"], 2_253_000_000)
            self.assertEqual(model["total_bytes"], 2_253_000_000)
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
            self.assertEqual(model["total_bytes"], 2_253_000_000)
            self.assertAlmostEqual(model["progress"], 25 / 2_253_000_000)

            release.set()
            component.wait_for_idle("multilingual-e5-large")

    def test_installed_files_override_stuck_downloading_state(self) -> None:
        """Required files on disk are the source of truth: a download job that
        hangs after the bytes arrive must not keep the model labeled 下载中."""
        ready = threading.Event()
        release = threading.Event()

        def download(_model_id: str, cache_dir: Path) -> None:
            model_dir = cache_dir / "models--qdrant--multilingual-e5-large-onnx"
            revision = "a" * 40
            (model_dir / "refs").mkdir(parents=True)
            (model_dir / "refs" / "main").write_text(revision, encoding="utf-8")
            snapshot = model_dir / "snapshots" / revision
            snapshot.mkdir(parents=True)
            for name in embedding_model_config(
                "multilingual-e5-large"
            ).required_files:
                (snapshot / name).write_bytes(b"x")
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
            self.assertTrue(model["installed"])
            self.assertEqual(model["state"], "installed")
            self.assertAlmostEqual(model["progress"], 1.0)

            release.set()
            component.wait_for_idle("multilingual-e5-large")

    def test_downloading_with_complete_bytes_reports_verifying(self) -> None:
        """Bytes at or above the estimate without an install receipt mean the
        job is in the verify/finalize stage, not still downloading."""
        ready = threading.Event()
        release = threading.Event()

        def download(_model_id: str, cache_dir: Path) -> None:
            blobs = (
                cache_dir
                / "models--qdrant--multilingual-e5-large-onnx"
                / "blobs"
            )
            blobs.mkdir(parents=True)
            (blobs / "model.onnx_data.incomplete").write_bytes(b"x" * 130)
            ready.set()
            release.wait(timeout=5)

        with tempfile.TemporaryDirectory() as temp_dir:
            component = ManagedEmbeddingModels(Path(temp_dir), downloader=download)
            component.perform(
                {"model_id": "multilingual-e5-large", "action": "download"}
            )
            self.assertTrue(ready.wait(timeout=5))

            fake_totals = [
                dict(item, size_bytes=100)
                if item["id"] == "multilingual-e5-large"
                else dict(item)
                for item in embedding_model_summaries()
            ]
            with mock.patch(
                "src.me_finder.managed_embedding_models.embedding_model_summaries",
                return_value=fake_totals,
            ):
                model = next(
                    item
                    for item in component.summary()["models"]
                    if item["id"] == "multilingual-e5-large"
                )
            self.assertEqual(model["state"], "verifying")
            self.assertFalse(model["installed"])
            self.assertEqual(model["progress"], 0.99)

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

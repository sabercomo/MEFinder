from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from src.me_finder.managed_embedding_models import ManagedEmbeddingModels


class ManagedEmbeddingModelsTests(unittest.TestCase):
    def test_download_uses_managed_cache_and_marks_model_installed(self) -> None:
        calls: list[tuple[str, Path]] = []
        release = threading.Event()

        def download(model_id: str, cache_dir: Path) -> None:
            calls.append((model_id, cache_dir))
            release.wait(timeout=5)

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
            self.assertEqual(
                calls,
                [
                    (
                        "multilingual-e5-large",
                        root / "components/text-alignment/models",
                    )
                ],
            )


if __name__ == "__main__":
    unittest.main()

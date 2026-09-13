"""删除对齐模型：删干净、报真实释放量、下载期间拒绝。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.me_finder.embedding_models import embedding_model_config
from src.me_finder.managed_embedding_models import (
    ManagedEmbeddingModels,
    ManagedEmbeddingModelsError,
)

MODEL_ID = "minilm-l12-v2"


class AlignmentModelDeleteTests(unittest.TestCase):
    def _manager(self, root: Path) -> ManagedEmbeddingModels:
        return ManagedEmbeddingModels(root, downloader=lambda *_: None)

    def _seed_installed_model(self, manager: ManagedEmbeddingModels) -> Path:
        model = embedding_model_config(MODEL_ID)
        cache_dir = Path(manager.summary()["cache_dir"])
        blobs = cache_dir / model.fastembed_cache_dirname / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        (blobs / "weights.onnx").write_bytes(b"x" * 4096)
        receipt = cache_dir / "installed" / f"{MODEL_ID}.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text("{}", encoding="utf-8")
        return cache_dir

    def test_delete_removes_files_and_reports_freed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager(Path(temp_dir))
            cache_dir = self._seed_installed_model(manager)
            model = embedding_model_config(MODEL_ID)

            result = manager.perform({"model_id": MODEL_ID, "action": "delete"})

            self.assertEqual(result["freed_bytes"], 4096)
            self.assertFalse((cache_dir / model.fastembed_cache_dirname).exists())
            self.assertFalse((cache_dir / "installed" / f"{MODEL_ID}.json").exists())
            state = next(m for m in result["models"] if m["id"] == MODEL_ID)
            self.assertFalse(state["installed"])
            self.assertEqual(state["state"], "not_installed")

    def test_delete_on_missing_model_is_a_no_op_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager(Path(temp_dir))
            result = manager.perform({"model_id": MODEL_ID, "action": "delete"})
            self.assertEqual(result["freed_bytes"], 0)

    def test_delete_is_refused_while_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager(Path(temp_dir))

            class RunningThread:
                @staticmethod
                def is_alive() -> bool:
                    return True

            manager._states[MODEL_ID].thread = RunningThread()
            with self.assertRaises(ManagedEmbeddingModelsError):
                manager.perform({"model_id": MODEL_ID, "action": "delete"})

    def test_unknown_action_is_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager(Path(temp_dir))
            with self.assertRaises(ManagedEmbeddingModelsError):
                manager.perform({"model_id": MODEL_ID, "action": "uninstall"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

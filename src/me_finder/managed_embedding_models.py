"""Download embedding models through the shared managed-component contract."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping

from .embedding_models import (
    EMBEDDING_MODELS,
    embedding_model_config,
    embedding_model_summaries,
)
from .semantic_alignment import embed_texts


class ManagedEmbeddingModelsError(RuntimeError):
    pass


@dataclass
class _ModelState:
    state: str = "not_installed"
    message: str = ""
    error: str = ""
    thread: threading.Thread | None = None


def download_embedding_model(model_id: str, cache_dir: Path) -> None:
    embed_texts(["MEFinder semantic alignment model probe"], cache_dir, model_id=model_id)


class ManagedEmbeddingModels:
    component_id = "text-alignment-models"

    def __init__(
        self,
        runtime_root: Path,
        *,
        downloader: Callable[[str, Path], None] = download_embedding_model,
    ) -> None:
        self._cache_dir = (
            Path(runtime_root) / "components" / "text-alignment" / "models"
        )
        self._downloader = downloader
        self._lock = threading.RLock()
        self._states = {
            model_id: _ModelState() for model_id in EMBEDDING_MODELS
        }

    def _receipt_path(self, model_id: str) -> Path:
        return self._cache_dir / "installed" / f"{model_id}.json"

    def _mark_installed(self, model_id: str) -> None:
        model = embedding_model_config(model_id)
        receipt = self._receipt_path(model_id)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"id": model.id, "hf_name": model.hf_name},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(receipt)

    def summary(self) -> Dict[str, object]:
        with self._lock:
            models = []
            for model in embedding_model_summaries():
                model_id = str(model["id"])
                state = self._states[model_id]
                installed = self._receipt_path(model_id).is_file()
                current_state = state.state
                if installed and current_state == "not_installed":
                    current_state = "installed"
                models.append(
                    {
                        **model,
                        "installed": installed,
                        "state": current_state,
                        "message": state.message,
                        "error": state.error,
                    }
                )
            return {
                "component_id": self.component_id,
                "cache_dir": str(self._cache_dir),
                "models": models,
            }

    def perform(self, payload: Mapping[str, object]) -> Dict[str, object]:
        model_id = str(payload.get("model_id") or "")
        embedding_model_config(model_id)
        action = str(payload.get("action") or "")
        if action != "download":
            raise ManagedEmbeddingModelsError("不支持的语义模型组件操作。")
        with self._lock:
            state = self._states[model_id]
            if state.thread is not None and state.thread.is_alive():
                raise ManagedEmbeddingModelsError("该语义模型正在下载。")
            if self._receipt_path(model_id).is_file():
                state.state = "installed"
                state.message = "模型已下载"
                state.error = ""
                return self.summary()
            state.state = "downloading"
            state.message = "正在下载模型…"
            state.error = ""
            state.thread = threading.Thread(
                target=self._download,
                args=(model_id,),
                name=f"embedding-model-{model_id}",
                daemon=True,
            )
            state.thread.start()
        return self.summary()

    def _download(self, model_id: str) -> None:
        try:
            self._downloader(model_id, self._cache_dir)
            self._mark_installed(model_id)
        except Exception as exc:
            with self._lock:
                state = self._states[model_id]
                state.state = "failed"
                state.message = "模型下载失败"
                state.error = str(exc)
            return
        with self._lock:
            state = self._states[model_id]
            state.state = "installed"
            state.message = "模型已下载"
            state.error = ""

    def wait_for_idle(self, model_id: str, timeout: float = 10.0) -> None:
        embedding_model_config(model_id)
        with self._lock:
            thread = self._states[model_id].thread
        if thread is not None:
            thread.join(timeout)

    def diagnostics(self) -> Dict[str, object]:
        summary = self.summary()
        return {
            "component_id": self.component_id,
            "models": [
                {
                    "id": model["id"],
                    "installed": model["installed"],
                    "state": model["state"],
                    "error": model["error"],
                }
                for model in summary["models"]
            ],
        }

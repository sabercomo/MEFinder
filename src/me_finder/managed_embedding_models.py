"""Download embedding models through the shared managed-component contract."""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping

from .runtime_location import component_runtime_root
from .embedding_models import (
    EMBEDDING_MODELS,
    embedding_model_config,
    embedding_model_summaries,
    model_component_installed,
)


class ManagedEmbeddingModelsError(RuntimeError):
    pass


@dataclass
class _ModelState:
    state: str = "not_installed"
    message: str = ""
    error: str = ""
    thread: threading.Thread | None = None


def download_embedding_model(model_id: str, cache_dir: Path) -> None:
    """Install one model component and verify it with a local probe embed.

    The algorithm module (and its numeric stack) is imported lazily: managing,
    summarising or uninstalling components must not require the compute stack.
    """

    from .semantic_alignment import embed_texts

    embed_texts(["MEFinder semantic alignment model probe"], cache_dir,
                model_id=model_id, local_files_only=False)


class ManagedEmbeddingModels:
    component_id = "text-alignment-models"

    def __init__(
        self,
        runtime_root: Path,
        *,
        downloader: Callable[[str, Path], None] = download_embedding_model,
    ) -> None:
        self._cache_dir = (
            component_runtime_root(runtime_root) / "components" / "text-alignment" / "models"
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

    def _downloaded_bytes(self, model_id: str) -> int:
        model = embedding_model_config(model_id)
        blobs_dir = self._cache_dir / model.fastembed_cache_dirname / "blobs"
        blob_bytes = 0
        if blobs_dir.is_dir():
            for path in blobs_dir.iterdir():
                try:
                    if path.is_file() and not path.is_symlink():
                        blob_bytes += path.stat().st_size
                except FileNotFoundError:
                    # Hugging Face renames completed .incomplete files atomically.
                    continue
        archive_bytes = 0
        if model.fastembed_archive_name:
            archive = self._cache_dir / model.fastembed_archive_name
            try:
                archive_bytes = archive.stat().st_size
            except FileNotFoundError:
                pass
        return max(blob_bytes, archive_bytes)

    def _delete_model_files(self, model_id: str) -> int:
        """Remove one model's cache directory, archive and receipt.

        Returns the bytes actually freed, so the UI can state the real number
        instead of the catalog estimate.
        """

        model = embedding_model_config(model_id)
        freed = 0
        targets = [self._cache_dir / model.fastembed_cache_dirname]
        if model.fastembed_archive_name:
            targets.append(self._cache_dir / model.fastembed_archive_name)
        for target in targets:
            if target.is_dir():
                for path in target.rglob("*"):
                    try:
                        if path.is_file() and not path.is_symlink():
                            freed += path.stat().st_size
                    except FileNotFoundError:
                        continue
                shutil.rmtree(target, ignore_errors=True)
            elif target.is_file():
                try:
                    freed += target.stat().st_size
                except FileNotFoundError:
                    pass
                target.unlink(missing_ok=True)
        self._receipt_path(model_id).unlink(missing_ok=True)
        return freed

    def summary(self) -> Dict[str, object]:
        with self._lock:
            models = []
            for model in embedding_model_summaries():
                model_id = str(model["id"])
                state = self._states[model_id]
                installed = model_component_installed(self._cache_dir, model_id)
                total_bytes = int(model["size_bytes"])
                downloaded_bytes = (
                    total_bytes if installed else self._downloaded_bytes(model_id)
                )
                current_state = state.state
                if installed:
                    # 磁盘上的必需文件是唯一事实来源：卡死的下载作业
                    # 不能让已装模型在界面上停留在外标"下载中"。
                    current_state = "installed"
                elif current_state in {"installed", "not_installed"}:
                    current_state = "not_installed"
                elif current_state == "downloading" and downloaded_bytes >= total_bytes:
                    # 字节已齐、安装回执未落：处于校验/落盘阶段，与"下载中"区分，
                    # 否则已下载字节超过估计值仍显示 99% 下载中，无法判断死活。
                    current_state = "verifying"
                progress = (
                    1.0
                    if installed
                    else min(downloaded_bytes / total_bytes, 0.99)
                )
                models.append(
                    {
                        **model,
                        "installed": installed,
                        "state": current_state,
                        "message": state.message,
                        "error": state.error,
                        "downloaded_bytes": downloaded_bytes,
                        "total_bytes": total_bytes,
                        "total_is_estimate": True,
                        "progress": progress,
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
        if action not in {"download", "delete"}:
            raise ManagedEmbeddingModelsError("不支持的译本对齐模型操作。")
        if action == "delete":
            with self._lock:
                state = self._states[model_id]
                if state.thread is not None and state.thread.is_alive():
                    raise ManagedEmbeddingModelsError("该译本对齐模型正在下载，先取消或等待完成。")
                freed = self._delete_model_files(model_id)
                state.state = "not_installed"
                state.message = "模型文件已删除"
                state.error = ""
                result = self.summary()
            result["freed_bytes"] = freed
            return result
        with self._lock:
            state = self._states[model_id]
            if state.thread is not None and state.thread.is_alive():
                raise ManagedEmbeddingModelsError("该译本对齐模型正在下载。")
            if model_component_installed(self._cache_dir, model_id):
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

    def delete_all_models(self) -> int:
        """Delete every managed model's files and receipts; return freed bytes.

        Used when the whole alignment compute component is uninstalled: per the
        confirmed product rule, uninstalling the component removes the models it
        owns. Documents, notes and existing alignment results are untouched —
        those live in the library database, not here.
        """

        freed = 0
        with self._lock:
            for model_id, state in self._states.items():
                if state.thread is not None and state.thread.is_alive():
                    raise ManagedEmbeddingModelsError(
                        "有译本对齐模型正在下载，先取消或等待完成再卸载组件。"
                    )
                freed += self._delete_model_files(model_id)
                state.state = "not_installed"
                state.message = "模型文件已删除"
                state.error = ""
        return freed

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

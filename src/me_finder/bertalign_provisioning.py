"""Explicit install-time model download and offline load verification."""
from pathlib import Path
import os

from .bertalign_backend import BERTALIGN_MODEL_HF_NAME, BERTALIGN_MODEL_REVISION


def verify_bertalign(argv, positional, emit):
    """Report native import failures through the existing worker control file."""
    from .bertalign_compute import BERTALIGN_REQUIRED, BERTALIGN_COMPUTE_PROTOCOL
    control = open(positional[-1], "a", encoding="utf-8")
    os.environ["NUMBA_CACHE_DIR"] = str(Path(positional[-1]).parent / "numba-cache")
    try:
        import numpy as np
        import torch
        import faiss
        import numba
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(1)
        faiss.omp_set_num_threads(1)
        torch.zeros(1) + 1
        faiss.IndexFlatIP(2).add(np.zeros((1, 2), dtype="float32"))
        numba.njit(lambda x: x + 1)(1)
        if "--verify" in argv:
            emit(control, type="hello", protocol=BERTALIGN_COMPUTE_PROTOCOL,
                  capabilities={name: True for name in BERTALIGN_REQUIRED}, pid=os.getpid())
            return 0
        model_dir = Path(positional[0])
        if "--download-bertalign-model" in argv:
            import shutil
            from huggingface_hub import snapshot_download
            from huggingface_hub.errors import LocalEntryNotFoundError
            # Reuse an exact revision already downloaded by this user. A partial
            # or corrupt cache still fails the actual encode check below.
            try:
                snapshot = snapshot_download(repo_id=BERTALIGN_MODEL_HF_NAME,
                    revision=BERTALIGN_MODEL_REVISION, local_files_only=True)
            except LocalEntryNotFoundError:
                snapshot = snapshot_download(repo_id=BERTALIGN_MODEL_HF_NAME,
                    revision=BERTALIGN_MODEL_REVISION,
                    allow_patterns=["*.json", "*.safetensors", "vocab.txt"])
            shutil.copytree(snapshot, model_dir, dirs_exist_ok=True)
        else:
            if (model_dir / "mefinder-revision.txt").read_text().strip() != BERTALIGN_MODEL_REVISION:
                raise ValueError("LaBSE 模型版本不匹配，请重新安装组件")
        model = SentenceTransformer(str(model_dir), device="cpu", local_files_only=True)
        encoded = model.encode(["验证本地模型。", "Verify the local model."], show_progress_bar=False)
        if encoded.shape != (2, 768) or not np.isfinite(encoded).all():
            raise ValueError("LaBSE 本地编码验证失败")
        (model_dir / "mefinder-revision.txt").write_text(BERTALIGN_MODEL_REVISION, encoding="utf-8")
        emit(control, type="result", model_revision=BERTALIGN_MODEL_REVISION)
        return 0
    except Exception as exc:
        import traceback
        emit(control, type="error", code="compute_failed", message=str(exc) + "\n" + traceback.format_exc()[-1500:])
        return 1

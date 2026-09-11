"""Registered embedding models and per-model alignment thresholds."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class AlignmentThresholds:
    low: float
    note_block: float
    margin: float


@dataclass(frozen=True)
class EmbeddingModelConfig:
    id: str
    hf_name: str
    fastembed_cache_dirname: str
    fastembed_archive_name: str | None
    dimension: int
    size_bytes: int
    size_label: str
    prefix_mode: str
    display_name: str
    description: str
    thresholds: AlignmentThresholds


DEFAULT_EMBEDDING_MODEL_ID = "minilm-l12-v2"
EMBEDDING_MODELS = {
    DEFAULT_EMBEDDING_MODEL_ID: EmbeddingModelConfig(
        id=DEFAULT_EMBEDDING_MODEL_ID,
        hf_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        fastembed_cache_dirname=(
            "models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
        ),
        fastembed_archive_name=None,
        dimension=384,
        size_bytes=220_000_000,
        size_label="约 220 MB",
        prefix_mode="none",
        display_name="MiniLM 多语言模型",
        description="默认模型，速度更快",
        thresholds=AlignmentThresholds(low=0.56, note_block=0.64, margin=0.05),
    ),
    "multilingual-e5-large": EmbeddingModelConfig(
        id="multilingual-e5-large",
        hf_name="intfloat/multilingual-e5-large",
        fastembed_cache_dirname="models--qdrant--multilingual-e5-large-onnx",
        fastembed_archive_name="fast-multilingual-e5-large.tar.gz",
        dimension=1024,
        size_bytes=2_240_000_000,
        size_label="约 2.24 GB",
        prefix_mode="query",
        display_name="E5 Large 多语言模型",
        description="实验档，更准但更慢",
        # low：2026-09-05 单书 37 条原书复核后的实验值，须结合区域排除使用。
        # note_block / margin 待标定；不能把单书分层样本外推为全库准确率。
        thresholds=AlignmentThresholds(low=0.83, note_block=0.80, margin=0.05),
    ),
}


def embedding_model_config(model_id: str) -> EmbeddingModelConfig:
    try:
        return EMBEDDING_MODELS[model_id]
    except KeyError as exc:
        raise ValueError(f"不支持的译本对齐模型：{model_id}") from exc


def model_component_dir(cache_root, model_id: str) -> Path:
    """Local cache directory of one managed model component."""

    return Path(cache_root) / embedding_model_config(model_id).fastembed_cache_dirname


def model_component_installed(cache_root, model_id: str) -> bool:
    """Whether the managed model files exist locally (compute is usable offline).

    The model files are a managed component downloaded from the settings UI;
    search, reading and locating stored alignment results never need them.
    """

    directory = model_component_dir(cache_root, model_id)
    if not directory.is_dir():
        return False
    return any(directory.rglob("*"))


def default_alignment_threshold_settings() -> dict[str, dict[str, float]]:
    return {
        model_id: {
            "low": config.thresholds.low,
            "note_block": config.thresholds.note_block,
            "margin": config.thresholds.margin,
        }
        for model_id, config in EMBEDDING_MODELS.items()
    }


def resolve_alignment_thresholds(
    model_id: str,
    settings: Mapping[str, Any] | None = None,
) -> AlignmentThresholds:
    defaults = embedding_model_config(model_id).thresholds
    values = settings.get(model_id) if isinstance(settings, Mapping) else None
    if not isinstance(values, Mapping):
        return defaults
    return AlignmentThresholds(
        low=float(values.get("low", defaults.low)),
        note_block=float(values.get("note_block", defaults.note_block)),
        margin=float(values.get("margin", defaults.margin)),
    )


def embedding_model_summaries() -> list[dict[str, object]]:
    return [
        {
            "id": config.id,
            "hf_name": config.hf_name,
            "dimension": config.dimension,
            "size_bytes": config.size_bytes,
            "size": config.size_label,
            "prefix_mode": config.prefix_mode,
            "display_name": config.display_name,
            "description": config.description,
        }
        for config in EMBEDDING_MODELS.values()
    ]

"""对齐配方快照的读取、还原与整体替换。

重建索引与备份还原时，先把可还原版本的对齐配方读成快照，重建后按配方重跑
对齐。位于 ``text_alignment`` 之上：消费对齐核心的生成能力，核心不反向依赖。
消费者是 ``database`` 的重建路径与 ``backup_service``。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Mapping

from .embedding_models import DEFAULT_EMBEDDING_MODEL_ID
from .persistence.alignment_store import (
    alignment_database_file,
    alignment_recipe_replace_transaction,
    read_alignment_recipe_rows,
    recipe_sources_and_group_exist,
)
from .persistence.schema_installers import install_text_alignment_schema
from .semantic_alignment import EmbeddingProvider
from .text_alignment import (
    ALIGNMENT_ALGORITHM,
    RESTORABLE_ALIGNMENT_VERSIONS,
    _default_alignment_model_cache,
    _generate_alignment_on_connection,
    _json_object,
)


def read_alignment_recipe_snapshot(db_path: Path) -> Dict[str, list]:
    path = Path(db_path)
    if not path.is_file():
        return {"alignment_pairs": []}
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            return {"alignment_pairs": []}
    return {
        "alignment_pairs": [
            {
                "document_group_id": row["document_group_id"],
                "pivot_source_file_id": row["pivot_source_file_id"],
                "target_source_file_id": row["target_source_file_id"],
                "algorithm": row["algorithm"],
                "algorithm_version": row["algorithm_version"],
                "embedding_model_id": _json_object(
                    row["parameters_json"]
                ).get("embedding_model_id", DEFAULT_EMBEDDING_MODEL_ID),
            }
            for row in read_alignment_recipe_rows(path)
        ]
    }


def restore_alignment_recipe_snapshot(
    connection: sqlite3.Connection,
    snapshot: Mapping[str, object],
    *,
    model_cache_dir: Path | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> int:
    install_text_alignment_schema(connection)
    if model_cache_dir is None:
        database_file = alignment_database_file(connection)
        model_cache_dir = _default_alignment_model_cache(Path(database_file))
    restored = 0
    for pair in snapshot.get("alignment_pairs", []):
        if not isinstance(pair, Mapping):
            continue
        if (
            pair.get("algorithm") != ALIGNMENT_ALGORITHM
            or pair.get("algorithm_version") not in RESTORABLE_ALIGNMENT_VERSIONS
        ):
            continue
        group_id = str(pair.get("document_group_id") or "")
        pivot_id = str(pair.get("pivot_source_file_id") or "")
        target_id = str(pair.get("target_source_file_id") or "")
        model_id = str(
            pair.get("embedding_model_id") or DEFAULT_EMBEDDING_MODEL_ID
        )
        if not recipe_sources_and_group_exist(connection, group_id, pivot_id, target_id):
            continue
        _generate_alignment_on_connection(
            connection,
            group_id,
            pivot_id,
            target_id,
            model_cache_dir=model_cache_dir,
            embedding_provider=embedding_provider,
            embedding_model_id=model_id,
        )
        restored += 1
    return restored


def replace_alignment_recipe_snapshot(
    snapshot: Mapping[str, object],
    db_path: Path,
    *,
    model_cache_dir: Path | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> int:
    with alignment_recipe_replace_transaction(db_path) as connection:
        restored = restore_alignment_recipe_snapshot(
            connection,
            snapshot,
            model_cache_dir=(
                Path(model_cache_dir)
                if model_cache_dir is not None
                else _default_alignment_model_cache(Path(db_path))
            ),
            embedding_provider=embedding_provider,
        )
        return restored

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
from .persistence.connection import open_writable_index
from .persistence.schema_installers import install_text_alignment_schema
from .semantic_alignment import EmbeddingProvider
from .text_alignment import (
    ALIGNMENT_ALGORITHM,
    RESTORABLE_ALIGNMENT_VERSIONS,
    _default_alignment_model_cache,
    _generate_alignment_on_connection,
    _json_object,
    _table_exists,
)


def read_alignment_recipe_snapshot(db_path: Path) -> Dict[str, list]:
    path = Path(db_path)
    if not path.is_file():
        return {"alignment_pairs": []}
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            return {"alignment_pairs": []}
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        if not _table_exists(connection, "alignment_runs"):
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
                for row in connection.execute(
                    "SELECT document_group_id, pivot_source_file_id, "
                    "target_source_file_id, algorithm, algorithm_version, "
                    "parameters_json "
                    "FROM alignment_runs WHERE status = 'completed' "
                    "ORDER BY document_group_id, pivot_source_file_id, target_source_file_id"
                )
            ]
        }
    finally:
        connection.close()


def restore_alignment_recipe_snapshot(
    connection: sqlite3.Connection,
    snapshot: Mapping[str, object],
    *,
    model_cache_dir: Path | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> int:
    install_text_alignment_schema(connection)
    if model_cache_dir is None:
        database_file = str(connection.execute("PRAGMA database_list").fetchone()[2])
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
        present = connection.execute(
            "SELECT COUNT(*) FROM source_files WHERE source_file_id IN (?, ?)",
            (pivot_id, target_id),
        ).fetchone()[0]
        group_present = connection.execute(
            "SELECT 1 FROM document_groups WHERE document_group_id = ?",
            (group_id,),
        ).fetchone()
        if present != 2 or group_present is None:
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
    connection = open_writable_index(Path(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        install_text_alignment_schema(connection)
        connection.execute("DELETE FROM alignment_runs")
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
        connection.commit()
        return restored
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

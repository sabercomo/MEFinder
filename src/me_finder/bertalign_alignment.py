"""Publish an optional Bertalign-backend alignment for one version pair.

Sibling of the default flow in :mod:`text_alignment` rather than threaded
through it: the default FastEmbed backend and its algorithm identity stay
byte-for-byte unchanged, and the two backends have genuinely different run
identities and parameter schemas. Segment preparation, body-range validation
and the two-phase write coordination are reused from ``text_alignment`` (kept in
one module so this file stays small and the line-budget boundary holds).

The compute (LaBSE embedding + Bertalign two-stage DP) runs in the optional
Bertalign runtime — in-process, or via an injected ``compute_runner`` driving
the isolated subprocess. Preparation and publication stay in the main process
with the same write coordination as the default backend.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Tuple

from . import text_alignment as ta
from .alignment_regions import alignment_body_bounds
from .bertalign_backend import (
    BERTALIGN_ALGORITHM,
    BERTALIGN_ALGORITHM_VERSION,
    BERTALIGN_MODEL_HF_NAME,
    BERTALIGN_MODEL_ID,
    BERTALIGN_UPSTREAM_COMMIT,
    BertalignParams,
    align_segments_bertalign,
    bertalign_model_dir,
)
from .persistence.connection import open_writable_index
from .persistence.schema_installers import install_text_alignment_schema
from .semantic_alignment import SemanticLink


def _bertalign_body_ranges_parameter(preparation) -> Dict[str, object]:
    if preparation.reviewed_body_ranges is not None:
        return {
            "body_range_source": "reviewed",
            "body_ranges": preparation.reviewed_body_ranges,
        }
    return {
        "body_range_source": "detected",
        "body_ranges": {
            "pivot": list(
                alignment_body_bounds([text for _, text in preparation.pivot_segments])
            ),
            "target": list(
                alignment_body_bounds([text for _, text in preparation.target_segments])
            ),
        },
    }


def _generate_bertalign_on_connection(
    connection: sqlite3.Connection,
    document_group_id: str,
    pivot_source_id: str,
    target_source_id: str,
    *,
    preparation,
    computed: Tuple[List[SemanticLink], list],
    params: BertalignParams,
) -> Dict[str, object]:
    aligned, _anchors = computed
    pivot_segments = preparation.pivot_segments
    target_segments = preparation.target_segments
    # Supersede only prior Bertalign runs for this pair; default-backend runs and
    # human corrections against them stay untouched and readable.
    connection.execute(
        "UPDATE alignment_runs SET status = 'superseded' "
        "WHERE document_group_id = ? AND pivot_source_file_id = ? "
        "AND target_source_file_id = ? AND status = 'completed' "
        "AND algorithm = ?",
        (document_group_id, pivot_source_id, target_source_id, BERTALIGN_ALGORITHM),
    )
    run_id = f"alignment-run-{uuid.uuid4().hex}"
    timestamp = ta._now()
    body_parameters = _bertalign_body_ranges_parameter(preparation)
    # Honest run identity: the backend, the pinned upstream commit, the model
    # (its own vector space) and the exact upstream parameters. No calibrated
    # accuracy, no default-backend thresholds.
    parameters = {
        "backend": BERTALIGN_ALGORITHM,
        "upstream": "bertalign",
        "upstream_commit": BERTALIGN_UPSTREAM_COMMIT,
        "embedding_model_id": BERTALIGN_MODEL_ID,
        "embedding_model_hf_name": BERTALIGN_MODEL_HF_NAME,
        "length_unit": "utf8_byte_length",
        "similarity": "labse_cosine",
        "score_meaning": "algorithmic_similarity_not_calibrated_accuracy",
        "bertalign_params": {
            "max_align": params.max_align,
            "top_k": params.top_k,
            "win": params.win,
            "skip": params.skip,
            "margin": params.margin,
            "len_penalty": params.len_penalty,
        },
        "pivot_language": preparation.pivot_language,
        "target_language": preparation.target_language,
        **body_parameters,
        "heading_anchors": [],
        "edition_folio_anchors": [],
    }
    connection.execute(
        "INSERT INTO alignment_runs(alignment_run_id, document_group_id, "
        "pivot_source_file_id, target_source_file_id, pivot_segment_set_id, "
        "target_segment_set_id, algorithm, algorithm_version, parameters_json, "
        "status, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            document_group_id,
            pivot_source_id,
            target_source_id,
            preparation.pivot_set_id,
            preparation.target_set_id,
            BERTALIGN_ALGORITHM,
            BERTALIGN_ALGORITHM_VERSION,
            json.dumps(parameters, ensure_ascii=False, separators=(",", ":")),
            "completed",
            timestamp,
            timestamp,
        ),
    )
    link_rows: List[Tuple[object, ...]] = []
    member_rows: List[Tuple[object, ...]] = []
    for order_index, link in enumerate(aligned):
        link_id = f"alignment-link-{uuid.uuid4().hex}"
        link_rows.append(
            (
                link_id,
                run_id,
                order_index,
                round(link.cost, 6),
                link.confidence,
                link.anchor_key or None,
                link.review_status,
            )
        )
        member_rows.extend(
            [
                (link_id, "pivot", pivot_segments[index][0], index - link.source_start)
                for index in range(link.source_start, link.source_end)
            ]
            + [
                (link_id, "target", target_segments[index][0], index - link.target_start)
                for index in range(link.target_start, link.target_end)
            ]
        )
    connection.executemany(
        "INSERT INTO alignment_links(alignment_link_id, alignment_run_id, "
        "order_index, cost, confidence, anchor_key, review_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        link_rows,
    )
    connection.executemany(
        "INSERT INTO alignment_link_members(alignment_link_id, side, "
        "segment_id, member_order) VALUES (?, ?, ?, ?)",
        member_rows,
    )
    rejected_count = sum(link.review_status == "rejected" for link in aligned)
    unmatched_count = sum(link.review_status == "unmatched" for link in aligned)
    return {
        "alignment_run_id": run_id,
        "document_group_id": document_group_id,
        "pivot_source_file_id": pivot_source_id,
        "target_source_file_id": target_source_id,
        "pivot_segment_count": len(pivot_segments),
        "target_segment_count": len(target_segments),
        "alignment_link_count": len(aligned),
        "accepted_link_count": len(aligned) - rejected_count - unmatched_count,
        "rejected_link_count": rejected_count,
        "unmatched_link_count": unmatched_count,
        "numbered_note_link_count": 0,
        "heading_anchor_count": 0,
        "folio_anchor_count": 0,
        "algorithm": BERTALIGN_ALGORITHM,
        "algorithm_version": BERTALIGN_ALGORITHM_VERSION,
        "embedding_model_id": BERTALIGN_MODEL_ID,
        "backend": BERTALIGN_ALGORITHM,
        "status": "completed",
        "reused": False,
    }


def generate_bertalign_alignment(
    db_path: Path,
    document_group_id: object,
    pivot_source_file_id: object,
    target_source_file_id: object,
    *,
    force: bool = False,
    model_cache_dir: Path | None = None,
    reviewed_body_ranges: Dict[str, List[int]] | None = None,
    expected_segment_set_ids: Mapping[str, str] | None = None,
    params: BertalignParams | None = None,
    write_window: ta.WriteWindow | None = None,
    compute_runner: Callable[..., Tuple[List[SemanticLink], list]] | None = None,
) -> Dict[str, object]:
    """Generate (or reuse) a Bertalign-backend alignment for one version pair."""

    group_id = str(document_group_id or "").strip()
    if not group_id:
        raise ta.InvalidAlignmentRequest("document_group_id is required")
    pivot_id = ta._validate_source_id(pivot_source_file_id)
    target_id = ta._validate_source_id(target_source_file_id)
    params = params or BertalignParams()
    cache_dir = (
        Path(model_cache_dir)
        if model_cache_dir is not None
        else ta._default_alignment_model_cache(Path(db_path))
    )
    transaction_window = write_window or nullcontext
    with transaction_window():
        connection = open_writable_index(Path(db_path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            install_text_alignment_schema(connection)
            ta._require_pair(connection, group_id, pivot_id, target_id)
            pivot_set_id, pivot_segments = ta._segment_set(connection, pivot_id)
            target_set_id, target_segments = ta._segment_set(connection, target_id)
            if expected_segment_set_ids is not None and expected_segment_set_ids != {
                "pivot": pivot_set_id,
                "target": target_set_id,
            }:
                raise ta.InvalidAlignmentRequest("文献解析文本已更新，请重新加载正文范围后再提交")
            if reviewed_body_ranges is None:
                reviewed_body_ranges = ta.latest_reviewed_body_ranges(
                    connection, pivot_set_id, target_set_id
                )
            if reviewed_body_ranges is not None:
                ta.validate_reviewed_body_ranges(
                    reviewed_body_ranges, len(pivot_segments), len(target_segments)
                )
            preparation = ta.AlignmentPreparation(
                pivot_set_id,
                target_set_id,
                tuple(pivot_segments),
                tuple(target_segments),
                (),
                (),
                (),
                pivot_language=ta._segment_set_language(connection, pivot_set_id),
                target_language=ta._segment_set_language(connection, target_set_id),
                reviewed_body_ranges=reviewed_body_ranges,
            )
            if not force:
                body_parameters = _bertalign_body_ranges_parameter(preparation)
                existing = next(
                    (
                        row
                        for row in connection.execute(
                            "SELECT alignment_run_id, parameters_json FROM alignment_runs "
                            "WHERE document_group_id = ? AND pivot_source_file_id = ? "
                            "AND target_source_file_id = ? AND pivot_segment_set_id = ? "
                            "AND target_segment_set_id = ? AND algorithm = ? "
                            "AND algorithm_version = ? AND status = 'completed' "
                            "ORDER BY completed_at DESC, rowid DESC",
                            (
                                group_id,
                                pivot_id,
                                target_id,
                                pivot_set_id,
                                target_set_id,
                                BERTALIGN_ALGORITHM,
                                BERTALIGN_ALGORITHM_VERSION,
                            ),
                        ).fetchall()
                        if (parameters := ta._json_object(row["parameters_json"])).get(
                            "embedding_model_id"
                        )
                        == BERTALIGN_MODEL_ID
                        and parameters.get("body_ranges") == body_parameters["body_ranges"]
                        and parameters.get("body_range_source")
                        == body_parameters["body_range_source"]
                    ),
                    None,
                )
                if existing is not None:
                    counts = {
                        str(row["review_status"]): int(row["link_count"])
                        for row in connection.execute(
                            "SELECT review_status, COUNT(*) AS link_count "
                            "FROM alignment_links WHERE alignment_run_id = ? "
                            "GROUP BY review_status",
                            (existing["alignment_run_id"],),
                        )
                    }
                    connection.commit()
                    return {
                        "alignment_run_id": str(existing["alignment_run_id"]),
                        "document_group_id": group_id,
                        "pivot_source_file_id": pivot_id,
                        "target_source_file_id": target_id,
                        "pivot_segment_count": len(pivot_segments),
                        "target_segment_count": len(target_segments),
                        "alignment_link_count": sum(counts.values()),
                        "accepted_link_count": counts.get("automatic", 0),
                        "rejected_link_count": counts.get("rejected", 0),
                        "unmatched_link_count": counts.get("unmatched", 0),
                        "numbered_note_link_count": 0,
                        "heading_anchor_count": 0,
                        "folio_anchor_count": 0,
                        "algorithm": BERTALIGN_ALGORITHM,
                        "algorithm_version": BERTALIGN_ALGORITHM_VERSION,
                        "embedding_model_id": BERTALIGN_MODEL_ID,
                        "backend": BERTALIGN_ALGORITHM,
                        "status": "completed",
                        "reused": True,
                    }
            connection.commit()
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            connection.rollback()
            raise
        finally:
            connection.close()

    compute = compute_runner if compute_runner is not None else align_segments_bertalign
    computed = compute(
        [text for _segment_id, text in preparation.pivot_segments],
        [text for _segment_id, text in preparation.target_segments],
        model_dir=bertalign_model_dir(cache_dir),
        reviewed_body_ranges=preparation.reviewed_body_ranges,
        source_language=preparation.pivot_language,
        target_language=preparation.target_language,
        params=params,
    )

    with transaction_window():
        connection = open_writable_index(Path(db_path))
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = _generate_bertalign_on_connection(
                connection,
                group_id,
                pivot_id,
                target_id,
                preparation=preparation,
                computed=computed,
                params=params,
            )
            connection.commit()
            return result
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            connection.rollback()
            raise
        finally:
            connection.close()

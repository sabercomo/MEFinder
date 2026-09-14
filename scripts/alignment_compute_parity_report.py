"""Compare in-process vs out-of-process alignment compute on a real book pair.

Copies a private, read-only-sourced snapshot to two throwaway databases, runs
``generate_alignment`` with ``force`` on each — one through the in-process
compute, one through the subprocess runner — and compares the *published*
results exactly (order, cost, confidence, review_status, anchor_key and member
segment identities). The report JSON contains only counts, booleans, hashes and
version identity — never document text. The user's real library, model cache and
snapshot are never modified.

Usage:
    PY=.venv-macos312-arm64/bin/python
    NO_PROXY=127.0.0.1,localhost $PY scripts/alignment_compute_parity_report.py \
      --db "$SNAP/index.sqlite3" --group "$GROUP" --pivot "$PIVOT" --target "$TARGET" \
      --models "$MODELS" --output reports/alignment-compute-parity-real-$(date +%F).json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.me_finder.alignment_compute import SubprocessAlignmentComputeRunner
from src.me_finder.text_alignment import generate_alignment


def _run_rows(db: Path, run_id: str):
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        links = connection.execute(
            "SELECT order_index, cost, confidence, anchor_key, review_status, "
            "alignment_link_id FROM alignment_links WHERE alignment_run_id=? "
            "ORDER BY order_index",
            (run_id,),
        ).fetchall()
        rows = []
        for link in links:
            members = connection.execute(
                "SELECT side, member_order, segment_id FROM alignment_link_members "
                "WHERE alignment_link_id=? ORDER BY side, member_order",
                (link["alignment_link_id"],),
            ).fetchall()
            rows.append(
                {
                    "order_index": link["order_index"],
                    "cost": link["cost"],
                    "confidence": link["confidence"],
                    "anchor_key": link["anchor_key"],
                    "review_status": link["review_status"],
                    "members": [
                        (m["side"], m["member_order"], m["segment_id"]) for m in members
                    ],
                }
            )
        return rows


def _digest(rows) -> str:
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _isolated_cache(tmp: Path, models: Path, name: str) -> Path:
    """Compute cache that reuses the model blobs read-only but keeps every
    writable location fresh, so the user's real cache is never written (not the
    vector cache and not the ``installed`` receipt) and each run is cold.

    Only ``models--*`` snapshot dirs are symlinked; ``installed`` and
    ``document-vectors`` are fresh writable dirs (a symlink there would let the
    receipt writer / vector cache write through into the user's cache)."""

    cache = tmp / name
    cache.mkdir(parents=True)
    for child in models.iterdir():
        if child.name.startswith("models--"):
            (cache / child.name).symlink_to(child)
    (cache / "installed").mkdir()
    (cache / "document-vectors").mkdir()
    tag = models / "CACHEDIR.TAG"
    if tag.exists():
        shutil.copy2(tag, cache / "CACHEDIR.TAG")
    return cache


def _generate(db: Path, args, *, cache: Path, runner) -> dict:
    return generate_alignment(
        db,
        args.group,
        args.pivot,
        args.target,
        force=True,
        model_cache_dir=cache,
        compute_runner=runner,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--pivot", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    code_revision = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
        capture_output=True, text=True,
    ).stdout.strip()

    with tempfile.TemporaryDirectory() as tmp:
        db_a = Path(tmp) / "in_process.sqlite3"
        db_b = Path(tmp) / "subprocess.sqlite3"
        shutil.copy2(args.db, db_a)
        shutil.copy2(args.db, db_b)
        # Isolated cold caches per path: models symlinked read-only, fresh
        # vectors. The user's model/vector cache is never modified.
        cache_a = _isolated_cache(Path(tmp), Path(args.models), "cache_in")
        cache_b = _isolated_cache(Path(tmp), Path(args.models), "cache_sub")

        res_a = _generate(db_a, args, cache=cache_a, runner=None)
        runner = SubprocessAlignmentComputeRunner(task_id="parity-report")
        caps = runner.probe()
        res_b = _generate(db_b, args, cache=cache_b, runner=runner)

        rows_a = _run_rows(db_a, str(res_a["alignment_run_id"]))
        rows_b = _run_rows(db_b, str(res_b["alignment_run_id"]))

        excluded = ["alignment_run_id", "alignment_link_id", "created_at", "completed_at"]
        # Redact private library identifiers from the committed report: keep a
        # one-way hash of the pair, drop the raw ids. Only counts/booleans/hashes
        # are persisted.
        private_ids = ("document_group_id", "pivot_source_file_id", "target_source_file_id")
        pair_hash = hashlib.sha256(
            "|".join(str(res_a.get(k, "")) for k in private_ids).encode("utf-8")
        ).hexdigest()[:16]
        summary_a = {k: v for k, v in res_a.items() if k not in excluded and k not in private_ids}
        summary_b = {k: v for k, v in res_b.items() if k not in excluded and k not in private_ids}

        report = {
            "report": "alignment-compute-parity-real",
            "code_revision": code_revision,
            "pair_hash": pair_hash,
            "worker_capabilities": caps,
            "excluded_nondeterministic_fields": excluded,
            "link_count": len(rows_a),
            "return_summary_identical": summary_a == summary_b,
            "rows_identical": rows_a == rows_b,
            "in_process_digest": _digest(rows_a),
            "subprocess_digest": _digest(rows_b),
            "return_summary_in_process": summary_a,
            "return_summary_subprocess": summary_b,
        }
        if rows_a != rows_b:
            diffs = [
                {"order_index": a.get("order_index"), "in_process": a, "subprocess": b}
                for a, b in zip(rows_a, rows_b)
                if a != b
            ][:20]
            report["first_diffs"] = diffs

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"rows_identical={report['rows_identical']} link_count={report['link_count']}")
        print(f"digest_in_process={report['in_process_digest'][:16]}")
        print(f"digest_subprocess={report['subprocess_digest'][:16]}")
        print(f"wrote {args.output}")
        return 0 if report["rows_identical"] and report["return_summary_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

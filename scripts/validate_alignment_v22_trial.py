"""Compare the formal v22 pipeline with saved candidate runs on a fresh DB copy.

Run from the repository root. --source-copy must name the existing experiment
database, never the production database. The input is opened read-only; all
generation runs against a new trial database. No monkeypatching is used.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from src.me_finder.text_alignment import generate_alignment


def link_snapshot(connection: sqlite3.Connection, run_id: str) -> list:
    """Return ID-independent links with complete ordered member lists."""
    members = {}
    for link_id, side, order in connection.execute(
        "SELECT m.alignment_link_id, m.side, s.order_index "
        "FROM alignment_link_members m JOIN text_segments s ON s.segment_id=m.segment_id "
        "JOIN alignment_links l ON l.alignment_link_id=m.alignment_link_id "
        "WHERE l.alignment_run_id=? ORDER BY s.order_index", (run_id,),
    ):
        members.setdefault(link_id, {}).setdefault(side, []).append(order)
    links = []
    for link_id, status, confidence in connection.execute(
        "SELECT alignment_link_id, review_status, confidence FROM alignment_links "
        "WHERE alignment_run_id=?", (run_id,),
    ):
        sides = members.get(link_id, {})
        links.append([sides.get("pivot", []), sides.get("target", []), status, confidence])
    return sorted(links)


def main() -> None:
    """Create an isolated copy and verify every saved candidate pair."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-copy", type=Path, required=True)
    parser.add_argument("--trial-root", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--candidate-report", type=Path, required=True)
    args = parser.parse_args()
    source = args.source_copy.resolve()
    trial = args.trial_root.resolve()
    database = trial / "data/index.sqlite3"
    if database.exists():
        raise FileExistsError(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    report = json.loads(args.candidate_report.read_text(encoding="utf-8"))
    records = []
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        original.row_factory = sqlite3.Row
        with closing(sqlite3.connect(database)) as copied:
            original.backup(copied)
        print(f"Trial copy ready: {database}", flush=True)
        for pair in report["all_pairs"]:
            pivot, target = pair["pair"].split(" -> ")
            run = original.execute(
                "SELECT * FROM alignment_runs WHERE pivot_source_file_id=? "
                "AND target_source_file_id=? AND algorithm_version='21' "
                "AND status='completed' ORDER BY completed_at DESC, rowid DESC LIMIT 1",
                (pivot, target),
            ).fetchone()
            parameters = json.loads(run["parameters_json"])
            expected = link_snapshot(original, run["alignment_run_id"])
            started = time.perf_counter()
            result = generate_alignment(
                database, run["document_group_id"], pivot, target,
                model_cache_dir=args.cache.resolve(), embedding_model_id="multilingual-e5-large",
                reviewed_body_ranges=parameters["body_ranges"],
            )
            with closing(sqlite3.connect(database)) as current:
                actual = link_snapshot(current, result["alignment_run_id"])
                new_parameters = json.loads(current.execute(
                    "SELECT parameters_json FROM alignment_runs WHERE alignment_run_id=?",
                    (result["alignment_run_id"],),
                ).fetchone()[0])
            same = actual == expected
            anchors_same = parameters["heading_anchors"] == new_parameters["heading_anchors"]
            record = {
                "pair": pair["pair"], "algorithm_version": result["algorithm_version"],
                "semantic_version": new_parameters["semantic_alignment_version"],
                "candidate_run": run["alignment_run_id"], "formal_run": result["alignment_run_id"],
                "links_equal": same, "anchors_equal": anchors_same,
                "candidate_links": len(expected), "formal_links": len(actual),
                "accepted": result["accepted_link_count"], "reused": result["reused"],
                "seconds": round(time.perf_counter() - started, 3),
                "links_sha256": hashlib.sha256(json.dumps(actual, ensure_ascii=False).encode()).hexdigest(),
            }
            records.append(record)
            (trial / "validation.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            print(json.dumps(record, ensure_ascii=False), flush=True)
            if not (same and anchors_same and result["algorithm_version"] == "22" and not result["reused"]):
                raise AssertionError(f"Formal/candidate mismatch: {pair['pair']}")
    with closing(sqlite3.connect(database)) as connection:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
    assert integrity == "ok", integrity
    print(f"Verified {len(records)} pairs; quick_check={integrity}", flush=True)


if __name__ == "__main__":
    main()

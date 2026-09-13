"""Round-2 baseline for issue #18: regenerate every snapshot pair with the
*current* formal algorithm on a writable copy of the 2026-09-11 real-library
snapshot, verify the raw-DP reproduction path, and re-locate the 11 fixtures
and the 61 control samples (60 correct + n74) on that fresh baseline.

Why regenerate everything: the snapshot's stored v22 runs were produced around
2026-09-09 by the then-installed build with *detected* body ranges; commit
``12ac741`` later folded author introductions into the body region, so several
stored runs no longer match current formal output.  The round-2 baseline is
the current formal algorithm on the same segment sets and the same frozen
vectors — no re-embedding, no production write.  Stored runs are kept as the
historical reference and diffed per pair.

Usage:
    python -m scripts.d_two_stage_baseline \
        --db .codex-tmp/d-round2/index-baseline.sqlite3 \
        --cache .codex-tmp/d-round2/cache \
        --out .codex-tmp/d-round2/baseline-2026-09-14.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.me_finder.semantic_alignment import (  # noqa: E402
    EMBEDDING_RUNTIME_VERSION,
    SEMANTIC_ALIGNMENT_VERSION,
    _align_partition,
    _group_rows,
    _sequence_cache_path,
    cached_text_sequence_vectors,
)
from src.me_finder.text_alignment import (  # noqa: E402
    ALIGNMENT_ALGORITHM_VERSION,
    SEGMENTER_VERSION,
    generate_alignment,
)

MODEL_ID = "multilingual-e5-large"
LOW_THRESHOLD = 0.83


def _texts(con, set_id):
    return [r[0] for r in con.execute(
        "SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index",
        (set_id,))]


def _frozen_links(con, run_id):
    """All links as (s0, s1, t0, t1, status, anchor_key), path-ordered."""
    out = []
    for r in con.execute(
        "SELECT alignment_link_id, review_status, anchor_key FROM alignment_links "
        "WHERE alignment_run_id=? ORDER BY order_index", (run_id,)):
        p = [x[0] for x in con.execute(
            "SELECT t.order_index FROM alignment_link_members m JOIN text_segments t "
            "ON t.segment_id=m.segment_id WHERE m.alignment_link_id=? AND m.side='pivot' "
            "ORDER BY t.order_index", (r["alignment_link_id"],))]
        t = [x[0] for x in con.execute(
            "SELECT t.order_index FROM alignment_link_members m JOIN text_segments t "
            "ON t.segment_id=m.segment_id WHERE m.alignment_link_id=? AND m.side='target' "
            "ORDER BY t.order_index", (r["alignment_link_id"],))]
        s0, s1 = (min(p), max(p) + 1) if p else (None, None)
        t0, t1 = (min(t), max(t) + 1) if t else (None, None)
        out.append((s0, s1, t0, t1, r["review_status"], r["anchor_key"]))
    return out


def _anchor_boundaries(frozen):
    return sorted((f[0], f[2]) for f in frozen
                  if f[5] is not None and f[0] is not None and f[2] is not None)


def _partitions(body, anchors):
    (bs0, bs1), (bt0, bt1) = body["pivot"], body["target"]
    knots = [(bs0, bt0, False)] + [(s, t, True) for s, t in anchors] + [(bs1, bt1, False)]
    parts = []
    for (as_, at_, consumed), (bs_, bt_, _c) in zip(knots, knots[1:]):
        s0 = as_ + (1 if consumed else 0)
        t0 = at_ + (1 if consumed else 0)
        if s0 < bs_ or t0 < bt_:
            parts.append((s0, bs_, t0, bt_))
    return parts


class PairView:
    """Read-only view of one pair's freshest completed v22 run."""

    def __init__(self, con: sqlite3.Connection, cache: Path, pair: tuple[str, str]):
        self.con = con
        self.cache = cache
        self.pair = pair
        row = con.execute(
            "SELECT alignment_run_id, pivot_segment_set_id, target_segment_set_id, "
            "parameters_json, created_at FROM alignment_runs WHERE pivot_source_file_id=? "
            "AND target_source_file_id=? AND algorithm_version='22' AND status='completed' "
            "ORDER BY created_at DESC LIMIT 1", pair).fetchone()
        if row is None:
            raise SystemExit(f"no completed v22 run for {pair}")
        self.run_id = row["alignment_run_id"]
        self.pivot_set = row["pivot_segment_set_id"]
        self.target_set = row["target_segment_set_id"]
        self.created_at = row["created_at"]
        params = json.loads(row["parameters_json"])
        self.body = params["body_ranges"]
        self.body_source = params.get("body_range_source")
        self.frozen = _frozen_links(con, self.run_id)
        self.anchors = _anchor_boundaries(self.frozen)
        self.parts = _partitions(self.body, self.anchors)
        self._vectors: dict[str, tuple] = {}

    def vectors(self, set_id: str):
        if set_id not in self._vectors:
            import numpy as np
            texts = _texts(self.con, set_id)
            vectors = cached_text_sequence_vectors(texts, self.cache, model_id=MODEL_ID)
            if vectors is None:
                raise SystemExit(f"vector cache miss for {set_id}")
            prefix = np.vstack([np.zeros((1, vectors.shape[1]), dtype=np.float32),
                                np.cumsum(vectors, axis=0)])
            lengths = [max(1, sum(not ch.isspace() for ch in t)) for t in texts]
            self._vectors[set_id] = (prefix, _group_rows(prefix), lengths)
        return self._vectors[set_id]

    def fidelity(self) -> dict:
        """Reproduce in-body frozen links with the production DP (raw layer)."""
        sp, sg, sl = self.vectors(self.pivot_set)
        tp, tg, tl = self.vectors(self.target_set)
        (bs0, bs1), (bt0, bt1) = self.body["pivot"], self.body["target"]
        rebuilt = set()
        for s0, s1, t0, t1 in self.parts:
            for link in _align_partition(sp, tp, sl, tl, s0, s1, t0, t1, sg, tg,
                                         LOW_THRESHOLD):
                rebuilt.add((link.source_start, link.source_end,
                             link.target_start, link.target_end))
        for s, t in self.anchors:
            rebuilt.add((s, s + 1, t, t + 1))
        frozen_spans = {(f[0], f[1], f[2], f[3]) for f in self.frozen
                        if f[0] is not None and f[2] is not None
                        and bs0 <= f[0] and f[1] <= bs1 and bt0 <= f[2] and f[3] <= bt1}
        return {
            "reproduced": len(frozen_spans & rebuilt),
            "frozen_in_body": len(frozen_spans),
            "raw_dp_links": len(rebuilt),
        }

    def status_counts(self) -> dict:
        counts: dict[str, int] = {}
        for f in self.frozen:
            counts[f[4]] = counts.get(f[4], 0) + 1
        return counts

    def link_at(self, pivot_order: int):
        for f in self.frozen:
            if f[0] is not None and f[0] <= pivot_order < f[1]:
                return f
        return None


def regenerate_all(db: Path, cache: Path) -> list[dict]:
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    pairs = sorted(
        (r[0], r[1]) for r in con.execute(
            "SELECT DISTINCT pivot_source_file_id, target_source_file_id FROM alignment_runs "
            "WHERE algorithm_version='22' AND status='completed'"))
    con.close()
    out = []
    for pivot, target in pairs:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
            row = con.execute(
                "SELECT document_group_id FROM alignment_runs WHERE pivot_source_file_id=? "
                "AND target_source_file_id=? AND status='completed' "
                "ORDER BY created_at DESC LIMIT 1", (pivot, target)).fetchone()
            group = row[0]
        started = time.monotonic()
        result = generate_alignment(
            db, group, pivot, target, force=True, model_cache_dir=cache,
            embedding_model_id=MODEL_ID,
        )
        seconds = round(time.monotonic() - started, 1)
        print(f"regen {pivot[:28]} -> {target[:28]}: "
              f"{result['alignment_link_count']} links ({seconds}s)", flush=True)
        out.append({"pair": f"{pivot} -> {target}", "seconds": seconds,
                    "links": result["alignment_link_count"]})
    return out


def diff_runs(old: PairView, new: PairView) -> dict:
    old_spans = {(f[0], f[1], f[2], f[3], f[4]) for f in old.frozen}
    new_spans = {(f[0], f[1], f[2], f[3], f[4]) for f in new.frozen}
    old_body = {b for b in old.body["pivot"]}
    return {
        "historical_run": old.run_id,
        "historical_created_at": old.created_at,
        "historical_body_source": old.body_source,
        "historical_body": old.body,
        "fresh_body": new.body,
        "fresh_body_source": new.body_source,
        "identical": old_spans == new_spans,
        "only_historical": len(old_spans - new_spans),
        "only_fresh": len(new_spans - old_spans),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--fixtures", default="reports/d-fixture-reverify-2026-09-07.json")
    parser.add_argument("--golds", default="reports/d-gap-penalty-experiment-2026-09-07.json")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    db = Path(args.db).resolve()
    cache = Path(args.cache).resolve()
    out: dict = {"generated_by": "scripts/d_two_stage_baseline.py"}

    out["regeneration"] = regenerate_all(db, cache)

    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    pairs = sorted(
        (r[0], r[1]) for r in con.execute(
            "SELECT DISTINCT pivot_source_file_id, target_source_file_id FROM alignment_runs "
            "WHERE algorithm_version='22' AND status='completed'"))
    views: dict[str, PairView] = {}
    historical: dict[str, PairView] = {}
    for pair in pairs:
        views[f"{pair[0]} -> {pair[1]}"] = PairView(con, cache, pair)
    # Historical reference = the newest run that is NOT the fresh one.
    for key, view in views.items():
        pivot, target = key.split(" -> ")
        rows = con.execute(
            "SELECT alignment_run_id, created_at FROM alignment_runs WHERE "
            "pivot_source_file_id=? AND target_source_file_id=? AND "
            "algorithm_version='22' AND status='completed' "
            "ORDER BY created_at DESC LIMIT 2", (pivot, target)).fetchall()
        if len(rows) > 1:
            historical[key] = {"run_id": rows[1]["alignment_run_id"],
                               "created_at": rows[1]["created_at"]}

    # ---- fingerprints ----
    import numpy
    import fastembed
    vector_hashes = {}
    for view in views.values():
        for set_id in (view.pivot_set, view.target_set):
            if set_id not in vector_hashes:
                texts = _texts(con, set_id)
                name = _sequence_cache_path(texts, cache, model_id=MODEL_ID).name
                path = cache / "document-vectors" / name
                vector_hashes[set_id] = {
                    "segments": len(texts),
                    "sha256": sha256_file(path) if path.is_file() else None,
                }
    out["environment"] = {
        "source_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parents[1])).stdout.strip(),
        "db_sha256": sha256_file(db),
        "model_id": MODEL_ID,
        "embedding_runtime_version": EMBEDDING_RUNTIME_VERSION,
        "segmenter_version": SEGMENTER_VERSION,
        "semantic_alignment_version": SEMANTIC_ALIGNMENT_VERSION,
        "alignment_algorithm_version": ALIGNMENT_ALGORITHM_VERSION,
        "low_confidence_threshold": LOW_THRESHOLD,
        "python": sys.version.split()[0],
        "numpy": numpy.__version__,
        "fastembed": fastembed.__version__,
        "vector_cache": vector_hashes,
    }

    # ---- fidelity + drift ----
    fidelity, drift = {}, {}
    for key, view in views.items():
        fid = view.fidelity()
        counts = view.status_counts()
        fidelity[key] = {"final_status_counts": counts, **fid}
        line = (f"fidelity {key[:44]}: {fid['reproduced']}/{fid['frozen_in_body']} "
                f"raw={fid['raw_dp_links']} final={sum(counts.values())}")
        if key in historical:
            old_view = PairView(con, cache, tuple(key.split(" -> ")))
            # temporarily pin the historical run
            old_view.run_id = historical[key]["run_id"]
            old_view.frozen = _frozen_links(con, historical[key]["run_id"])
            old_params = json.loads(con.execute(
                "SELECT parameters_json FROM alignment_runs WHERE alignment_run_id=?",
                (historical[key]["run_id"],)).fetchone()["parameters_json"])
            old_view.body = old_params["body_ranges"]
            old_view.body_source = old_params.get("body_range_source")
            old_view.anchors = _anchor_boundaries(old_view.frozen)
            old_view.parts = _partitions(old_view.body, old_view.anchors)
            d = diff_runs(old_view, view)
            drift[key] = d
            line += (f" | drift only_old={d['only_historical']} only_new={d['only_fresh']}"
                     f" body {d['historical_body_source']}{d['historical_body']['pivot']}"
                     f"→{d['fresh_body_source']}{d['fresh_body']['pivot']}")
        print(line, flush=True)
    out["fidelity"] = fidelity
    out["drift_vs_historical"] = drift

    # ---- relocate fixtures ----
    fixtures = json.loads(Path(args.fixtures).read_text(encoding="utf-8"))["fixtures"]
    relocated = []
    for fx in fixtures:
        key = fx["pair"]
        if key not in views:
            raise SystemExit(f"fixture pair missing from baseline: {key}")
        view = views[key]
        pivot = fx["pivot_v13_order"] if "pivot_v13_order" in fx else fx["pivot_v13"]
        texts = _texts(con, view.pivot_set)
        text_ok = 0 <= pivot < len(texts) and texts[pivot] == fx["pivot_text"]
        link = view.link_at(pivot)
        record = {
            "n": fx["n"],
            "pair": key,
            "pivot_v13": pivot,
            "pivot_text_verbatim_match": text_ok,
            "baseline_link": {
                "source": [link[0], link[1]] if link and link[0] is not None else None,
                "target": [link[2], link[3]] if link and link[2] is not None else None,
                "status": link[4] if link else None,
            } if link else {"source": [pivot, pivot + 1], "target": None,
                            "status": "unmatched"},
            "correct_target_v13": fx["correct_target_v13"],
            "historical_v21_status": fx.get("new_v21_status") or fx.get("new_status"),
        }
        relocated.append(record)
        print(f"fixture n{fx['n']:>2}: {record['baseline_link']['status']:<10} "
              f"target={record['baseline_link']['target']} text_ok={text_ok}", flush=True)
    out["fixtures"] = relocated

    # ---- relocate 61 control samples ----
    golds = json.loads(Path(args.golds).read_text(encoding="utf-8"))["golds"]
    gold_rows = []
    for gold in golds:
        key = gold["pair"]
        if key not in views:
            raise SystemExit(f"gold pair missing from baseline: {key}")
        view = views[key]
        link = view.link_at(gold["pivot_v13"])
        target_now = [link[2], link[3]] if link and link[2] is not None else None
        status_now = link[4] if link else "unmatched"
        members_now = (set(range(target_now[0], target_now[1]))
                       if target_now else set())
        members_gold = set(gold["gold_target_v13"])
        row = {
            "n": gold["n"], "label": gold["label"], "pair": key,
            "pivot_v13": gold["pivot_v13"],
            "gold_target_v13": gold["gold_target_v13"],
            "baseline_target": target_now, "baseline_status": status_now,
            "baseline_correct_accepted": bool(
                members_now == members_gold
                and status_now in ("automatic", "note_automatic")),
        }
        gold_rows.append(row)
    ok = sum(1 for r in gold_rows if r["baseline_correct_accepted"])
    out["controls"] = {"total": len(gold_rows), "correct_accepted": ok,
                       "changed_or_wrong": len(gold_rows) - ok, "rows": gold_rows}
    print(f"controls: {ok}/{len(gold_rows)} accepted at gold target on fresh baseline",
          flush=True)
    con.close()

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

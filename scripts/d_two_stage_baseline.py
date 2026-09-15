"""Round-2 baseline for issue #18: regenerate every snapshot pair with the
*current* formal algorithm on a writable copy of the 2026-09-11 real-library
snapshot, reproduce the raw-DP layer through the exact production call path,
and re-locate the 11 fixtures and the 61 control samples (60 correct + n74).

Why regenerate everything: the snapshot's stored v22 runs were produced around
2026-09-09 by the then-installed build with *detected* body ranges; commit
``12ac741`` later folded author introductions into the body region, so several
stored runs no longer match current formal output.  The round-2 baseline is
the current formal algorithm on the same segment sets and the same frozen
vectors — no re-embedding, no production write.

Reproduction path: the raw DP layer is re-created by calling production's
``_align_monotonic_sequences`` with exactly the inputs production feeds it
(raw cached vectors sliced to the stored body range, structural anchors from
the full texts shifted into body coordinates, stored verified folios, stored
languages, default E5 thresholds).  This also returns the *validated* anchor
list — the true partition knots, which cannot be recovered from stored links
alone because note-channel overrides replace DP links (and can consume a
validated anchor's row) after the DP.

Usage:
    python -m scripts.d_two_stage_baseline \
        --db .codex-tmp/d-round2/index-baseline.sqlite3 \
        --cache .codex-tmp/d-round2/cache \
        --out .codex-tmp/d-round2/baseline-2026-09-14.json [--skip-regen]
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

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.me_finder.semantic_alignment import (  # noqa: E402
    EMBEDDING_RUNTIME_VERSION,
    SEMANTIC_ALIGNMENT_VERSION,
    _align_monotonic_sequences,
    _normalized_rows,
    _sequence_cache_path,
    embedding_model_config,
)
from src.me_finder.alignment_anchors import HeadingAnchor  # noqa: E402
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


class PairView:
    """Read-only view of one pair's freshest completed v22 run + exact raw-DP
    reproduction through the production call path."""

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
        self.params = params
        self.body = params["body_ranges"]
        self.body_source = params.get("body_range_source")
        self.pivot_language = params.get("pivot_language", "und")
        self.target_language = params.get("target_language", "und")
        self.frozen = _frozen_links(con, self.run_id)
        self._raw: dict | None = None

    # ---- inputs ----
    def _raw_vectors(self, set_id: str) -> np.ndarray:
        texts = _texts(self.con, set_id)
        path = _sequence_cache_path(texts, self.cache, model_id=MODEL_ID)
        if not path.is_file():
            raise SystemExit(f"vector cache miss for {set_id}")
        return np.load(path, allow_pickle=False, mmap_mode="r")

    def _lengths(self, set_id: str) -> list[int]:
        return [max(1, sum(not ch.isspace() for ch in t)) for t in _texts(self.con, set_id)]

    def status_counts(self) -> dict:
        counts: dict[str, int] = {}
        for f in self.frozen:
            counts[f[4]] = counts.get(f[4], 0) + 1
        return counts

    # ---- exact raw-DP reproduction (body-local coordinates) ----
    def raw_reproduction(self) -> dict:
        if self._raw is None:
            from src.me_finder.semantic_alignment import find_heading_anchors
            ps, pe = self.body["pivot"]
            ts, te = self.body["target"]
            source_texts = _texts(self.con, self.pivot_set)
            target_texts = _texts(self.con, self.target_set)
            source_raw = np.asarray(self._raw_vectors(self.pivot_set), dtype=np.float32)
            target_raw = np.asarray(self._raw_vectors(self.target_set), dtype=np.float32)
            embeddings = np.vstack([
                source_raw[ps:pe], target_raw[ts:te]])
            normalized = _normalized_rows(embeddings)
            source_vectors = normalized[: pe - ps]
            target_vectors = normalized[pe - ps:]
            structural = [
                HeadingAnchor(a.source_index - ps, a.target_index - ts, a.key)
                for a in find_heading_anchors(source_texts, target_texts)
                if ps <= a.source_index < pe and ts <= a.target_index < te
            ]
            folios = [
                HeadingAnchor(f["pivot_order_index"] - ps,
                              f["target_order_index"] - ts,
                              f["key"])
                for f in self.params.get("edition_folio_anchors", [])
                if ps <= f["pivot_order_index"] < pe
                and ts <= f["target_order_index"] < te
            ]
            thresholds = embedding_model_config(MODEL_ID).thresholds
            links, anchors = _align_monotonic_sequences(
                source_texts[ps:pe], target_texts[ts:te],
                source_vectors, target_vectors,
                folios,
                source_language=self.pivot_language,
                target_language=self.target_language,
                thresholds=thresholds,
                structural_anchors=structural,
            )
            self._raw = {
                "links": [  # back to absolute coordinates
                    (l.source_start + ps, l.source_end + ps,
                     l.target_start + ts, l.target_end + ts,
                     l.review_status, l.anchor_key)
                    for l in links
                ],
                "anchors_absolute": [
                    (a.source_index + ps, a.target_index + ts, a.key) for a in anchors
                ],
            }
        return self._raw

    def fidelity(self) -> dict:
        raw = self.raw_reproduction()
        raw_spans = {(l[0], l[1], l[2], l[3]) for l in raw["links"]
                     if l[0] is not None and l[2] is not None}
        frozen_spans = {(f[0], f[1], f[2], f[3]) for f in self.frozen
                        if f[0] is not None and f[2] is not None}
        one_sided_frozen = sum(1 for f in self.frozen
                               if f[0] is None or f[2] is None)
        return {
            "raw_dp_links": len(raw_spans),
            "frozen_final_links": len(self.frozen),
            "frozen_one_sided": one_sided_frozen,
            "raw_span_in_frozen": len(raw_spans & frozen_spans),
            "frozen_span_in_raw": len(frozen_spans & raw_spans),
        }

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
    parser.add_argument("--skip-regen", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    db = Path(args.db).resolve()
    cache = Path(args.cache).resolve()
    out: dict = {"generated_by": "scripts/d_two_stage_baseline.py"}

    if not args.skip_regen:
        out["regeneration"] = regenerate_all(db, cache)

    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    pairs = sorted(
        (r[0], r[1]) for r in con.execute(
            "SELECT DISTINCT pivot_source_file_id, target_source_file_id FROM alignment_runs "
            "WHERE algorithm_version='22' AND status='completed'"))
    views: dict[str, PairView] = {}
    for pair in pairs:
        views[f"{pair[0]} -> {pair[1]}"] = PairView(con, cache, pair)

    # ---- fingerprints ----
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
        "numpy": np.__version__,
        "fastembed": fastembed.__version__,
        "vector_cache": vector_hashes,
    }

    # ---- fidelity ----
    fidelity = {}
    for key, view in views.items():
        fid = view.fidelity()
        counts = view.status_counts()
        fidelity[key] = {"final_status_counts": counts, **fid}
        print(f"fidelity {key[:44]}: raw={fid['raw_dp_links']} "
              f"raw∩frozen={fid['raw_span_in_frozen']} frozen={fid['frozen_final_links']} "
              f"one_sided={fid['frozen_one_sided']} "
              f"final_counts={ {k: v for k, v in counts.items()} }", flush=True)
    out["fidelity"] = fidelity

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

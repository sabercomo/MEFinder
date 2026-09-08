"""Limited reading-localization acceptance of the n53 anchor candidate (issue #18).

Goal is coarse localization for reading: can a reader find the corresponding
passage near the linked target?  Not exact sentence/segment boundaries.  We
sample changed reading positions from the two pairs the rule touched (R9 权威,
DE->ZH number:1642), covering newly-matched / target-changed / pairing-lost, and
classify each into: direct (linked target IS the corresponding passage) / nearby
(the corresponding passage is within a few segments) / wrong (different passage or
chapter) / none (no result).  Accepted count is NOT used as correctness.

A similarity "reference passage" (the pivot's best cross-side match in a wide
window) is shown only as a judging aid; the actual source/target texts are printed
for the human to decide.  Reads the copy read-only.

    python -m scripts.d_anchor_acceptance --db .codex-tmp/d-experiment/index-v21.sqlite3 \
        --cache dist/... --out reports/d-anchor-acceptance-2026-09-07.json
"""
import argparse
import json
from pathlib import Path
import random
import sqlite3
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.me_finder.semantic_alignment import cached_text_sequence_vectors  # noqa: E402

PAIRS = {
    "R9(权威)": ("pdf-import-a47c115e247c8194", "pdf-import-1d4016f99fc5dbba",
               "reports/d-anchor-experiment-2026-09-07.json"),
    "DE->ZH(number:1642 spared)": ("pdf-import-6a0325052e779b9e", "pdf-hegel-philosophy-right",
                                   ".codex-tmp/d-experiment/p12b.json"),
}


def _texts(con, sid):
    return [r[0] for r in con.execute(
        "SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index", (sid,))]


def _classify(best_j, tgt, radius_direct=3, radius_near=12):
    if tgt is None:
        return "none"
    if tgt[0] - radius_direct <= best_j < tgt[1] + radius_direct:
        return "direct"
    if tgt[0] - radius_near <= best_j < tgt[1] + radius_near:
        return "nearby"
    return "wrong"


def run(args):
    con = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cache = Path(args.cache).resolve()
    rng = random.Random(20260907)
    out = {"pairs": {}}
    for name, (pv, tg, report) in PAIRS.items():
        rr = con.execute(
            "SELECT alignment_run_id, pivot_segment_set_id, target_segment_set_id FROM alignment_runs WHERE pivot_source_file_id=? "
            "AND target_source_file_id=? AND algorithm_version='21' ORDER BY created_at DESC LIMIT 1", (pv, tg)).fetchone()
        pt, tt = _texts(con, rr["pivot_segment_set_id"]), _texts(con, rr["target_segment_set_id"])
        # Pivot's FINAL (current/enhanced) target span, so "lost" spans are judged by
        # where the pivot actually ended up, not by the disappeared bookkeeping span.
        final = {}
        for row in con.execute("SELECT alignment_link_id, review_status FROM alignment_links WHERE alignment_run_id=?", (rr["alignment_run_id"],)):
            p = [x[0] for x in con.execute("SELECT t.order_index FROM alignment_link_members m JOIN text_segments t ON t.segment_id=m.segment_id WHERE m.alignment_link_id=? AND m.side='pivot'", (row["alignment_link_id"],))]
            t = [x[0] for x in con.execute("SELECT t.order_index FROM alignment_link_members m JOIN text_segments t ON t.segment_id=m.segment_id WHERE m.alignment_link_id=? AND m.side='target'", (row["alignment_link_id"],))]
            for s in p:
                final[s] = ([min(t), max(t) + 1] if t else None, row["review_status"])
        sv = cached_text_sequence_vectors(pt, cache, model_id="multilingual-e5-large")
        tv = cached_text_sequence_vectors(tt, cache, model_id="multilingual-e5-large")
        changed = json.loads(Path(report).read_text(encoding="utf-8"))["changed_links"]
        typed = {"new": [], "target-changed": [], "lost": []}
        for c in changed:
            b, e = (c["baseline"] or {}).get("target"), (c["enhanced"] or {}).get("target")
            bs, es = (c["baseline"] or {}).get("status"), (c["enhanced"] or {}).get("status")
            if not b and e and es == "automatic":
                typed["new"].append(c)
            elif b and e and b != e:
                typed["target-changed"].append(c)
            elif b and (bs == "automatic") and not e:
                typed["lost"].append(c)
        picks = []
        for kind, items in typed.items():
            rng.shuffle(items)
            picks += [(kind, c) for c in items[:args.per_type]]
        rows = []
        for kind, c in picks:
            s0, s1 = c["source"]
            i = s0  # representative pivot segment
            best_j = int(np.argmax(sv[i] @ tv.T))
            best_sim = float(sv[i] @ tv[best_j])
            final_target, final_status = final.get(i, (None, None))  # where the pivot ACTUALLY ends up
            cls = _classify(best_j, final_target)
            baseline_t = (c["baseline"] or {}).get("target")
            rows.append({
                "type": kind, "pivot_source": [s0, s1], "source_text": pt[i][:120],
                "baseline_target": baseline_t,
                "baseline_target_text": (tt[baseline_t[0]][:100] if baseline_t else None),
                "final_target": final_target, "final_status": final_status,
                "final_target_text": (tt[final_target[0]][:120] if final_target else None),
                "reference_best_target": best_j, "reference_best_sim": round(best_sim, 3),
                "reference_best_text": tt[best_j][:120],
                "classification": cls,
            })
        counts = {}
        for r in rows:
            counts[r["classification"]] = counts.get(r["classification"], 0) + 1
        out["pairs"][name] = {"removed": json.loads(Path(report).read_text(encoding="utf-8"))["removed_anchors"],
                              "type_totals": {k: len(v) for k, v in typed.items()},
                              "classification_counts": counts, "samples": rows}
    con.close()
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, d in out["pairs"].items():
        print(f"\n===== {name}  removed={d['removed']}  types={d['type_totals']}  classes={d['classification_counts']}")
        for r in d["samples"]:
            print(f"  [{r['type']:<14}] {r['classification']:<6} src{r['pivot_source']} "
                  f"base->T{r['baseline_target']} final->T{r['final_target']}({r['final_status']}) | ref T{r['reference_best_target']}({r['reference_best_sim']})")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--per-type", type=int, default=6)
    run(p.parse_args())

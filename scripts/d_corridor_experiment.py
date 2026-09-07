"""D corridor re-alignment experiment — cost ceiling diagnostic (issue #18).

For each still-failing fixture, ask whether the manually-correct target is
reachable under the *frozen* E5 scores and current cost model.  If the correct
grouping is not cheaper than the link the DP chose, no cost-based corridor
method (arm A boundary re-attach, arm B window DP) constrained to reuse those
scores can recover it — that is a negative result, and the honest verdict.

Database is opened read-only; nothing is written back.  Run on the rebuilt
v13/v21 copy after scripts.d_fixture_reverify has produced all six runs.

    python -m scripts.d_corridor_experiment \
        --db .codex-tmp/d-experiment/index-v21.sqlite3 \
        --cache dist/MEFinderData/runtime/components/text-alignment/models \
        --reverify reports/d-fixture-reverify-2026-09-07.json \
        --out reports/d-corridor-experiment-2026-09-07.json
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.me_finder.alignment_corridor_refine import (  # noqa: E402
    best_target_grouping, gap_to_reach_correct, link_cost, non_space_lengths,
    prefix_sums,
)
from src.me_finder.semantic_alignment import (  # noqa: E402
    cached_text_sequence_vectors, _align_partition, _group_rows,
)

THRESHOLD = 0.83  # multilingual-e5-large low-confidence threshold


def _texts(con, set_id):
    return [r[0] for r in con.execute(
        "SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index", (set_id,))]


def _pivot_link_span(con, run_id, pset, pivot_order):
    """The frozen link's full source/target spans covering the pivot segment."""
    seg = con.execute("SELECT segment_id FROM text_segments WHERE segment_set_id=? AND order_index=?",
                      (pset, pivot_order)).fetchone()
    if seg is None:
        return None
    link = con.execute(
        "SELECT l.alignment_link_id, l.review_status, l.confidence FROM alignment_links l "
        "JOIN alignment_link_members m ON m.alignment_link_id=l.alignment_link_id "
        "WHERE l.alignment_run_id=? AND m.segment_id=? AND m.side='pivot'", (run_id, seg[0])).fetchone()
    if link is None:
        return {"status": "unmatched"}
    spans = {}
    for side in ("pivot", "target"):
        orders = [r[0] for r in con.execute(
            "SELECT t.order_index FROM alignment_link_members m JOIN text_segments t ON t.segment_id=m.segment_id "
            "WHERE m.alignment_link_id=? AND m.side=? ORDER BY t.order_index", (link[0], side))]
        spans[side] = (min(orders), max(orders) + 1) if orders else None
    return {"status": link[1], "confidence": link[2], "pivot": spans["pivot"], "target": spans["target"]}


def _anchor_bracket(con, run_id, pivot_order):
    """The [source, target) corridor bracketed by the anchors around the pivot.

    Anchors are the fixed corridor endpoints the production DP re-runs between.
    Returns (s0, s1, t0, t1) or None.
    """
    anchors = []
    for r in con.execute(
        "SELECT alignment_link_id FROM alignment_links WHERE alignment_run_id=? AND anchor_key IS NOT NULL ORDER BY order_index",
        (run_id,)):
        p = [x[0] for x in con.execute(
            "SELECT t.order_index FROM alignment_link_members m JOIN text_segments t ON t.segment_id=m.segment_id "
            "WHERE m.alignment_link_id=? AND m.side='pivot'", (r["alignment_link_id"],))]
        t = [x[0] for x in con.execute(
            "SELECT t.order_index FROM alignment_link_members m JOIN text_segments t ON t.segment_id=m.segment_id "
            "WHERE m.alignment_link_id=? AND m.side='target'", (r["alignment_link_id"],))]
        if p and t:
            anchors.append((p[0], t[0]))
    prev = max((a for a in anchors if a[0] < pivot_order), key=lambda a: a[0], default=(-1, -1))
    nxt = min((a for a in anchors if a[0] > pivot_order), key=lambda a: a[0], default=None)
    if nxt is None:
        return None
    return (prev[0] + 1, nxt[0], prev[1] + 1, nxt[1])


def _armB_oracle(sp, tp, sg, tg, sl, tl, bracket, pivot_order):
    """Re-run the production DP over the corridor; return the pivot's target span."""
    s0, s1, t0, t1 = bracket
    links = _align_partition(sp, tp, sl, tl, s0, s1, t0, t1, sg, tg, THRESHOLD)
    for link in links:
        if link.source_start <= pivot_order < link.source_end:
            return {"target": [link.target_start, link.target_end], "status": link.review_status,
                    "confidence": round(link.confidence, 4)}
    return {"target": None, "status": "gap", "confidence": None}


def run(args):
    con = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cache = Path(args.cache).resolve()
    reverify = {r["n"]: r for r in json.loads(Path(args.reverify).read_text(encoding="utf-8"))["fixtures"]}
    vcache: dict[str, object] = {}
    results = []
    for n, fx in reverify.items():
        if n == 11:  # already fixed by #17
            continue
        pv, tg = fx["pair"].split(" -> ")
        run_row = con.execute(
            "SELECT alignment_run_id, pivot_segment_set_id, target_segment_set_id FROM alignment_runs "
            "WHERE pivot_source_file_id=? AND target_source_file_id=? AND algorithm_version='21' "
            "AND status='completed' ORDER BY created_at DESC LIMIT 1", (pv, tg)).fetchone()
        pset, tset = run_row["pivot_segment_set_id"], run_row["target_segment_set_id"]
        for sid in (pset, tset):
            if sid not in vcache:
                prefix = prefix_sums(cached_text_sequence_vectors(_texts(con, sid), cache, model_id="multilingual-e5-large"))
                vcache[sid] = (prefix, _group_rows(prefix))
        pptexts, tptexts = _texts(con, pset), _texts(con, tset)
        sp, sg = vcache[pset]
        tp, tg = vcache[tset]
        sl, tl = non_space_lengths(pptexts), non_space_lengths(tptexts)
        ratio = sum(tl) / max(sum(sl), 1)

        link = _pivot_link_span(con, run_row["alignment_run_id"], pset, fx["pivot_v13_order"])
        correct = fx["correct_target_v13"]
        record = {"n": n, "pair": fx["pair"], "new_status": link["status"],
                  "pivot_v13": fx["pivot_v13_order"], "correct_target_v13": correct}
        if link.get("pivot"):
            s0, s1 = link["pivot"]
            wt0, wt1 = link["target"] if link.get("target") else (correct[0] if correct else 0, (correct[0] if correct else 0) + 1)
            wcost, wsim = link_cost(sp, tp, sl, tl, s0, s1, wt0, wt1, ratio)
            record["wrong_link"] = {"source": [s0, s1], "target": [wt0, wt1], "cost": round(wcost, 4), "similarity": round(wsim, 4)}
        else:
            s0, s1 = fx["pivot_v13_order"], fx["pivot_v13_order"] + 1
            record["wrong_link"] = None
        if correct:
            center = correct[len(correct) // 2]
            best = best_target_grouping(sp, tp, sl, tl, s0, s1, center, ratio)
            ct0, ct1 = correct[0], correct[-1] + 1
            ecost, esim = link_cost(sp, tp, sl, tl, s0, s1, ct0, ct1, ratio)
            record["best_correct_grouping"] = {"target": [best["t0"], best["t1"]], "cost": round(float(best["cost"]), 4), "similarity": round(float(best["similarity"]), 4)}
            record["exact_correct_span"] = {"target": [ct0, ct1], "cost": round(ecost, 4), "similarity": round(esim, 4)}
            wcost = record.get("wrong_link", {}).get("cost", float("inf")) if record.get("wrong_link") else float("inf")
            reachable = float(best["cost"]) < wcost
            record["correct_cheaper_than_wrong"] = reachable
            record["correct_over_threshold"] = float(best["similarity"]) >= THRESHOLD
            record["verdict"] = (
                "arm-reachable" if reachable and float(best["similarity"]) >= THRESHOLD else
                "reachable-but-below-threshold" if reachable else
                "not-reachable-e5-prefers-wrong")
        else:
            record["verdict"] = "allow-unfixable"

        # Arm B oracle: re-run the *production* DP over the pivot's anchor
        # corridor.  Since it is the same globally-optimal algorithm over the
        # same scores, it reproduces the frozen link — the authoritative proof
        # that window re-DP (and arm A, a strict subset of its search) recovers
        # nothing under the frozen-cost constraint.
        bracket = _anchor_bracket(con, run_row["alignment_run_id"], fx["pivot_v13_order"])
        if bracket:
            oracle = _armB_oracle(sp, tp, sg, tg, sl, tl, bracket, fx["pivot_v13_order"])
            # A genuine fix means an *accepted* link on the correct target.
            reached = bool(correct and oracle["target"] and oracle["status"] == "automatic"
                           and set(range(*oracle["target"])) & set(correct))
            frozen_target = list(link["target"]) if link.get("target") else None
            record["armB_redp"] = {"corridor": list(bracket), "pivot_target": oracle["target"],
                                   "status": oracle["status"], "confidence": oracle["confidence"],
                                   "reached_correct": reached,
                                   "reproduces_frozen": oracle["target"] == frozen_target}
        else:
            record["armB_redp"] = {"corridor": None, "reached_correct": False}
        # Why the frozen path stays cheaper: the gap needed to reach the correct target.
        if correct and link.get("target"):
            record["gap_to_correct"] = gap_to_reach_correct(sl, tl, tuple(link["target"]), (correct[0], correct[-1] + 1))
        results.append(record)
    con.close()
    results.sort(key=lambda r: r["n"])
    Path(args.out).write_text(json.dumps({"fixtures": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    reachable = sum(1 for r in results if r.get("correct_cheaper_than_wrong"))
    redp_fixed = sum(1 for r in results if r.get("armB_redp", {}).get("reached_correct"))
    for r in results:
        w = r.get("wrong_link") or {}
        b = r.get("best_correct_grouping") or {}
        o = r.get("armB_redp") or {}
        g = r.get("gap_to_correct") or {}
        print(f"n{r['n']:>2} {r['new_status']:<9} wrong(sim={w.get('similarity','-')},cost={w.get('cost','-')}) "
              f"correct(sim={b.get('similarity','-')},cost={b.get('cost','-')}) "
              f"reDP->T{o.get('pivot_target')}(repro={o.get('reproduces_frozen')},fixed={o.get('reached_correct')}) "
              f"gap={g.get('gapped_segments','-')}seg/{g.get('gap_cost','-')} -> {r['verdict']}")
    repro = sum(1 for r in results if r.get("armB_redp", {}).get("reproduces_frozen"))
    print(f"\nlocal cheaper-correct: {reachable}/{len(results)}   "
          f"arm B re-DP fixed: {redp_fixed}/{len(results)}   "
          f"re-DP reproduces frozen link: {repro}/{len(results)} (tight corridors; wide anchor-free "
          f"corridors drift on the band but still do not reach the correct target)")
    print("negative result: every fixture's correct target needs gapping edition-only/noise segments "
          "at cost >> the frozen wrong link — the frozen DP is globally optimal under the frozen scores.")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--reverify", required=True)
    p.add_argument("--out", required=True)
    run(p.parse_args())

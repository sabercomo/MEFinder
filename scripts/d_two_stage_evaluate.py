"""Evaluate the round-2 corridor ablation output (issue #18).

Reads the corridor JSON produced by ``scripts.d_two_stage_corridors`` and the
baseline JSON, then reports, per the frozen acceptance protocol:

- fixture ledger (11): historical status, A baseline, per-arm states, net gain;
- controls (61): regression vs the baseline's accepted-at-gold set, n74 state;
- violations: newly accepted links touching apparatus/noise segments;
- corridor A'==A self-check coverage (validity instrument);
- embedder cost: texts, seconds, token truncation.

Arm A states come from ``A_recheck`` (the production-settings DP re-run inside
the corridor job, which was proven link-identical to the frozen raw path by the
``a_selfcheck_exact`` flag and carries real confidences) combined with the
run's frozen review status.  Gold answers are used only for evaluation.
Strict reading: the pivot link's accepted target member set must equal the
gold member set.  Auxiliary: the pivot link's target range lies inside the
gold span (rough location).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.me_finder.alignment_corridor_refine import (  # noqa: E402
    detect_edition_apparatus,
)
from scripts.d_two_stage_baseline import PairView, _texts  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corridors", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    corridors_doc = json.loads(Path(args.corridors).read_text(encoding="utf-8"))
    baseline_doc = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    samples = corridors_doc["samples"]
    baseline_controls = {row["n"]: row for row in baseline_doc["controls"]["rows"]}
    baseline_fixtures = {row["n"]: row for row in baseline_doc["fixtures"]}

    con = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    text_cache: dict[str, list[str]] = {}
    view_cache: dict[str, PairView] = {}
    flagged_cache: dict[tuple[str, int], bool] = {}

    def flag_target(pair_key: str, segment_order: int) -> bool:
        cache_key = (pair_key, segment_order)
        if cache_key in flagged_cache:
            return flagged_cache[cache_key]
        if pair_key not in view_cache:
            pivot_id, target_id = pair_key.split(" -> ")
            view_cache[pair_key] = PairView(
                con, Path(args.cache).resolve(), (pivot_id, target_id))
        view = view_cache[pair_key]
        if view.target_set not in text_cache:
            text_cache[view.target_set] = _texts(con, view.target_set)
        texts = text_cache[view.target_set]
        result = bool(
            0 <= segment_order < len(texts)
            and detect_edition_apparatus(texts, segment_order, segment_order + 1))
        flagged_cache[cache_key] = result
        return result

    def arm_state_from_path(path, pivot):
        link = next(((s0, s1, t0, t1, conf) for s0, s1, t0, t1, conf in path
                     if s0 <= pivot < s1), None)
        if link is None:
            return {"matched": False, "target": None, "confidence": None}
        matched = link[0] < link[1] and link[2] < link[3]
        return {"matched": matched,
                "target": [link[2], link[3]] if matched else None,
                "confidence": link[4] if matched else None}

    def accepted_members(state):
        if not state["matched"] or state["confidence"] is None:
            return None
        if state["confidence"] < 0.83:
            return None
        return set(range(state["target"][0], state["target"][1]))

    def arm_states(row):
        """A from A_recheck + frozen status; B/C/D4 from their paths."""
        key = f"{row['pair']}#{row['corridor_id']}"
        arms_data = corridors_doc["corridors"][key]["arms"]
        frozen = baseline_controls.get(row["n"]) or baseline_fixtures.get(row["n"])
        a_state = arm_state_from_path(arms_data["A_recheck"]["path"], row["pivot"])
        a_state["frozen_status"] = (frozen or {}).get(
            "baseline_link", {}).get("status") or (frozen or {}).get("baseline_status")
        out = {"A": a_state}
        for arm in ("B", "C", "D4"):
            out[arm] = arm_state_from_path(arms_data[arm]["path"], row["pivot"])
        return out

    # ---- fixtures ----
    fixture_rows = [row for row in samples.values() if row["kind"] == "fixture"]
    fixture_rows.sort(key=lambda row: row["n"])
    print("== fixtures (strict: accepted members == gold; aux: inside gold span) ==")
    ledger = []
    for row in fixture_rows:
        n = row["n"]
        gold = set(row["gold"])
        gold_span = ((min(row["gold"]), max(row["gold"]) + 1)
                     if row["gold"] else None)
        entry = {"n": n, "pair": row["pair"], "gold": row["gold"],
                 "corridor": row.get("corridor_id"),
                 "skipped": row.get("skipped")}
        if row.get("corridor_id") is None or row.get("skipped"):
            print(f"n{n:>2}: skipped={row.get('skipped', 'pivot not in any corridor')}")
            entry["arms"] = {}
            ledger.append(entry)
            continue
        states = arm_states(row)
        per_arm = {}
        for arm, state in states.items():
            members = accepted_members(state)
            accepted = bool(members) or (
                arm == "A" and state.get("frozen_status") in
                ("automatic", "note_automatic"))
            strict = bool(members) and members == gold
            aux = bool(state["target"] and gold_span
                       and gold_span[0] <= state["target"][0]
                       and state["target"][1] <= gold_span[1])
            per_arm[arm] = {
                "target": state["target"],
                "confidence": state["confidence"],
                "frozen_status": state.get("frozen_status"),
                "accepted": accepted, "strict_correct": strict,
                "inside_gold": aux,
            }
        entry["arms"] = per_arm
        ledger.append(entry)
        line = f"n{n:>2}: " + "  ".join(
            f"{arm}={per_arm[arm]['target']}({'*' if per_arm[arm]['strict_correct'] else ('~' if per_arm[arm]['inside_gold'] else 'x')})"
            for arm in ("A", "B", "C", "D4"))
        print(line)

    strict_fixed = {
        arm: sum(1 for e in ledger
                 if e.get("arms", {}).get(arm, {}).get("strict_correct"))
        for arm in ("A", "B", "C", "D4")}
    aux_fixed = {
        arm: sum(1 for e in ledger
                 if e.get("arms", {}).get(arm, {}).get("inside_gold"))
        for arm in ("A", "B", "C", "D4")}
    print(f"strict fixed: {strict_fixed}  aux inside-gold: {aux_fixed}")

    # ---- controls ----
    control_rows = [row for row in samples.values() if row["kind"] == "control"]
    control_rows.sort(key=lambda row: row["n"])
    a_reference = {n: r["baseline_correct_accepted"]
                   for n, r in baseline_controls.items()}
    print(f"\n== controls: baseline {sum(a_reference.values())}/{len(a_reference)} "
          "accepted-at-gold ==")
    stats = {arm: {"changed": 0, "regressions": [], "new_fixes": []}
             for arm in ("B", "C", "D4")}
    control_detail = []
    for row in control_rows:
        if row.get("corridor_id") is None or row.get("skipped"):
            continue
        n = row["n"]
        gold = set(row["gold"])
        states = arm_states(row)
        a_state = states["A"]
        a_members = (set(range(a_state["target"][0], a_state["target"][1]))
                     if accepted_members(a_state) else None)
        detail = {"n": n, "A": a_state["target"]}
        for arm in ("B", "C", "D4"):
            members = accepted_members(states[arm])
            detail[arm] = states[arm]["target"]
            if members != a_members:
                stats[arm]["changed"] += 1
            if a_reference.get(n) and members != gold:
                stats[arm]["regressions"].append(n)
            if not a_reference.get(n) and members == gold:
                stats[arm]["new_fixes"].append(n)
        control_detail.append(detail)
    for arm in ("B", "C", "D4"):
        s = stats[arm]
        print(f"{arm}: changed={s['changed']} regressions={s['regressions']} "
              f"new-fixes={len(s['new_fixes'])}{s['new_fixes'][:12]}")
    n74_states = next((arm_states(r) for r in control_rows if r["n"] == 74), None)
    if n74_states:
        n74_line = {arm: {"target": n74_states[arm]["target"],
                          "members_equal_gold": accepted_members(n74_states[arm])
                          == {1484, 1485, 1486}}
                    for arm in ("A", "B", "C", "D4")}
        print("n74:", json.dumps(n74_line, ensure_ascii=False))

    # ---- violations ----
    print("\n== violations: new accepted links touching apparatus/noise segments ==")
    violation_counts = {arm: 0 for arm in ("B", "C", "D4")}
    violation_samples = {arm: [] for arm in ("B", "C", "D4")}
    for key, corridor_doc in corridors_doc["corridors"].items():
        a_accepted = {(a, b, c, d) for a, b, c, d, conf
                      in corridor_doc["arms"]["A_recheck"]["path"]
                      if conf >= 0.83 and a < b and c < d}
        pair_key = key.rsplit("#", 1)[0]
        for arm in ("B", "C", "D4"):
            for a, b, c, d, conf in corridor_doc["arms"][arm]["path"]:
                if conf < 0.83 or a >= b or c >= d:
                    continue
                if (a, b, c, d) in a_accepted:
                    continue
                if flag_target(pair_key, c) or flag_target(pair_key, d - 1):
                    violation_counts[arm] += 1
                    if len(violation_samples[arm]) < 5:
                        violation_samples[arm].append(
                            {"corridor": key, "link": [a, b, c, d],
                             "confidence": conf})
    for arm in ("B", "C", "D4"):
        print(f"{arm}: {violation_counts[arm]} {violation_samples[arm]}")

    checked = sum(1 for doc in corridors_doc["corridors"].values()
                  if doc["a_selfcheck_exact"])
    total = len(corridors_doc["corridors"])
    print(f"\n== A'==A self-check: {checked}/{total} corridors exact ==")
    con.close()

    out = {
        "fixtures": ledger,
        "fixtures_strict_fixed": strict_fixed,
        "fixtures_inside_gold": aux_fixed,
        "controls_baseline_correct": sum(a_reference.values()),
        "controls_total": len(a_reference),
        "arm_stats": stats,
        "controls_detail": control_detail,
        "n74": ({arm: {"target": n74_states[arm]["target"],
                       "members_equal_gold": accepted_members(n74_states[arm])
                       == {1484, 1485, 1486}}
                 for arm in ("A", "B", "C", "D4")}) if n74_states else None,
        "violations": {"counts": violation_counts, "samples": violation_samples},
        "selfcheck": {"exact": checked, "total": total},
        "embedder": corridors_doc.get("embedder"),
        "wall_seconds": corridors_doc.get("wall_seconds"),
        "peak_rss_mib": corridors_doc.get("peak_rss_mib"),
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

"""Round-2 A/B/C(+D4) corridor ablation for issue #18.

Question: do the residual mis-alignments come from the search path or from the
group representation / scoring?  Four arms over the same anchor-bounded body
corridors, same cost shape, same threshold:

  A  production raw DP (frozen in the run)              — baseline
  B  extended path: production transitions + 1:4 / 4:1  — path change, agg repr
  D4 production transitions, concat-re-embedding repr   — repr change, prod path
  C  extended path + concat repr                        — both

The concat representation follows upstream Bertalign (commit ``df8c63f``): a
group (s0, s1) is scored by embedding the *joined text* of its segments with
the same E5 model and "query: " prefix the production pipeline uses, instead
of the production aggregation (prefix-sum difference of per-segment vectors).
Overlap vectors are precomputed for spans of length 1..4 ending at each
segment — exactly the shapes the transition tables can address — and only
inside the selected corridors (incremental, no whole-library re-embedding).

Gold answers are used only for evaluation; they never enter any path, cost or
candidate.  Everything is body-local and read-only.
"""
from __future__ import annotations

import argparse
import json
import math
import resource
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.d_two_stage_baseline import MODEL_ID, PairView, _texts  # noqa: E402
from src.me_finder.semantic_alignment import (  # noqa: E402
    _TRANSITIONS as PRODUCTION_TRANSITIONS,
    cached_text_sequence_vectors,
)

EXTENDED_TRANSITIONS = PRODUCTION_TRANSITIONS + ((1, 4, 0.55), (4, 1, 0.55))
LOW_THRESHOLD = 0.83
SEARCH_BAND = 96
MAX_SPAN = 4


def make_agg_sim(prefix_s: np.ndarray, prefix_t: np.ndarray,
                 off_s: int, off_t: int):
    """Production aggregation similarity over body-local prefix sums.

    ``prefix_*`` are cumsums starting at the body's first row; ``off_*`` convert
    the absolute indices the DP works with back to body-local rows.
    """
    def sim(s0: int, s1: int, t0: int, t1: int) -> float:
        if s0 == s1 or t0 == t1:
            return 0.0
        vs = prefix_s[s1 - off_s] - prefix_s[s0 - off_s]
        vt = prefix_t[t1 - off_t] - prefix_t[t0 - off_t]
        vs = vs / max(float(np.linalg.norm(vs)), 1e-12)
        vt = vt / max(float(np.linalg.norm(vt)), 1e-12)
        return float(np.clip(float(vs @ vt), -1.0, 1.0))
    return sim


def make_concat_sim(spans_s: list[np.ndarray], spans_t: list[np.ndarray],
                    s0: int, t0: int):
    """spans[k][i_local] = unit vector of the joined text ending at that row."""
    def sim(a0: int, a1: int, b0: int, b1: int) -> float:
        ds, dt = a1 - a0, b1 - b0
        if ds == 0 or dt == 0:
            return 0.0
        if ds > len(spans_s) or dt > len(spans_t):
            raise ValueError(f"span {ds}x{dt} exceeds concat overlap table")
        value = float(spans_s[ds - 1][a1 - 1 - s0] @ spans_t[dt - 1][b1 - 1 - t0])
        return float(np.clip(value, -1.0, 1.0))
    return sim


def corridor_dp(
    sim, source_lengths: list[int], target_lengths: list[int],
    s0: int, s1: int, t0: int, t1: int,
    transitions,
) -> dict:
    """Banded min-cost DP over one corridor; cost shape = production's.

    matched (di>0, dj>0): penalty + (1 - sim) * 3 + 0.18 * |log(tlen/(ratio*slen))|
    gap     (di==0|dj==0): penalty + log1p(slen + tlen) / 12
    with the corridor's own target/source length ratio and production's band
    rule.  Returns absolute links with per-link confidence.
    """
    sc, tc = s1 - s0, t1 - t0
    if sc <= 0 or tc <= 0:
        return {"cost": 0.0, "path": []}
    ratio = sum(target_lengths[t0:t1]) / max(sum(source_lengths[s0:s1]), 1)
    band = max(SEARCH_BAND, math.ceil(tc / max(sc, 1)) + 3)
    slen_prefix = np.concatenate(([0], np.cumsum(source_lengths[s0:s1], dtype=np.int64)))
    tlen_prefix = np.concatenate(([0], np.cumsum(target_lengths[t0:t1], dtype=np.int64)))

    def link_cost(i0: int, i1: int, j0: int, j1: int) -> tuple[float, float]:
        di, dj = i1 - i0, j1 - j0
        penalty = next(p for d, e, p in transitions if d == di and e == dj)
        slen = int(slen_prefix[i1 - s0] - slen_prefix[i0 - s0])
        tlen = int(tlen_prefix[j1 - t0] - tlen_prefix[j0 - t0])
        if di == 0 or dj == 0:
            return penalty + math.log1p(slen + tlen) / 12.0, 0.0
        value = sim(i0, i1, j0, j1)
        expected = max(ratio * slen, 1.0)
        length_cost = 0.18 * abs(math.log(max(tlen, 1) / expected))
        return penalty + (1.0 - value) * 3.0 + length_cost, value

    def bounds(i: int) -> tuple[int, int]:
        expected = round(i * tc / max(sc, 1))
        return max(0, expected - band), min(tc, expected + band)

    cost: list[dict[int, float]] = [dict() for _ in range(sc + 1)]
    back: list[dict[int, tuple[int, int]]] = [dict() for _ in range(sc + 1)]
    cost[0][0] = 0.0
    for i in range(sc + 1):
        lo, hi = bounds(i)
        for j in range(lo, hi + 1):
            if i == 0 and j == 0:
                continue
            best, bdi, bdj = math.inf, 0, 0
            for di, dj, _penalty in transitions:
                pi, pj = i - di, j - dj
                if pi < 0 or pj < 0:
                    continue
                previous = cost[pi].get(pj)
                if previous is None or previous == math.inf:
                    continue
                candidate = previous + link_cost(s0 + i - di, s0 + i,
                                                 t0 + j - dj, t0 + j)[0]
                if candidate < best:
                    best, bdi, bdj = candidate, di, dj
            if best < math.inf:
                cost[i][j] = best
                back[i][j] = (bdi, bdj)
    total = cost[sc].get(tc)
    if total is None:
        raise RuntimeError("corridor path unreachable within band")
    path = []
    i, j = sc, tc
    while i > 0 or j > 0:
        di, dj = back[i][j]
        value = sim(s0 + i - di, s0 + i, t0 + j - dj, t0 + j) if (di and dj) else 0.0
        path.append((s0 + i - di, s0 + i, t0 + j - dj, t0 + j, round(value, 4)))
        i, j = i - di, j - dj
    path.reverse()
    return {"cost": round(total, 4), "path": path}


def build_corridors(view: PairView) -> list[dict]:
    """Open corridors between consecutive validated anchor knots (absolute)."""
    raw = view.raw_reproduction()
    knots = []
    for source, target, key in raw["anchors_absolute"]:
        knots.append((source, target, not key.startswith("folio:")))
    knots.sort()
    (bs0, bs1), (bt0, bt1) = view.body["pivot"], view.body["target"]
    chain = [(bs0, bt0, False)] + knots + [(bs1, bt1, False)]
    corridors = []
    for (as_, at_, consume_a), (bs_, bt_, _c) in zip(chain, chain[1:]):
        s0 = as_ + (1 if consume_a else 0)
        t0 = at_ + (1 if consume_a else 0)
        if s0 < bs_ or t0 < bt_:
            corridors.append({"s0": s0, "s1": bs_, "t0": t0, "t1": bt_})
    return corridors


def corridor_of(corridors: list[dict], pivot: int) -> dict | None:
    for corridor in corridors:
        if corridor["s0"] <= pivot < corridor["s1"]:
            return corridor
    return None


class ConcatEmbedder:
    """E5 overlap embeddings for spans ending at each segment, computed only
    for requested corridors."""

    def __init__(self, cache_dir: Path):
        import onnxruntime
        from fastembed import TextEmbedding
        onnxruntime.disable_telemetry_events()
        from src.me_finder.embedding_models import embedding_model_config
        config = embedding_model_config(MODEL_ID)
        self._model = TextEmbedding(
            model_name=config.hf_name, cache_dir=str(cache_dir),
            threads=4, local_files_only=True,
        )
        self._prefix = "query: " if config.prefix_mode == "query" else ""
        wrapper = getattr(self._model, "model", None)
        self.tokenizer = getattr(wrapper, "tokenizer", None)
        self.embedded_texts = 0
        self.embed_seconds = 0.0
        self.token_lengths: list[int] = []

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        started = time.monotonic()
        for text in texts:
            if self.tokenizer is not None:
                self.token_lengths.append(len(self.tokenizer.encode(self._prefix + text)))
        vectors = np.stack(
            [np.asarray(v, dtype=np.float32)
             for v in self._model.embed([self._prefix + t for t in texts],
                                        batch_size=4)],
            axis=0,
        )
        self.embed_seconds += time.monotonic() - started
        self.embedded_texts += len(texts)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-12)

    def overlap_table(self, texts: list[str], s0: int, s1: int) -> list[np.ndarray]:
        """tables[k][i-s0] = unit vector of texts[max(s0,i-k+1)..i], k=1..MAX_SPAN."""
        span = s1 - s0
        if span <= 0:
            return [np.zeros((0, 1), dtype=np.float32) for _ in range(MAX_SPAN)]
        pending: list[tuple[int, int, str]] = []
        for k in range(1, MAX_SPAN + 1):
            for i in range(s0, s1):
                lo = max(s0, i - k + 1)
                pending.append((k, i - s0, "".join(texts[lo:i + 1])))
        unique = list(dict.fromkeys(text for _k, _i, text in pending))
        vectors = self.embed(unique)
        cache = dict(zip(unique, vectors))
        dim = vectors.shape[1]
        tables = [np.zeros((span, dim), dtype=np.float32) for _ in range(MAX_SPAN)]
        for k, local, text in pending:
            tables[k - 1][local] = cache[text]
        return tables


def arm_state(path: list[tuple], pivot: int) -> dict:
    link = next(((s0, s1, t0, t1, conf) for s0, s1, t0, t1, conf in path
                 if s0 <= pivot < s1), None)
    if link is None:
        return {"matched": False, "target": None, "confidence": None,
                "link": None}
    matched = link[0] < link[1] and link[2] < link[3]
    return {"matched": matched,
            "target": [link[2], link[3]] if matched else None,
            "confidence": link[4] if matched else None,
            "link": [link[0], link[1], link[2], link[3]]}


def accepted_members(state: dict) -> set[int] | None:
    if not state["matched"] or state["confidence"] is None:
        return None
    if state["confidence"] < LOW_THRESHOLD:
        return None
    t0, t1 = state["target"]
    return set(range(t0, t1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--fixtures", default="reports/d-fixture-reverify-2026-09-07.json")
    parser.add_argument("--golds", default="reports/d-gap-penalty-experiment-2026-09-07.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-corridor-segments", type=int, default=8000)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    db = Path(args.db).resolve()
    cache = Path(args.cache).resolve()

    fixtures = json.loads(Path(args.fixtures).read_text(encoding="utf-8"))["fixtures"]
    golds = json.loads(Path(args.golds).read_text(encoding="utf-8"))["golds"]

    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    texts_cache: dict[str, list[str]] = {}

    def texts_of(set_id: str) -> list[str]:
        if set_id not in texts_cache:
            texts_cache[set_id] = _texts(con, set_id)
        return texts_cache[set_id]

    wanted: dict[str, dict[int, dict]] = {}
    for fx in fixtures:
        pivot = fx["pivot_v13_order"] if "pivot_v13_order" in fx else fx["pivot_v13"]
        wanted.setdefault(fx["pair"], {})[pivot] = {
            "kind": "fixture", "n": fx["n"], "gold": fx["correct_target_v13"],
        }
    for gold in golds:
        wanted.setdefault(gold["pair"], {})[gold["pivot_v13"]] = {
            "kind": "control", "n": gold["n"], "gold": gold["gold_target_v13"],
            "label": gold["label"],
        }

    pair_views: dict[str, PairView] = {}
    pair_corridors: dict[str, list[dict]] = {}
    pair_texts: dict[str, tuple[list[str], list[str]]] = {}
    selected: dict[str, dict[int, int]] = {}
    for pair_key, pivots in sorted(wanted.items()):
        pivot_id, target_id = pair_key.split(" -> ")
        view = PairView(con, cache, (pivot_id, target_id))
        pair_views[pair_key] = view
        corridors = build_corridors(view)
        pair_corridors[pair_key] = corridors
        pair_texts[pair_key] = (texts_of(view.pivot_set), texts_of(view.target_set))
        chosen: dict[int, int] = {}
        for pivot in pivots:
            corridor = corridor_of(corridors, pivot)
            if corridor is None:
                print(f"  pivot {pivot} ({pair_key[:36]}) outside all corridors")
                continue
            chosen[pivot] = corridors.index(corridor)
        selected[pair_key] = chosen
        print(f"{pair_key[:52]}: {len(corridors)} corridors, "
              f"selected {sorted(set(chosen.values()))}", flush=True)

    results: dict = {"corridors": {}, "samples": {}, "embedder": None}
    embedder: ConcatEmbedder | None = None
    started_all = time.monotonic()

    for pair_key, chosen in sorted(selected.items()):
        view = pair_views[pair_key]
        corridors = pair_corridors[pair_key]
        source_texts, target_texts = pair_texts[pair_key]
        raw = view.raw_reproduction()
        ps, pe = view.body["pivot"]
        ts_, te_ = view.body["target"]
        sv = np.asarray(cached_text_sequence_vectors(
            source_texts, cache, model_id=MODEL_ID), dtype=np.float32)
        tv = np.asarray(cached_text_sequence_vectors(
            target_texts, cache, model_id=MODEL_ID), dtype=np.float32)
        prefix_s = np.vstack([np.zeros((1, sv.shape[1]), dtype=np.float32),
                              np.cumsum(sv[ps:pe], axis=0)])
        prefix_t = np.vstack([np.zeros((1, tv.shape[1]), dtype=np.float32),
                              np.cumsum(tv[ts_:te_], axis=0)])
        sl_all = view._lengths(view.pivot_set)
        tl_all = view._lengths(view.target_set)

        for cid in sorted(set(chosen.values())):
            corridor = corridors[cid]
            s0, s1, t0, t1 = corridor["s0"], corridor["s1"], corridor["t0"], corridor["t1"]
            if ((s1 - s0) > args.max_corridor_segments
                    or (t1 - t0) > args.max_corridor_segments):
                print(f"  skip {pair_key[:40]}#{cid} ({s1-s0}x{t1-t0}) — over size cap",
                      flush=True)
                continue
            key = f"{pair_key}#{cid}"
            arms: dict[str, dict] = {}
            arms["A"] = {"path": [
                (l[0], l[1], l[2], l[3], 0.0) for l in raw["links"]
                if l[0] is not None and l[2] is not None
                and s0 <= l[0] and l[1] <= s1 and t0 <= l[2] and l[3] <= t1
            ]}

            agg = make_agg_sim(prefix_s, prefix_t, ps, ts_)
            wall = time.monotonic()
            arms["B"] = corridor_dp(agg, sl_all, tl_all, s0, s1, t0, t1,
                                    EXTENDED_TRANSITIONS)
            b_seconds = time.monotonic() - wall

            if embedder is None:
                embedder = ConcatEmbedder(cache)
            embed_wall = time.monotonic()
            spans_s = embedder.overlap_table(source_texts, s0, s1)
            spans_t = embedder.overlap_table(target_texts, t0, t1)
            embed_seconds = time.monotonic() - embed_wall
            concat = make_concat_sim(spans_s, spans_t, s0, t0)
            wall = time.monotonic()
            arms["D4"] = corridor_dp(concat, sl_all, tl_all, s0, s1, t0, t1,
                                     PRODUCTION_TRANSITIONS)
            d4_seconds = time.monotonic() - wall
            arms["C"] = corridor_dp(concat, sl_all, tl_all, s0, s1, t0, t1,
                                    EXTENDED_TRANSITIONS)
            c_seconds = time.monotonic() - wall

            # A self-check: production transitions + agg sim must equal the raw path
            a_rebuilt = corridor_dp(agg, sl_all, tl_all, s0, s1, t0, t1,
                                    PRODUCTION_TRANSITIONS)
            arms["A_recheck"] = {"path": a_rebuilt["path"], "cost": a_rebuilt["cost"]}
            a_frozen = {(x[0], x[1], x[2], x[3]) for x in arms["A"]["path"]}
            a_rebuilt_set = {(x[0], x[1], x[2], x[3]) for x in a_rebuilt["path"]}

            results["corridors"][key] = {
                "corridor": [s0, s1, t0, t1],
                "seconds": {"B": round(b_seconds, 2),
                            "D4": round(d4_seconds, 2),
                            "C": round(c_seconds, 2),
                            "embed": round(embed_seconds, 2)},
                "a_selfcheck_exact": a_frozen == a_rebuilt_set,
                "arms": arms,
            }
            print(f"  {key[:58]}: A={len(arms['A']['path'])} "
                  f"A'={len(a_rebuilt['path'])} eq={a_frozen == a_rebuilt_set} "
                  f"B={len(arms['B']['path'])} D4={len(arms['D4']['path'])} "
                  f"C={len(arms['C']['path'])}", flush=True)

    if embedder is not None:
        results["embedder"] = {
            "embedded_texts": embedder.embedded_texts,
            "embed_seconds": round(embedder.embed_seconds, 1),
            "token_truncated_over_512": sum(1 for t in embedder.token_lengths if t > 512),
            "token_counted": len(embedder.token_lengths),
            "token_max": max(embedder.token_lengths) if embedder.token_lengths else None,
        }

    # ---- sample judgement (gold used ONLY here) ----
    for pair_key, chosen in selected.items():
        view = pair_views[pair_key]
        corridors = pair_corridors[pair_key]
        for pivot, meta in wanted[pair_key].items():
            row = {"n": meta["n"], "kind": meta["kind"], "pair": pair_key,
                   "pivot": pivot, "gold": meta["gold"], "label": meta.get("label")}
            corridor = corridor_of(corridors, pivot)
            if corridor is None:
                row["corridor"] = None
                results["samples"][str(meta["n"])] = row
                continue
            cid = corridors.index(corridor)
            key = f"{pair_key}#{cid}"
            row["corridor_id"] = cid
            if key not in results["corridors"]:
                row["skipped"] = "over size cap"
                results["samples"][str(meta["n"])] = row
                continue
            arms_data = results["corridors"][key]["arms"]
            gold_set = set(meta["gold"])
            per_arm = {}
            for arm in ("A", "B", "C", "D4"):
                state = arm_state(arms_data[arm]["path"], pivot)
                members = accepted_members(state)
                if arm == "A":
                    frozen = view.link_at(pivot)
                    status = frozen[4] if frozen else "unmatched"
                    per_arm[arm] = {
                        **state,
                        "frozen_status": status,
                        "accepted_members": sorted(members) if members else None,
                        "correct_accepted": bool(
                            members == gold_set
                            and status in ("automatic", "note_automatic")),
                    }
                else:
                    per_arm[arm] = {
                        **state,
                        "accepted_members": sorted(members) if members else None,
                        "correct_accepted": bool(members == gold_set),
                    }
            row["arms"] = per_arm
            results["samples"][str(meta["n"])] = row

    results["wall_seconds"] = round(time.monotonic() - started_all, 1)
    results["peak_rss_mib"] = round(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    con.close()
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

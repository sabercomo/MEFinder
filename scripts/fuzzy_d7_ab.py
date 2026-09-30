"""模糊搜索 D7 的原型与对照工具(RapidFuzz 窗口评分 + 5～8 字合并两字扫描)。

D7 = search_scoring.best_window_ratio 的 RapidFuzz 窗口评分,加上
search_contract.fuzzy_needs_bigram_scan 的 5～8 字合并两字扫描。本模块保留:

- legacy_best_window_ratio:D7 之前的评分函数(原样),供对照与回归测试;
- prototype_best_window_ratio:验收用的原型评分(rf7),须与正式实现逐一相等;
- use_variant:在当前代码上切换 legacy(旧评分 + 旧退回规则)与 d7;
- apply_prototype_to_legacy_tree:在 D7 之前的代码树上套用原型,
  供“原型 vs 正式实现”逐条完整响应对照(dump 子命令);
- validate / analyze:按查询交替跑两种变体并按查询、分模式汇总。

明细含原书句子,只写到本机 --out,不入库。证据与用法:
reports/fuzzy-search-benchmark-2026-09-30.md 第十节。

用法::

    PYTHONPATH=. python -m scripts.fuzzy_d7_ab validate --db <副本> --seed 20261002 --out <明细.json>
    PYTHONPATH=. python -m scripts.fuzzy_d7_ab analyze <明细.json>
    python scripts/fuzzy_d7_ab.py dump --root <代码树> [--prototype] --db <副本> --cases <明细.json> --out <哈希.json>
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sqlite3
import statistics
import sys
from contextlib import closing
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from rapidfuzz import process
from rapidfuzz.distance import Indel, Levenshtein

VARIANTS = ("legacy", "d7")
PROTOTYPE_UNION_LENGTHS = range(5, 9)


def legacy_best_window_ratio(query_plain: str, plain: str) -> Tuple[float, int, int]:
    """D7 之前的评分函数,原样保留(仅改名)。"""

    if not query_plain or not plain:
        return 0.0, 0, 0
    if query_plain in plain:
        start = plain.find(query_plain)
        return 0.91, start, start + len(query_plain) - 1
    q_len = len(query_plain)
    if len(plain) <= q_len + 8:
        return difflib.SequenceMatcher(None, query_plain, plain).ratio(), 0, max(0, len(plain) - 1)
    window_sizes = sorted(set([q_len, int(q_len * 1.25) + 1, int(q_len * 1.6) + 1, q_len + 8]))
    step = max(1, q_len // 3)
    # A fine window scan across a whole book page (footnote-dense, 2000+ chars)
    # is O(len) SequenceMatcher calls and dominates fuzzy search time.  For long
    # paragraphs, first locate the promising region with a coarse stride, then
    # refine only around it.  Short paragraphs keep the exhaustive scan so their
    # behaviour (and the regression fixtures) is unchanged.
    if len(plain) > 600:
        primary = window_sizes[0]
        coarse_step = max(step, q_len)
        anchor = 0
        anchor_ratio = -1.0
        for start in range(0, max(1, len(plain) - primary + 1), coarse_step):
            ratio = difflib.SequenceMatcher(
                None, query_plain, plain[start : start + primary]
            ).ratio()
            if ratio > anchor_ratio:
                anchor_ratio = ratio
                anchor = start
        region_lo = max(0, anchor - q_len)
        region_hi = min(len(plain), anchor + primary + q_len)
    else:
        region_lo = 0
        region_hi = len(plain)
    best = (0.0, 0, min(len(plain) - 1, q_len))
    for size in window_sizes:
        if size <= 0:
            continue
        for start in range(region_lo, max(region_lo + 1, region_hi - size + 1), step):
            window = plain[start : start + size]
            ratio = difflib.SequenceMatcher(None, query_plain, window).ratio()
            if ratio > best[0]:
                best = (ratio, start, start + len(window) - 1)
        tail_start = max(0, len(plain) - size)
        window = plain[tail_start:]
        ratio = difflib.SequenceMatcher(None, query_plain, window).ratio()
        if ratio > best[0]:
            best = (ratio, tail_start, len(plain) - 1)
    return best


def prototype_best_window_ratio(query_plain: str, plain: str, anchors: int = 3) -> Tuple[float, int, int]:
    """验收用原型(rf7),与 search_scoring.best_window_ratio 必须逐一相等。"""

    if not query_plain or not plain:
        return (0.0, 0, 0)
    if query_plain in plain:
        start = plain.find(query_plain)
        return (0.91, start, start + len(query_plain) - 1)
    q_len = len(query_plain)
    delta = min(3, q_len // 5)
    sizes = [size for size in range(q_len - delta, q_len + delta + 1) if 1 <= size <= len(plain)]
    if not sizes:
        return (difflib.SequenceMatcher(None, query_plain, plain).ratio(), 0, len(plain) - 1)
    width = min(q_len, len(plain))
    windows = [plain[i:i + width] for i in range(len(plain) - width + 1)]
    top = process.extract(query_plain, windows, scorer=Indel.normalized_similarity, limit=anchors)
    shift = max(1, delta)
    best_key, best = None, (0.0, 0, 0)
    for _choice, _score, anchor in top:
        for size in sizes:
            for start in range(max(0, anchor - shift), min(len(plain) - size, anchor + shift) + 1):
                window = plain[start:start + size]
                key = (-Levenshtein.distance(query_plain, window), -abs(size - q_len),
                       Indel.normalized_similarity(query_plain, window), -start)
                if best_key is None or key > best_key:
                    best_key = key
                    best = (Levenshtein.normalized_similarity(query_plain, window), start, start + size - 1)
    return best


def legacy_needs_bigram_scan(query_length: int, fts_found: bool) -> bool:
    """D7 之前的退回规则:4～8 字且 FTS 没给出模糊候选时才走两字扫描。"""

    return 4 <= query_length <= 8 and not fts_found


_ORIGINALS: Dict[str, object] = {}


def use_variant(name: str, bench=None) -> None:
    """在当前代码上切换评分与退回规则;bench 为基准模块时同步它的诊断评分。"""

    from src.me_finder import search_recall

    if not _ORIGINALS:
        _ORIGINALS.update(scorer=search_recall.best_window_ratio,
                          rule=search_recall.fuzzy_needs_bigram_scan)
    legacy = name == "legacy"
    scorer = legacy_best_window_ratio if legacy else _ORIGINALS["scorer"]
    search_recall.best_window_ratio = scorer
    search_recall.fuzzy_needs_bigram_scan = legacy_needs_bigram_scan if legacy else _ORIGINALS["rule"]
    if bench is not None:
        bench.best_window_ratio = scorer


def apply_prototype_to_legacy_tree() -> None:
    """在 D7 之前的代码树(如 aaa19a4)上套用验收原型:评分 + 5～8 字合并两字扫描。"""

    from src.me_finder import search_recall
    from src.me_finder.search_recall import CandidateRecall

    original_pass = CandidateRecall._sql_fuzzy_pass

    def union_pass(self, q_plain, candidates, *args):
        truncated = original_pass(self, q_plain, candidates, *args)
        if len(q_plain) in PROTOTYPE_UNION_LENGTHS:
            self.fts_match_expression = lambda *a, **k: None  # force the bigram branch
            try:
                truncated = original_pass(self, q_plain, candidates, *args) or truncated
            finally:
                del self.fts_match_expression
        return truncated

    search_recall.best_window_ratio = prototype_best_window_ratio
    CandidateRecall._sql_fuzzy_pass = union_pass


def validate(db: Path, seed: int, out: Path, per_category: int = 20, repeats: int = 2) -> dict:
    """按查询轮流跑 legacy 与 d7(交替计时),写明细并返回。"""

    from scripts import fuzzy_search_benchmark as bench
    from src.me_finder.search import SearchEngine

    with closing(sqlite3.connect(db)) as connection:
        cases = bench.build_cases(connection, per_category=per_category, seed=seed)
    engine = SearchEngine(db)
    try:
        bench.execute_with_script_folding(engine, bench.SearchRequest(query="社会", mode="fuzzy"), enabled=True)
        runs: Dict[str, List[dict]] = {name: [] for name in VARIANTS}
        for index, case in enumerate(cases):
            for mode in bench.MODES:
                for name in (VARIANTS if index % 2 == 0 else VARIANTS[::-1]):
                    use_variant(name, bench)
                    runs[name].append({"case_id": case["id"],
                                       **bench.run_case(engine, case, mode, limit=10, repeats=repeats)})
    finally:
        use_variant("d7", bench)
        engine.close()
    report = {"cases": cases, "seed": seed, "db": str(db)}
    for name in VARIANTS:
        report[name] = {"summary": bench.summarize(cases, runs[name]), "runs": runs[name]}
    out.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report


def analyze(report: dict, before: str = "legacy", after: str = "d7") -> str:
    """按查询、分模式汇总(两种模式分开,从不相加)。"""

    cases = {case["id"]: case for case in report["cases"]}
    categories = list(dict.fromkeys(case["category"] for case in report["cases"]))
    lines = [f"seed {report['seed']} {Path(report['db']).name} queries {len(cases)}"]
    for mode in ("auto", "fuzzy"):
        old = {run["case_id"]: run for run in report[before]["runs"] if run["mode"] == mode}
        new = {run["case_id"]: run for run in report[after]["runs"] if run["mode"] == mode}
        lines += ["", f"## {mode}(每个查询计一次)",
                  "| 类别 | 找到/首位 | 严格高亮 | 锚点页正确 | 引用页码正确 | 平均非目标 | 中位 ms |",
                  "|---|---|---|---|---|---|---|"]
        for category in categories:
            ids = [key for key, case in cases.items() if case["category"] == category]
            cells = [f"{a} → {b}" for a, b in zip(_cells([old[k] for k in ids]), _cells([new[k] for k in ids]))]
            lines.append(f"| {category} | " + " | ".join(cells) + " |")
        lines.append("变化: " + json.dumps(_changes(cases, old, new), ensure_ascii=False))
    return "\n".join(lines)


def _cells(runs: Sequence[dict]) -> Tuple[str, ...]:
    def ratio(key: str) -> str:
        return f"{sum(bool(run.get(key)) for run in runs)}/{sum(run.get(key) is not None for run in runs)}"

    return (
        f"{sum(run['found_rank'] is not None for run in runs)}/{sum(run['found_rank'] == 1 for run in runs)}",
        f"{sum(run['highlight_ratio'] == 1.0 for run in runs)}/"
        f"{sum(run['highlight_ratio'] is not None for run in runs)}",
        ratio("anchor_page_ok"), ratio("citation_page_ok"),
        f"{statistics.mean(run['noise'] for run in runs):.2f}",
        f"{statistics.median(run['latency_ms'] for run in runs):.0f}",
    )


def _changes(cases: dict, old: dict, new: dict) -> dict:
    changes: Dict[str, list] = {key: [] for key in (
        "gained", "lost", "better", "worse", "highlight_worse",
        "anchor_fixed", "anchor_broken", "anchor_new", "citation_fixed", "citation_broken", "citation_new")}
    for key in cases:
        a, b = old[key], new[key]
        ra, rb = a["found_rank"], b["found_rank"]
        if ra is None and rb is not None:
            changes["gained"].append(key)
        if ra is not None and rb is None:
            changes["lost"].append(key)
        if ra and rb and rb < ra:
            changes["better"].append(key)
        if ra and rb and rb > ra:
            changes["worse"].append([key, ra, rb])
        if a["highlight_ratio"] == 1.0 and b["highlight_ratio"] is not None and b["highlight_ratio"] < 1.0:
            changes["highlight_worse"].append(key)
        for field, label in (("anchor_page_ok", "anchor"), ("citation_page_ok", "citation")):
            if a.get(field) is False and b.get(field) is True:
                changes[f"{label}_fixed"].append(key)
            if a.get(field) is True and b.get(field) is False:
                changes[f"{label}_broken"].append(key)
            if a.get(field) is None and b.get(field) is not None:
                changes[f"{label}_new"].append(key)
    return {key: value if key in ("lost", "worse", "highlight_worse", "anchor_broken", "citation_broken")
            else len(value) for key, value in changes.items()}


COMMON_QUERIES = ("社会", "历史", "主义", "国家", "马克思", "资本主义", "社", "社會", "社会学", "gender", "the")
PASSAGE_QUERIES = ("社会", "历史", "主义", "国家", "社", "社會", "马克", "ab", "社会学", "马克思主义")


def dump(root: Path, db: Path, cases_path: Path, out: Path, prototype: bool) -> int:
    """对 root 代码树逐请求输出完整响应 SHA-256;prototype 时先套用原型。"""

    sys.path.insert(0, str(root))
    import src.me_finder.search as search_module
    from src.me_finder.application.script_search import execute_with_script_folding
    from src.me_finder.application.search_service import SearchRequest

    if not Path(search_module.__file__).resolve().is_relative_to(root.resolve()):
        raise SystemExit(f"imported {search_module.__file__}, not from {root}")
    if prototype:
        apply_prototype_to_legacy_tree()
    queries = sorted({case["query"] for case in json.loads(cases_path.read_text(encoding="utf-8"))["cases"]})
    queries += list(COMMON_QUERIES)
    engine = search_module.SearchEngine(db)
    sources = [row[0] for row in engine.db.execute(
        "SELECT source_file_id FROM source_files ORDER BY source_file_id LIMIT 3")]
    digest = lambda response: hashlib.sha256(  # noqa: E731
        json.dumps(response, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    scopes: List[Tuple[str, Optional[str], Optional[tuple]]] = [
        ("all", None, None), ("pdf", None, None), ("epub", None, None),
        ("all", sources[0], None), ("all", None, tuple(sources[:2]))]
    result: Dict[str, str] = {}
    for query in queries:
        for mode in ("fuzzy", "auto"):
            for source_type, source_file_id, source_file_ids in (scopes if len(query) <= 8 else scopes[:2]):
                response = execute_with_script_folding(engine, SearchRequest(
                    query=query, mode=mode, limit=10, source_type=source_type,
                    source_file_id=source_file_id, source_file_ids=source_file_ids), enabled=True)
                result[f"{mode}|{source_type}|{source_file_id}|{source_file_ids}|{query}"] = digest(response)
    for query in PASSAGE_QUERIES:
        for source_type, source_file_id, source_file_ids in scopes:
            for limit in (10, 50):
                response = engine.search_passages(query, limit=limit, source_type=source_type,
                                                  source_file_id=source_file_id, source_file_ids=source_file_ids)
                result[f"passages|{source_type}|{source_file_id}|{source_file_ids}|{limit}|{query}"] = digest(response)
    engine.close()
    out.write_text(json.dumps(result, ensure_ascii=False, indent=0), encoding="utf-8")
    return len(result)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("validate")
    run.add_argument("--db", type=Path, required=True)
    run.add_argument("--seed", type=int, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--repeats", type=int, default=2)
    show = sub.add_parser("analyze")
    show.add_argument("report", type=Path)
    hashes = sub.add_parser("dump")
    hashes.add_argument("--root", type=Path, required=True)
    hashes.add_argument("--db", type=Path, required=True)
    hashes.add_argument("--cases", type=Path, required=True)
    hashes.add_argument("--out", type=Path, required=True)
    hashes.add_argument("--prototype", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "validate":
        report = validate(args.db, args.seed, args.out, repeats=args.repeats)
        print(analyze(report))
    elif args.command == "analyze":
        print(analyze(json.loads(args.report.read_text(encoding="utf-8"))))
    else:
        print(dump(args.root, args.db, args.cases, args.out, args.prototype), "responses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

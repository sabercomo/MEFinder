"""模糊搜索召回、噪音与耗时基准(模糊搜索改进第 ② 步)。

从库内真实段落抽取原句,按类别人为制造错字 / 漏字 / 多字 / OCR 形近字,
因此每条查询自带标准答案:原句所在段落与原文字符区间,无需人工标注。
另有一组库里不存在的随机查询,专门量噪音。

每条查询在 ``fuzzy`` 与 ``auto`` 两种模式下走与桌面端相同的繁简联合路径
(``execute_with_script_folding``),记录:

- 失败卡在哪一关:``not_recalled``(没进评分候选)/ ``below_threshold``
  (进了但分数不够)/ ``outside_top``(分数够但不在前 ``limit`` 条)/ ``found``;
- 命中名次、第一条正确命中的高亮文字与原句的相似度、前 ``limit`` 条里的噪音条数;
- 计时:``repeats`` 次中位数(计时运行不插桩,插桩单独再跑一次)。

只读库副本;明细 JSON 含原书句子,写到 ``--out``(勿入库),仓库报告只写汇总。

用法::

    PYTHONPATH=. .venv-macos312-arm64/bin/python -m scripts.fuzzy_search_benchmark \\
        --db <库副本.sqlite3> --out <明细.json> [--per-category 20] [--repeats 3]
    PYTHONPATH=. ... -m scripts.fuzzy_search_benchmark --fixture --out <明细.json>
"""

from __future__ import annotations

import argparse
import difflib
import json
import platform
import random
import re
import sqlite3
import statistics
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from unittest.mock import patch

from src.me_finder.application.script_search import execute_with_script_folding
from src.me_finder.application.search_service import SearchRequest
from src.me_finder.normalization import punctuationless_text
from src.me_finder.persistence import short_gram_index as sgi
from src.me_finder.search import SearchEngine
from src.me_finder.search_recall import CandidateRecall
from src.me_finder.search_scoring import best_window_ratio

FUZZY_THRESHOLD = 0.58  # 与 search_recall._score_fuzzy_window 一致;只用于归因
HIGHLIGHT_EXACT = 0.95  # 高亮文字与原句(去标点)相似度达到即算边界准确
MODES = ("fuzzy", "auto")
CJK = re.compile(r"[一-鿿]")
SUBSTITUTE_POOL = "".join(dict.fromkeys(
    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工"
    "也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小"
    "物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形"
))
OCR_CONFUSIONS = [
    "己已巳", "未末", "日曰", "人入八", "士土", "戊戌戍", "大太犬", "天夭", "千干于",
    "刀力", "王玉主", "木本术", "白自", "且旦", "候侯", "拨拔", "辨辩辫", "析折",
    "暑署", "贷货", "苦若", "免兔", "壁璧", "真直", "历厉", "治冶", "即既", "炙灸",
]
OCR_MAP = {ch: group.replace(ch, "") for group in OCR_CONFUSIONS for ch in group}

# 类别 -> (原句长度区间, 段落最短长度, 扰动)
CATEGORIES: Dict[str, tuple] = {
    "exact_control": ((12, 20), 20, "none"),
    "short3_sub": ((3, 3), 20, "sub1"),
    "short4_sub": ((4, 4), 20, "sub1"),
    "long_sub": ((16, 30), 40, "sub2"),
    "long_omit": ((16, 30), 40, "omit2"),
    "long_insert": ((16, 30), 40, "insert1"),
    "ocr": ((12, 30), 30, "ocr"),
    "long_paragraph": ((16, 30), 1500, "sub2"),
}
NEGATIVE_LENGTHS = (4, 12)
# 2026-09-30 追加:错字恰好打断每个三字片段的中短查询(FTS 召回为空的情形)。
# 用各自独立的随机源,上面的原有样本逐条不变。
EXTRA_CATEGORIES: Dict[str, tuple] = {
    "mid5_sub_center": ((5, 5), 20, "sub_center"),
    "mid68_sub2_spread": ((6, 8), 20, "sub2_spread"),
    # 2026-09-30 追加:超过 64 字的长查询(RapidFuzz partial_ratio 只保证 64 字内最优)。
    "vlong_sub3": ((70, 120), 150, "sub3"),
    # 2026-09-30 追加:短段落(段长不超过原句长 + 8,评分走整段/短段分支)。第 4 项是该上限。
    "short_para_sub1": ((4, 6), 5, "sub1", 8),
    "short_para_sub2": ((8, 14), 9, "sub2", 8),
}
# 2026-09-30 追加:原句真正跨过 PDF 页界(取自跨页合并段落的接缝两侧)。
CROSS_PAGE_CATEGORY = "cross_page_sub2"
EXTRA_NEGATIVE_LENGTHS = (8,)
PURE_KINDS = {"sub_center", "sub2_spread"}  # 按位置打错字,原句须全是汉字


def _window(rng: random.Random, text: str, length: int, *, pure: bool) -> Optional[int]:
    """Start of a CJK-dominant window without line breaks, or None."""

    if len(text) < length:
        return None
    for _ in range(20):
        start = rng.randrange(0, len(text) - length + 1)
        piece = text[start:start + length]
        if any(ch.isspace() for ch in piece):
            continue
        cjk = sum(1 for ch in piece if CJK.match(ch))
        if (cjk == length) if pure else (cjk >= 0.85 * length and CJK.match(piece[0])):
            return start
    return None


def perturb(rng: random.Random, original: str, kind: str) -> Optional[str]:
    """Apply one deterministic corruption; None when this original cannot take it."""

    positions = [i for i, ch in enumerate(original) if CJK.match(ch)]
    chars = list(original)
    if kind == "none":
        return original
    if kind in {"sub1", "sub2", "sub3"}:
        count = {"sub1": 1, "sub2": 2, "sub3": 3}[kind]
        if len(positions) < count:
            return None
        for index in rng.sample(positions, count):
            chars[index] = rng.choice([ch for ch in SUBSTITUTE_POOL if ch != chars[index]])
        return "".join(chars)
    if kind in {"sub_center", "sub2_spread"}:
        # 中间一字 / 第 3 字与倒数第 3 字:长度 ≤8 时每个三字片段都含错字。
        targets = [len(original) // 2] if kind == "sub_center" else [2, len(original) - 3]
        if len(original) < 5 or any(index not in positions for index in targets):
            return None
        for index in dict.fromkeys(targets):
            chars[index] = rng.choice([ch for ch in SUBSTITUTE_POOL if ch != chars[index]])
        return "".join(chars)
    if kind == "omit2":
        inner = [i for i in positions if 0 < i < len(original) - 1]
        if len(inner) < 2:
            return None
        drop = set(rng.sample(inner, 2))
        return "".join(ch for i, ch in enumerate(chars) if i not in drop)
    if kind == "insert1":
        index = rng.randrange(1, len(original))
        return original[:index] + rng.choice(SUBSTITUTE_POOL) + original[index:]
    if kind == "ocr":
        confusable = [i for i in positions if chars[i] in OCR_MAP]
        if not confusable:
            return None
        for index in rng.sample(confusable, min(2, len(confusable))):
            chars[index] = rng.choice(OCR_MAP[chars[index]])
        return "".join(chars)
    raise ValueError(kind)


def _containing(connection: sqlite3.Connection, original: str, query: str) -> tuple:
    """Paragraph ids containing the original, and whether the query itself occurs."""

    rows = connection.execute(
        "SELECT paragraph_id, instr(plain_text, ?) > 0, instr(plain_text, ?) > 0 "
        "FROM paragraphs WHERE eligible_for_search = 1 "
        "AND (instr(plain_text, ?) > 0 OR instr(plain_text, ?) > 0)",
        (original, query, original, query),
    ).fetchall()
    return [row[0] for row in rows if row[1]], any(row[2] for row in rows)


def _labelled_cases(connection: sqlite3.Connection, rng: random.Random, lengths: Dict[int, int],
                    category: str, spec: tuple, per_category: int) -> List[dict]:
    (low, high), min_paragraph, kind, *rest = spec
    slack = rest[0] if rest else None
    pool = sorted(rowid for rowid, size in lengths.items()
                  if (size or 0) >= min_paragraph and (slack is None or (size or 0) <= high + slack))
    rng.shuffle(pool)
    cases: List[dict] = []
    for rowid in pool[: per_category * 40]:
        if len(cases) >= per_category:
            break
        paragraph_id, text = connection.execute(
            "SELECT paragraph_id, text_raw FROM paragraphs WHERE rowid = ?", (rowid,)
        ).fetchone()
        length = rng.randint(low, high)
        if slack is not None and len(text or "") > length + slack:
            continue
        start = _window(rng, text or "", length, pure=length <= 4 or kind in PURE_KINDS)
        if start is None:
            continue
        original = text[start:start + length]
        query = perturb(rng, original, kind)
        if query is None:
            continue
        plain_original, plain_query = punctuationless_text(original), punctuationless_text(query)
        if kind != "none" and plain_query == plain_original:
            continue
        acceptable, query_occurs = _containing(connection, plain_original, plain_query)
        if kind != "none" and query_occurs:
            continue  # 查询本身原样存在,不是模糊场景
        cases.append({
            "id": f"{category}-{len(cases) + 1:03d}", "category": category, "query": query,
            "original": original, "target_paragraph_id": paragraph_id,
            "target_start": start, "target_end": start + length,
            "acceptable_ids": acceptable,
        })
    return cases


def _negative_cases(connection: sqlite3.Connection, rng: random.Random,
                    length: int, per_category: int) -> List[dict]:
    cases: List[dict] = []
    while len(cases) < per_category:
        query = "".join(rng.choice(SUBSTITUTE_POOL) for _ in range(length))
        if _containing(connection, query, query)[1]:
            continue
        cases.append({
            "id": f"negative{length}-{len(cases) + 1:03d}", "category": f"negative{length}",
            "query": query, "original": None, "target_paragraph_id": None,
            "target_start": None, "target_end": None, "acceptable_ids": [],
        })
    return cases


def build_cases(connection: sqlite3.Connection, *, per_category: int, seed: int) -> List[dict]:
    """Sample labelled cases; every original is a verbatim slice of ``text_raw``."""

    rng = random.Random(seed)
    lengths = dict(connection.execute(
        "SELECT rowid, length(text_raw) FROM paragraphs WHERE eligible_for_search = 1"
    ).fetchall())
    cases: List[dict] = []
    for category, spec in CATEGORIES.items():
        cases += _labelled_cases(connection, rng, lengths, category, spec, per_category)
    for length in NEGATIVE_LENGTHS:
        cases += _negative_cases(connection, rng, length, per_category)
    for category, spec in EXTRA_CATEGORIES.items():
        extra = random.Random(f"{seed}:{category}")
        cases += _labelled_cases(connection, extra, lengths, category, spec, per_category)
    for length in EXTRA_NEGATIVE_LENGTHS:
        cases += _negative_cases(connection, random.Random(f"{seed}:negative{length}"), length, per_category)
    cases += _cross_page_cases(connection, random.Random(f"{seed}:{CROSS_PAGE_CATEGORY}"), per_category)
    return cases


def _cross_page_cases(connection: sqlite3.Connection, rng: random.Random, per_category: int) -> List[dict]:
    """Originals straddling the page seam of a cross-page paragraph, two typos each."""

    rowids = [row[0] for row in connection.execute(
        "SELECT rowid FROM paragraphs WHERE eligible_for_search = 1 AND paragraph_id LIKE '%-CROSS-%' "
        "ORDER BY rowid")]
    rng.shuffle(rowids)
    cases: List[dict] = []
    for rowid in rowids[: per_category * 40]:
        if len(cases) >= per_category:
            break
        paragraph_id, text, payload = connection.execute(
            "SELECT paragraph_id, text_raw, payload_json FROM paragraphs WHERE rowid = ?", (rowid,)
        ).fetchone()
        spans = (json.loads(payload or "{}").get("text_source_spans") or [])
        if len(spans) < 2 or not text:
            continue
        seam = int(spans[0]["paragraph_char_end"])
        length = rng.randint(16, 30)
        low, high = max(0, seam - length + 4), min(seam - 4, len(text) - length)
        if low > high:
            continue
        start = rng.randint(low, high)
        original = text[start:start + length]
        visible = [ch for ch in original if not ch.isspace()]
        if not visible or sum(1 for ch in visible if CJK.match(ch)) < 0.85 * len(visible):
            continue
        query = perturb(rng, original, "sub2")
        if query is None:
            continue
        plain_original, plain_query = punctuationless_text(original), punctuationless_text(query)
        if plain_query == plain_original:
            continue
        acceptable, query_occurs = _containing(connection, plain_original, plain_query)
        if query_occurs or paragraph_id not in acceptable:
            continue
        cases.append({
            "id": f"{CROSS_PAGE_CATEGORY}-{len(cases) + 1:03d}", "category": CROSS_PAGE_CATEGORY,
            "query": query, "original": original, "target_paragraph_id": paragraph_id,
            "target_start": start, "target_end": start + length, "acceptable_ids": acceptable,
        })
    return cases


def _expected_pages(engine: SearchEngine, paragraph_id: str, original: str) -> Optional[set]:
    """PDF page ranges the original actually occupies in ``paragraph_id``.

    Every exact occurrence counts (some paragraphs repeat their text); a cross-page
    paragraph maps characters to pages through ``text_source_spans``.  None when
    the document has no PDF pages or the original is not verbatim in the text.
    """

    row = engine.db.execute(
        "SELECT text_raw, pdf_page_start_index, pdf_page_end_index, payload_json "
        "FROM paragraphs WHERE paragraph_id = ?", (paragraph_id,)).fetchone()
    if row is None or row[1] is None:
        return None
    text, first, last, payload = row
    spans = json.loads(payload or "{}").get("text_source_spans") or []
    ranges, start = set(), (text or "").find(original)
    while start >= 0:
        end = start + len(original)
        pages = [int(span["pdf_page_index"]) for span in spans
                 if int(span["paragraph_char_start"]) < end and int(span["paragraph_char_end"]) > start]
        ranges.add((min(pages), max(pages)) if pages else (int(first), int(last)))
        start = text.find(original, start + 1)
    return ranges or None

def _page_checks(engine: SearchEngine, hit: dict, original: str) -> tuple:
    """(高亮锚点页是否正确, 显示/复制的引用页码是否正确);无从判断的为 None。

    预期值只取入库记录,不取返回结果本身:原句所在物理页由段落的
    ``text_source_spans`` 推出,每页的引用页码取 ``pdf_pages`` 表。
    锚点看 ``page_match_spans`` 落在哪些物理页;库里该段本无页面对照时记 None,
    库里有而结果没带锚点记失败。引用页码看 ``citation_page_start/end``
    是否正好是原句所在页(段落跨两页而原句只在一页时,报出两页范围即判错)。
    """

    paragraph_id = str(hit.get("paragraph_id"))
    expected = _expected_pages(engine, paragraph_id, original)
    if expected is None:
        return None, None
    source_file_id, payload = engine.db.execute(
        "SELECT source_file_id, payload_json FROM paragraphs WHERE paragraph_id = ?", (paragraph_id,)
    ).fetchone()
    has_source_anchors = bool(json.loads(payload or "{}").get("text_source_spans"))
    anchors = {int(span["pdf_page_index"]) for span in hit.get("page_match_spans") or []}
    if anchors:
        anchor_ok = any(anchors == set(range(a, b + 1)) for a, b in expected)
    else:
        anchor_ok = False if has_source_anchors else None
    labels = {}
    for a, b in expected:
        for index in (a, b):
            row = engine.db.execute(
                "SELECT payload_json FROM pdf_pages WHERE source_file_id = ? AND pdf_page_index = ?",
                (source_file_id, index)).fetchone()
            page = json.loads(row[0] or "{}") if row else {}
            labels[index] = (page.get("citation_page_start"), page.get("citation_page_end"))
    wanted = [(labels[a][0], labels[b][1]) for a, b in expected]
    cited = (hit.get("citation_page_start"), hit.get("citation_page_end"))
    if any(None in pair for pair in wanted):
        return anchor_ok, None
    return anchor_ok, cited in wanted


def run_case(engine: SearchEngine, case: dict, mode: str, *, limit: int, repeats: int) -> dict:
    """Time ``repeats`` plain runs, then one instrumented run for the diagnosis."""

    request = SearchRequest(query=case["query"], mode=mode, limit=limit)
    timings = []
    for _ in range(repeats):
        started = time.perf_counter()
        response = execute_with_script_folding(engine, request, enabled=True)
        timings.append((time.perf_counter() - started) * 1000)
    scored: set = set()
    original_window = CandidateRecall._score_fuzzy_window

    def spy(self, q_plain, paragraph, plain):
        scored.add(str(paragraph.get("paragraph_id")))
        return original_window(self, q_plain, paragraph, plain)

    with patch.object(CandidateRecall, "_score_fuzzy_window", spy):
        execute_with_script_folding(engine, request, enabled=True)
    acceptable = set(case["acceptable_ids"])
    results = response.get("results", [])
    ids = [str(item.get("paragraph_id")) for item in results]
    found_rank = next((rank for rank, pid in enumerate(ids, 1) if pid in acceptable), None)
    target = case["target_paragraph_id"]
    highlight_ratio = anchor_page_ok = citation_page_ok = None
    if found_rank is not None:
        # 高亮核对看第一条正确命中的高亮文字与原句的相似度,不比位置:
        # 同一句在段内重复(真实库有整段正文重复两遍的段落)时,高亮任一处都算对。
        hit = results[found_rank - 1]
        highlighted = str(hit.get("paragraph_text") or "")[hit["match_start"]:hit["match_end"]]
        highlight_ratio = round(difflib.SequenceMatcher(
            None, punctuationless_text(highlighted), punctuationless_text(case["original"])
        ).ratio(), 4)
        anchor_page_ok, citation_page_ok = _page_checks(engine, hit, case["original"])
    target_ratio = None
    if target is not None:
        (plain,) = engine.db.execute(
            "SELECT plain_text FROM paragraphs WHERE paragraph_id = ?", (target,)
        ).fetchone()
        target_ratio = round(best_window_ratio(punctuationless_text(case["query"]), plain or "")[0], 4)
    if target is None:
        stage = "negative"
    elif found_rank is not None:
        stage = "found"
    elif results and "ngram_fuzzy" not in {item.get("match_type") for item in results}:
        stage = "other_stage_hit"  # auto 在更早的精确阶段就有命中,没走到模糊
    elif not (acceptable & scored):
        stage = "not_recalled"
    elif (target_ratio or 0) < FUZZY_THRESHOLD:
        stage = "below_threshold"
    else:
        stage = "outside_top"
    return {
        "mode": mode, "stage": stage, "found_rank": found_rank,
        "highlight_ratio": highlight_ratio,
        "anchor_page_ok": anchor_page_ok, "citation_page_ok": citation_page_ok,
        "target_ratio": target_ratio, "target_scored": target in scored if target else None,
        "scored_count": len(scored), "returned": len(ids),
        "noise": sum(1 for pid in ids if pid not in acceptable),
        "match_types": sorted({str(item.get("match_type")) for item in results}),
        "total": response.get("total"), "total_is_exact": response.get("total_is_exact"),
        "latency_ms": round(statistics.median(timings), 1),
    }


def summarize(cases: List[dict], runs: List[dict]) -> List[dict]:
    """One row per (category, mode)."""

    by_id = {case["id"]: case for case in cases}
    rows = []
    negatives = (*NEGATIVE_LENGTHS, *EXTRA_NEGATIVE_LENGTHS)
    categories = [*CATEGORIES, *EXTRA_CATEGORIES, CROSS_PAGE_CATEGORY]
    for category in [*categories, *(f"negative{n}" for n in negatives)]:
        for mode in MODES:
            group = [run for run in runs if run["mode"] == mode and by_id[run["case_id"]]["category"] == category]
            if not group:
                continue
            latencies = sorted(run["latency_ms"] for run in group)
            stages: Dict[str, int] = {}
            for run in group:
                stages[run["stage"]] = stages.get(run["stage"], 0) + 1
            ratios = [run["highlight_ratio"] for run in group if run["highlight_ratio"] is not None]
            rows.append({
                "category": category, "mode": mode, "n": len(group),
                "found": sum(1 for run in group if run["found_rank"] is not None),
                "found_at_1": sum(1 for run in group if run["found_rank"] == 1),
                "stages": stages,
                "highlight_exact": sum(1 for value in ratios if value >= HIGHLIGHT_EXACT),
                "highlight_measured": len(ratios),
                "highlight_strict": sum(1 for value in ratios if value == 1.0),
                "anchor_ok": sum(1 for run in group if run.get("anchor_page_ok")),
                "anchor_measured": sum(1 for run in group if run.get("anchor_page_ok") is not None),
                "citation_ok": sum(1 for run in group if run.get("citation_page_ok")),
                "citation_measured": sum(1 for run in group if run.get("citation_page_ok") is not None),
                "highlight_min": min(ratios) if ratios else None,
                "mean_noise": round(statistics.mean(run["noise"] for run in group), 2),
                "with_any_result": sum(1 for run in group if run["returned"]),
                "latency_median_ms": latencies[len(latencies) // 2],
                "latency_p90_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.9))],
                "latency_max_ms": latencies[-1],
            })
    return rows


def render_summary(rows: List[dict]) -> str:
    lines = [
        "| 类别 | 模式 | 条数 | 找到 | 首位 | 卡点分布 | 高亮准确 | 高亮完全一致 | 锚点页正确 | 引用页码正确 | 高亮最低 | 平均非目标 | 中位 ms | P90 ms | 最大 ms |",
        "|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        stages = ", ".join(f"{key} {value}" for key, value in sorted(row["stages"].items()))
        lines.append(
            f"| {row['category']} | {row['mode']} | {row['n']} | {row['found']} | {row['found_at_1']} | "
            f"{stages} | {row['highlight_exact']}/{row['highlight_measured']} | "
            f"{row['highlight_strict']}/{row['highlight_measured']} | {row['anchor_ok']}/{row['anchor_measured']} | "
            f"{row['citation_ok']}/{row['citation_measured']} | "
            f"{row['highlight_min']} | {row['mean_noise']} | "
            f"{row['latency_median_ms']} | {row['latency_p90_ms']} | {row['latency_max_ms']} |"
        )
    return "\n".join(lines)


def drain_short_grams(path: Path) -> int:
    """Finish the optional short-gram index so the measured path matches a settled library."""

    drained = 0
    while True:
        batch = sgi.drain_short_gram_backlog_at(path)
        if not batch:
            return drained
        drained += batch


def run_benchmark(path: Path, *, per_category: int, seed: int, limit: int, repeats: int) -> dict:
    drained = drain_short_grams(path)
    with closing(sqlite3.connect(path)) as connection:
        cases = build_cases(connection, per_category=per_category, seed=seed)
        paragraphs = connection.execute(
            "SELECT COUNT(*) FROM paragraphs WHERE eligible_for_search = 1"
        ).fetchone()[0]
    engine = SearchEngine(path)
    try:
        execute_with_script_folding(engine, SearchRequest(query="社会", mode="fuzzy"), enabled=True)
        runs = [
            {"case_id": case["id"], **run_case(engine, case, mode, limit=limit, repeats=repeats)}
            for case in cases for mode in MODES
        ]
    finally:
        engine.close()
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=False).stdout.strip()
    except OSError:
        head = ""
    return {
        "environment": {
            "git_head": head, "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "machine": platform.machine(), "database_bytes": path.stat().st_size,
            "eligible_paragraphs": paragraphs, "short_grams_drained": drained,
            "seed": seed, "per_category": per_category, "limit": limit, "repeats": repeats,
        },
        "summary": summarize(cases, runs), "cases": cases, "runs": runs,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="库副本(会被迁移/补索引,勿传生产库)")
    source.add_argument("--fixture", action="store_true", help="用 scripts/performance_fixture 合成库")
    parser.add_argument("--out", type=Path, required=True, help="明细 JSON(含原书句子,勿入库)")
    parser.add_argument("--per-category", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as temporary:
        if args.fixture:
            from scripts.performance_fixture import create_fixture

            create_fixture(Path(temporary))
            path = Path(temporary) / "data" / "index.sqlite3"
        else:
            path = args.db
        report = run_benchmark(path, per_category=args.per_category, seed=args.seed,
                               limit=args.limit, repeats=args.repeats)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report["environment"], ensure_ascii=False))
    print(render_summary(report["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

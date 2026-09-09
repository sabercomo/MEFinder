"""繁简统一检索大库性能基准 (issue #16 / #23 未决项).

在一份真实生产库快照副本上,对比 OpenCC 双轨扩展 关闭 vs 开启 的检索延迟。
查询集直接从库内真实段落抽取子串,保证命中;并区分:
  - CJK 查询(会产生繁/简两个变体, 开启后走 2x 变体路径)
  - 纯 ASCII/无变体查询(单变体, 用作零开销对照)
只读,不改动任何库文件。
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import time
from pathlib import Path

SRC = Path(r"D:\ME_Finder\src")
sys.path.insert(0, str(SRC.parent))  # for `import src.me_finder`

from src.me_finder import script_conversion  # noqa: E402
from src.me_finder.application.script_search import execute_with_script_folding  # noqa: E402
from src.me_finder.application.search_service import SearchRequest  # noqa: E402
from src.me_finder.search import SearchEngine  # noqa: E402

BENCH_DB = sys.argv[1] if len(sys.argv) > 1 else r"D:\ME_Finder\.codex-tmp\bench-index.sqlite3"
OUT_JSON = sys.argv[2] if len(sys.argv) > 2 else "bench_out.json"

CJK = re.compile(r"[\u4e00-\u9fff]")


def sample_queries(engine: SearchEngine, n_cjk: int = 40, seed: int = 17) -> dict:
    """Pull real substrings so every query has hits; keep a no-variant control set."""
    import random

    rng = random.Random(seed)
    cur = engine.db.execute(
        "SELECT text_raw FROM paragraphs WHERE eligible_for_search=1 "
        "AND length(text_raw) BETWEEN 40 AND 4000"
    )
    cjk_q: list[str] = []
    ascii_q: list[str] = []
    for (text,) in cur:
        if not text:
            continue
        t = text.strip()
        cjk_chars = [i for i, ch in enumerate(t) if CJK.match(ch)]
        if len(cjk_chars) >= 12 and len(cjk_q) < n_cjk * 3:
            start = rng.choice(cjk_chars[: max(1, len(cjk_chars) - 10)])
            frag = t[start:start + rng.randint(8, 14)].strip()
            if len(frag) >= 6 and CJK.search(frag):
                # keep only queries whose folded variant actually differs (real 2x path)
                variants = script_conversion.query_variants(frag, enabled=True)
                if len(variants) >= 2:
                    cjk_q.append(frag)
        elif not CJK.search(t) and len(ascii_q) < n_cjk:
            words = [w for w in re.findall(r"[A-Za-z]{3,}", t)]
            if len(words) >= 3:
                i = rng.randint(0, len(words) - 3)
                ascii_q.append(" ".join(words[i:i + 3]))
    rng.shuffle(cjk_q)
    rng.shuffle(ascii_q)
    # Heavy bucket: frequent short CJK terms → many hits, worst case for 2x folding.
    common = ["社会", "主义", "国家", "权力", "现代", "批判", "理论", "性别",
              "主体", "历史", "资本", "政治", "自由", "生产", "关系", "问题",
              "发展", "文化", "民主", "革命"]
    common = [w for w in common if len(script_conversion.query_variants(w, enabled=True)) >= 2]
    return {"cjk": cjk_q[:n_cjk], "ascii": ascii_q[: n_cjk // 2], "cjk_common": common}


def time_query(engine: SearchEngine, query: str, enabled: bool, repeats: int = 5,
               limit: int = 10) -> tuple[float, int, int]:
    req = SearchRequest(query=query, mode="auto", limit=limit, source_type="all")
    samples = []
    total = variants = 0
    for _ in range(repeats):
        t0 = time.perf_counter()
        res = execute_with_script_folding(engine, req, enabled=enabled)
        samples.append((time.perf_counter() - t0) * 1000.0)
        total = int(res.get("total", 0))
        variants = len(script_conversion.query_variants(query, enabled=enabled))
    return statistics.median(samples), total, variants


def run() -> None:
    engine = SearchEngine(Path(BENCH_DB))
    try:
        qs = sample_queries(engine)
        print(f"query set: {len(qs['cjk'])} CJK(2-variant), {len(qs['ascii'])} ASCII(1-variant)", flush=True)
        print(f"heavy CJK common terms: {len(qs['cjk_common'])}", flush=True)
        rows = []
        for label, queries in (("cjk", qs["cjk"]), ("cjk_common", qs["cjk_common"]),
                               ("ascii", qs["ascii"])):
            limit = 50 if label == "cjk_common" else 10
            for q in queries:
                # warm the FTS/page caches once so we measure steady-state, not cold I/O
                execute_with_script_folding(engine, SearchRequest(query=q, limit=limit), enabled=True)
                off_ms, off_total, _ = time_query(engine, q, enabled=False, limit=limit)
                on_ms, on_total, nvar = time_query(engine, q, enabled=True, limit=limit)
                rows.append({
                    "label": label, "query": q, "variants": nvar,
                    "off_ms": round(off_ms, 2), "on_ms": round(on_ms, 2),
                    "overhead_ms": round(on_ms - off_ms, 2),
                    "ratio": round(on_ms / off_ms, 3) if off_ms else None,
                    "off_total": off_total, "on_total": on_total,
                })
                print(f"  [{label}] var={nvar} off={off_ms:6.1f}ms on={on_ms:6.1f}ms "
                      f"x{rows[-1]['ratio']} hits {off_total}->{on_total}  {q[:24]!r}", flush=True)

        def agg(label):
            sub = [r for r in rows if r["label"] == label]
            if not sub:
                return {}
            return {
                "n": len(sub),
                "off_p50": round(statistics.median(r["off_ms"] for r in sub), 2),
                "on_p50": round(statistics.median(r["on_ms"] for r in sub), 2),
                "off_p95": round(sorted(r["off_ms"] for r in sub)[int(len(sub) * 0.95) - 1], 2),
                "on_p95": round(sorted(r["on_ms"] for r in sub)[int(len(sub) * 0.95) - 1], 2),
                "overhead_p50": round(statistics.median(r["overhead_ms"] for r in sub), 2),
                "ratio_p50": round(statistics.median(r["ratio"] for r in sub if r["ratio"]), 3),
                "ratio_max": round(max((r["ratio"] for r in sub if r["ratio"]), default=0), 3),
            }

        meta = engine.index.get("metadata", {})
        summary = {
            "db": BENCH_DB,
            "paragraphs": engine.db.execute("SELECT count(*) FROM paragraphs").fetchone()[0],
            "source_files": engine.db.execute("SELECT count(*) FROM source_files").fetchone()[0],
            "opencc_available": script_conversion.is_available(),
            "cjk_2variant_exacthit": agg("cjk"),
            "cjk_2variant_common_heavy": agg("cjk_common"),
            "ascii_1variant": agg("ascii"),
        }
        print("\nSUMMARY:", json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        Path(OUT_JSON).write_text(
            json.dumps({"summary": summary, "rows": rows, "metadata": meta},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print("wrote", OUT_JSON, flush=True)
    finally:
        engine.close()


if __name__ == "__main__":
    run()

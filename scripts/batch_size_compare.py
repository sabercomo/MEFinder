"""batch64-vs-batch16 前置实验:逐向量 + 完整对齐链接身份比较。

回答"把嵌入 batch 从 64 改成 16 是否值得采用"这一**前置诊断**(纯测量,不改
产品代码,不改默认)。它在**同一模型文件、同一线程数、同一批文本与顺序、同一
缓存状态、同一运行环境**下,把相同文本分别用 batch 64 和 16 嵌入并**逐向量**
比较,再用两种 batch 各跑一次真实对齐、读出**完整链接集**(不是只比 967 这个
计数),比较每条链接的成员段、review_status、confidence、cost、anchor,以及分数
逐条差异;覆盖有代表性的长/短文本与边界样本。最后给出采纳建议(不直接改默认)。

**运行需要本地模型缓存**(`--models <fastembed 缓存目录>`);缺缓存时联网下载
违反本地优先,故本环境未运行——脚本已就绪,模型缓存就位且机器安静时执行:

    PY=.venv-macos312-arm64/bin/python
    $PY scripts/batch_size_compare.py --db .codex-tmp/real-library-20260911/index.sqlite3 \
      --group <group> --pivot <pivot> --target <target> --models <cache-root> \
      --output .codex-tmp/batch-compare.json

纯比较函数(`vector_diff_stats` / `link_identity` / `link_set_diff` / `verdict`)
由 `tests/test_batch_size_compare.py` 覆盖,不依赖模型。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# ---------------------------------------------------------------------------
# Pure comparison helpers (model-independent; unit-tested)
# ---------------------------------------------------------------------------

# Below this per-vector max-abs difference we treat two embeddings as "the same
# up to float noise"; ONNX kernels can differ in the last bits across batch
# shapes without changing downstream ranking. Anything above is a real change.
FLOAT_NOISE_MAX_ABS = 1e-4


def vector_diff_stats(a, b, *, lengths=None) -> dict:
    """Per-vector difference stats between two (n, d) embedding matrices.

    Reports the worst and mean absolute element difference, the minimum
    per-row cosine similarity, the fraction of bitwise-identical rows, and —
    when ``lengths`` (per-row source text length) is given — the same worst-case
    broken down into short/medium/long buckets so boundary samples are visible.
    """

    import numpy as np

    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch: {a.shape} vs {b.shape}")
    if a.size == 0:
        return {"rows": 0}
    diff = np.abs(a - b)
    row_max = diff.max(axis=1)
    dot = (a * b).sum(axis=1)
    na = np.linalg.norm(a, axis=1)
    nb = np.linalg.norm(b, axis=1)
    denom = np.where((na * nb) == 0, 1.0, na * nb)
    cosine = dot / denom
    bitwise_equal = int((row_max == 0).sum())
    stats = {
        "rows": int(a.shape[0]),
        "dim": int(a.shape[1]),
        "max_abs": float(diff.max()),
        "mean_abs": float(diff.mean()),
        "min_cosine": float(cosine.min()),
        "bitwise_equal_rows": bitwise_equal,
        "bitwise_equal_fraction": bitwise_equal / a.shape[0],
        "rows_above_noise": int((row_max > FLOAT_NOISE_MAX_ABS).sum()),
    }
    if lengths is not None:
        lengths = list(lengths)
        buckets = {"short(<20)": [], "medium(20-99)": [], "long(>=100)": []}
        for index, length in enumerate(lengths):
            key = "short(<20)" if length < 20 else ("medium(20-99)" if length < 100 else "long(>=100)")
            buckets[key].append(float(row_max[index]))
        stats["by_length_max_abs"] = {
            key: (max(values) if values else None) for key, values in buckets.items()
        }
        stats["by_length_rows"] = {key: len(values) for key, values in buckets.items()}
    return stats


def link_identity(link: dict) -> tuple:
    """Structural identity of one alignment link (ignores float scores)."""

    return (
        int(link["order_index"]),
        str(link.get("review_status") or ""),
        str(link.get("anchor_key") or ""),
        tuple(link.get("pivot_segments") or ()),
        tuple(link.get("target_segments") or ()),
    )


def link_set_diff(links_a: list[dict], links_b: list[dict]) -> dict:
    """Compare two complete link sets by structure AND per-link scores.

    Structural identity uses membership + review_status + anchor (not counts);
    for links present in both, reports the worst confidence/cost delta. Any
    structural difference (added/removed/flipped) is surfaced explicitly.
    """

    by_a = {link_identity(link): link for link in links_a}
    by_b = {link_identity(link): link for link in links_b}
    keys_a, keys_b = set(by_a), set(by_b)
    common = keys_a & keys_b
    max_conf_delta = 0.0
    max_cost_delta = 0.0
    for key in common:
        la, lb = by_a[key], by_b[key]
        if la.get("confidence") is not None and lb.get("confidence") is not None:
            max_conf_delta = max(max_conf_delta, abs(float(la["confidence"]) - float(lb["confidence"])))
        if la.get("cost") is not None and lb.get("cost") is not None:
            max_cost_delta = max(max_cost_delta, abs(float(la["cost"]) - float(lb["cost"])))
    # Structural flips: same order_index but different membership/status/anchor.
    order_a = {int(link["order_index"]): link_identity(link) for link in links_a}
    order_b = {int(link["order_index"]): link_identity(link) for link in links_b}
    flipped = sorted(
        idx for idx in (set(order_a) & set(order_b)) if order_a[idx] != order_b[idx]
    )
    return {
        "count_a": len(links_a),
        "count_b": len(links_b),
        "identical_structure": keys_a == keys_b,
        "only_in_a": len(keys_a - keys_b),
        "only_in_b": len(keys_b - keys_a),
        "flipped_order_indices": flipped[:50],
        "flipped_count": len(flipped),
        "max_confidence_delta": max_conf_delta,
        "max_cost_delta": max_cost_delta,
    }


def verdict(vector_stats: dict, link_diff: dict) -> dict:
    """Adoption recommendation for batch16 — never auto-applies a default."""

    if link_diff.get("identical_structure") and link_diff.get("flipped_count", 0) == 0:
        if vector_stats.get("bitwise_equal_fraction") == 1.0:
            level = "safe-bitwise-identical"
            note = "逐向量完全一致、链接身份完全一致:batch16 数值等价,可采纳(仍需人评估峰值收益是否值得)。"
        elif vector_stats.get("rows_above_noise", 1) == 0:
            level = "safe-within-noise-but-cache-version"
            note = ("向量仅有浮点末位差异、链接身份一致:数值可接受,但缓存向量非逐位相同,"
                    "采纳前须决定是否 bump EMBEDDING_RUNTIME_VERSION(否则新旧 batch 的缓存向量混用)。")
        else:
            level = "review-vectors-changed-links-stable"
            note = "存在超噪声阈值的向量差异但链接身份仍一致:需人评估是否影响其它下游(定位/复用),不得仅凭'链接没变'采纳。"
    else:
        level = "not-safe-links-changed"
        note = ("链接身份发生变化(新增/消失/翻转):batch16 改变了对齐结果,"
                "**不得**据'差异很小'采纳,须逐条核对代表样本与质量门槛。")
    return {"recommendation": level, "note": note,
            "do_not_change_product_default": True, "current_default_batch": 64}


# ---------------------------------------------------------------------------
# DB readback (pure; unit-tested with a fixture connection)
# ---------------------------------------------------------------------------

def read_links(connection: sqlite3.Connection, run_id: str) -> list[dict]:
    """Full link set for a run: order, status, anchor, scores, member segments."""

    rows = connection.execute(
        "SELECT alignment_link_id, order_index, cost, review_status, confidence, anchor_key "
        "FROM alignment_links WHERE alignment_run_id = ? ORDER BY order_index",
        (run_id,),
    ).fetchall()
    links = []
    for link_id, order_index, cost, review_status, confidence, anchor_key in rows:
        members = connection.execute(
            "SELECT side, segment_id FROM alignment_link_members "
            "WHERE alignment_link_id = ? ORDER BY side, member_order",
            (link_id,),
        ).fetchall()
        pivot = [seg for side, seg in members if side == "pivot"]
        target = [seg for side, seg in members if side == "target"]
        links.append({
            "order_index": order_index, "cost": cost, "review_status": review_status,
            "confidence": confidence, "anchor_key": anchor_key,
            "pivot_segments": pivot, "target_segments": target,
        })
    return links


def _latest_run_id(connection: sqlite3.Connection) -> str:
    return connection.execute(
        "SELECT alignment_run_id FROM alignment_runs WHERE status = 'completed' "
        "ORDER BY completed_at DESC, rowid DESC LIMIT 1"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Model-dependent driver (requires --models cache; not run without it)
# ---------------------------------------------------------------------------

def _forced_batch_text_embedding(forced_batch: int):
    """A TextEmbedding subclass-like wrapper that pins batch_size."""

    import fastembed

    real = fastembed.TextEmbedding

    class _Pinned:
        def __init__(self, *args, **kwargs):
            self._impl = real(*args, **kwargs)

        def __getattr__(self, name):
            attribute = getattr(self._impl, name)
            if name in ("embed", "query_embed") and callable(attribute):
                def bounded(texts, batch_size=None, _call=attribute):
                    return _call(texts, batch_size=forced_batch)
                return bounded
            return attribute

    return _Pinned


def _segment_texts(connection: sqlite3.Connection, run_id: str) -> list[str]:
    """Pivot then target segment texts, in embedding order, for a run."""

    run = connection.execute(
        "SELECT pivot_segment_set_id, target_segment_set_id FROM alignment_runs "
        "WHERE alignment_run_id = ?", (run_id,),
    ).fetchone()
    texts: list[str] = []
    for set_id in run:
        texts.extend(
            str(row[0]) for row in connection.execute(
                "SELECT text_raw FROM text_segments WHERE segment_set_id = ? ORDER BY order_index",
                (set_id,),
            )
        )
    return texts


def run_experiment(arguments) -> dict:  # pragma: no cover - requires model cache
    import numpy as np
    from unittest import mock

    from src.me_finder import semantic_alignment
    from src.me_finder import text_alignment as text_alignment_module
    from src.me_finder.embedding_models import EMBEDDING_MODELS, DEFAULT_EMBEDDING_MODEL_ID
    from src.me_finder.embedding_runtime import begin_embedding_run

    model = EMBEDDING_MODELS[DEFAULT_EMBEDDING_MODEL_ID]

    with TemporaryDirectory(prefix="mefinder-batch-cmp-") as temporary:
        root = Path(temporary)
        (root / "data").mkdir(parents=True)
        db = root / "data/index.sqlite3"
        shutil.copy2(arguments.db, db)
        cache_dir = root / "models"
        shutil.copytree(arguments.models, cache_dir)

        def align(batch: int) -> str:
            begin_embedding_run()
            # Force a fresh recompute each time so the same texts are embedded.
            with mock.patch.object(
                __import__("fastembed"), "TextEmbedding",
                _forced_batch_text_embedding(batch),
            ):
                text_alignment_module.generate_alignment(
                    db, arguments.group, arguments.pivot, arguments.target,
                    force=True, model_cache_dir=cache_dir, embedding_model_id=model.id,
                )
            with sqlite3.connect(db) as connection:
                return _latest_run_id(connection)

        # 1) full alignment with each batch, then full link comparison.
        run64 = align(64)
        with sqlite3.connect(db) as connection:
            links64 = read_links(connection, run64)
            texts = _segment_texts(connection, run64)
        run16 = align(16)
        with sqlite3.connect(db) as connection:
            links16 = read_links(connection, run16)

        # 2) per-vector comparison of the identical text list at both batches.
        def embed(batch: int):
            begin_embedding_run()
            with mock.patch.object(
                __import__("fastembed"), "TextEmbedding",
                _forced_batch_text_embedding(batch),
            ):
                provider = semantic_alignment.FastEmbedEmbeddingProvider(
                    semantic_alignment.embedding_model_config(model.id)
                )
                return np.asarray(provider(texts, cache_dir=root / "vec-tmp-%d" % batch))

        vec64 = embed(64)
        vec16 = embed(16)
        vstats = vector_diff_stats(vec64, vec16, lengths=[len(t) for t in texts])
        ldiff = link_set_diff(links64, links16)
        return {
            "model_id": model.id,
            "text_count": len(texts),
            "vector_diff": vstats,
            "link_diff": ldiff,
            "verdict": verdict(vstats, ldiff),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--pivot", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--models", type=Path, required=True,
                        help="fastembed cache root containing the model dir")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if not arguments.models.exists():
        parser.error(
            f"model cache not found: {arguments.models} — batch comparison needs a "
            "local model; a network download would violate local-first."
        )
    report = run_experiment(arguments)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["verdict"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

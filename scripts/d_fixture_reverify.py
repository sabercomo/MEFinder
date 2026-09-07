"""Rebuild v13/v21 alignment for the D-experiment fixture pairs on a read-only
copy of the production index, then reuse the result to re-verify issue #18's
11 golden fixtures.

The production index is never opened here; the caller passes a writable *copy*.
Segmentation is regenerated on demand from already-parsed database text (no PDF
re-parse, no OCR), and E5 vectors are reused from the production model cache
(only genuinely new segment strings are embedded).

Usage (segment + realign one pair, append its run to a JSONL ledger):

    python -m scripts.d_fixture_reverify realign \
        --db .codex-tmp/d-experiment/index-v21.sqlite3 \
        --cache dist/MEFinderData/runtime/components/text-alignment/models \
        --ranges reports/alignment-reviewed-body-ranges-2026-09-05.json \
        --pivot <source_id> --target <source_id> \
        --out reports/d-fixture-reverify-runs.jsonl
"""
import argparse
from contextlib import closing
import difflib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.me_finder.text_alignment import (  # noqa: E402
    SEGMENTER_VERSION,
    _segment_set,
    generate_alignment,
    open_writable_index,
)


def _segment_texts(con: sqlite3.Connection, set_id: str) -> list[str]:
    return [
        r[0]
        for r in con.execute(
            "SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index",
            (set_id,),
        )
    ]


def _v12_run_sets(con: sqlite3.Connection, pivot: str, target: str) -> tuple[str, str]:
    row = con.execute(
        "SELECT pivot_segment_set_id, target_segment_set_id FROM alignment_runs "
        "WHERE pivot_source_file_id=? AND target_source_file_id=? AND status='completed' "
        "ORDER BY created_at DESC LIMIT 1",
        (pivot, target),
    ).fetchone()
    if row is None:
        raise SystemExit(f"no completed run for {pivot} -> {target}")
    return str(row[0]), str(row[1])


def _map_order(old_texts: list[str], new_texts: list[str]) -> dict[int, int]:
    """Monotonic old-order -> new-order map over verbatim-equal segment blocks.

    Re-segmentation only inserts heading segments (and rarely splits), so the
    bulk of old segments reappear verbatim and in order.  SequenceMatcher's
    'equal' opcodes give an exact, deterministic index correspondence.
    """
    mapping: dict[int, int] = {}
    matcher = difflib.SequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                mapping[i1 + offset] = j1 + offset
    return mapping


def _map_range(old_texts: list[str], new_texts: list[str], rng: list[int]) -> list[int]:
    if old_texts == new_texts:  # unchanged text: identity map (only the set id changed)
        return [rng[0], rng[1]]
    mapping = _map_order(old_texts, new_texts)
    a = mapping.get(rng[0])
    b_last = mapping.get(rng[1] - 1)
    if a is None or b_last is None:
        # Fall back to the nearest mapped neighbours so the body window still
        # brackets the same interior corridor the fixtures live in.
        keys = sorted(mapping)
        a = a if a is not None else mapping[min(keys, key=lambda k: abs(k - rng[0]))]
        b_last = (
            b_last
            if b_last is not None
            else mapping[min(keys, key=lambda k: abs(k - (rng[1] - 1)))]
        )
    return [a, b_last + 1]


def realign(args: argparse.Namespace) -> None:
    db = Path(args.db).resolve()
    cache = Path(args.cache).resolve()
    reviewed = json.loads(Path(args.ranges).read_text(encoding="utf-8"))

    with closing(open_writable_index(db)) as con:
        con.row_factory = sqlite3.Row
        v12_pivot_set, v12_target_set = _v12_run_sets(con, args.pivot, args.target)
        v12_pivot_texts = _segment_texts(con, v12_pivot_set)
        v12_target_texts = _segment_texts(con, v12_target_set)
        # Regenerate v13 segment sets from stored text (no re-parse).
        con.execute("BEGIN IMMEDIATE")
        v13_pivot_set, v13_pivot_segs = _segment_set(con, args.pivot)
        v13_target_set, v13_target_segs = _segment_set(con, args.target)
        con.commit()
        v13_pivot_texts = [t for _, t in v13_pivot_segs]
        v13_target_texts = [t for _, t in v13_target_segs]

    assert v12_pivot_set in reviewed and v12_target_set in reviewed, "missing reviewed range"
    pivot_range = _map_range(v12_pivot_texts, v13_pivot_texts, reviewed[v12_pivot_set])
    target_range = _map_range(v12_target_texts, v13_target_texts, reviewed[v12_target_set])
    bounds = {"pivot": pivot_range, "target": target_range}

    started = time.monotonic()
    result = generate_alignment(
        db,
        _document_group(db, args.pivot, args.target),
        args.pivot,
        args.target,
        force=True,
        model_cache_dir=cache,
        embedding_model_id="multilingual-e5-large",
        reviewed_body_ranges=bounds,
    )
    record = {
        "pivot": args.pivot,
        "target": args.target,
        "segmenter_version": SEGMENTER_VERSION,
        "v12_pivot_set": v12_pivot_set,
        "v12_target_set": v12_target_set,
        "v13_pivot_set": v13_pivot_set,
        "v13_target_set": v13_target_set,
        "v12_pivot_segments": len(v12_pivot_texts),
        "v13_pivot_segments": len(v13_pivot_texts),
        "v12_target_segments": len(v12_target_texts),
        "v13_target_segments": len(v13_target_texts),
        "reviewed_range_v13": bounds,
        "result": result,
        "seconds": round(time.monotonic() - started, 2),
    }
    line = json.dumps(record, ensure_ascii=False)
    if args.out:
        with open(args.out, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    print(line, flush=True)


def _document_group(db: Path, pivot: str, target: str) -> str:
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        row = con.execute(
            "SELECT document_group_id FROM alignment_runs WHERE pivot_source_file_id=? "
            "AND target_source_file_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1",
            (pivot, target),
        ).fetchone()
    if row is None:
        raise SystemExit(f"no group for {pivot} -> {target}")
    return str(row[0])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("realign", help="segment to v13 and realign one pair at v21")
    p.add_argument("--db", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--ranges", required=True)
    p.add_argument("--pivot", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--out", default=None)
    p.set_defaults(func=realign)
    args = parser.parse_args()
    args.func(args)

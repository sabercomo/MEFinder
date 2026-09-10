"""Deterministic, public synthetic corpus for the HTTP performance baseline."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from pathlib import Path

from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text

FIXTURE_VERSION = 1
QUERIES = [
    {"id": "zh_exact", "query": "青铜指南针记录了这次独特的观察", "mode": "exact"},
    {"id": "script_variant", "query": "圖書館保留獨特的閱讀記錄", "mode": "auto"},
    {"id": "en_exact", "query": "The violet compass marks a unique observation", "mode": "exact"},
    {"id": "common_zh", "query": "社会", "mode": "auto"},
    {"id": "common_en", "query": "social relations", "mode": "auto"},
    {"id": "normalized", "query": "青铜指南针，记录了这次独特的观察", "mode": "auto"},
    {"id": "no_hit", "query": "不存在的橙色卫星档案XYZ", "mode": "exact"},
    {"id": "scoped", "query": "社会", "mode": "auto", "source_type": "pdf"},
]
QUERIES = [{"limit": 10, "source_type": "all", **query} for query in QUERIES]


def create_fixture(root: Path, *, documents: int = 32, paragraphs: int = 2000,
                   alignment_paragraphs: int = 320, seed: int = 20260910) -> dict:
    """Build a disposable index with fixed text, PDF anchors and a bilingual pair."""
    if documents < 2 or paragraphs < 4 or alignment_paragraphs < 4:
        raise ValueError("documents >= 2; paragraphs and alignment_paragraphs >= 4")
    rng = random.Random(seed)
    index = {key: [] for key in ("source_files", "volumes", "paragraphs", "pdf_pages")}
    index["metadata"] = {"database_built_at": "2026-09-10T00:00:00+00:00"}
    topics = ["社会", "历史", "生产", "理论", "实践", "知识", "劳动", "制度"]
    for doc in range(documents + 2):
        pair = doc >= documents
        english = doc % 2 == 1
        source_id = f"bench-{doc:03d}"
        title = f"Synthetic volume {doc:03d}"
        count = alignment_paragraphs if pair else paragraphs
        source_type = "word" if pair or doc % 3 else "pdf"
        source_format = "epub" if pair else ("pdf" if source_type == "pdf" else "docx")
        index["source_files"].append({
            "source_file_id": source_id, "source_type": source_type,
            "file_format": source_format, "file_name": f"{source_id}.{source_format}",
            "title": title, "language_code": "en" if english else "zh-Hans",
            "bibliographic_metadata": {"title": title},
        })
        index["volumes"].append({
            "volume_id": source_id, "source_file_id": source_id,
            "source_type": source_type, "display_title": title, "volume_number": doc + 1,
        })
        page_texts: dict[int, list[str]] = {}
        for number in range(count):
            topic = topics[number % len(topics)]
            token = rng.randrange(10**9)
            raw = (
                f"Observation {number:04d} considers social relations and historical knowledge. "
                f"Workers discuss institutions and practical experience in archive {token:09d}."
                if english else
                f"第{number:04d}项观察讨论{topic}及其历史条件。人们通过共同劳动形成制度，"
                f"并在实践经验中检验知识与解释之间的关系。档案编号{token:09d}保留了本次记录。"
            )
            if doc == 0 and number == 0:
                raw += "青铜指南针记录了这次独特的观察。图书馆保留独特的阅读记录。"
            if doc == 1 and number == 0:
                raw += " The violet compass marks a unique observation."
            page = number // 4
            texts = page_texts.setdefault(page, [])
            offset = sum(len(text) + 1 for text in texts)
            texts.append(raw)
            paragraph = {
                "paragraph_id": f"{source_id}-p{number:06d}",
                "source_file_id": source_id, "source_type": source_type,
                "volume_id": source_id, "volume_number": doc + 1,
                "paragraph_index": number, "eligible_for_search": True,
                "text_raw": raw, "normalized_text": normalize_text(raw),
                "compact_text": compact_text(raw), "plain_text": punctuationless_text(raw),
                "document_title": title, "work_title": title, "volume_display": title,
                "page_display": "引用页码尚未校准", "page_source_type": "uncalibrated",
            }
            if source_type == "pdf":
                paragraph.update({
                    "pdf_page_start_index": page, "pdf_page_end_index": page,
                    "text_source_spans": [{"pdf_page_id": f"{source_id}-PAGE-{page:06d}", "paragraph_char_start": 0,
                               "paragraph_char_end": len(raw), "page_char_start": offset,
                               "page_char_end": offset + len(raw)}],
                })
            index["paragraphs"].append(paragraph)
        if source_type == "pdf":
            for page, texts in page_texts.items():
                index["pdf_pages"].append({
                    "source_file_id": source_id, "source_type": "pdf",
                    "pdf_page_id": f"{source_id}-PAGE-{page:06d}",
                    "pdf_page_index": page, "physical_pdf_page": page + 1,
                    "pdf_page_number_1based": page + 1, "text_raw": "\n".join(texts),
                    "blocks": [{"text": text, "type": "text"} for text in texts],
                    "parser": "synthetic-baseline",
                })
    database = root / "data" / "index.sqlite3"
    build_database(index, database)
    pivot, target = f"bench-{documents:03d}", f"bench-{documents + 1:03d}"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO document_groups VALUES (?, ?, ?, ?, ?)",
            ("bench-pair", "Synthetic bilingual pair", pivot, "t", "t"),
        )
        connection.executemany(
            "INSERT INTO document_group_members VALUES (?, ?, ?, ?, ?)",
            [("bench-pair", pivot, "pivot", 0, "t"), ("bench-pair", target, "target", 1, "t")],
        )
        schema = connection.execute("PRAGMA user_version").fetchone()[0]
    manifest = {
        "fixture_version": FIXTURE_VERSION, "seed": seed, "documents": documents + 2,
        "search_documents": documents, "paragraphs_per_document": paragraphs,
        "alignment_paragraphs": alignment_paragraphs,
        "paragraphs": len(index["paragraphs"]), "pdf_pages": len(index["pdf_pages"]),
        "content_sha256": hashlib.sha256(json.dumps(index, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "database_bytes": database.stat().st_size,
        "schema_version": schema, "queries": QUERIES,
        "alignment_request": {"document_group_id": "bench-pair",
                              "pivot_source_file_id": pivot, "target_source_file_id": target,
                              "force": True},
    }
    (root / "fixture.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest

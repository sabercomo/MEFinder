"""Search pipeline contract: shared constants, candidate spec and invariants.

The pipeline is split into one-way stages::

    search.py (facade) -> search_recall -> search_contract
                       -> search_scoring -> search_anchors / search_contract
                       -> search_assembly -> search_anchors / search_citation

The HTTP payload produced by :mod:`search_assembly` is frozen by
``tests/test_search_pipeline_contract.py``; match offsets are Unicode
code-point indices into ``text_raw`` (JavaScript consumers must convert
UTF-16 code units), and every PDF hit must carry ``page_match_spans``.
"""

from __future__ import annotations

from typing import Dict, TypedDict

SEARCH_MODES = {"auto", "exact", "compact", "punctuation", "fuzzy"}
MAX_FTS_QUERY_TRIGRAMS = 48
SQL_CANDIDATE_FLOOR = 64
SQL_CANDIDATE_MULTIPLIER = 8
# The FTS trigram MATCH already surfaces up to ~700 candidates ordered by
# relevance, but the per-candidate SequenceMatcher window search is expensive
# on long (footnote-heavy) pages — scoring all of them made a single fuzzy
# query take 30s+.  Cheap n-gram overlap ranks the candidates first so the
# window search only runs on the strongest few; the weaker ones cannot clear
# the ratio gate anyway.
FUZZY_RESCORE_LIMIT = 64
# Query lengths whose typos can break every trigram: when FTS then yields no
# fuzzy candidate, retry through the bigram scan. 3 characters stay out (most
# hits would be unrelated); from 9 on, two typos always leave a trigram intact.
# Decided per engine query, so each script-folding variant decides for itself.
# Evidence: reports/fuzzy-search-benchmark-2026-09-30.md section 9.
FUZZY_BIGRAM_FALLBACK_LENGTHS = range(4, 9)
# From 5 characters the bigram scan runs even when FTS already produced fuzzy
# candidates: a weak FTS candidate must not hide the intended sentence (the
# 4-character rule stays as above). Evidence: same report, section 10.
FUZZY_BIGRAM_UNION_LENGTHS = range(5, 9)


def fuzzy_needs_bigram_scan(query_length: int, fts_found: bool) -> bool:
    """Whether the fuzzy pass also runs the bigram scan after the FTS branch."""

    if query_length not in FUZZY_BIGRAM_FALLBACK_LENGTHS:
        return False
    return not fts_found or query_length in FUZZY_BIGRAM_UNION_LENGTHS


class CandidateSpec(TypedDict):
    """One recalled occurrence before scoring-aware formatting."""

    paragraph_id: str
    paragraph: Dict[str, object]
    match_type: str
    match_score: float
    match_start: int
    match_end: int

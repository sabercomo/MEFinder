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


class CandidateSpec(TypedDict):
    """One recalled occurrence before scoring-aware formatting."""

    paragraph_id: str
    paragraph: Dict[str, object]
    match_type: str
    match_score: float
    match_start: int
    match_end: int

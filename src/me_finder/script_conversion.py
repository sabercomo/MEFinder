"""OpenCC-backed Traditional/Simplified Chinese script conversion.

Leaf module: it must not import other ``me_finder`` modules, so it can be
reused from the normalization, search, and UI layers without import cycles.

Two granularities are provided on purpose:

* Phrase level -- ``to_simplified`` / ``to_traditional`` run OpenCC's full
  dictionaries (e.g. "軟體" -> "软体").  Used for query expansion and for
  display text, where natural output matters and no offset mapping is
  required.
* Segment level -- ``fold_to_simplified_with_map`` folds text with the same
  phrase-level dictionaries, but sentence segment by sentence segment: a
  folded segment is kept only when the conversion preserved its length,
  otherwise the original segment is kept verbatim.  The output therefore
  always has the same length as the input and the returned map is the
  identity by construction, so highlight offsets computed on the folded
  text still land on the original text -- a standalone utility, not used by query expansion.  Length-changing phrase conversions (rare
  in the generic ``t2s`` dictionary) simply opt their segment out of
  folding instead of breaking offsets.

The generic ``t2s``/``s2t`` configurations are used deliberately: locale
variants (``tw2s``, ``hk2s``...) would split the folded search space, which
contradicts the "good enough, OpenCC-based" scope of issue #16.

OpenCC is an optional dependency.  When it is not installed every function
degrades to an identity conversion: script folding is disabled but the rest
of the application keeps working.
"""

from __future__ import annotations

import re
import threading
from functools import lru_cache
from typing import List, Tuple

try:
    import opencc as _opencc_module
except Exception:  # pragma: no cover - depends on the environment
    _opencc_module = None

_T2S_CONFIG = "t2s"
_S2T_CONFIG = "s2t"

# Phrase-level conversions almost never cross sentence/clause punctuation,
# so folding per segment confines the length-change fallback to one segment
# instead of discarding the fold of a whole paragraph.
_SEGMENT_BOUNDARIES = re.compile(r"([。，、；：？！「」『』（）《》〈〉…—·\n])")

_thread_local = threading.local()


def is_available() -> bool:
    """Return True when both converters and their dictionary data load."""
    return _converter(_T2S_CONFIG) is not None and _converter(_S2T_CONFIG) is not None


def _converter(config: str):
    """Return a per-thread OpenCC converter for ``config``, or None.

    Converters are created lazily and kept per thread so callers never have
    to worry about OpenCC thread-safety.  A failed construction is cached as
    None so the failure is paid only once per thread.
    """
    if _opencc_module is None:
        return None
    converters = getattr(_thread_local, "converters", None)
    if converters is None:
        converters = {}
        _thread_local.converters = converters
    if config not in converters:
        try:
            converters[config] = _opencc_module.OpenCC(config)
        except Exception:
            converters[config] = None
    return converters[config]


@lru_cache(maxsize=16384)
def _convert_cached(config: str, text: str) -> str:
    converter = _converter(config)
    if converter is None:
        return text
    return converter.convert(text)


def to_simplified(text: str) -> str:
    """Phrase-level Traditional -> Simplified (identity without OpenCC)."""
    if not text:
        return text
    return _convert_cached(_T2S_CONFIG, text)


def to_traditional(text: str) -> str:
    """Phrase-level Simplified -> Traditional (identity without OpenCC)."""
    if not text:
        return text
    return _convert_cached(_S2T_CONFIG, text)


def fold_to_simplified_with_map(text: str) -> Tuple[str, List[int]]:
    """Fold ``text`` to Simplified with phrase-level dictionaries.

    Returns ``(folded_text, source_map)`` following the
    ``normalize_with_map`` contract: ``source_map[i]`` is the index in
    ``text`` that produced ``folded_text[i]``.

    Folding happens per sentence segment.  A folded segment is kept only
    when the conversion preserved the segment length; otherwise the
    original segment is kept verbatim.  The output therefore always has the
    same length as the input and the map is the identity by construction --
    offsets computed on the folded text are valid on the original text.
    Without OpenCC this degrades to the identity map.
    """
    if not text:
        return "", []
    if not is_available():
        return text, list(range(len(text)))
    pieces: List[str] = []
    for segment in _SEGMENT_BOUNDARIES.split(text):
        if not segment:
            continue
        folded = to_simplified(segment)
        pieces.append(folded if len(folded) == len(segment) else segment)
    folded_text = "".join(pieces)
    return folded_text, list(range(len(text)))


def query_variants(query: str, *, enabled: bool = True) -> List[str]:
    """Return the de-duplicated script variants a search should try.

    The original query always comes first, followed by its Simplified and
    Traditional phrase-level forms when they differ.  With the toggle off,
    an empty query, or no OpenCC installed, this is just ``[query]``.
    """
    if not enabled or not query or not is_available():
        return [query]
    ordered: List[str] = []
    seen = set()
    for variant in (query, to_simplified(query), to_traditional(query)):
        if variant and variant not in seen:
            seen.add(variant)
            ordered.append(variant)
    return ordered

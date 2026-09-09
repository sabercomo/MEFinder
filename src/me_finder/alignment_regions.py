"""Body/frontmatter/backmatter region bounds for alignment.

Split out of :mod:`semantic_alignment` to keep that module within its size
budget. This is the cohesive "which segments are body text" logic that
``tests/test_alignment_regions.py`` covers; it reuses the heading detection and
regexes that still live in :mod:`semantic_alignment`.
"""

from __future__ import annotations

import re
from typing import Sequence, Tuple

from .semantic_alignment import (
    _CHINESE_HEADING,
    _DECIMAL_SECTION,
    _INTRODUCTION_HEADING,
    _document_heading_positions,
)


def alignment_body_bounds(texts: Sequence[str]) -> Tuple[int, int]:
    """Bound the main text using existing TOC-aware chapter detection.

    A missing body heading is not evidence that the whole document is frontmatter.
    Only leading title lines open backmatter; a prose mention never does.
    Inline footnotes inside the body are left to the existing note workflow.
    """
    positions = _document_heading_positions(texts)
    body_positions = [
        index for key, index in positions.items()
        if key.startswith("chapter:")
        # Roman-numbered prose and CIP entries are not body boundaries.
        and (key.count(":") == 1 or _DECIMAL_SECTION.fullmatch(texts[index].splitlines()[0].strip()))
        and not re.match(r"^\d+\s+[a-z]", texts[index].strip())
    ]
    if "paragraph:1" in positions and re.match(
        r"^(?:§|第\s*1\s*节)", texts[positions["paragraph:1"]].strip()
    ):
        body_positions = [positions["paragraph:1"]]
    # A leading author Introduction (导论/绪论/引言/…) is body: it must not be
    # discarded as frontmatter just because it precedes the first numbered
    # chapter. Its heading line stays short even with a subtitle.
    introduction_positions = [
        index
        for index, text in enumerate(texts)
        if (lines := text.strip().splitlines())
        and len(lines[0]) <= 80
        and _INTRODUCTION_HEADING.match(lines[0])
    ]
    start = min(body_positions + introduction_positions, default=0)
    end = len(texts)
    backmatter_titles = {
        "译后记", "譯後記", "译者后记", "譯者後記", "后记", "後記",
        "致谢", "致謝", "鸣谢", "鳴謝", "索引", "尾注", "尾註",
        "注释", "註釋", "参考文献", "參考文獻",
        "afterword", "translator'safterword", "translator’safterword",
        "acknowledgments", "acknowledgements", "index", "notes", "endnotes",
        "bibliography", "references", "anmerkungen", "nachwort", "register",
        "bibliographie", "remerciements",
    }
    note_titles = {"注释", "註釋", "notes", "anmerkungen"}
    note_positions = [
        index for index in range(start + 1, len(texts))
        if texts[index].strip().splitlines()
        and re.sub(r"\s+", "", texts[index].strip().splitlines()[0]).casefold() in note_titles
    ]
    for index in range(start + 1, len(texts)):
        lines = texts[index].strip().splitlines()
        title = re.sub(r"\s+", "", lines[0]).casefold() if lines else ""
        if title in backmatter_titles:
            # Repeated chapter-note blocks are not a single end-of-book region.
            # The existing anchor reader stops at Notes, so inspect later
            # explicit chapter headings here without changing anchor/DP logic.
            if title in note_titles and (
                len(note_positions) > 1
                or any(
                    any(
                        (match := _CHINESE_HEADING.fullmatch(line.strip())) is not None
                        and match.group(2) in {"章", "篇", "部"}
                        for line in text.splitlines()
                    )
                    for text in texts[index + 1:]
                )
            ):
                continue
            end = index
            break
    return start, end

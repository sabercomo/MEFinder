"""对齐产出的段质量门：噪声段与注释块不得单独承载正文配对。

DP 冻结不动；这一层只在对齐产出后，把“整侧仅由噪声段或脚注块承载”的 automatic
正文链接降级为 rejected（清空 anchor_key，保留可查但不作定位）。仅作用于正文链接，
``note_automatic`` 注释通道合法配对短标记，不受影响。

本模块只依赖标准库（``re`` 与 ``dataclasses.replace``），对 ``SemanticLink`` 只做
鸭子类型访问，不 import 它，故不引入包内循环依赖。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import List, Sequence, Tuple

# A segment carrying fewer informative characters than this (a lone OCR page mark
# such as "|", a stray rule, an empty line) is noise: it must not carry a body
# match by itself.  Kept deliberately small so short but real sentences -- a
# four-character Chinese clause -- always pass.
_MIN_BODY_SEGMENT_INFO_CHARS = 2
_SEGMENT_INFORMATION = re.compile(
    "[0-9A-Za-z"
    "À-ÖØ-öø-ÿ"  # Latin-1 letters
    "぀-ヿ"  # kana
    "㐀-鿿"  # CJK ideographs
    "가-힯]"  # Hangul
)
# A footnote entry leads with a bare circled digit (①..⑳, U+2460-U+2473).  A run
# of at least this many with increasing numbers is a footnote block; the
# marker-less lines between entries are their continuations.  A body paragraph
# that merely *references* a note carries the marker inside a "$^{②}$"
# superscript wrapper and so never starts with a bare circled digit.
_CIRCLED_NOTE_MARKER = re.compile("^\\s*([①-⑳])")
_MIN_CIRCLED_NOTE_RUN = 3


def _is_low_information_segment(text: str) -> bool:
    """A segment with almost no linguistic content (a lone OCR page mark such as
    ``|``, a stray rule, an empty line) that must not carry a body match."""

    return len(_SEGMENT_INFORMATION.findall(text)) < _MIN_BODY_SEGMENT_INFO_CHARS


def _circled_note_block_indices(texts: Sequence[str]) -> set[int]:
    """Return indices covered by circled-digit footnote blocks.

    A block is a run of >= ``_MIN_CIRCLED_NOTE_RUN`` segments that lead with a
    bare circled digit whose numbers increase (allowing a small gap for a missed
    marker); the marker-less segments between entries are their continuations and
    are included in the block span.  Runs shorter than the threshold -- a lone
    circled item, an enumerated pair -- are left alone.
    """

    candidates: List[Tuple[int, int]] = []
    for index, text in enumerate(texts):
        match = _CIRCLED_NOTE_MARKER.match(text)
        if match is not None:
            candidates.append((index, ord(match.group(1)) - 0x2460 + 1))
    note_indices: set[int] = set()
    position = 0
    while position < len(candidates):
        run = [candidates[position]]
        following = position + 1
        while following < len(candidates):
            previous_number = run[-1][1]
            number = candidates[following][1]
            if previous_number < number <= previous_number + 3:
                run.append(candidates[following])
                following += 1
            else:
                break
        if len(run) >= _MIN_CIRCLED_NOTE_RUN:
            note_indices.update(range(run[0][0], run[-1][0] + 1))
        position = following
    return note_indices


def demote_noise_carried_links(
    links: Sequence,
    source_texts: Sequence[str],
    target_texts: Sequence[str],
) -> List:
    """Reject accepted links whose entire pivot or target side is noise.

    A body match presented on the strength of a lone symbol segment (e.g. an OCR
    page-layout ``|`` paired with real paragraphs, R14 P2512) is spurious: the
    counterpart paragraphs have no genuine parallel here.  Such a link is demoted
    to ``rejected`` with its anchor key cleared, so it is preserved for
    inspection but never offered as a location.  This is a segment-quality gate
    on the alignment output; the frozen DP is untouched.
    """

    demoted: List = []
    for link in links:
        # Only body matches are gated.  Note-channel links (``note_automatic``)
        # legitimately pair short markers such as a bare "1", so they pass.
        if link.review_status == "automatic":
            source_segments = source_texts[link.source_start : link.source_end]
            target_segments = target_texts[link.target_start : link.target_end]
            source_all_noise = bool(source_segments) and all(
                _is_low_information_segment(text) for text in source_segments
            )
            target_all_noise = bool(target_segments) and all(
                _is_low_information_segment(text) for text in target_segments
            )
            if source_all_noise or target_all_noise:
                demoted.append(
                    replace(link, review_status="rejected", anchor_key="")
                )
                continue
        demoted.append(link)
    return demoted


def demote_note_block_links(
    links: Sequence,
    source_texts: Sequence[str],
    target_texts: Sequence[str],
) -> List:
    """Reject body links carried entirely by a footnote block.

    A footnote block (R2 P3461-3472: circled-digit entries plus continuations)
    that survives in the body range would otherwise be matched to the other
    edition's body paragraphs (R2/n33, conf 0.841).  A body ``automatic`` link
    whose whole pivot or target side falls inside such a block is demoted to
    ``rejected`` -- notes belong to the note channel, not to body location.  Only
    body links are gated; ``note_automatic`` pairings pass through.
    """

    source_notes = _circled_note_block_indices(source_texts)
    target_notes = _circled_note_block_indices(target_texts)
    if not source_notes and not target_notes:
        return list(links)
    demoted: List = []
    for link in links:
        if link.review_status == "automatic":
            source_all_notes = link.source_end > link.source_start and all(
                index in source_notes
                for index in range(link.source_start, link.source_end)
            )
            target_all_notes = link.target_end > link.target_start and all(
                index in target_notes
                for index in range(link.target_start, link.target_end)
            )
            if source_all_notes or target_all_notes:
                demoted.append(
                    replace(link, review_status="rejected", anchor_key="")
                )
                continue
        demoted.append(link)
    return demoted

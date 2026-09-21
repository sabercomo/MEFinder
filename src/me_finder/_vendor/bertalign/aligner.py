# Vendored from Bertalign @ df8c63f51aa203faed9f2fe45ae39e6fca75e667 (GPLv3).
# Modifications vs upstream (see MODIFICATIONS.md):
#   * __init__ takes a pre-built Encoder and *already-segmented* sentence lists
#     plus language codes. Upstream ran clean_text -> detect_lang (googletrans,
#     network) -> split_sents (sentence-splitter, re-splitting) and imported a
#     global ``model``. MEFinder supplies located segments and languages, so all
#     of that is removed; the caller owns segmentation and model loading.
#   * ``from bertalign.corelib import *`` is replaced with explicit imports (no
#     wildcard) to keep the lint surface clean.
#   * Dropped the stdout ``print`` progress lines and the unused ``print_sents``
#     helper.
# The two-stage scheduling in ``align_sents`` (first pass top-k 1-1, second pass
# m-n with margin + length penalty, then back-track) is the upstream algorithm.
import numpy as np

from .corelib import (
    find_first_search_path,
    find_second_search_path,
    find_top_k_sents,
    first_back_track,
    first_pass_align,
    get_alignment_types,
    second_back_track,
    second_pass_align,
)


class Bertalign:
    def __init__(self,
                 encoder,
                 src_sents,
                 tgt_sents,
                 *,
                 src_lang="und",
                 tgt_lang="und",
                 max_align=5,
                 top_k=3,
                 win=5,
                 skip=-0.1,
                 margin=True,
                 len_penalty=True,
               ):

        self.max_align = max_align
        self.top_k = top_k
        self.win = win
        self.skip = skip
        self.margin = margin
        self.len_penalty = len_penalty

        # MEFinder passes located segments verbatim: no clean_text, no
        # detect_lang, no split_sents. One input sentence stays one column, so
        # the returned bead indices map straight back to MEFinder segments.
        src_sents = list(src_sents)
        tgt_sents = list(tgt_sents)

        src_num = len(src_sents)
        tgt_num = len(tgt_sents)

        src_vecs, src_lens = encoder.transform(src_sents, max_align - 1)
        tgt_vecs, tgt_lens = encoder.transform(tgt_sents, max_align - 1)

        char_ratio = np.sum(src_lens[0,]) / np.sum(tgt_lens[0,])

        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self.src_sents = src_sents
        self.tgt_sents = tgt_sents
        self.src_num = src_num
        self.tgt_num = tgt_num
        self.src_lens = src_lens
        self.tgt_lens = tgt_lens
        self.char_ratio = char_ratio
        self.src_vecs = src_vecs
        self.tgt_vecs = tgt_vecs

    def align_sents(self):
        D, I = find_top_k_sents(self.src_vecs[0,:], self.tgt_vecs[0,:], k=self.top_k)
        first_alignment_types = get_alignment_types(2) # 0-1, 1-0, 1-1
        first_w, first_path = find_first_search_path(self.src_num, self.tgt_num)
        first_pointers = first_pass_align(self.src_num, self.tgt_num, first_w, first_path, first_alignment_types, D, I)
        first_alignment = first_back_track(self.src_num, self.tgt_num, first_pointers, first_path, first_alignment_types)

        second_alignment_types = get_alignment_types(self.max_align)
        second_w, second_path = find_second_search_path(first_alignment, self.win, self.src_num, self.tgt_num)
        second_pointers = second_pass_align(self.src_vecs, self.tgt_vecs, self.src_lens, self.tgt_lens,
                                            second_w, second_path, second_alignment_types,
                                            self.char_ratio, self.skip, margin=self.margin, len_penalty=self.len_penalty)
        second_alignment = second_back_track(self.src_num, self.tgt_num, second_pointers, second_path, second_alignment_types)

        self.result = second_alignment
        return second_alignment

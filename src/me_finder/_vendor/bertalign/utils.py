# Vendored from Bertalign @ df8c63f51aa203faed9f2fe45ae39e6fca75e667 (GPLv3).
# Modifications vs upstream (see MODIFICATIONS.md):
#   * Removed ``detect_lang`` (used googletrans -> network) and ``clean_text``,
#     ``split_sents`` / ``_split_zh`` (used sentence-splitter to RE-SPLIT text).
#     MEFinder feeds its own already-segmented, already-located segments, so no
#     re-splitting and no language auto-detection happen here; the languages are
#     supplied by MEFinder. This also drops the googletrans and sentence-splitter
#     dependencies and the ``LANG`` table they needed.
#   * Kept only the grouped-overlap helpers, byte-for-byte from upstream: they
#     build the m-n candidate spans the two-stage DP scores.


def yield_overlaps(lines, num_overlaps):
    lines = [_preprocess_line(line) for line in lines]
    for overlap in range(1, num_overlaps + 1):
        for out_line in _layer(lines, overlap):
            # check must be here so all outputs are unique
            out_line2 = out_line[:10000]  # limit line so dont encode arbitrarily long sentences
            yield out_line2

def _layer(lines, num_overlaps, comb=' '):
    if num_overlaps < 1:
        raise Exception('num_overlaps must be >= 1')
    out = ['PAD', ] * min(num_overlaps - 1, len(lines))
    for ii in range(len(lines) - num_overlaps + 1):
        out.append(comb.join(lines[ii:ii + num_overlaps]))
    return out

def _preprocess_line(line):
    line = line.strip()
    if len(line) == 0:
        line = 'BLANK_LINE'
    return line

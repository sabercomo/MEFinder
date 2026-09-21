# Bertalign — vendored source and modifications

- **Upstream**: https://github.com/bfsujason/bertalign
- **Pinned commit**: `df8c63f51aa203faed9f2fe45ae39e6fca75e667` ("Update requirements.txt")
- **Upstream license**: GNU GPL v3 (`LICENSE` in this directory, copied from the
  upstream `LICENCE` file verbatim).
- **How MEFinder ships it**: MEFinder is AGPL-3.0-only. GPLv3 source combined
  into an AGPLv3 work is license-compatible. The upstream copyright and license
  are preserved here; see `THIRD_PARTY_NOTICES.txt` for the distribution notice.

Only the files MEFinder actually uses are vendored. `eval.py` is not vendored
(evaluation harness, unused). The changes below are the complete diff relative
to the pinned commit.

## `__init__.py`
- **Removed the import-time model instantiation** (`model = Encoder("LaBSE")`).
  Upstream downloaded/loaded LaBSE as a side-effect of importing the package.
  MEFinder must not download at import or compute time and must load a *local*
  model inside the isolated compute process, so the global model is gone. Kept
  `__author__` / `__version__`; added `__upstream_commit__`.

## `utils.py`
- **Removed `detect_lang`** (used `googletrans`, i.e. a Google network call).
  MEFinder supplies language codes; no auto-detection, no network.
- **Removed `clean_text`, `split_sents`, `_split_zh`** (used `sentence-splitter`
  to re-split text into sentences) and the `LANG` table they needed. MEFinder
  feeds already-segmented, already-located segments; re-splitting would change
  indices and break locating, so it is not done here.
- **Kept `yield_overlaps`, `_layer`, `_preprocess_line` byte-for-byte** — the
  grouped-overlap span builder the two-stage DP scores.
- Net dependency effect: `googletrans` and `sentence-splitter` are no longer
  needed by the backend.

## `encoder.py`
- **`Encoder.__init__(model_name_or_path, device="cpu")`** instead of
  `Encoder(model_name)`: the caller loads a resolved local LaBSE directory on a
  chosen device (CPU by default). No behaviour change to embeddings.
- **`transform` unchanged**: grouped-overlap concatenation → `SentenceTransformer.encode`
  → reshape to `(num_overlaps, num_sents, dim)` + per-overlap UTF-8 byte-length
  vectors. This is the upstream grouped-embedding logic.
- Import of `yield_overlaps` retargeted to the vendored path.

## `corelib.py`
- **`find_top_k_sents`: CPU-only.** Upstream chose a faiss **GPU** index when
  `torch.cuda.is_available() and platform == 'linux'`. That branch is removed so
  the backend never needs CUDA or `faiss-gpu`; it always uses
  `faiss.IndexFlatIP` on CPU (exact inner product — identical result to the
  upstream CPU path). The now-unused `torch` and `from sys import platform`
  imports are dropped.
- **Everything else is byte-for-byte upstream**: `first_pass_align`,
  `second_pass_align`, `calculate_similarity_score` (incl. the `margin`
  neighbour term), `calculate_length_penalty`, `find_first_search_path`,
  `find_second_search_path`, `first_back_track`, `second_back_track`,
  `get_alignment_types`, `nb_dot` — the numba two-stage DP and scoring.

## `aligner.py`
- **`Bertalign.__init__(encoder, src_sents, tgt_sents, *, src_lang, tgt_lang, …)`**:
  takes a pre-built `Encoder` and *already-segmented* sentence lists plus
  language codes. Removed `clean_text` → `detect_lang` → `split_sents` and the
  global `model` import. MEFinder owns segmentation, locating and model loading.
- **`from bertalign.corelib import *` → explicit imports** (no wildcard), to
  keep the lint surface clean.
- **Dropped the stdout `print` progress lines and the unused `print_sents` /
  `_get_line` helpers.** `align_sents` now `return`s the result in addition to
  storing `self.result`.
- **`align_sents` scheduling is the upstream algorithm**: first pass (top-k 1-1)
  → build second search path → second pass (m-n, margin + length penalty) →
  back-track.

## Parameters
Upstream defaults are preserved: `max_align=5, top_k=3, win=5, skip=-0.1,
margin=True, len_penalty=True`. LaBSE remains the model. No re-tuning.

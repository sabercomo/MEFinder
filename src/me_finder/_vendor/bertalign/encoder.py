# Vendored from Bertalign @ df8c63f51aa203faed9f2fe45ae39e6fca75e667 (GPLv3).
# Modifications vs upstream (see MODIFICATIONS.md):
#   * Encoder.__init__ takes an explicit model path/name plus a device, so the
#     caller loads a *local* LaBSE snapshot in the isolated compute process.
#     Upstream took only ``model_name`` and was instantiated at package import
#     (a network download side-effect). MEFinder never downloads here; the model
#     is provisioned beforehand through the managed-component mechanism and this
#     process runs with HF offline flags set.
#   * ``transform`` is unchanged: grouped-overlap concatenation + LaBSE encoding
#     + per-overlap byte-length vectors (the upstream grouped-embedding logic).
import numpy as np

from sentence_transformers import SentenceTransformer

from .utils import yield_overlaps


class Encoder:
    def __init__(self, model_name_or_path, device="cpu"):
        # A local directory path loads without any network access; passing a
        # bare model name would only work if it were already in the HF cache and
        # offline flags allow reading it. The MEFinder adapter always passes a
        # resolved local path.
        self.model = SentenceTransformer(model_name_or_path, device=device, local_files_only=True)
        self.model_name = str(model_name_or_path)

    def transform(self, sents, num_overlaps):
        overlaps = []
        for line in yield_overlaps(sents, num_overlaps):
            overlaps.append(line)

        sent_vecs = self.model.encode(overlaps)
        embedding_dim = sent_vecs.size // (len(sents) * num_overlaps)
        sent_vecs.resize(num_overlaps, len(sents), embedding_dim)

        len_vecs = [len(line.encode("utf-8")) for line in overlaps]
        len_vecs = np.array(len_vecs)
        len_vecs.resize(num_overlaps, len(sents))

        return sent_vecs, len_vecs

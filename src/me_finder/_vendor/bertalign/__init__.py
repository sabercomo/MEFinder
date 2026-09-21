"""Vendored Bertalign (https://github.com/bfsujason/bertalign).

Upstream commit: df8c63f51aa203faed9f2fe45ae39e6fca75e667
Upstream license: GNU GPL v3 (see ``LICENSE`` in this directory).

MEFinder distributes this under AGPL-3.0-only; GPLv3 code is compatible when
combined into an AGPLv3 whole. See ``MODIFICATIONS.md`` for the exact diffs
relative to the upstream commit.

Unlike upstream, importing this package does **not** instantiate a
``SentenceTransformer`` model. Upstream's ``__init__`` created a global
``model = Encoder("LaBSE")`` at import time, which triggers a network model
download. MEFinder loads the model explicitly, from a local path, inside the
isolated alignment compute process only (see :class:`bertalign.encoder.Encoder`
and the MEFinder ``bertalign_backend`` adapter). This keeps import side-effect
free and honours the local-first, no-implicit-download rule.
"""

__author__ = "Jason (bfsujason@163.com)"
__version__ = "1.1.0"
__upstream_commit__ = "df8c63f51aa203faed9f2fe45ae39e6fca75e667"

"""Single source of truth for slimming the desktop main package (phase 2C).

The alignment *compute* phase (embeddings + monotonic alignment) is the only
part of the application that needs the numeric stack (NumPy / ONNX Runtime /
fastembed and their heavy transitive dependencies). Phase 2A moved that phase
into a subprocess; phase 2B made an isolated, on-demand ``uv`` runtime own the
stack. Phase 2C removes the stack from the *main* package so the shipped app no
longer carries ~94 MiB of compute dependencies it never imports in-process.

Two facts make this safe and are pinned by tests:

* Every main-process import of the numeric stack is lazy (function-local) or
  guarded by ``TYPE_CHECKING`` — see ``tests/test_slim_main_package.py`` and the
  runtime proof in ``tests/test_core_without_alignment.py``. PyInstaller follows
  those imports *statically* regardless, so the only way to keep them out of the
  bundle is an explicit ``excludes`` list; that is ``ALIGNMENT_COMPUTE_STACK``.
* The compute worker runs under a *different* interpreter (the independent
  runtime's venv, or — as a development fallback — the app's own interpreter),
  so it needs the pure-Python ``me_finder`` source as real ``.py`` files on
  disk, not compiled into the app's PYZ. ``worker_source_datas`` ships that
  source tree next to the frozen app (see ``default_worker_context``).

Both desktop specs (``packaging/desktop.spec``,
``packaging/desktop_macos.spec``) import from here so the exclude list and the
worker-source delivery never drift between platforms.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

# Modules excluded from the main desktop package. The first group is the compute
# stack itself; the second is its heavy transitive dependencies (pulled in only
# by ONNX Runtime / fastembed) that nothing else in the app imports — verified
# zero-reference across ``src/me_finder`` so excluding them cannot break a
# retained path. The independent alignment runtime installs these into its own
# venv; the app never imports them in-process.
ALIGNMENT_COMPUTE_STACK: Tuple[str, ...] = (
    "numpy",
    "onnxruntime",
    "fastembed",
    "tokenizers",
    "huggingface_hub",
    "hf_xet",
    "py_rust_stemmers",
    "sympy",
    "mpmath",
    "flatbuffers",
    "coloredlogs",
    "humanfriendly",
)

# Where the pure-Python worker source is shipped inside the frozen bundle, and
# the module an external interpreter runs. ``default_worker_context`` resolves
# ``sys._MEIPASS`` (the bundle's data root) as the source root, so the worker is
# launched as ``python -m me_finder.alignment_compute_worker`` with that root on
# ``PYTHONPATH``.
WORKER_SOURCE_DEST_ROOT = "me_finder"
FROZEN_WORKER_MODULE = "me_finder.alignment_compute_worker"


def worker_source_datas(project_root: Path) -> List[Tuple[str, str]]:
    """PyInstaller ``datas`` entries shipping the pure-Python ``me_finder`` source.

    Returns ``(absolute_source_file, dest_directory)`` pairs for every ``.py``
    file under ``src/me_finder`` (excluding bytecode caches), remapped to a
    top-level ``me_finder/`` tree in the bundle. The tree is shipped verbatim as
    data (PyInstaller does not analyse ``datas``), so it never re-introduces the
    excluded numeric stack into the main graph; it exists only so the compute
    worker's interpreter can import ``me_finder`` from ``sys._MEIPASS``.
    """

    package_dir = Path(project_root) / "src" / "me_finder"
    entries: List[Tuple[str, str]] = []
    for path in sorted(package_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative_parent = path.parent.relative_to(package_dir)
        dest = Path(WORKER_SOURCE_DEST_ROOT)
        if relative_parent != Path("."):
            dest = dest / relative_parent
        entries.append((str(path), str(dest)))
    return entries

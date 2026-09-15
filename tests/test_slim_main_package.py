"""Phase 2C: the main desktop package must not carry the alignment compute stack.

These tests pin the packaging invariants that make the slim main package safe:

* both desktop specs exclude the numeric stack and ship the pure-Python worker
  source (so the exclude list and the source delivery never drift between
  platforms — the historical failure mode for cross-platform packaging);
* every main-process module that the app imports at load loads *without* the
  numeric stack (the runtime justification for the exclude);
* the shipped worker-source tree is a complete, importable ``me_finder`` package
  runnable as ``python -m me_finder.alignment_compute_worker`` by an external
  interpreter; and
* ``default_worker_context`` resolves that tree from ``sys._MEIPASS`` when
  frozen, so the launch path agrees with where the specs put the source.

The end-to-end proof that a *built* bundle omits the stack lives in the 2C
report (a real clean build); these tests are the fast, platform-independent
guard that keeps the wiring correct between builds.
"""

from __future__ import annotations

import ast
import importlib
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.slim_main_package import (
    ALIGNMENT_COMPUTE_STACK,
    FROZEN_WORKER_MODULE,
    WORKER_SOURCE_DEST_ROOT,
    worker_source_datas,
)

ROOT = Path(__file__).resolve().parents[1]


class SpecWiringTests(unittest.TestCase):
    """Both specs import the shared slimming helpers and use them."""

    def setUp(self) -> None:
        self.macos_spec = (ROOT / "packaging" / "desktop_macos.spec").read_text(
            encoding="utf-8"
        )
        self.windows_spec = (ROOT / "packaging" / "desktop.spec").read_text(
            encoding="utf-8"
        )

    def test_both_specs_exclude_the_compute_stack(self) -> None:
        for spec in (self.macos_spec, self.windows_spec):
            self.assertIn("from tools.slim_main_package import", spec)
            self.assertIn("ALIGNMENT_COMPUTE_STACK", spec)
            # Splatted into the excludes list, not merely imported.
            self.assertIn("*ALIGNMENT_COMPUTE_STACK", spec)

    def test_both_specs_ship_the_worker_source(self) -> None:
        for spec in (self.macos_spec, self.windows_spec):
            self.assertIn("*worker_source_datas(", spec)

    def test_compute_stack_names_are_the_known_removable_set(self) -> None:
        # Guard against silently dropping a name from the single source of truth;
        # the numeric core must always be present.
        for required in ("numpy", "onnxruntime", "fastembed"):
            self.assertIn(required, ALIGNMENT_COMPUTE_STACK)


class WorkerSourceDatasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.datas = worker_source_datas(ROOT)
        self.dests = {dest for _src, dest in self.datas}
        self.sources = {src for src, _dest in self.datas}

    def test_ships_the_worker_and_its_transitive_compute_modules(self) -> None:
        shipped = {Path(src).name for src in self.sources}
        for expected in (
            "alignment_compute_worker.py",
            "alignment_compute.py",
            "semantic_alignment.py",
            "text_alignment.py",
            "embedding_models.py",
            "managed_embedding_models.py",
        ):
            self.assertIn(expected, shipped)

    def test_dest_tree_is_top_level_me_finder(self) -> None:
        # The worker is launched as ``me_finder.alignment_compute_worker``; every
        # dest must live under the ``me_finder`` top-level tree.
        self.assertEqual(FROZEN_WORKER_MODULE.split(".")[0], WORKER_SOURCE_DEST_ROOT)
        for dest in self.dests:
            self.assertTrue(
                dest == WORKER_SOURCE_DEST_ROOT
                or dest.startswith(WORKER_SOURCE_DEST_ROOT + "/")
                or dest.startswith(WORKER_SOURCE_DEST_ROOT + "\\"),
                dest,
            )
        # Subpackages are preserved (relative imports must resolve).
        self.assertTrue(
            any(dest.endswith("persistence") for dest in self.dests),
            self.dests,
        )
        self.assertTrue(
            any(dest.endswith("application") for dest in self.dests),
            self.dests,
        )

    def test_never_ships_bytecode_caches(self) -> None:
        for src in self.sources:
            self.assertNotIn("__pycache__", Path(src).parts)


class MainPathLoadsWithoutStackTests(unittest.TestCase):
    """Every module the app imports at load must import without the numeric stack.

    This is the runtime justification for excluding the stack from the bundle: a
    module-level (non-lazy) compute import on a main path would crash the slim
    app at startup. Complements ``test_core_without_alignment`` (a full HTTP
    smoke) with a fast, direct in-process guard.
    """

    def test_key_main_modules_import_without_numeric_stack(self) -> None:
        script = (
            "import importlib, importlib.abc, sys\n"
            "STACK = set(%r)\n"
            "class NoStack(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in STACK:\n"
            "            raise ModuleNotFoundError(name, name=name)\n"
            "sys.meta_path.insert(0, NoStack())\n"
            "mods = [\n"
            "    'src.me_finder.web_runtime',\n"
            "    'src.me_finder.managed_component_assembly',\n"
            "    'src.me_finder.managed_alignment_runtime',\n"
            "    'src.me_finder.managed_embedding_models',\n"
            "    'src.me_finder.alignment_compute',\n"
            "    'src.me_finder.application.text_alignment_coordinator',\n"
            "    'src.me_finder.text_alignment',\n"
            "    'src.me_finder.semantic_alignment',\n"
            "]\n"
            "for m in mods:\n"
            "    importlib.import_module(m)\n"
            "assert not any(n in sys.modules for n in STACK), "
            "[n for n in STACK if n in sys.modules]\n"
        ) % (tuple(ALIGNMENT_COMPUTE_STACK),)
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ShippedWorkerSourceIsImportableTests(unittest.TestCase):
    """The shipped tree is a self-contained, runnable ``me_finder`` package.

    Copies exactly what ``worker_source_datas`` would ship into a scratch
    ``me_finder/`` tree and runs ``python -m me_finder.alignment_compute_worker
    --probe`` against it with only that tree on ``PYTHONPATH`` (no ``src`` on the
    path). This proves the frozen-source-delivery contract: an external
    interpreter can import the worker as the top-level ``me_finder`` package with
    every relative import intact. (This interpreter has the numeric stack, so the
    probe reports it present; the *stack-absent* interpreter is covered by the
    independent-runtime install/verify path and the real build.)
    """

    def test_probe_runs_from_shipped_tree(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for src, dest in worker_source_datas(ROOT):
                target_dir = root / dest
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target_dir / Path(src).name)
            control = root / "control.ndjson"
            control.write_text("", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    FROZEN_WORKER_MODULE,
                    "--probe",
                    str(control),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=root,
                env={**_clean_env(), "PYTHONPATH": str(root)},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            messages = [
                line for line in control.read_text(encoding="utf-8").splitlines() if line.strip()
            ]
            self.assertTrue(messages, "worker emitted no control messages")
            self.assertIn('"type": "hello"'.replace(" ", ""), messages[0].replace(" ", ""))


class SlimModelDownloadGuardTests(unittest.TestCase):
    """A slim main package cannot download a model in-process.

    With no independent runtime installed and no bundled numeric stack, the model
    download must fail with a clear, actionable reason (install the runtime) — not
    a raw ImportError from attempting an impossible in-process fastembed load, and
    never a silent no-op. When the stack *is* bundled (development), the
    in-process fallback still runs.
    """

    def test_download_without_runtime_or_stack_raises_actionable_error(self) -> None:
        mod = importlib.import_module("src.me_finder.managed_alignment_runtime")
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloader = mod.make_model_downloader(root)
            # No runtime installed under root ⇒ resolve_installed_runtime_launch
            # returns None; force the "no bundled stack" (slim) condition.
            with mock.patch.object(mod, "_builtin_stack_present", return_value=False):
                with self.assertRaises(mod.ManagedAlignmentRuntimeError) as ctx:
                    downloader("intfloat/multilingual-e5-small", root / "models")
        self.assertIn("运行时未安装", str(ctx.exception))

    def test_download_falls_back_in_process_when_stack_bundled(self) -> None:
        mod = importlib.import_module("src.me_finder.managed_alignment_runtime")
        import tempfile

        calls = []
        fake = types_module_with_download(calls)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloader = mod.make_model_downloader(root)
            with mock.patch.object(mod, "_builtin_stack_present", return_value=True), \
                    mock.patch.dict(
                        sys.modules,
                        {"src.me_finder.managed_embedding_models": fake},
                    ):
                downloader("model-x", root / "models")
        self.assertEqual(calls, [("model-x", str(root / "models"))])


def types_module_with_download(calls):
    import types

    module = types.ModuleType("src.me_finder.managed_embedding_models")

    def download_embedding_model(model_id, cache_dir):
        calls.append((model_id, str(cache_dir)))

    module.download_embedding_model = download_embedding_model
    return module


class WorkerContextResolutionTests(unittest.TestCase):
    def test_frozen_worker_context_resolves_meipass(self) -> None:
        mod = importlib.import_module("src.me_finder.managed_alignment_runtime")
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "_MEIPASS", "/opt/mefinder/data", create=True
        ):
            module, source_root = mod.default_worker_context()
        self.assertEqual(module, FROZEN_WORKER_MODULE)
        self.assertEqual(source_root, Path("/opt/mefinder/data"))

    def test_development_worker_context_uses_src_layout(self) -> None:
        mod = importlib.import_module("src.me_finder.managed_alignment_runtime")
        # Not frozen in the test process.
        module, source_root = mod.default_worker_context()
        self.assertEqual(module, "src.me_finder.alignment_compute_worker")
        self.assertEqual(source_root, ROOT)


def _clean_env() -> dict:
    import os

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


if __name__ == "__main__":
    unittest.main()

"""Diagnostic unittest runner: verbose names + a timed faulthandler dump.

Purpose (temporary, CI diagnostics): the Windows gate has stalled for the full
``timeout-minutes`` without unittest surfacing which case hangs, because the
runner buffers and GitHub's hard cancel produces no stack. This wrapper streams
each test name (verbosity=2) *before* it runs and arms
``faulthandler.dump_traceback_later`` so, if the suite exceeds the deadline, all
thread stacks are dumped and the process exits *before* the CI hard kill — the
log then shows both the last-started test and its live stack.

It only observes; it never skips a test, relaxes a lock, or changes semantics.

Usage:
    python scripts/run_tests_with_faulthandler.py [-t . -s tests ...]

The dump deadline (seconds) comes from ``$MEFINDER_FAULTHANDLER_SECONDS`` and
defaults to 1000 (below the 1200s / 20-minute CI timeout). Passing no unittest
arguments defaults to ``discover -t . -s tests``.
"""

from __future__ import annotations

import faulthandler
import os
import runpy
import sys


def main() -> None:
    # `python -m unittest` runs with cwd on sys.path; a plain script invocation
    # does not, so add it back for both discovery (-t .) and dotted module names.
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    seconds = float(os.environ.get("MEFINDER_FAULTHANDLER_SECONDS", "1000"))
    # Dump every thread's stack after `seconds`, then hard-exit, so the stuck
    # case's stack lands in the log ahead of the CI runner's own cancellation.
    faulthandler.dump_traceback_later(seconds, repeat=False, exit=True)

    unittest_args = sys.argv[1:] or ["discover", "-t", ".", "-s", "tests"]
    if "-v" not in unittest_args and "--verbose" not in unittest_args:
        unittest_args.append("-v")

    # Reproduce `python -m unittest <args>` so discovery and reporting are stock.
    sys.argv = ["python -m unittest", *unittest_args]
    runpy.run_module("unittest", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()

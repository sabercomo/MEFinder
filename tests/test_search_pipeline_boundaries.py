"""Boundary guard for the split search pipeline modules.

The pipeline is one-way: ``search/engine.py`` (facade) orchestrates recall,
scoring and assembly; assembly consumes anchors, cross-page narrowing and
citation helpers; recall only needs scoring's fuzzy-window helper plus its two
leaf sub-stages (``recall_memory``, ``recall_passages``).  Stages must never
import the facade or reach backwards across the pipeline — this keeps every
stage testable and the frozen response contract reviewable in one place.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

SRC = Path(__file__).resolve().parents[1] / "src" / "me_finder" / "search"

# Stage modules of the search package each pipeline module may import.
FACADE_ALLOWED = {"assembly", "citation", "contract", "recall", "scoring"}
ALLOWED: dict[str, set[str]] = {
    "contract": set(),
    "anchors": set(),
    "citation": set(),
    "cross_page": {"anchors"},
    "scoring": {"anchors"},
    "recall_memory": set(),
    "recall_passages": {"contract"},
    "recall": {
        "contract",
        "recall_memory",
        "recall_passages",
        "scoring",
    },
    "assembly": {
        "anchors",
        "citation",
        "cross_page",
        "scoring",
    },
    "engine": FACADE_ALLOWED,
}


def internal_imports(module: str) -> set[str]:
    """Sibling pipeline stages (``from .stage import``) declared by one module."""

    tree = ast.parse((SRC / f"{module}.py").read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            if node.module in ALLOWED:
                found.add(node.module)
    return found


class SearchPipelineBoundaryTests(unittest.TestCase):
    def test_stage_imports_stay_one_way(self) -> None:
        for module, allowed in ALLOWED.items():
            imported = internal_imports(module)
            self.assertEqual(
                imported - allowed,
                set(),
                f"{module}.py imports outside its allowed stage set: {sorted(imported - allowed)}",
            )
            self.assertEqual(
                allowed - imported,
                set(),
                f"{module}.py no longer uses {sorted(allowed - imported)}; update the boundary table",
            )

    def test_no_stage_imports_the_facade(self) -> None:
        for module in ALLOWED:
            if module == "engine":
                continue
            self.assertNotIn(
                "engine",
                internal_imports(module),
                f"{module}.py must not import the search facade",
            )


if __name__ == "__main__":
    unittest.main()

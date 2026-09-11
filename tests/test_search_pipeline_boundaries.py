"""Boundary guard for the split search pipeline modules.

The pipeline is one-way: ``search.py`` (facade) orchestrates recall, scoring
and assembly; assembly consumes anchors and citation helpers; recall only
needs scoring's fuzzy-window helper.  Stages must never import the facade or
reach backwards across the pipeline — this keeps every stage testable and the
frozen response contract reviewable in one place.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

SRC = Path(__file__).resolve().parents[1] / "src" / "me_finder"

# Lowercase module names each pipeline module may import internally.
FACADE_ALLOWED = {"search_assembly", "search_citation", "search_contract", "search_recall", "search_scoring"}
ALLOWED: dict[str, set[str]] = {
    "search_contract": set(),
    "search_anchors": set(),
    "search_citation": set(),
    "search_scoring": {"search_anchors"},
    "search_recall": {"search_contract", "search_scoring"},
    "search_assembly": {"search_anchors", "search_citation", "search_scoring"},
    "search": FACADE_ALLOWED,
}


def internal_imports(module: str) -> set[str]:
    """Relative ``.search_*`` imports declared by one pipeline module."""

    tree = ast.parse((SRC / f"{module}.py").read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level >= 1 and node.module:
            if node.module == "search" or node.module.startswith("search_"):
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
            if module == "search":
                continue
            self.assertNotIn(
                "search",
                internal_imports(module),
                f"{module}.py must not import the search facade",
            )


if __name__ == "__main__":
    unittest.main()

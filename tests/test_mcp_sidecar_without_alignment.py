"""Phase 2E: the MCP sidecar must read stored alignments without the compute stack.

The sidecar only *reads* persisted alignment results (recall + locate over the
index DB); it never runs the embedding / monotonic-alignment compute path. This
is the runtime justification for excluding ``ALIGNMENT_COMPUTE_STACK`` from
``packaging/mcp_sidecar.spec`` (the packaging invariant itself is pinned by
``tests/test_slim_main_package.py``).

The proof runs the real MCP tool dispatch (``_call_tool``) in a subprocess whose
import system forbids ``numpy`` / ``fastembed`` / ``onnxruntime``, exercising the
full stored-alignment read chain
(``mcp_server`` -> ``parallel_passage_service`` -> ``text_alignment`` read
symbols) and asserting the stack was never imported.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class SidecarSpecWiringTests(unittest.TestCase):
    """The sidecar spec excludes the compute stack via the shared source of truth."""

    def setUp(self) -> None:
        self.spec = (
            Path(__file__).resolve().parents[1] / "packaging" / "mcp_sidecar.spec"
        ).read_text(encoding="utf-8")

    def test_sidecar_spec_excludes_the_compute_stack(self) -> None:
        # Same single source of truth as the desktop specs, splatted into excludes
        # rather than merely imported — so the sidecar can never re-acquire the
        # stack when tools/slim_main_package.py changes.
        self.assertIn("from tools.slim_main_package import ALIGNMENT_COMPUTE_STACK", self.spec)
        self.assertIn("*ALIGNMENT_COMPUTE_STACK", self.spec)


class MCPSidecarWithoutAlignmentTests(unittest.TestCase):
    def test_stored_alignment_read_chain_runs_without_compute_stack(self) -> None:
        script = r'''
import importlib.abc, sys, tempfile
from pathlib import Path
class NoCompute(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'numpy', 'fastembed', 'onnxruntime'}:
            raise ModuleNotFoundError(fullname, name=fullname)
sys.meta_path.insert(0, NoCompute())

from src.me_finder.application import LiteratureVerificationService
from src.me_finder.mcp_server import _call_tool
from tests.mcp_v1_fixture import (
    CALIBRATED_QUOTE, PDF_SOURCE_ID, PARALLEL_SOURCE_ID,
    build_mcp_v1_fixture, add_mcp_parallel_fixture,
)

root = Path(tempfile.mkdtemp()) / 'runtime'
db = root / 'data' / 'index.sqlite3'
build_mcp_v1_fixture(db, include_quality_cases=True)
add_mcp_parallel_fixture(db)
service = LiteratureVerificationService(lambda: root)

def call(tool_name, arguments):
    result = _call_tool(service, tool_name, arguments)
    assert not result.is_error, (tool_name, result.structured_content)
    return result.structured_content

# Reading a persisted cross-language alignment: the core sidecar read path.
parallel = call('find_parallel_passages', {
    'quote': CALIBRATED_QUOTE, 'mode': 'exact',
    'source_file_id': PDF_SOURCE_ID, 'target_source_file_id': PARALLEL_SOURCE_ID,
})
assert parallel['candidate_set_count'] == 1, parallel
english = parallel['correspondences'][0]['candidates'][1]['text']
assert english == 'Technical judgments must be supported by verifiable evidence.', parallel

# Locating a quote and reading manual corrections must also stay stack-free.
located = call('locate_quote', {'quote': CALIBRATED_QUOTE, 'mode': 'exact'})
assert located.get('matches'), located
corrections = call('list_alignment_corrections', {})
assert 'overrides' in corrections, corrections
docs = call('list_documents', {})
assert docs.get('documents'), docs

leaked = [n for n in ('numpy', 'fastembed', 'onnxruntime') if n in sys.modules]
assert not leaked, 'compute stack leaked into the sidecar read path: ' + repr(leaked)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=Path(__file__).resolve().parents[1],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

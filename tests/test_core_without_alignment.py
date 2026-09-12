"""Cold HTTP backend with compute imports forbidden, also runnable in a core-only venv.

The stored-link fixture is public synthetic data from performance_fixture (2
books, 20 paragraphs, 8 pair paragraphs), generated once through the alignment
seam. Loading it exercises reading results without regenerating them.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CoreWithoutAlignmentTests(unittest.TestCase):
    def test_http_search_read_stored_alignment_and_shutdown(self):
        script = r'''
import importlib.abc, json, sys, sqlite3, threading, urllib.request
from unittest.mock import patch
from pathlib import Path
class NoCompute(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'numpy', 'fastembed', 'onnxruntime'}:
            raise ModuleNotFoundError(fullname, name=fullname)
sys.meta_path.insert(0, NoCompute())
from scripts.performance_fixture import create_fixture
from src.me_finder.app_context import AppContext
from src.me_finder.web import make_handler
from src.me_finder.web import ManagedThreadingHTTPServer
from src.me_finder.text_alignment import locate_alignment
from src.me_finder.managed_embedding_models import ManagedEmbeddingModels
from src.me_finder.application.text_alignment_coordinator import TextAlignmentCoordinator, TextAlignmentFailed
from src.me_finder.semantic_alignment import cached_text_sequence_vectors, _sequence_cache_path
root=Path(sys.argv[1]); create_fixture(root,documents=2,paragraphs=20,alignment_paragraphs=8)
db=root/'data/index.sqlite3'
fixture=json.loads(Path('tests/fixtures/core-stored-alignment.json').read_text())
with sqlite3.connect(db) as connection:
    for table, rows in fixture.items():
        for row in rows:
            connection.execute('INSERT INTO '+table+' ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',list(row.values()))
app=AppContext.create(root,index_path=db)
handler=make_handler(db,app_context=app)
server=ManagedThreadingHTTPServer(('127.0.0.1',0),handler)
thread=threading.Thread(target=server.serve_forever);thread.start()
url='http://127.0.0.1:'+str(server.server_port)
try:
    request=urllib.request.Request(url+'/api/search',data=json.dumps({'query':'社会'}).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=10) as response:
        assert json.load(response)['total'] > 0
    with urllib.request.urlopen(url+'/reader-window',timeout=10) as response:
        assert b'reader-window-status' in response.read()
    with urllib.request.urlopen(url+'/api/text-alignments/targets?source_id=bench-002',timeout=10) as response:
        assert json.load(response)['targets']
    result=locate_alignment(db,'bench-002','bench-003',start_page_index=0,start_offset=0,end_page_index=0,end_offset=6)
    assert result['page_match_spans'],result
    assert not ManagedEmbeddingModels(root).summary()['models'][0]['installed']
    # Optional cached vector refinement must not disable stored-link navigation.
    cache=root/'vectors'; path=_sequence_cache_path(['fixture'],cache)
    path.parent.mkdir(parents=True);path.write_bytes(b'unread without numpy')
    assert cached_text_sequence_vectors(['fixture'],cache) is None
    coordinator=TextAlignmentCoordinator(app.paths,None,None)
    try:
        coordinator.generate('bench-pair','bench-002','bench-003',force=True)
    except TextAlignmentFailed as exc:
        assert '未安装' in str(exc)
    else:
        raise AssertionError('Generation must reject a missing component')
    with patch('src.me_finder.application.text_alignment_coordinator.model_component_installed',return_value=True), patch('src.me_finder.application.text_alignment_coordinator.find_spec',return_value=None):
        try:
            coordinator.generate('bench-pair','bench-002','bench-003',force=True)
        except TextAlignmentFailed as exc:
            assert '运行时未安装' in str(exc)
        else:
            raise AssertionError('Model files cannot substitute for compute dependencies')
finally:
    server.shutdown();thread.join(5);server.server_close()
    assert handler.close_runtime()
assert not any(name in sys.modules for name in ('numpy','fastembed','onnxruntime'))
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-B', '-c', script, directory],
                                    capture_output=True, text=True, timeout=30,
                                    cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

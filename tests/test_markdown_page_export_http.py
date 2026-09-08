"""HTTP page selection and original whole-document compatibility."""
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
import json
import tempfile
import threading
import unittest

from src.me_finder.app_context import AppContext
from src.me_finder.database import build_database
from src.me_finder.document_heading import DOCUMENT_HEADING_VERSION
from src.me_finder.web import make_handler
from tests.test_markdown_export import footnote_fixture


class MarkdownPageExportHttpTests(unittest.TestCase):
    def test_range_success_validation_whole_and_preserved_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'data/index.sqlite3'
            pages = footnote_fixture()
            for page in pages: page['source_file_id'] = 'book'
            build_database({'metadata':{},'source_files':[{'source_file_id':'book','source_type':'pdf',
                'file_name':'book.pdf','display_title':'測試書','document_heading_profile':{
                    'version':DOCUMENT_HEADING_VERSION,'status':'complete'}}],'pdf_pages':pages},database)
            before = database.read_bytes()
            handler = make_handler(database, app_context=AppContext.create(root,index_path=database))
            handler.log_message = lambda *_: None
            server = ThreadingHTTPServer(('127.0.0.1',0),handler)
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            opener = build_opener(ProxyHandler({}))
            def call(selection=None, include=False):
                payload = {'source_id':'book','output_dir':str(root)}
                if include: payload['page_selection'] = selection
                request = Request(f'http://127.0.0.1:{server.server_port}/api/document/export-markdown',
                    data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
                try:
                    with opener.open(request,timeout=10) as response:return response.status,json.loads(response.read())
                except HTTPError as e:return e.code,json.loads(e.read())
            try:
                status, full = call()
                self.assertEqual(status,200)
                whole = Path(full['path']).read_bytes()
                status, part = call({'mode':'printed','pages':'2'},True)
                self.assertEqual(status,200,part)
                self.assertEqual(part['page_count'],1)
                self.assertEqual(part['page_selection']['note_count'],1)
                self.assertIn('下一页重新出现',Path(part['path']).read_text(encoding='utf-8'))
                self.assertEqual(Path(full['path']).read_bytes(),whole)
                for invalid in (None,{}, {'mode':'printed','pages':'99'}, {'mode':'physical','pages':'0'},
                                {'mode':'printed','pages':'2-1'}):
                    with self.subTest(invalid=invalid):self.assertEqual(call(invalid,True)[0],400)
                self.assertEqual(database.read_bytes(),before)
            finally:
                server.shutdown();server.server_close();handler.close_runtime();thread.join(timeout=2)

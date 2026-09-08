"""Real SQLite/JSON and HTTP regressions for issue #16 (no private corpus)."""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

from src.me_finder.app_context import AppContext
from src.me_finder.application.search_service import SearchRequest, SearchService
from src.me_finder.application.script_search import execute_with_script_folding
from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.preferences import read_preferences, save_preferences
from src.me_finder.script_conversion import is_available
from src.me_finder.search import SearchEngine
from src.me_finder.web import make_handler


def make_index(texts, *, epub=False):
    source_type = 'word' if epub else 'pdf'
    paragraphs = []
    for i, text in enumerate(texts):
        paragraphs.append({
            'paragraph_id': f'p{i}', 'source_file_id': 'book', 'volume_id': 'v',
            'work_id': 'w', 'source_type': source_type, 'paragraph_index': i,
            'volume_number': 1, 'document_title': '測試書', 'work_title': '測試章', 'volume_display': '測試書',
            'source_format': 'epub' if epub else 'pdf', 'eligible_for_search': True,
            'text_raw': text, 'normalized_text': normalize_text(text),
            'compact_text': compact_text(text), 'plain_text': punctuationless_text(text),
            'pdf_page_start_index': i + 8, 'pdf_page_end_index': i + 8,
            'original_page_start': str(i + 1), 'style_name': 'p',
            'citation_page_start': str(i + 1), 'citation_page_end': str(i + 1),
            'page_display': f'第 {i+1} 页',
        })
    return {'metadata': {}, 'source_files': [
        {'source_file_id': 'book', 'source_type': source_type,
         'file_name': 'book.epub' if epub else 'book.pdf',
         'file_format': 'epub' if epub else 'pdf', 'display_title': '測試書'}],
        'volumes': [{'volume_id': 'v', 'source_file_id': 'book', 'source_type': source_type}],
        'works': [{'work_id': 'w', 'volume_id': 'v', 'source_type': source_type, 'title': '測試章'}],
        'paragraphs': paragraphs}


class _SearchCases:
    backend = 'sqlite'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ('index.sqlite3' if self.backend == 'sqlite' else 'index.json')

    def engine(self, texts):
        index = make_index(texts)
        if self.backend == 'sqlite':
            build_database(index, self.path)
        else:
            self.path.write_text(json.dumps(index), encoding='utf-8')
        engine = SearchEngine(self.path)
        self.addCleanup(engine.close)
        return engine

    def test_traditional_exact_hit_beats_original_script_fuzzy_hit(self):
        engine = self.engine(['剩余价值的概念在这里被仔细讨论。', '剩餘價值的概念在這裏被仔細討論。'])
        query = '剩余价值的概念在这里被仔细讨论'
        traditional = '剩餘價值的概念在這裏被仔細討論'
        # Limit source scope to the traditional paragraph by changing the first
        # query's last word: the original branch can only offer a fuzzy hit.
        with patch('src.me_finder.script_conversion.query_variants', return_value=[query+'了', traditional]):
            result = execute_with_script_folding(engine, SearchRequest(query=query+'了', limit=1))
        self.assertEqual(result['results'][0]['paragraph_id'], 'p1')
        self.assertEqual(result['results'][0]['match_type'], 'exact')

    def test_total_and_more_include_cross_script_truncation(self):
        engine = self.engine(['剩餘價值。'] * 3 + ['剩余价值。'] * 3)
        result = execute_with_script_folding(engine, SearchRequest(query='剩余价值', limit=1))
        self.assertEqual(len(result['results']), 1)
        self.assertGreaterEqual(result['total'], 3)
        self.assertFalse(result['total_is_exact'])
        self.assertTrue(result['has_more'])
        all_hits = execute_with_script_folding(engine, SearchRequest(query='剩余价值', limit='all'))
        self.assertEqual(all_hits['total'], 6)
        self.assertTrue(all_hits['total_is_exact'])
        self.assertFalse(all_hits['has_more'])

    def test_same_paragraph_different_offsets_are_distinct(self):
        engine = self.engine(['剩余价值與剩餘價值。'])
        result = execute_with_script_folding(engine, SearchRequest(query='剩余价值'))
        self.assertEqual({h['match_start'] for h in result['results']}, {0, 5})
        self.assertEqual(result['total'], 2)

    def test_pdf_unicode_offsets_pages_and_source_bytes_stay_original(self):
        engine = self.engine(['𠮷😀前言：剩餘價值。'])
        before = self.path.read_bytes()
        direct = engine.search('剩餘價值', mode='exact')['results']
        result = execute_with_script_folding(engine, SearchRequest(query='剩余价值'))
        self.assertEqual(result['results'], direct)
        self.assertEqual(result['results'][0]['match_start'], 5)
        self.assertEqual(self.path.read_bytes(), before)

    def test_short_reverse_and_explicit_empty_scope(self):
        engine = self.engine(['價值。', '价值。'])
        for query in ('价值', '價值'):
            result = execute_with_script_folding(engine, SearchRequest(query=query, mode='exact'))
            self.assertEqual(result['total'], 2)
        for scope in ((), ('missing',)):
            result = execute_with_script_folding(engine, SearchRequest(query='价值', source_file_ids=scope))
            self.assertEqual(result['total'], 0)
        self.assertEqual(execute_with_script_folding(engine, SearchRequest(query='价值', source_type='word'))['total'], 0)

    def test_disabled_or_unavailable_matches_original_response(self):
        engine = self.engine(['剩餘價值。'])
        request = SearchRequest(query='剩余价值')
        expected = SearchService.execute(engine, request)
        self.assertEqual(execute_with_script_folding(engine, request, enabled=False), expected)
        with patch('src.me_finder.script_conversion.is_available', return_value=False):
            self.assertEqual(execute_with_script_folding(engine, request), expected)

    def test_limits_follow_engine_contract(self):
        engine = self.engine(['剩餘價值。'] * 12 + ['剩余价值。'] * 12)
        for limit, count in ((1, 1), ('1', 1), (-1, 1), (0, 10), (None, 10), ('0', 24), ('all', 24), (999, 24)):
            with self.subTest(limit=limit):
                result = execute_with_script_folding(engine, SearchRequest(query='剩余价值', limit=limit))
                self.assertEqual(len(result['results']), count)

    def test_compact_fallback_preserves_raw_highlight(self):
        engine = self.engine(['剩 餘 價 值。'])
        result = execute_with_script_folding(engine, SearchRequest(query='剩余价值'))
        direct = engine.search('剩餘價值', mode='compact')
        self.assertEqual(result['results'], direct['results'])
        self.assertGreater(result['total'], 0)


@unittest.skipUnless(is_available(), 'OpenCC is unavailable')
class SQLiteScriptSearchTests(_SearchCases, unittest.TestCase):
    pass


@unittest.skipUnless(is_available(), 'OpenCC is unavailable')
class JsonScriptSearchTests(_SearchCases, unittest.TestCase):
    backend = 'json'


class ScriptPreferenceTests(unittest.TestCase):
    def test_strict_bool_and_partial_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preferences.json'
            self.assertTrue(read_preferences(path)['script_folding'])
            save_preferences({'script_folding': False}, path)
            save_preferences({'auto_update': True}, path)
            self.assertFalse(read_preferences(path)['script_folding'])
            for value in ('false', 0, None, [], {}):
                with self.assertRaises(ValueError):
                    save_preferences({'script_folding': value}, path)
            path.write_text('{"script_folding":"false"}', encoding='utf-8')
            self.assertTrue(read_preferences(path)['script_folding'])

    @unittest.skipUnless(is_available(), 'OpenCC is unavailable')
    def test_http_toggle_applies_live_and_md_export_remains_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'data' / 'index.sqlite3'
            build_database(make_index(['剩餘價值。'], epub=True), database)
            handler = make_handler(database, app_context=AppContext.create(root, index_path=database))
            handler.log_message = lambda *_: None
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = build_opener(ProxyHandler({}))
            def call(path, payload=None):
                request = Request(f'http://127.0.0.1:{server.server_port}'+path,
                                  data=json.dumps(payload).encode() if payload is not None else None,
                                  headers={'Content-Type': 'application/json'})
                with opener.open(request, timeout=10) as response:
                    return json.loads(response.read())
            try:
                prefs = call('/api/preferences')
                self.assertTrue(prefs['script_folding_available'])
                self.assertTrue(prefs['script_folding'])
                query = {'query': '剩余价值', 'mode': 'exact'}
                self.assertEqual(call('/api/search', query)['total'], 1)
                exported = call('/api/document/export-markdown', {'source_id': 'book', 'output_dir': str(root)})
                before = Path(exported['path']).read_bytes()
                self.assertIn('剩餘價值'.encode(), before)
                call('/api/preferences', {'script_folding': False})
                self.assertEqual(call('/api/search', query)['total'], 0)
                self.assertFalse(call('/api/preferences')['script_folding'])
                exported = call('/api/document/export-markdown', {'source_id': 'book', 'output_dir': str(root)})
                self.assertEqual(Path(exported['path']).read_bytes(), before)
                call('/api/preferences', {'script_folding': True})
                self.assertEqual(call('/api/search', query)['total'], 1)
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                thread.join(timeout=2)

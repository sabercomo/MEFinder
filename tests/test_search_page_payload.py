"""Characterize page payload lookup and its cache before moving the SQL."""

import json
from pathlib import Path
import tempfile
import unittest

from src.me_finder.database import build_database
from src.me_finder.search.engine import SearchEngine
from src.me_finder.persistence.connection import open_write


class SearchPagePayloadTests(unittest.TestCase):
    def test_payload_anchors_missing_malformed_and_cached_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'index.sqlite3'
            pages = [{
                'source_file_id': 'pdf-book', 'pdf_page_index': index,
                'pdf_page_id': f'pdf-book-PAGE-{index:06d}',
                'text_raw': '😀正文', 'printed_page': 42,
                'blocks': [{'text': '正文', 'page_char_start': 1, 'page_char_end': 3}],
            } for index in range(3)]
            build_database({
                'metadata': {}, 'volumes': [], 'works': [], 'paragraphs': [],
                'source_files': [{'source_file_id': 'pdf-book', 'source_type': 'pdf'}],
                'pdf_pages': pages,
            }, db)
            with open_write(db) as connection:
                connection.execute('UPDATE pdf_pages SET payload_json = ? WHERE pdf_page_index = 1', ('invalid json',))
                connection.execute('UPDATE pdf_pages SET payload_json = ? WHERE pdf_page_index = 2', (json.dumps(['not a page']),))
                connection.commit()
            engine = SearchEngine(db)
            try:
                statements = []
                engine.db.set_trace_callback(statements.append)
                first = engine.pdf_page_record('pdf-book', 0)
                self.assertEqual(first['pdf_page_id'], pages[0]['pdf_page_id'])
                self.assertEqual(first['printed_page'], 42)
                block = first['blocks'][0]
                self.assertEqual(first['text_raw'][block['page_char_start']:block['page_char_end']], '正文')
                for page in (1, 2, 99):
                    self.assertIsNone(engine.pdf_page_record('pdf-book', page))
                self.assertIsNone(engine.pdf_page_record('missing', 0))
                count = len(statements)
                self.assertIs(engine.pdf_page_record('pdf-book', 0), first)
                for page in (1, 2, 99):
                    self.assertIsNone(engine.pdf_page_record('pdf-book', page))
                self.assertIsNone(engine.pdf_page_record('missing', 0))
                self.assertEqual(len(statements), count, 'valid and empty results are cached')
            finally:
                engine.close()

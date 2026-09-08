"""Page-range and linked-note export from real persisted/normalized fixtures."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from src.me_finder.database import build_database
from src.me_finder.document_export import DocumentExportError
from src.me_finder.document_export_service import export_indexed_pdf_markdown
from src.me_finder.export_footnotes import Footnote, FootnoteText, FootnoteReference, NormalizedDocument, normalize_document_export
from src.me_finder.export_page_reconstruction import attach_export_layout
from src.me_finder.markdown_export import document_to_markdown
from src.me_finder.markdown_export_normalize import ExportOptions
from src.me_finder.markdown_page_selection import PageSelection, resolve_pdf_pages, select_normalized_pages, select_epub_paragraphs
from tests.test_markdown_export import _page, _block, footnote_fixture
from tests.test_export_page_reconstruction import reconstruction_fixture


def selection(pages='2', mode='printed'):
    return PageSelection.from_mapping({'mode': mode, 'pages': pages})


class PageSelectionTests(unittest.TestCase):
    def test_parse_mixed_ranges_duplicates_and_labels(self):
        self.assertEqual(selection('12-14,13，iv').labels, ('12', '13', '14', 'iv'))
        for value in (None, {}, {'mode': 'other', 'pages': '1'}, {'mode': 'printed', 'pages': 1},
                      {'mode': 'printed', 'pages': '1', 'ignore': True}):
            with self.subTest(value=value), self.assertRaises(DocumentExportError):
                PageSelection.from_mapping(value)
        for text in ('', '1,,2', '5-1', '0-2', '1-10001', '<script>', '1,' , 'iv-vi'):
            with self.subTest(text=text), self.assertRaises(DocumentExportError):
                selection(text)
        for text in ('0', '-1', 'ix', '1.5', '１'):
            with self.subTest(text=text), self.assertRaises(DocumentExportError):
                selection(text, 'physical')

    def test_resolve_printed_physical_roman_missing_and_ambiguous(self):
        pages = [_page(8, 'iv', []), _page(9, '1', []), _page(10, '2', [])]
        self.assertEqual(resolve_pdf_pages(pages, selection('iv,2')), [9, 11])
        self.assertEqual(resolve_pdf_pages(pages, selection('10-11', 'physical')), [10, 11])
        for spec in (selection('1-3'), selection('1', 'physical')):
            with self.assertRaises(DocumentExportError): resolve_pdf_pages(pages, spec)
        with self.assertRaises(DocumentExportError):
            resolve_pdf_pages(pages + [_page(11, '1', [])], selection('1'))

    def test_note_at_unselected_chapter_end_follows_selected_body(self):
        pages = footnote_fixture()
        snapshot = deepcopy(pages)
        whole = normalize_document_export(pages)
        chosen = select_normalized_pages(whole, pages, [11], ExportOptions())
        text = document_to_markdown([], normalized=chosen)
        self.assertIn('下一页重新出现', text)
        self.assertNotIn('先引用', text)
        self.assertNotIn('新章正文', text)
        self.assertEqual(sum(isinstance(i, Footnote) for i in chosen.items), 1)
        self.assertIn('相同文献。', text)
        for body in chosen.items:
            if isinstance(body, FootnoteText):
                for ref in body.references: self.assertIn(f'[^{ref.note_id}]:', text)
        self.assertEqual(pages, snapshot)
        self.assertEqual(document_to_markdown(pages), document_to_markdown([], normalized=whole))

    def test_disjoint_pages_keep_chapter_notes_without_middle_page(self):
        pages = footnote_fixture()
        whole = normalize_document_export(pages)
        chosen = select_normalized_pages(whole, pages, [10, 12], ExportOptions())
        text = document_to_markdown([], normalized=chosen)
        self.assertNotIn('下一页重新出现', text)
        self.assertIn('先引用', text)
        self.assertIn('新章正文', text)
        notes = [i for i in chosen.items if isinstance(i, Footnote)]
        self.assertEqual(len(notes), 3)
        self.assertLess(text.index('[^'+notes[0].note_id+']:'), text.index('第二部'))

    def test_shared_note_multiple_refs_emitted_once_even_on_other_page(self):
        note = Footnote('n', 1, 1, '注释原文', '①', 30, '20', 2, ('r1', 'r2'))
        body = FootnoteText('甲①乙①', references=(FootnoteReference(1, 2, 'n', 'r1', 1),
                            FootnoteReference(3, 4, 'n', 'r2', 1)), export_physical_page=10)
        whole = NormalizedDocument([body, note], {})
        chosen = select_normalized_pages(whole, [_page(9, '1', [])], [10], ExportOptions())
        text = document_to_markdown([], normalized=chosen)
        self.assertEqual(text.count('[^n]:'), 1)
        self.assertEqual(text.count('[^n]'), 3)
        with self.assertRaises(DocumentExportError):
            select_normalized_pages(replace(whole, items=[body]), [_page(9, '1', [])], [10], ExportOptions())

    def test_marker_options_do_not_control_page_selection(self):
        pages = footnote_fixture()
        for mode in ('none', 'printed', 'full'):
            options = ExportOptions(page_marker_mode=mode)
            whole = normalize_document_export(pages, options=options)
            chosen = select_normalized_pages(whole, pages, [11], options)
            text = document_to_markdown([], normalized=chosen)
            self.assertIn('下一页重新出现', text)
            self.assertEqual('<!-- printed_page:' in text, mode == 'printed')
            self.assertEqual('<!-- pdf_page:' in text, mode == 'full')

    def test_proven_cross_page_fragments_are_selected_by_target_page(self):
        pages, layout = reconstruction_fixture(prefix='前页正文', tail='下一页正文①。', note=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'layout.json').write_text(json.dumps(layout), encoding='utf-8')
            for page in pages:
                for block in page['blocks']: block['result_dir'] = str(root)
            attach_export_layout(pages, root)
            whole = normalize_document_export(pages)
        self.assertEqual(whole.reconstruction_report['reconstructed_block_count'], 1)
        chosen = select_normalized_pages(whole, pages, [12], ExportOptions())
        text = document_to_markdown([], normalized=chosen)
        self.assertNotIn('前页正文', text)
        self.assertIn('下一页正文', text)
        self.assertIn('注释原文。', text)
        self.assertEqual(sum(isinstance(i, Footnote) for i in chosen.items), 1)

    def test_unresolved_cross_page_blocks_are_not_silently_sliced(self):
        pages = [_page(0, '1', [{**_block('跨页正文'), 'cross_page': True}])]
        whole = normalize_document_export(pages)
        with self.assertRaisesRegex(DocumentExportError, '跨页'):
            select_normalized_pages(whole, pages, [1], ExportOptions())

    def test_epub_uses_publisher_labels_and_rejects_guessed_or_repeated_labels(self):
        paragraphs = [{'text_raw': f'p{i}', 'original_page_start': page, 'original_page_end': page,
                       'page_source_type': 'epub_pagebreak'} for i, page in enumerate(('iv','1','1','2'))]
        self.assertEqual(select_epub_paragraphs(paragraphs, selection('1')), paragraphs[1:3])
        with self.assertRaises(DocumentExportError): select_epub_paragraphs(paragraphs, selection('1', 'physical'))
        with self.assertRaises(DocumentExportError): select_epub_paragraphs(paragraphs, selection('3'))
        with self.assertRaises(DocumentExportError): select_epub_paragraphs(paragraphs+[paragraphs[1]], selection('1'))
        with self.assertRaises(DocumentExportError): select_epub_paragraphs([{**paragraphs[1], 'page_source_type':'unknown'}], selection('1'))

    def test_epub_service_exports_publisher_page_and_reports_note_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'index.sqlite3'
            paragraphs = [dict(paragraph_id=f'p{i}',source_file_id='book',source_type='word',
                volume_id='v',volume_number=1,paragraph_index=i,text_raw=f'第{page}頁正文',
                original_page_start=page,original_page_end=page,page_source_type='epub_pagebreak',
                style_name='p',eligible_for_search=True) for i,page in enumerate(('iv','1','2'))]
            build_database({'metadata':{},'source_files':[dict(source_file_id='book',source_type='word',
                file_name='book.epub',file_format='epub',epub_page_count=3)],
                'paragraphs':paragraphs},database)
            args=dict(database_path=database,source_file_id='book',output_dir=root)
            result=export_indexed_pdf_markdown(**args,page_selection=selection('iv,2'))
            text=Path(result['path']).read_text(encoding='utf-8')
            self.assertEqual(result['page_count'],2)
            self.assertEqual(result['paragraph_count'],2)
            self.assertIn('第iv頁正文',text)
            self.assertIn('第2頁正文',text)
            self.assertNotIn('第1頁正文',text)
            self.assertTrue(result['warnings'])
            self.assertIn('页外脚注',text)
            with self.assertRaises(DocumentExportError):
                export_indexed_pdf_markdown(**args,page_selection=selection('1','physical'))

    def test_spread_printed_page_requires_physical_selection(self):
        pages=[{**_page(3,'8',[]),'layout_mode':'spread'}]
        with self.assertRaises(DocumentExportError):resolve_pdf_pages(pages,selection('8'))
        self.assertEqual(resolve_pdf_pages(pages,selection('4','physical')),[4])

    def test_service_preserves_full_export_and_writes_distinct_range_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pages = footnote_fixture()
            for page in pages: page['source_file_id'] = 'book'
            database = root / 'index.sqlite3'
            build_database({'metadata': {}, 'source_files':[{'source_file_id':'book', 'source_type':'pdf',
                'file_name':'book.pdf', 'display_title':'測試書'}], 'pdf_pages': pages}, database)
            args = dict(database_path=database, source_file_id='book', output_dir=root)
            full = export_indexed_pdf_markdown(**args)
            before = Path(full['path']).read_bytes()
            source_before = database.read_bytes()
            part = export_indexed_pdf_markdown(**args, page_selection=selection('2'))
            self.assertNotEqual(part['path'], full['path'])
            self.assertEqual(part['page_count'], 1)
            self.assertEqual(part['page_selection']['physical_pages'], [11])
            self.assertEqual(Path(full['path']).read_bytes(), before)
            self.assertEqual(database.read_bytes(), source_before)
            text = Path(part['path']).read_text(encoding='utf-8')
            self.assertTrue(text.startswith('---\n'))
            self.assertIn('下一页重新出现', text)
            self.assertNotIn('先引用', text)
            self.assertEqual(part['page_selection']['note_count'], 1)
            with patch('src.me_finder.document_export_service.document_to_markdown') as render:
                with self.assertRaises(DocumentExportError):
                    export_indexed_pdf_markdown(**args, page_selection=selection('99'))
                render.assert_not_called()

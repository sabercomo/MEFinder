"""The private baseline snapshots SQLite (including WAL) without copying secrets."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.bench_real_library import digest, prepare_snapshot
from scripts.performance_fixture import create_fixture


class RealLibraryBaselineTests(unittest.TestCase):
    def test_freezes_database_queries_and_wal_without_copying_private_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            library, snapshot = root / 'library', root / 'snapshot'
            fixture = create_fixture(library / 'runtime', documents=2, paragraphs=24, alignment_paragraphs=24)
            db = library / 'runtime/data/index.sqlite3'
            (library / 'preferences.json').write_text('{"private_setting":"do-not-copy"}')
            connection = sqlite3.connect(db)
            try:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute("UPDATE paragraphs SET normalized_text=normalized_text || normalized_text || normalized_text")
                connection.commit()
                before = digest(db)
                args = dict(export_source='bench-000', group='bench-pair', pivot='bench-002', target='bench-003', english_source='bench-001')
                manifest = prepare_snapshot(library, snapshot, **args)
                self.assertEqual(digest(db), before)
                self.assertEqual(manifest['counts']['paragraphs'], fixture['paragraphs'])
                self.assertEqual(manifest['content_sha256'], digest(snapshot / 'index.sqlite3'))
                self.assertEqual({p.name for p in snapshot.iterdir()}, {'index.sqlite3', 'manifest.json'})
                self.assertEqual(len(manifest['queries']), 8)
                with self.assertRaises(FileExistsError):
                    prepare_snapshot(library, snapshot, **args)
                self.assertEqual(json.loads((snapshot / 'manifest.json').read_text()), manifest)
                frozen = digest(snapshot / 'index.sqlite3')
                connection.execute('DELETE FROM paragraphs'); connection.commit()
                self.assertEqual(digest(snapshot / 'index.sqlite3'), frozen)
            finally:
                connection.close()

    def test_rejects_a_pair_outside_the_requested_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            create_fixture(root / 'library/runtime', documents=2, paragraphs=24, alignment_paragraphs=24)
            with self.assertRaisesRegex(ValueError, 'distinct members'):
                prepare_snapshot(root / 'library', root / 'snapshot', export_source='bench-000',
                                 group='bench-pair', pivot='bench-000', target='bench-003', english_source='bench-001')

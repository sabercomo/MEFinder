from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import (
    build_database,
    load_database_index,
    replace_source_in_database,
)
from src.me_finder.indexer import build_index


class AnchorMetadataTests(unittest.TestCase):
    def test_anchor_spec_version_is_written_to_index_and_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            corpus_dir = root / "corpus" / "raw_docx"
            corpus_dir.mkdir(parents=True)
            database_path = root / "data" / "index.sqlite3"

            index = build_index(
                corpus_dir=corpus_dir,
                database_path=database_path,
                root=root,
            )

            self.assertEqual(index["metadata"]["anchor_spec_version"], 1)
            stored = load_database_index(database_path)
            self.assertEqual(stored["metadata"]["anchor_spec_version"], 1)

    def test_targeted_import_upgrades_anchor_capability_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "index.sqlite3"
            build_database({"metadata": {}}, database_path)
            with sqlite3.connect(str(database_path)) as connection:
                connection.execute(
                    "DELETE FROM metadata WHERE key = 'anchor_spec_version'"
                )
                connection.commit()

            replace_source_in_database(
                {
                    "source_files": [
                        {
                            "source_file_id": "docx-anchor-test",
                            "source_type": "word",
                            "file_name": "anchor-test.docx",
                        }
                    ],
                    "volumes": [],
                    "works": [],
                    "paragraphs": [],
                },
                database_path,
                backup_existing=False,
            )

            stored = load_database_index(database_path)
            self.assertEqual(stored["metadata"]["anchor_spec_version"], 1)


if __name__ == "__main__":
    unittest.main()

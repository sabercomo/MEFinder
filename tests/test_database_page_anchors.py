from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import (
    SCHEMA,
    _delete_page_anchors_for_source,
    build_database,
    delete_source_from_database,
    replace_source_in_database,
)


def _paragraph(source_id: str, number: int) -> dict[str, object]:
    return {
        "paragraph_id": f"{source_id}-P{number:06d}",
        "source_file_id": source_id,
        "source_type": "word",
        "paragraph_index": number,
        "eligible_for_search": True,
        "text_raw": f"第 {number} 段",
    }


def _source_index(
    source_id: str,
    *,
    first_paragraph: int = 0,
) -> dict[str, object]:
    start = _paragraph(source_id, first_paragraph)
    end = _paragraph(source_id, first_paragraph + 1)
    return {
        "metadata": {},
        "source_files": [
            {
                "source_file_id": source_id,
                "source_type": "word",
                "file_name": f"{source_id}.docx",
            }
        ],
        "volumes": [],
        "works": [],
        "paragraphs": [start, end],
        "page_anchors": [
            {
                "page_anchor_id": f"{source_id}-PAGE-{first_paragraph}",
                "source_file_id": source_id,
                "start_paragraph_id": start["paragraph_id"],
                "end_paragraph_id": end["paragraph_id"],
                "original_page_label": str(first_paragraph),
            }
        ],
    }


class DatabasePageAnchorTests(unittest.TestCase):
    def test_typed_delete_uses_subquery_beyond_sqlite_parameter_limit(self) -> None:
        paragraph_count = 32_767
        source_id = "word-large"
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(SCHEMA)
            connection.executemany(
                """
                INSERT INTO paragraphs(
                    paragraph_id, source_file_id, source_type, paragraph_index,
                    eligible_for_search, text_raw, normalized_text,
                    compact_text, plain_text, payload_json
                ) VALUES (?, ?, 'word', ?, 1, '', '', '', '', '{}')
                """,
                (
                    (f"{source_id}-P{number:06d}", source_id, number)
                    for number in range(paragraph_count)
                ),
            )
            connection.executemany(
                "INSERT INTO page_anchors(paragraph_id, payload_json) VALUES (?, '{}')",
                (
                    (f"{source_id}-P{number:06d}",)
                    for number in range(paragraph_count)
                ),
            )
            # Keep one old NULL-typed row in the large source to prove the
            # compatibility payload scan still runs after the typed subquery.
            connection.execute(
                "INSERT INTO page_anchors(paragraph_id, payload_json) VALUES (NULL, ?)",
                (
                    json.dumps(
                        {"end_paragraph_id": f"{source_id}-P{paragraph_count - 1:06d}"}
                    ),
                ),
            )
            connection.execute(
                "INSERT INTO page_anchors(paragraph_id, payload_json) VALUES (?, ?)",
                ("word-other-P000000", json.dumps({"source_file_id": "word-other"})),
            )
            connection.execute(
                "INSERT INTO page_anchors(paragraph_id, payload_json) VALUES (NULL, ?)",
                (json.dumps({"source_file_id": "word-other"}),),
            )
            # Reproduce the Windows SQLite variable ceiling when the host
            # Python exposes sqlite3_limit (3.11+).  Older supported Python
            # runtimes lack Connection.setlimit, so the SQL-shape assertion
            # below remains the portable regression guard there.
            setlimit = getattr(connection, "setlimit", None)
            if setlimit is not None:
                setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32_766)
            statements: list[str] = []
            connection.set_trace_callback(statements.append)

            deleted = _delete_page_anchors_for_source(connection, source_id)
            remaining = connection.execute(
                "SELECT COUNT(*) FROM page_anchors"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(deleted, paragraph_count + 1)
        self.assertEqual(remaining, 2)
        self.assertTrue(
            any(
                "DELETE FROM page_anchors" in statement
                and "SELECT paragraph_id FROM paragraphs" in statement
                for statement in statements
            ),
            "typed anchor deletion must stay a subquery, not a bound-id list",
        )

    def test_full_build_maps_v2_paragraph_column_to_anchor_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "index.sqlite3"
            index = _source_index("word-a")
            build_database(index, database)

            connection = sqlite3.connect(str(database))
            try:
                paragraph_id, raw_payload = connection.execute(
                    "SELECT paragraph_id, payload_json FROM page_anchors"
                ).fetchone()
                payload = json.loads(raw_payload)
                user_version = connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
            finally:
                connection.close()

        expected = index["page_anchors"][0]
        self.assertEqual(user_version, 6)
        self.assertEqual(paragraph_id, expected["start_paragraph_id"])
        self.assertEqual(payload["source_file_id"], "word-a")
        self.assertEqual(
            payload["start_paragraph_id"], expected["start_paragraph_id"]
        )
        self.assertEqual(
            payload["end_paragraph_id"], expected["end_paragraph_id"]
        )

    def test_targeted_replace_rewrites_anchor_with_canonical_relationships(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "index.sqlite3"
            build_database(_source_index("word-a"), database)
            replacement = _source_index("word-a", first_paragraph=10)

            replace_source_in_database(
                replacement,
                database,
                backup_existing=False,
            )

            connection = sqlite3.connect(str(database))
            try:
                rows = connection.execute(
                    "SELECT paragraph_id, payload_json FROM page_anchors"
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(len(rows), 1)
        paragraph_id, raw_payload = rows[0]
        payload = json.loads(raw_payload)
        expected = replacement["page_anchors"][0]
        self.assertEqual(paragraph_id, expected["start_paragraph_id"])
        self.assertEqual(payload["source_file_id"], "word-a")
        self.assertEqual(
            payload["start_paragraph_id"], expected["start_paragraph_id"]
        )
        self.assertEqual(
            payload["end_paragraph_id"], expected["end_paragraph_id"]
        )

    def test_delete_cleans_legacy_v2_orphans_by_source_and_end_paragraph(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "index.sqlite3"
            index = _source_index("word-a")
            build_database(index, database)
            replace_source_in_database(
                _source_index("word-b"),
                database,
                backup_existing=False,
            )

            connection = sqlite3.connect(str(database))
            try:
                # Reproduce rows written by the old v2 field mismatch: the
                # typed paragraph_id was NULL although payload_json retained
                # the canonical source/start/end relationships.
                connection.execute(
                    "UPDATE page_anchors SET paragraph_id = NULL "
                    "WHERE paragraph_id = ?",
                    ("word-a-P000000",),
                )
                connection.execute(
                    "INSERT INTO page_anchors(paragraph_id, payload_json) "
                    "VALUES (NULL, ?)",
                    (
                        json.dumps(
                            {
                                "page_anchor_id": "legacy-end-only",
                                "end_paragraph_id": "word-a-P000001",
                            }
                        ),
                    ),
                )
                columns = [
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(page_anchors)"
                    )
                ]
                user_version = connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
                connection.commit()
            finally:
                connection.close()

            result = delete_source_from_database(
                "word-a",
                database,
                backup_existing=False,
            )

            connection = sqlite3.connect(str(database))
            try:
                remaining_payloads = [
                    json.loads(row[0])
                    for row in connection.execute(
                        "SELECT payload_json FROM page_anchors"
                    )
                ]
            finally:
                connection.close()

        self.assertEqual(user_version, 6)
        self.assertEqual(columns, ["row_id", "paragraph_id", "payload_json"])
        self.assertEqual(result["deleted"]["page_anchors"], 2)
        self.assertEqual(len(remaining_payloads), 1)
        self.assertEqual(remaining_payloads[0]["source_file_id"], "word-b")


if __name__ == "__main__":
    unittest.main()

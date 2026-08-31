"""DocumentGroup + membership data layer (Commit B).

Verifies the confirmed architecture and its five data constraints against a
throwaway index DB: additive migration, UNIQUE(source_file_id), base-must-be-member,
base-cleared-on-member-removal, and group deletion never touching source_files.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from contextlib import contextmanager

from src.me_finder import document_groups as dg
from src.me_finder.application.document_group_coordinator import (
    DocumentGroupCoordinator,
)
from src.me_finder.database import DATABASE_SCHEMA_VERSION, build_database
from src.me_finder.document_group_controller import DocumentGroupController
from src.me_finder.document_group_metadata import member_display_name


def _make_v2_source_db(path: Path, sources) -> None:
    """A pre-feature (v2) index: source_files only, no group tables."""

    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE source_files ("
            "source_file_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, "
            "file_name TEXT, relative_path TEXT, volume_number INTEGER, "
            "payload_json TEXT NOT NULL)"
        )
        for source in sources:
            connection.execute(
                "INSERT INTO source_files"
                "(source_file_id, source_type, file_name, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    source["source_file_id"],
                    source.get("source_type", "pdf"),
                    source.get("file_name", ""),
                    json.dumps(source, ensure_ascii=False),
                ),
            )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    finally:
        connection.close()


class DocumentGroupDataLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "index.sqlite3"
        _make_v2_source_db(
            self.db,
            [
                {
                    "source_file_id": "src-zh",
                    "file_name": "leviathan-zh.pdf",
                    "title": "利维坦",
                    "translator": "黎思复",
                    "language_code": "zh-Hans",
                },
                {
                    "source_file_id": "src-en",
                    "file_name": "leviathan-en.pdf",
                    "title": "Leviathan",
                    "language_code": "en",
                },
                {
                    "source_file_id": "src-de",
                    "file_name": "leviathan-de.pdf",
                    "title": "Leviathan (DE)",
                    "language_code": "de",
                },
            ],
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    # ── helpers ──
    def _tables(self):
        connection = sqlite3.connect(str(self.db))
        try:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()

    def _user_version(self) -> int:
        connection = sqlite3.connect(str(self.db))
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()

    def _membership_rows(self):
        connection = sqlite3.connect(str(self.db))
        try:
            return connection.execute(
                "SELECT document_group_id, source_file_id FROM "
                "document_group_members ORDER BY source_file_id"
            ).fetchall()
        finally:
            connection.close()

    # ── constraint 5: additive migration ──
    def test_migration_is_additive_and_preserves_sources(self) -> None:
        self.assertNotIn("document_groups", self._tables())
        before = sqlite3.connect(str(self.db))
        source_count = before.execute("SELECT COUNT(*) FROM source_files").fetchone()[0]
        before.close()

        dg.create_document_group("霍布斯《利维坦》", self.db)

        tables = self._tables()
        self.assertIn("document_groups", tables)
        self.assertIn("document_group_members", tables)
        self.assertEqual(self._user_version(), DATABASE_SCHEMA_VERSION)
        after = sqlite3.connect(str(self.db))
        self.assertEqual(
            after.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
            source_count,
        )
        after.close()

    def test_migration_adds_base_column_to_legacy_837_group_table(self) -> None:
        # An index migrated under 837d808 has a document_groups table WITHOUT
        # base_source_file_id and no members table; ensure() must add the column
        # additively so reads/writes don't crash on "no such column".
        connection = sqlite3.connect(str(self.db))
        connection.execute(
            "CREATE TABLE document_groups (document_group_id TEXT PRIMARY KEY, "
            "title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO document_groups VALUES ('g-old', '旧组', 't', 't')"
        )
        connection.commit()
        connection.close()

        dg.ensure_document_group_schema(self.db)
        groups = dg.list_document_groups(self.db)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["title"], "旧组")
        self.assertIsNone(groups[0]["base_source_file_id"])
        # base management works after the additive migration
        dg.add_group_member("g-old", "src-zh", self.db)
        dg.set_document_group_base("g-old", "src-zh", self.db)
        self.assertEqual(
            dg.list_document_groups(self.db)[0]["base_source_file_id"], "src-zh"
        )

    def test_migration_copies_legacy_837_group_memberships(self) -> None:
        connection = sqlite3.connect(str(self.db))
        connection.execute("ALTER TABLE source_files ADD COLUMN document_group_id TEXT")
        connection.execute(
            "CREATE TABLE document_groups (document_group_id TEXT PRIMARY KEY, "
            "title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO document_groups VALUES (?, ?, 't', 't')",
            [("g-old", "旧组"), ("g-empty", "空组")],
        )
        connection.execute(
            "UPDATE source_files SET document_group_id = 'g-old' "
            "WHERE source_file_id IN ('src-zh', 'src-en')"
        )
        connection.execute(
            "UPDATE source_files SET document_group_id = 'missing-group' "
            "WHERE source_file_id = 'src-de'"
        )
        connection.commit()
        connection.close()

        groups = {g["document_group_id"]: g for g in dg.list_document_groups(self.db)}
        self.assertEqual(
            [m["source_file_id"] for m in groups["g-old"]["members"]],
            ["src-en", "src-zh"],
        )
        self.assertEqual(
            [m["member_order"] for m in groups["g-old"]["members"]], [0, 1]
        )
        self.assertEqual(groups["g-empty"]["members"], [])

    def test_create_rename_delete_roundtrip(self) -> None:
        created = dg.create_document_group("初版", self.db)
        gid = created["document_group_id"]
        self.assertIsNone(created["base_source_file_id"])

        dg.rename_document_group(gid, "利维坦", self.db)
        groups = dg.list_document_groups(self.db)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["title"], "利维坦")

        dg.delete_document_group(gid, self.db)
        self.assertEqual(dg.list_document_groups(self.db), [])

    def test_empty_title_rejected(self) -> None:
        with self.assertRaises(ValueError):
            dg.create_document_group("   ", self.db)

    # ── combine: create + add members + set base in one transaction ──
    def test_combine_creates_group_with_members_and_base(self) -> None:
        result = dg.combine_into_group(
            "利维坦",
            ["src-de", "src-zh", "src-en"],
            self.db,
            base_source_file_id="src-de",
        )
        gid = result["document_group_id"]
        self.assertEqual(result["member_source_file_ids"], ["src-de", "src-zh", "src-en"])
        groups = dg.list_document_groups(self.db)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["title"], "利维坦")
        self.assertEqual(groups[0]["base_source_file_id"], "src-de")
        self.assertEqual(
            [m["source_file_id"] for m in groups[0]["members"]],
            ["src-de", "src-zh", "src-en"],
        )
        for source_id in ("src-de", "src-zh", "src-en"):
            self.assertEqual(dg.document_group_for_source(source_id, self.db), gid)

    def test_combine_moves_members_out_of_previous_groups(self) -> None:
        old = dg.create_document_group("旧组", self.db)["document_group_id"]
        dg.add_group_member(old, "src-zh", self.db)
        dg.combine_into_group("利维坦", ["src-de", "src-zh"], self.db)
        self.assertEqual(len(self._membership_rows()), 2)
        self.assertNotEqual(dg.document_group_for_source("src-zh", self.db), old)

    def test_combine_requires_two_members_and_valid_base(self) -> None:
        with self.assertRaises(ValueError):
            dg.combine_into_group("利维坦", ["src-zh"], self.db)
        with self.assertRaises(ValueError):
            dg.combine_into_group(
                "利维坦", ["src-zh", "src-en"], self.db, base_source_file_id="src-de"
            )
        with self.assertRaises(ValueError):
            dg.combine_into_group("利维坦", ["src-zh", "no-such-source"], self.db)
        self.assertEqual(dg.list_document_groups(self.db), [])

    # ── constraint 1: one group per source (UNIQUE) ──
    def test_source_belongs_to_at_most_one_group(self) -> None:
        g1 = dg.create_document_group("组一", self.db)["document_group_id"]
        g2 = dg.create_document_group("组二", self.db)["document_group_id"]
        dg.add_group_member(g1, "src-zh", self.db)
        # Re-assigning to another group MOVES it, never duplicates.
        dg.add_group_member(g2, "src-zh", self.db)

        rows = self._membership_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], g2)
        self.assertEqual(dg.document_group_for_source("src-zh", self.db), g2)

    def test_add_member_requires_existing_group_and_source(self) -> None:
        gid = dg.create_document_group("组", self.db)["document_group_id"]
        with self.assertRaises(ValueError):
            dg.add_group_member("no-such-group", "src-zh", self.db)
        with self.assertRaises(ValueError):
            dg.add_group_member(gid, "no-such-source", self.db)

    def test_remove_member(self) -> None:
        gid = dg.create_document_group("组", self.db)["document_group_id"]
        dg.add_group_member(gid, "src-zh", self.db)
        dg.remove_group_member("src-zh", self.db)
        self.assertEqual(self._membership_rows(), [])
        with self.assertRaises(ValueError):
            dg.remove_group_member("src-zh", self.db)

    # ── constraint 2: base must be a member ──
    def test_base_must_be_a_member(self) -> None:
        gid = dg.create_document_group("组", self.db)["document_group_id"]
        with self.assertRaises(ValueError):
            dg.set_document_group_base(gid, "src-zh", self.db)  # not a member yet
        dg.add_group_member(gid, "src-zh", self.db)
        dg.set_document_group_base(gid, "src-zh", self.db)
        group = dg.list_document_groups(self.db)[0]
        self.assertEqual(group["base_source_file_id"], "src-zh")
        self.assertTrue(
            next(m for m in group["members"] if m["source_file_id"] == "src-zh")[
                "is_base"
            ]
        )
        # Clearing the base with an empty value is allowed.
        dg.set_document_group_base(gid, "", self.db)
        self.assertIsNone(dg.list_document_groups(self.db)[0]["base_source_file_id"])

    # ── constraint 3: removing the base member clears the base ──
    def test_removing_base_member_clears_base(self) -> None:
        gid = dg.create_document_group("组", self.db)["document_group_id"]
        dg.add_group_member(gid, "src-zh", self.db)
        dg.add_group_member(gid, "src-en", self.db)
        dg.set_document_group_base(gid, "src-zh", self.db)
        dg.remove_group_member("src-zh", self.db)
        self.assertIsNone(dg.list_document_groups(self.db)[0]["base_source_file_id"])

    def test_moving_base_member_out_clears_old_base(self) -> None:
        g1 = dg.create_document_group("组一", self.db)["document_group_id"]
        g2 = dg.create_document_group("组二", self.db)["document_group_id"]
        dg.add_group_member(g1, "src-zh", self.db)
        dg.set_document_group_base(g1, "src-zh", self.db)
        dg.add_group_member(g2, "src-zh", self.db)  # move out of g1
        by_id = {g["document_group_id"]: g for g in dg.list_document_groups(self.db)}
        self.assertIsNone(by_id[g1]["base_source_file_id"])

    # ── constraint 4: deleting a group never deletes source_files ──
    def test_delete_group_keeps_source_files(self) -> None:
        gid = dg.create_document_group("组", self.db)["document_group_id"]
        dg.add_group_member(gid, "src-zh", self.db)
        dg.add_group_member(gid, "src-en", self.db)
        result = dg.delete_document_group(gid, self.db)
        self.assertEqual(result["unlinked_count"], 2)
        connection = sqlite3.connect(str(self.db))
        try:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM source_files"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(remaining, 3)

    # ── version metadata + display-name fallback ──
    def test_version_label_and_display_name_fallback(self) -> None:
        gid = dg.create_document_group("组", self.db)["document_group_id"]
        dg.add_group_member(gid, "src-zh", self.db, version_label="通行本")
        dg.add_group_member(gid, "src-en", self.db)  # no label; en has no translator
        members = {
            m["source_file_id"]: m
            for m in dg.list_document_groups(self.db)[0]["members"]
        }
        self.assertEqual(members["src-zh"]["display_name"], "通行本")
        # src-en falls back to language.
        self.assertEqual(members["src-en"]["display_name"], "英文")

        dg.set_member_version_label("src-en", "企鹅版", self.db)
        members = {
            m["source_file_id"]: m
            for m in dg.list_document_groups(self.db)[0]["members"]
        }
        self.assertEqual(members["src-en"]["display_name"], "企鹅版")

    def test_display_name_prefers_translator_over_language(self) -> None:
        self.assertEqual(
            member_display_name(
                "", {"translator": "黎思复", "language_code": "zh-Hans"}
            ),
            "黎思复 译",
        )

    def test_resolve_source_ids_ordered_by_member_order(self) -> None:
        gid = dg.create_document_group("组", self.db)["document_group_id"]
        dg.add_group_member(gid, "src-zh", self.db)
        dg.add_group_member(gid, "src-en", self.db)
        self.assertEqual(
            dg.resolve_document_group_source_ids(gid, self.db), ["src-zh", "src-en"]
        )

    def test_resolve_missing_group_raises_not_found(self) -> None:
        with self.assertRaises(dg.DocumentGroupNotFound):
            dg.resolve_document_group_source_ids("no-such-group", self.db)

    def test_resolve_empty_group_returns_empty_list(self) -> None:
        gid = dg.create_document_group("空组", self.db)["document_group_id"]
        self.assertEqual(dg.resolve_document_group_source_ids(gid, self.db), [])

    def test_version_label_too_long_rejected(self) -> None:
        gid = dg.create_document_group("组", self.db)["document_group_id"]
        with self.assertRaises(ValueError):
            dg.add_group_member(gid, "src-zh", self.db, version_label="x" * 201)


class _StubRuntime:
    @contextmanager
    def mutation(self):
        yield

    def suspend(self):
        pass

    def reopen(self, *, attempts: int = 1) -> bool:
        return True


class _StubDurable:
    @contextmanager
    def operation(self):
        yield


class _Paths:
    def __init__(self, index_path: Path) -> None:
        self.index_path = index_path


class DocumentGroupControllerTests(unittest.TestCase):
    """The coordinator + controller plumbing over the data layer (stubbed runtime)."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "index.sqlite3"
        _make_v2_source_db(
            self.db,
            [
                {"source_file_id": "src-zh", "file_name": "a.pdf", "translator": "张三"},
                {"source_file_id": "src-en", "file_name": "b.pdf", "language_code": "en"},
            ],
        )
        coordinator = DocumentGroupCoordinator(
            _Paths(self.db), _StubRuntime(), _StubDurable()
        )
        self.controller = DocumentGroupController(coordinator)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_create_add_base_and_list(self) -> None:
        status, body = self.controller.create({"title": "组"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        gid = body["result"]["document_group_id"]

        status, _ = self.controller.add_member(
            {"document_group_id": gid, "source_file_id": "src-zh"}
        )
        self.assertEqual(status, 200)
        status, _ = self.controller.set_base(
            {"document_group_id": gid, "base_source_file_id": "src-zh"}
        )
        self.assertEqual(status, 200)

        status, body = self.controller.list()
        self.assertEqual(status, 200)
        groups = body["document_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["base_source_file_id"], "src-zh")
        self.assertEqual(groups[0]["members"][0]["display_name"], "张三 译")

    def test_invalid_input_maps_to_400(self) -> None:
        status, body = self.controller.create({"title": "  "})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_set_base_non_member_maps_to_400(self) -> None:
        _, body = self.controller.create({"title": "组"})
        gid = body["result"]["document_group_id"]
        status, body = self.controller.set_base(
            {"document_group_id": gid, "base_source_file_id": "src-en"}
        )
        self.assertEqual(status, 400)


def _rebuild_index(source_ids):
    return {
        "metadata": {},
        "source_files": [
            {
                "source_file_id": sid,
                "source_type": "pdf",
                "file_name": f"{sid}.pdf",
                "title": sid,
            }
            for sid in source_ids
        ],
        "volumes": [],
        "works": [],
        "paragraphs": [],
        "page_anchors": [],
        "pdf_pages": [],
        "pdf_page_mappings": [],
    }


class DocumentGroupRebuildPreservationTests(unittest.TestCase):
    """Groups are user data and must survive a from-scratch index rebuild (B1)."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "index.sqlite3"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _build(self, source_ids) -> None:
        build_database(
            _rebuild_index(source_ids), self.db, backup_existing=self.db.exists()
        )

    def test_rebuild_preserves_groups_members_base_and_order(self) -> None:
        self._build(["a", "b", "c"])
        gid = dg.create_document_group("组", self.db)["document_group_id"]
        dg.add_group_member(gid, "a", self.db, version_label="甲本")
        dg.add_group_member(gid, "b", self.db)
        dg.set_document_group_base(gid, "a", self.db)

        self._build(["a", "b", "c"])  # simulate a full index rebuild

        groups = dg.list_document_groups(self.db)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["title"], "组")
        self.assertEqual(groups[0]["base_source_file_id"], "a")
        members = {m["source_file_id"]: m for m in groups[0]["members"]}
        self.assertEqual(members["a"]["version_label"], "甲本")
        self.assertEqual(members["a"]["member_order"], 0)
        self.assertEqual(members["b"]["member_order"], 1)

    def test_rebuild_skips_missing_source_and_clears_base(self) -> None:
        self._build(["a", "b"])
        gid = dg.create_document_group("组", self.db)["document_group_id"]
        dg.add_group_member(gid, "a", self.db)
        dg.add_group_member(gid, "b", self.db)
        dg.set_document_group_base(gid, "a", self.db)

        self._build(["b"])  # "a" is no longer imported

        group = dg.list_document_groups(self.db)[0]
        self.assertEqual({m["source_file_id"] for m in group["members"]}, {"b"})
        self.assertIsNone(group["base_source_file_id"])

    def test_rebuild_keeps_group_when_all_members_missing(self) -> None:
        self._build(["a"])
        gid = dg.create_document_group("孤组", self.db)["document_group_id"]
        dg.add_group_member(gid, "a", self.db)

        self._build(["b"])  # every member gone

        groups = dg.list_document_groups(self.db)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["members"], [])
        self.assertIsNone(groups[0]["base_source_file_id"])

    def test_backup_snapshot_replaces_current_groups_and_skips_missing_sources(self) -> None:
        self._build(["a", "b"])
        current_id = dg.create_document_group("当前组", self.db)["document_group_id"]
        dg.add_group_member(current_id, "a", self.db)
        snapshot = {
            "document_groups": [
                {
                    "document_group_id": "backup-group",
                    "title": "备份组",
                    "base_source_file_id": "missing",
                    "created_at": "t",
                    "updated_at": "t",
                }
            ],
            "document_group_members": [
                {
                    "document_group_id": "backup-group",
                    "source_file_id": "b",
                    "version_label": "译本",
                    "member_order": 0,
                    "added_at": "t",
                },
                {
                    "document_group_id": "backup-group",
                    "source_file_id": "missing",
                    "version_label": "原版",
                    "member_order": 1,
                    "added_at": "t",
                },
            ],
        }

        dg.replace_document_group_snapshot(snapshot, self.db)

        groups = dg.list_document_groups(self.db)
        self.assertEqual([group["document_group_id"] for group in groups], ["backup-group"])
        self.assertEqual([member["source_file_id"] for member in groups[0]["members"]], ["b"])
        self.assertIsNone(groups[0]["base_source_file_id"])

    def test_rebuild_does_not_reintroduce_folders(self) -> None:
        self._build(["a"])
        connection = sqlite3.connect(str(self.db))
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            source_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(source_files)")
            }
        finally:
            connection.close()
        self.assertNotIn("folders", tables)
        self.assertNotIn("folder_id", source_columns)
        self.assertIn("document_groups", tables)
        self.assertIn("document_group_members", tables)


if __name__ == "__main__":
    unittest.main()

"""Translation-comparison workspace: v7 storage, pair overview, link window,
reader review writes and moving books between works."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from src.me_finder import translation_works
from src.me_finder.alignment_overrides import (
    confirm_override,
    create_override_proposal,
    revoke_override,
)
from src.me_finder.document_groups import (
    list_document_groups,
    move_members_into_group,
)
from src.me_finder.persistence.index_schema import SCHEMA
from src.me_finder.persistence.migrations import migrate_index_database
from src.me_finder.text_alignment import (
    AlignmentNotFound,
    InvalidAlignmentRequest,
    generate_alignment,
    locate_alignment,
)
from src.me_finder.translation_work_controller import TranslationWorkController
from tests import test_alignment_overrides as override_fixture
from tests.test_text_alignment import _page


class _ThreeVersionWork(unittest.TestCase):
    """German base with a Chinese and an English version."""

    def setUp(self) -> None:
        override_fixture.AlignmentOverrideTests.setUp(self)
        self.addCleanup(self.embedding_patch.stop)
        self.addCleanup(self.directory.cleanup)
        english_text = "The spirit is actual. The true is the whole."
        english = _page(
            "pdf-en",
            0,
            english_text,
            [{"block_index": 0, "text": english_text, "bbox": [1, 2, 3, 4],
              "bbox_normalized": [0.1, 0.1, 0.2, 0.2]}],
        )
        connection = sqlite3.connect(str(self.db))
        connection.execute(
            "INSERT INTO source_files(source_file_id, source_type, file_name, "
            "relative_path, volume_number, payload_json) VALUES "
            "('pdf-en', 'pdf', 'phenomenology.pdf', NULL, NULL, ?)",
            (json.dumps({"source_file_id": "pdf-en", "title": "Phenomenology",
                         "language_code": "en"}),),
        )
        connection.execute(
            "INSERT INTO document_group_members(document_group_id, source_file_id, "
            "version_label, member_order, added_at) VALUES ('work-one', 'pdf-en', NULL, 2, 't')"
        )
        connection.execute(
            "INSERT INTO pdf_pages(source_file_id, pdf_page_index, payload_json) "
            "VALUES ('pdf-en', 0, ?)",
            (json.dumps(english),),
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        pass

    def _pair(self, overview, left, right):
        work = overview["works"][0]
        for pair in work["pairs"]:
            if set(pair["source_file_ids"]) == {left, right}:
                return pair
        self.fail("pair missing")

    def _model_id(self) -> str:
        connection = sqlite3.connect(str(self.db))
        try:
            parameters = connection.execute(
                "SELECT parameters_json FROM alignment_runs LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()
        return json.loads(parameters)["embedding_model_id"]


class TranslationWorkOverviewTests(_ThreeVersionWork):
    def test_lightweight_overview_keeps_status_without_reading_link_statistics(self):
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        full = translation_works.alignment_overview(self.db)
        with mock.patch.object(translation_works, "_direct_run_statistics", side_effect=AssertionError("eager statistics")):
            light = translation_works.alignment_overview(self.db, include_statistics=False)
        for work in full["works"]:
            for pair in work["pairs"]:
                pair["matched_segment_ratio"] = None
                pair["review_count"] = None
        self.assertEqual(light, full)

    def test_scoped_overview_only_computes_requested_pair(self):
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-en")
        full = translation_works.alignment_overview(self.db)
        with mock.patch.object(translation_works, "_direct_run_statistics", wraps=translation_works._direct_run_statistics) as stats:
            pair = translation_works.alignment_overview(self.db, source_id="pdf-zh", target_id="pdf-de")
        self.assertEqual(stats.call_count, 1)
        self.assertEqual(pair["works"][0]["pairs"], [self._pair(full, "pdf-zh", "pdf-de")])
        self.assertEqual(translation_works.alignment_overview(self.db, source_id="not-grouped", include_statistics=False)["works"], [])

    def test_overview_reports_direct_indirect_and_none(self) -> None:
        overview = translation_works.alignment_overview(self.db)
        self.assertEqual(self._pair(overview, "pdf-de", "pdf-zh")["status"], "none")

        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-en")
        model_id = self._model_id()
        overview = translation_works.alignment_overview(
            self.db, active_model_id=model_id
        )
        direct = self._pair(overview, "pdf-de", "pdf-zh")
        self.assertEqual(direct["status"], "direct")
        self.assertIsNone(direct["stale_reason"])
        self.assertIsInstance(direct["matched_segment_ratio"], float)
        self.assertGreaterEqual(direct["review_count"], 0)
        indirect = self._pair(overview, "pdf-zh", "pdf-en")
        self.assertEqual(indirect["status"], "indirect")
        self.assertEqual(indirect["via_source_file_id"], "pdf-de")
        self.assertIsNone(indirect["matched_segment_ratio"])

    def test_model_change_marks_pair_stale_but_readable(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        overview = translation_works.alignment_overview(
            self.db, active_model_id="another-model"
        )
        pair = self._pair(overview, "pdf-de", "pdf-zh")
        self.assertEqual(pair["status"], "direct")
        self.assertEqual(pair["stale_reason"], "model_changed")

    def test_corrected_body_detection_marks_detected_run_stale(self) -> None:
        run = generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        model_id = self._model_id()
        with sqlite3.connect(str(self.db)) as connection:
            parameters = json.loads(connection.execute(
                "SELECT parameters_json FROM alignment_runs WHERE alignment_run_id=?",
                (run["alignment_run_id"],),
            ).fetchone()[0])
            self.assertEqual(parameters["body_range_source"], "detected")
            stored_target = list(parameters["body_ranges"]["target"])
            # Simulate a run saved by older detection that truncated the body.
            parameters["body_ranges"]["target"] = [stored_target[0], stored_target[0] + 1]
            connection.execute(
                "UPDATE alignment_runs SET parameters_json=? WHERE alignment_run_id=?",
                (json.dumps(parameters), run["alignment_run_id"]),
            )
        translation_works._DETECTED_BOUNDS_CACHE.clear()
        pair = self._pair(
            translation_works.alignment_overview(self.db, active_model_id=model_id),
            "pdf-de", "pdf-zh",
        )
        self.assertEqual(pair["stale_reason"], "body_range_changed")

        # Reviewed ranges are the user's decision, never second-guessed.
        with sqlite3.connect(str(self.db)) as connection:
            parameters["body_range_source"] = "reviewed"
            connection.execute(
                "UPDATE alignment_runs SET parameters_json=? WHERE alignment_run_id=?",
                (json.dumps(parameters), run["alignment_run_id"]),
            )
        pair = self._pair(
            translation_works.alignment_overview(self.db, active_model_id=model_id),
            "pdf-de", "pdf-zh",
        )
        self.assertIsNone(pair["stale_reason"])


class TranslationWorkLinkWindowTests(_ThreeVersionWork):
    def test_window_returns_links_with_spans_on_both_sides(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        window = translation_works.alignment_link_window(self.db, "pdf-de", "pdf-zh", 0, 0)
        self.assertIsNone(window["via_source_file_id"])
        self.assertTrue(window["links"])
        first = window["links"][0]
        self.assertTrue(first["source_spans"])
        self.assertEqual(first["source_spans"][0]["item_index"], 0)
        self.assertIsNone(first["manual"])
        self.assertFalse(first["deferred"])

    def test_indirect_window_composes_through_base(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-en")
        window = translation_works.alignment_link_window(self.db, "pdf-zh", "pdf-en", 0, 0)
        self.assertEqual(window["via_source_file_id"], "pdf-de")
        self.assertTrue(any(link["target_spans"] for link in window["links"]))

    def test_window_bounds_are_validated(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        with self.assertRaises(InvalidAlignmentRequest):
            translation_works.alignment_link_window(self.db, "pdf-de", "pdf-zh", 3, 1)
        with self.assertRaises(InvalidAlignmentRequest):
            translation_works.alignment_link_window(self.db, "pdf-de", "pdf-zh", 0, 5000)

    def test_unaligned_pair_is_not_found(self) -> None:
        with self.assertRaises(AlignmentNotFound):
            translation_works.alignment_link_window(self.db, "pdf-de", "pdf-zh", 0, 0)


class TranslationWorkReviewTests(_ThreeVersionWork):
    def test_contained_correction_keeps_scope_precedence_and_revocation(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        links = translation_works.alignment_link_window(
            self.db, "pdf-de", "pdf-zh", 0, 0
        )["links"]
        source_ids = [sid for link in links for sid in link["source_segment_ids"]]
        targets = [sid for link in links for sid in link["target_segment_ids"]]
        selection = dict(start_page_index=0, end_page_index=0, start_offset=1, end_offset=2)
        # Agent corrections continue to apply to the exact proposed selection.
        proposal = create_override_proposal(self.db, "pdf-de", "pdf-zh", source_ids, targets)
        confirm_override(self.db, proposal["override_id"], proposal["confirmation_token"])
        self.assertEqual(locate_alignment(self.db, "pdf-de", "pdf-zh", **selection)["alignment_source"], "automatic")
        broad = translation_works.save_correction(self.db, "pdf-de", "pdf-zh", source_ids, [])
        exact = translation_works.save_correction(self.db, "pdf-de", "pdf-zh", [source_ids[0]], targets)
        self.assertEqual(locate_alignment(self.db, "pdf-de", "pdf-zh", **selection)["manual_override_id"], exact["override_id"])
        revoke_override(self.db, exact["override_id"])
        with self.assertRaisesRegex(AlignmentNotFound, "已人工确认"):
            locate_alignment(self.db, "pdf-de", "pdf-zh", **selection)
        revoke_override(self.db, broad["override_id"])
        self.assertEqual(locate_alignment(self.db, "pdf-de", "pdf-zh", **selection)["alignment_source"], "automatic")

    def test_multi_source_correction_applies_to_scroll_selection(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        connection = sqlite3.connect(self.db)
        links = connection.execute(
            "SELECT alignment_link_id FROM alignment_links ORDER BY order_index"
        ).fetchall()
        # The algorithm supports links joining several source segments.
        for order, (other,) in enumerate(links[1:], 1):
            connection.execute(
                "UPDATE alignment_link_members SET alignment_link_id = ?, "
                "member_order = member_order + ? WHERE alignment_link_id = ?",
                (links[0][0], order * 100, other),
            )
            connection.execute(
                "DELETE FROM alignment_links WHERE alignment_link_id = ?", (other,)
            )
        connection.commit()
        connection.close()
        link = self._first_link()
        self.assertGreater(len(link["source_segment_ids"]), 1)
        for targets in (link["target_segment_ids"][-1:], []):
            with self.subTest(targets=targets):
                translation_works.save_correction(
                    self.db, "pdf-de", "pdf-zh", link["source_segment_ids"], targets
                )
                self.assertIsNotNone(self._first_link()["manual"])
                selection = dict(start_page_index=0, end_page_index=0,
                                 start_offset=1, end_offset=2)
                if targets:
                    located = locate_alignment(self.db, "pdf-de", "pdf-zh", **selection)
                    self.assertEqual(located["alignment_source"], "manual_review")
                else:
                    with self.assertRaisesRegex(AlignmentNotFound, "已人工确认"):
                        locate_alignment(self.db, "pdf-de", "pdf-zh", **selection)

    def _first_link(self):
        return translation_works.alignment_link_window(
            self.db, "pdf-de", "pdf-zh", 0, 0
        )["links"][0]

    def _reject_all_links(self) -> None:
        """Make the generated links low-confidence so they await a human."""

        connection = sqlite3.connect(str(self.db))
        try:
            connection.execute(
                "UPDATE alignment_links SET review_status = 'rejected'"
            )
            connection.commit()
        finally:
            connection.close()

    def _review_count(self) -> int:
        overview = translation_works.alignment_overview(self.db)
        return self._pair(overview, "pdf-de", "pdf-zh")["review_count"]

    def test_correction_settles_link_for_locate_window_and_count(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        self._reject_all_links()
        link = self._first_link()
        self.assertTrue(link["needs_review"])
        before = self._review_count()
        translation_works.save_correction(
            self.db, "pdf-de", "pdf-zh", link["source_segment_ids"],
            link["target_segment_ids"],
        )
        corrected = self._first_link()
        self.assertEqual(corrected["manual"], "corrected")
        self.assertFalse(corrected["needs_review"])
        self.assertEqual(self._review_count(), before - 1)
        located = locate_alignment(
            self.db, "pdf-de", "pdf-zh", start_page_index=0, end_page_index=0,
            start_offset=1, end_offset=2,
        )
        self.assertEqual(located["alignment_source"], "manual_review")

    def test_correction_from_the_other_side_settles_the_same_link(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        self._reject_all_links()
        link = self._first_link()
        before = self._review_count()
        translation_works.save_correction(
            self.db, "pdf-zh", "pdf-de", link["target_segment_ids"],
            link["source_segment_ids"],
        )
        self.assertFalse(self._first_link()["needs_review"])
        self.assertEqual(self._review_count(), before - 1)

    def test_correction_left_behind_by_resegmentation_is_stale_everywhere(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        self._reject_all_links()
        link = self._first_link()
        before = self._review_count()
        translation_works.save_correction(
            self.db, "pdf-de", "pdf-zh", link["source_segment_ids"],
            link["target_segment_ids"],
        )
        self.assertEqual(self._review_count(), before - 1)
        # A re-alignment re-segments the target: the stored correction now
        # names segments this alignment no longer uses.
        connection = sqlite3.connect(str(self.db))
        try:
            connection.execute(
                "UPDATE alignment_manual_overrides SET target_segment_set_id = ?",
                ("segment-set-retired",),
            )
            connection.commit()
        finally:
            connection.close()
        stale = self._first_link()
        self.assertIsNone(stale["manual"])
        self.assertTrue(stale["needs_review"])
        self.assertEqual(self._review_count(), before)
        # Back to the algorithm's own verdict, which for a rejected link is
        # "too low to locate" rather than a correction on a retired segment.
        with self.assertRaisesRegex(AlignmentNotFound, "置信度过低"):
            locate_alignment(
                self.db, "pdf-de", "pdf-zh", start_page_index=0, end_page_index=0,
                start_offset=1, end_offset=2,
            )

    def test_one_to_many_correction_applies_immediately(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        link = self._first_link()
        candidates = translation_works.review_candidates(
            self.db, "pdf-de", "pdf-zh", link["source_segment_ids"],
            link["target_segment_ids"], 2,
        )
        all_targets = [item["segment_id"] for item in candidates["candidates"]]
        self.assertGreaterEqual(len(all_targets), 2)
        result = translation_works.save_correction(
            self.db, "pdf-de", "pdf-zh", link["source_segment_ids"], all_targets[:2]
        )
        self.assertEqual(result["manual"], "corrected")
        corrected = self._first_link()
        self.assertEqual(corrected["manual"], "corrected")
        self.assertEqual(corrected["target_segment_ids"], all_targets[:2])

    def test_no_counterpart_correction_is_reported_by_locate(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        link = self._first_link()
        translation_works.save_correction(
            self.db, "pdf-de", "pdf-zh", link["source_segment_ids"], []
        )
        self.assertEqual(self._first_link()["manual"], "no_counterpart")
        with self.assertRaises(AlignmentNotFound):
            locate_alignment(
                self.db, "pdf-de", "pdf-zh", start_page_index=0, end_page_index=0,
                start_offset=0, end_offset=len("Der Geist ist wirklich."),
            )

    def test_deferral_marks_link_and_correction_clears_it(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        link = self._first_link()
        translation_works.defer_review(
            self.db, "pdf-de", "pdf-zh", link["source_segment_ids"]
        )
        translation_works.defer_review(
            self.db, "pdf-de", "pdf-zh", link["source_segment_ids"]
        )
        self.assertTrue(self._first_link()["deferred"])
        translation_works.save_correction(
            self.db, "pdf-de", "pdf-zh", link["source_segment_ids"],
            link["target_segment_ids"],
        )
        connection = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM alignment_review_deferrals"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()


class TranslationWorkStorageTests(_ThreeVersionWork):
    def test_reading_position_round_trip_and_membership_check(self) -> None:
        self.assertIsNone(
            translation_works.read_reading_position(self.db, "work-one")["position"]
        )
        translation_works.save_reading_position(
            self.db, "work-one", "pdf-zh", "pdf-de", 3, 17
        )
        translation_works.save_reading_position(
            self.db, "work-one", "pdf-zh", None, 4, 0
        )
        position = translation_works.read_reading_position(self.db, "work-one")["position"]
        self.assertEqual(position["left_source_file_id"], "pdf-zh")
        self.assertIsNone(position["right_source_file_id"])
        self.assertEqual(position["item_index"], 4)
        with self.assertRaises(InvalidAlignmentRequest):
            translation_works.save_reading_position(
                self.db, "work-one", "pdf-zh", "pdf-zh", 0
            )

    def test_suggestion_dismissal_is_order_independent(self) -> None:
        translation_works.dismiss_suggestion(self.db, ["pdf-zh", "pdf-de"])
        translation_works.dismiss_suggestion(self.db, ["pdf-de", "pdf-zh"])
        self.assertEqual(
            translation_works.list_suggestion_dismissals(self.db)["dismissals"],
            [["pdf-de", "pdf-zh"]],
        )
        with self.assertRaises(InvalidAlignmentRequest):
            translation_works.dismiss_suggestion(self.db, ["pdf-de"])


class MoveMembersTests(_ThreeVersionWork):
    def test_new_work_takes_first_as_base_and_empties_are_deleted(self) -> None:
        connection = sqlite3.connect(str(self.db))
        connection.execute(
            "INSERT INTO document_groups VALUES ('work-two', '其他', NULL, 't', 't')"
        )
        connection.execute("DELETE FROM document_group_members WHERE source_file_id = 'pdf-en'")
        connection.execute(
            "INSERT INTO document_group_members VALUES ('work-two', 'pdf-en', NULL, 0, 't')"
        )
        connection.commit()
        connection.close()

        result = move_members_into_group(["pdf-en", "pdf-zh"], self.db, title="新作品")
        self.assertTrue(result["created"])
        self.assertEqual(result["base_source_file_id"], "pdf-en")
        self.assertEqual(result["deleted_document_group_ids"], ["work-two"])
        groups = {group["document_group_id"]: group for group in list_document_groups(self.db)}
        self.assertNotIn("work-two", groups)
        self.assertEqual(
            [m["source_file_id"] for m in groups[result["document_group_id"]]["members"]],
            ["pdf-en", "pdf-zh"],
        )
        self.assertEqual(len(groups["work-one"]["members"]), 1)

    def test_move_into_existing_work_drops_moved_alignment_runs(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        created = move_members_into_group(["pdf-en"], self.db, title="英译")
        result = move_members_into_group(
            ["pdf-zh", "pdf-en"], self.db, document_group_id=created["document_group_id"]
        )
        self.assertFalse(result["created"])
        self.assertEqual([item["source_file_id"] for item in result["moved"]], ["pdf-zh"])
        connection = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM alignment_runs").fetchone()[0], 0
            )
        finally:
            connection.close()

    def test_move_requires_sources(self) -> None:
        with self.assertRaises(ValueError):
            move_members_into_group([], self.db, title="空")


class GroupMemberPageSourceTests(_ThreeVersionWork):
    def test_epub_members_report_publisher_pages_from_import_audit(self) -> None:
        connection = sqlite3.connect(str(self.db))
        connection.execute(
            "INSERT INTO source_files VALUES ('epub-a', 'word', 'a.epub', NULL, NULL, ?)",
            (json.dumps({"source_format": "epub"}),),
        )
        connection.execute(
            "INSERT INTO source_files VALUES ('epub-b', 'word', 'b.epub', NULL, NULL, '{}')"
        )
        connection.execute(
            "INSERT INTO audit_issues(source_file_id, issue_type, payload_json) "
            "VALUES ('epub-b', 'epub_page_list_missing', '{}')"
        )
        connection.commit()
        connection.close()
        move_members_into_group(["epub-a", "epub-b"], self.db, document_group_id="work-one")
        members = {
            m["source_file_id"]: m for m in list_document_groups(self.db)[0]["members"]
        }
        self.assertTrue(members["epub-a"]["epub_publisher_pages"])
        self.assertFalse(members["epub-b"]["epub_publisher_pages"])
        self.assertIsNone(members["pdf-de"]["epub_publisher_pages"])


class TranslationWorkMigrationTests(unittest.TestCase):
    def test_v6_database_gains_v7_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.sqlite3"
            connection = sqlite3.connect(str(path))
            connection.executescript(SCHEMA.replace("PRAGMA user_version = 7;", ""))
            connection.execute("PRAGMA user_version = 6")
            connection.commit()
            connection.close()
            self.assertTrue(migrate_index_database(path))
            connection = sqlite3.connect(str(path))
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 7
                )
            finally:
                connection.close()
            self.assertTrue(
                {
                    "document_group_reading_positions",
                    "alignment_review_deferrals",
                    "document_group_suggestion_dismissals",
                }
                <= tables
            )


class TranslationWorkControllerTests(unittest.TestCase):
    def test_overview_validates_and_passes_lightweight_scope(self):
        controller = TranslationWorkController(
            lambda operation: operation(Path("unused.sqlite3")),
            active_model_id=lambda: "model", log_exception=lambda message: None,
        )
        with mock.patch.object(translation_works, "alignment_overview", return_value={"works": []}) as overview:
            self.assertEqual(controller.overview({"include_statistics": ["0"], "source_id": ["book"]}), (200, {"works": []}))
            self.assertEqual(overview.call_args.kwargs, {"active_model_id": "model", "include_statistics": False, "source_id": "book", "target_id": ""})
            overview.reset_mock()
            for params in ({"include_statistics": ["bad"]}, {"source_id": ["a", "b"]}, {"source_id": [""]}, {"unknown": ["x"]}):
                self.assertEqual(controller.overview(params)[0], 400)
            overview.assert_not_called()

    def test_controller_maps_errors_and_rebuild(self) -> None:
        def ready(operation):
            return operation(Path("/nonexistent/index.sqlite3"))

        logged = []
        controller = TranslationWorkController(
            ready, active_model_id=lambda: "m", log_exception=logged.append
        )
        status, body = controller.links({"source_file_id": ["a"]})
        self.assertEqual(status, 400)
        status, body = controller.save_reading_position(["not", "a", "mapping"])
        self.assertEqual(status, 400)
        busy = TranslationWorkController(
            lambda operation: None, active_model_id=lambda: "m", log_exception=logged.append
        )
        self.assertEqual(busy.overview()[0], 503)


if __name__ == "__main__":
    unittest.main()

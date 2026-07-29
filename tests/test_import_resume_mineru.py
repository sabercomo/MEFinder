from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder.mineru_api import (
    MinerUError,
    download_done_results,
    load_segment_manifest,
    save_segment_manifest,
    save_state,
    submit_local_pdf_segments,
)
from src.me_finder.pdf_extractors import load_mineru_pdf_pages
from src.me_finder.pdf_import_service import parse_pdf_with_mineru, register_pdf


class MinerUResumeTests(unittest.TestCase):
    def test_partial_submission_checkpoint_reuses_active_batch_and_retries_failed_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"stable-pdf")
            manifests = root / "manifests"
            tasks = root / "tasks"
            results = root / "results"

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=3),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    side_effect=[
                        {"batch_id": "batch-1"},
                        MinerUError("upload interrupted"),
                    ],
                ),
            ):
                with self.assertRaisesRegex(
                    MinerUError,
                    "upload interrupted",
                ) as raised:
                    submit_local_pdf_segments(
                        pdf,
                        state_dir=tasks,
                        manifest_dir=manifests,
                        result_dir=results,
                        data_id_prefix="source",
                        segment_size=2,
                    )
            self.assertFalse(raised.exception.allow_parser_fallback)

            checkpoint = load_segment_manifest("source", manifests)
            self.assertIsNotNone(checkpoint)
            segments = checkpoint["segments"]
            self.assertEqual(segments[0]["batch_id"], "batch-1")
            self.assertEqual(segments[0]["status"], "submitted")
            self.assertEqual(segments[1]["status"], "failed")

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=3),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "batch-2"},
                ) as submit,
            ):
                resumed = submit_local_pdf_segments(
                    pdf,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                    segment_size=2,
                )

            submit.assert_called_once()
            self.assertEqual(submit.call_args.kwargs["page_ranges"], "3-3")
            self.assertEqual(resumed["segments"][0]["batch_id"], "batch-1")
            self.assertTrue(resumed["segments"][0]["resumed_existing_batch"])
            self.assertEqual(resumed["segments"][1]["batch_id"], "batch-2")
            self.assertEqual(resumed["resume_count"], 1)
            self.assertEqual(resumed["failed_pages"], [])

    def test_stale_result_cannot_override_active_batch_after_fresh_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            manifests = root / "manifests"
            tasks = root / "tasks"
            results = root / "custom-results"
            complete = results / "source-p001-001"
            complete.mkdir(parents=True)
            (complete / "content_list.json").write_text("[]", encoding="utf-8")

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "initial"},
                ) as submit,
            ):
                manifest = submit_local_pdf_segments(
                    pdf,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                )

            submit.assert_called_once()
            self.assertEqual(manifest["segments"][0]["status"], "submitted")
            self.assertEqual(manifest["segments"][0]["batch_id"], "initial")

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch("src.me_finder.mineru_api.submit_local_pdf") as submit,
            ):
                resumed = submit_local_pdf_segments(
                    pdf,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                )

            submit.assert_not_called()
            self.assertEqual(resumed["segments"][0]["status"], "submitted")
            self.assertEqual(resumed["segments"][0]["batch_id"], "initial")
            self.assertTrue(resumed["segments"][0]["resumed_existing_batch"])
            self.assertEqual(resumed["completed_pages"], [])

    def test_completed_result_requires_matching_identity_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            manifests = root / "manifests"
            results = root / "results"

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "initial"},
                ),
            ):
                seeded = submit_local_pdf_segments(
                    pdf,
                    state_dir=root / "tasks",
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                )

            complete = results / "source-p001-001"
            complete.mkdir(parents=True)
            (complete / "content_list.json").write_text("[]", encoding="utf-8")
            (complete / ".mefinder-result-complete.json").write_text(
                json.dumps(
                    {
                        "batch_id": "initial",
                        "data_id": "source-p001-001",
                        "file_hash": seeded["file_hash"],
                        "parse_options_fingerprint": seeded[
                            "parse_options_fingerprint"
                        ],
                    }
                ),
                encoding="utf-8",
            )
            checkpoint = load_segment_manifest("source", manifests)
            checkpoint["segments"][0].update(
                {"status": "completed", "result_dir": str(complete)}
            )
            save_segment_manifest("source", checkpoint, manifests)

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch("src.me_finder.mineru_api.submit_local_pdf") as submit,
            ):
                resumed = submit_local_pdf_segments(
                    pdf,
                    state_dir=root / "tasks",
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                )

            submit.assert_not_called()
            self.assertEqual(resumed["segments"][0]["status"], "skipped_existing_result")
            self.assertEqual(Path(resumed["segments"][0]["result_dir"]), complete)
            self.assertEqual(resumed["completed_pages"], [1])

            marker = json.loads(
                (complete / ".mefinder-result-complete.json").read_text(
                    encoding="utf-8"
                )
            )
            marker["parse_options_fingerprint"] = "wrong-options"
            (complete / ".mefinder-result-complete.json").write_text(
                json.dumps(marker),
                encoding="utf-8",
            )
            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch("src.me_finder.mineru_api.submit_local_pdf") as submit,
            ):
                invalid_marker = submit_local_pdf_segments(
                    pdf,
                    state_dir=root / "tasks",
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                )

            submit.assert_not_called()
            self.assertEqual(invalid_marker["segments"][0]["status"], "processing")
            self.assertTrue(
                invalid_marker["segments"][0]["resumed_existing_batch"]
            )

    def test_exact_resume_rejects_bare_partial_result_without_active_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            manifests = root / "manifests"
            tasks = root / "tasks"
            results = root / "custom-results"

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "initial"},
                ),
            ):
                submit_local_pdf_segments(
                    pdf,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                )

            checkpoint = load_segment_manifest("source", manifests)
            checkpoint["segments"][0].pop("batch_id", None)
            checkpoint["segments"][0]["status"] = "failed"
            partial = results / "source-p001-001"
            partial.mkdir(parents=True)
            (partial / "download.tmp").write_text("partial", encoding="utf-8")
            save_segment_manifest("source", checkpoint, manifests)

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "replacement"},
                ) as submit,
            ):
                resumed = submit_local_pdf_segments(
                    pdf,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                )

            submit.assert_called_once()
            self.assertEqual(resumed["segments"][0]["batch_id"], "replacement")
            self.assertEqual(resumed["segments"][0]["status"], "submitted")

    def test_changed_file_does_not_reuse_modern_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"version-one")
            manifests = root / "manifests"
            tasks = root / "tasks"
            results = root / "results"

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "old-batch"},
                ),
            ):
                old_manifest = submit_local_pdf_segments(
                    pdf,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="manual-prefix",
                )

            complete = results / "manual-prefix-p001-001"
            complete.mkdir(parents=True)
            (complete / "content_list.json").write_text("[]", encoding="utf-8")
            (complete / ".mefinder-result-complete.json").write_text(
                json.dumps(
                    {
                        "batch_id": "old-batch",
                        "data_id": "manual-prefix-p001-001",
                        "file_hash": old_manifest["file_hash"],
                        "parse_options_fingerprint": old_manifest[
                            "parse_options_fingerprint"
                        ],
                    }
                ),
                encoding="utf-8",
            )
            pdf.write_bytes(b"version-two")
            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "new-batch"},
                ) as submit,
            ):
                manifest = submit_local_pdf_segments(
                    pdf,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="manual-prefix",
                )

            submit.assert_called_once()
            self.assertEqual(manifest["segments"][0]["batch_id"], "new-batch")
            self.assertNotIn("resumed_existing_batch", manifest["segments"][0])
            self.assertEqual(manifest["resume_count"], 0)

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch("src.me_finder.mineru_api.submit_local_pdf") as submit,
            ):
                resumed = submit_local_pdf_segments(
                    pdf,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="manual-prefix",
                )

            submit.assert_not_called()
            self.assertEqual(resumed["segments"][0]["batch_id"], "new-batch")
            self.assertEqual(resumed["segments"][0]["status"], "submitted")
            self.assertTrue(resumed["segments"][0]["resumed_existing_batch"])

    def test_legacy_manifest_reuses_neither_stale_result_nor_active_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            manifests = root / "manifests"
            manifests.mkdir()
            legacy = {
                "api": "precision",
                "data_id_prefix": "source",
                "total_pages": 1,
                "segments": [
                    {
                        "data_id": "source-p001-001",
                        "page_ranges": "1-1",
                        "status": "submitted",
                        "batch_id": "legacy-batch",
                        "result_dir": str(root / "results/source-p001-001"),
                    }
                ],
            }
            (manifests / "segments-source.json").write_text(
                json.dumps(legacy),
                encoding="utf-8",
            )
            stale_result = root / "results/source-p001-001"
            stale_result.mkdir(parents=True)
            (stale_result / "content_list.json").write_text("[]", encoding="utf-8")

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "fresh-batch"},
                ) as submit,
            ):
                manifest = submit_local_pdf_segments(
                    pdf,
                    state_dir=root / "tasks",
                    manifest_dir=manifests,
                    result_dir=root / "results",
                    data_id_prefix="source",
                )

            submit.assert_called_once()
            self.assertEqual(manifest["segments"][0]["batch_id"], "fresh-batch")

    def test_corrupt_manifest_quarantine_does_not_authorize_stale_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            manifests = root / "manifests"
            manifests.mkdir()
            manifest_path = manifests / "segments-source.json"
            manifest_path.write_text("{not-json", encoding="utf-8")
            stale_result = root / "results/source-p001-001"
            stale_result.mkdir(parents=True)
            (stale_result / "content_list.json").write_text("[]", encoding="utf-8")

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "fresh-batch"},
                ) as submit,
            ):
                manifest = submit_local_pdf_segments(
                    pdf,
                    state_dir=root / "tasks",
                    manifest_dir=manifests,
                    result_dir=root / "results",
                    data_id_prefix="source",
                )

            submit.assert_called_once()
            self.assertEqual(manifest["segments"][0]["batch_id"], "fresh-batch")
            self.assertEqual(
                len(list(manifests.glob("segments-source.json.corrupt-*"))),
                1,
            )

    def test_semantically_invalid_segment_lists_are_quarantined(self) -> None:
        invalid_segments = (
            7,
            [{"data_id": "source-p001-001"}, 8],
        )
        for raw_segments in invalid_segments:
            with self.subTest(raw_segments=raw_segments):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    pdf = root / "paper.pdf"
                    pdf.write_bytes(b"pdf")
                    manifests = root / "manifests"
                    manifests.mkdir()
                    (manifests / "segments-source.json").write_text(
                        json.dumps({"segments": raw_segments}),
                        encoding="utf-8",
                    )

                    with (
                        patch(
                            "src.me_finder.mineru_api.get_pdf_page_count",
                            return_value=1,
                        ),
                        patch(
                            "src.me_finder.mineru_api.submit_local_pdf",
                            return_value={"batch_id": "fresh-batch"},
                        ) as submit,
                    ):
                        manifest = submit_local_pdf_segments(
                            pdf,
                            state_dir=root / "tasks",
                            manifest_dir=manifests,
                            result_dir=root / "results",
                            data_id_prefix="source",
                        )

                    submit.assert_called_once()
                    self.assertEqual(
                        manifest["segments"][0]["batch_id"],
                        "fresh-batch",
                    )
                    self.assertEqual(
                        len(
                            list(
                                manifests.glob(
                                    "segments-source.json.corrupt-*"
                                )
                            )
                        ),
                        1,
                    )

    def test_interruption_preserves_later_segment_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            manifests = root / "manifests"

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=3),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    side_effect=[
                        {"batch_id": "batch-1"},
                        {"batch_id": "batch-2"},
                        {"batch_id": "batch-3"},
                    ],
                ),
            ):
                submit_local_pdf_segments(
                    pdf,
                    state_dir=root / "tasks",
                    manifest_dir=manifests,
                    result_dir=root / "results",
                    data_id_prefix="source",
                    segment_size=1,
                )

            checkpoint = load_segment_manifest("source", manifests)
            checkpoint["segments"][0]["status"] = "failed"
            save_segment_manifest("source", checkpoint, manifests)

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=3),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    side_effect=KeyboardInterrupt(),
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    submit_local_pdf_segments(
                        pdf,
                        state_dir=root / "tasks",
                        manifest_dir=manifests,
                        result_dir=root / "results",
                        data_id_prefix="source",
                        segment_size=1,
                    )

            interrupted = load_segment_manifest("source", manifests)
            self.assertEqual(len(interrupted["segments"]), 3)
            self.assertEqual(interrupted["segments"][0]["status"], "submitting")
            self.assertEqual(interrupted["segments"][1]["batch_id"], "batch-2")
            self.assertEqual(interrupted["segments"][1]["status"], "submitted")
            self.assertEqual(interrupted["segments"][2]["batch_id"], "batch-3")
            self.assertEqual(interrupted["segments"][2]["status"], "submitted")

    def test_resume_recovers_batch_state_saved_before_manifest_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            manifests = root / "manifests"
            tasks = root / "tasks"

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "initial"},
                ),
            ):
                manifest = submit_local_pdf_segments(
                    pdf,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=root / "results",
                    data_id_prefix="source",
                )

            checkpoint = load_segment_manifest("source", manifests)
            checkpoint["segments"][0].pop("batch_id", None)
            checkpoint["segments"][0]["status"] = "submitting"
            save_segment_manifest("source", checkpoint, manifests)
            save_state(
                "recovered-batch",
                {
                    "batch_id": "recovered-batch",
                    "data_id": "source-p001-001",
                    "file_hash": manifest["file_hash"],
                    "parse_options_fingerprint": manifest[
                        "parse_options_fingerprint"
                    ],
                    "submitted_at": 99,
                },
                tasks,
            )

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch("src.me_finder.mineru_api.submit_local_pdf") as submit,
            ):
                resumed = submit_local_pdf_segments(
                    pdf,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=root / "results",
                    data_id_prefix="source",
                )

            submit.assert_not_called()
            self.assertEqual(
                resumed["segments"][0]["batch_id"],
                "recovered-batch",
            )
            self.assertTrue(resumed["segments"][0]["resumed_existing_batch"])

    def test_exact_manifest_result_path_cannot_escape_result_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            manifests = root / "manifests"
            results = root / "results"

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "initial"},
                ),
            ):
                submit_local_pdf_segments(
                    pdf,
                    state_dir=root / "tasks",
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                )

            outside = root / "outside"
            outside.mkdir()
            (outside / "content_list.json").write_text("[]", encoding="utf-8")
            checkpoint = load_segment_manifest("source", manifests)
            (outside / ".mefinder-result-complete.json").write_text(
                json.dumps(
                    {
                        "batch_id": "initial",
                        "data_id": "source-p001-001",
                        "file_hash": checkpoint["file_hash"],
                        "parse_options_fingerprint": checkpoint[
                            "parse_options_fingerprint"
                        ],
                    }
                ),
                encoding="utf-8",
            )
            checkpoint["segments"][0].update(
                {
                    "status": "completed",
                    "result_dir": str(outside),
                }
            )
            checkpoint["segments"][0].pop("batch_id", None)
            save_segment_manifest("source", checkpoint, manifests)

            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "safe-replacement"},
                ) as submit,
            ):
                resumed = submit_local_pdf_segments(
                    pdf,
                    state_dir=root / "tasks",
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                )

            submit.assert_called_once()
            self.assertEqual(resumed["segments"][0]["batch_id"], "safe-replacement")
            self.assertNotEqual(
                resumed["segments"][0].get("result_dir"),
                str(outside),
            )

    def test_download_completion_marker_carries_resume_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "tasks"
            result_root = root / "results"
            save_state(
                "batch-1",
                {
                    "batch_id": "batch-1",
                    "data_id": "source-p001-001",
                    "file_hash": "file-sha",
                    "parse_options_fingerprint": "options-sha",
                },
                state_dir,
            )

            def fake_extract(_zip_path: Path, output_dir: Path) -> None:
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "content_list.json").write_text(
                    "[]",
                    encoding="utf-8",
                )

            with (
                patch(
                    "src.me_finder.mineru_api.load_mineru_config",
                    return_value=object(),
                ),
                patch("src.me_finder.mineru_api.MinerUClient") as client_class,
                patch(
                    "src.me_finder.mineru_api.extract_zip",
                    side_effect=fake_extract,
                ),
            ):
                client_class.return_value.batch_status.return_value = {
                    "data": {
                        "extract_result": [
                            {
                                "state": "done",
                                "data_id": "source-p001-001",
                                "full_zip_url": "https://example.test/result.zip",
                            }
                        ]
                    }
                }
                paths = download_done_results(
                    "batch-1",
                    state_dir=state_dir,
                    result_dir=result_root,
                )

            marker = json.loads(
                (paths[0] / ".mefinder-result-complete.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(marker["batch_id"], "batch-1")
            self.assertEqual(marker["data_id"], "source-p001-001")
            self.assertEqual(marker["file_hash"], "file-sha")
            self.assertEqual(
                marker["parse_options_fingerprint"],
                "options-sha",
            )

    def test_poll_failure_and_timeout_keep_batch_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            source = register_pdf(root, pdf)
            source_id = str(source["source_file_id"])
            manifest = {
                "api": "precision",
                "parser": "mineru",
                "data_id_prefix": source_id,
                "total_pages": 1,
                "segments": [
                    {
                        "data_id": f"{source_id}-p001-001",
                        "page_ranges": "1-1",
                        "page_index_offset": 0,
                        "status": "submitted",
                        "batch_id": "active-batch",
                    }
                ],
            }

            with (
                patch(
                    "src.me_finder.pdf_import_service.submit_local_pdf_segments",
                    return_value=manifest,
                ),
                patch(
                    "src.me_finder.pdf_import_service.get_batch_status",
                    side_effect=MinerUError("temporary network error"),
                ),
            ):
                with self.assertRaisesRegex(
                    MinerUError,
                    "temporary network error",
                ) as raised:
                    parse_pdf_with_mineru(root, pdf, source_id, poll_seconds=0)
            self.assertFalse(raised.exception.allow_parser_fallback)

            stored = json.loads(
                (root / "corpus/processed/mineru/manifests" / f"segments-{source_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(stored["segments"][0]["batch_id"], "active-batch")
            self.assertEqual(stored["segments"][0]["status"], "processing")
            self.assertIn("temporary network error", stored["segments"][0]["last_error"])

            stored["segments"][0]["status"] = "submitted"
            stored["segments"][0].pop("last_error", None)
            with patch(
                "src.me_finder.pdf_import_service.submit_local_pdf_segments",
                return_value=stored,
            ):
                with self.assertRaisesRegex(MinerUError, "超时") as raised:
                    parse_pdf_with_mineru(
                        root,
                        pdf,
                        source_id,
                        poll_seconds=0,
                        timeout_minutes=0,
                    )
            self.assertFalse(raised.exception.allow_parser_fallback)
            timed_out = json.loads(
                (root / "corpus/processed/mineru/manifests" / f"segments-{source_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(timed_out["segments"][0]["batch_id"], "active-batch")
            self.assertEqual(timed_out["status"], "processing")
            self.assertIn("等待下次继续检查", timed_out["segments"][0]["last_error"])

    def test_permanently_missing_batch_is_resubmitted_on_next_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            source = register_pdf(root, pdf)
            source_id = str(source["source_file_id"])
            manifest_dir = root / "corpus/processed/mineru/manifests"
            state_dir = root / "corpus/processed/mineru/tasks"
            result_dir = root / "corpus/processed/mineru/results"
            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "expired-batch"},
                ),
            ):
                manifest = submit_local_pdf_segments(
                    pdf,
                    state_dir=state_dir,
                    manifest_dir=manifest_dir,
                    result_dir=result_dir,
                    data_id_prefix=source_id,
                )

            with (
                patch(
                    "src.me_finder.pdf_import_service.submit_local_pdf_segments",
                    return_value=manifest,
                ),
                patch(
                    "src.me_finder.pdf_import_service.get_batch_status",
                    side_effect=MinerUError(
                        "MinerU HTTP 404: batch not found",
                        retry_with_new_task=True,
                    ),
                ),
            ):
                with self.assertRaisesRegex(MinerUError, "batch not found"):
                    parse_pdf_with_mineru(root, pdf, source_id, poll_seconds=0)

            failed = load_segment_manifest(source_id, manifest_dir)
            self.assertEqual(failed["segments"][0]["status"], "failed")
            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "replacement-batch"},
                ) as submit,
            ):
                resumed = submit_local_pdf_segments(
                    pdf,
                    state_dir=state_dir,
                    manifest_dir=manifest_dir,
                    result_dir=result_dir,
                    data_id_prefix=source_id,
                )
            submit.assert_called_once()
            self.assertEqual(
                resumed["segments"][0]["batch_id"],
                "replacement-batch",
            )

    def test_api_base_change_does_not_resume_old_remote_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            config = root / "mineru.json"
            manifests = root / "manifests"
            tasks = root / "tasks"
            results = root / "results"
            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "old-endpoint-batch"},
                ),
            ):
                submit_local_pdf_segments(
                    pdf,
                    config_path=config,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                )
            config.write_text(
                '{"api_base":"https://other.example.test"}',
                encoding="utf-8",
            )
            with (
                patch("src.me_finder.mineru_api.get_pdf_page_count", return_value=1),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    return_value={"batch_id": "new-endpoint-batch"},
                ) as submit,
            ):
                changed = submit_local_pdf_segments(
                    pdf,
                    config_path=config,
                    state_dir=tasks,
                    manifest_dir=manifests,
                    result_dir=results,
                    data_id_prefix="source",
                )
            submit.assert_called_once()
            self.assertEqual(
                changed["segments"][0]["batch_id"],
                "new-endpoint-batch",
            )

    def test_server_task_id_cannot_escape_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(MinerUError, "unsafe task identifier"):
                save_state(
                    "../../outside",
                    {"batch_id": "../../outside"},
                    root / "tasks",
                )
            self.assertFalse((root / "outside.json").exists())

    def test_segment_local_page_zero_uses_explicit_global_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "content_list.json").write_text(
                json.dumps([{"page_idx": 0, "type": "text", "text": "第一页"}]),
                encoding="utf-8",
            )
            (second / "content_list.json").write_text(
                json.dumps([{"page_idx": 0, "type": "text", "text": "第二百零一页"}]),
                encoding="utf-8",
            )
            segments = [
                {
                    "page_ranges": "1-1",
                    "page_index_offset": 0,
                    "result_dir": str(first),
                },
                {
                    "page_ranges": "201-201",
                    "page_index_offset": 200,
                    "result_dir": str(second),
                },
            ]

            with patch(
                "src.me_finder.pdf_extractors.get_pdf_page_labels",
                return_value=[None] * 201,
            ):
                pages = load_mineru_pdf_pages(
                    root / "unused.pdf",
                    "source",
                    "document",
                    {},
                    segments,
                )

            self.assertEqual([page["pdf_page_index"] for page in pages], [0, 200])
            self.assertEqual(
                [page["pdf_page_id"] for page in pages],
                ["source-PAGE-000000", "source-PAGE-000200"],
            )
            self.assertEqual(pages[1]["blocks"][0]["page_index_offset"], 200)

    def test_failed_remote_segment_is_persisted_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            source = register_pdf(root, pdf)
            source_id = str(source["source_file_id"])
            manifest = {
                "api": "precision",
                "parser": "mineru",
                "data_id_prefix": source_id,
                "total_pages": 2,
                "segments": [
                    {
                        "data_id": f"{source_id}-p001-002",
                        "page_ranges": "1-2",
                        "page_index_offset": 0,
                        "status": "submitted",
                        "batch_id": "failed-batch",
                    }
                ],
            }
            failed_status = {
                "data": {
                    "extract_result": [
                        {"state": "failed", "err_msg": "bad scan"}
                    ]
                }
            }

            with (
                patch(
                    "src.me_finder.pdf_import_service.submit_local_pdf_segments",
                    return_value=manifest,
                ),
                patch(
                    "src.me_finder.pdf_import_service.get_batch_status",
                    return_value=failed_status,
                ),
            ):
                with self.assertRaisesRegex(MinerUError, "分段解析失败"):
                    parse_pdf_with_mineru(root, pdf, source_id, poll_seconds=0)

            stored = json.loads(
                (root / "corpus/processed/mineru/manifests" / f"segments-{source_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(stored["segments"][0]["status"], "failed")
            self.assertEqual(stored["failed_page_count"], 2)
            self.assertEqual(
                [item["page"] for item in stored["failed_pages"]],
                [1, 2],
            )

    def test_completed_parse_attaches_secret_free_resume_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            source = register_pdf(root, pdf)
            source_id = str(source["source_file_id"])
            result_dir = root / "corpus/processed/mineru/results" / "segment"
            result_dir.mkdir(parents=True)
            (result_dir / "content_list.json").write_text("[]", encoding="utf-8")
            manifest = {
                "api": "precision",
                "parser": "mineru",
                "file_hash": "safe-hash",
                "data_id_prefix": source_id,
                "total_pages": 1,
                "segments": [
                    {
                        "data_id": f"{source_id}-p001-001",
                        "page_ranges": "1-1",
                        "page_index_offset": 0,
                        "status": "submitted",
                        "batch_id": "done-batch",
                    }
                ],
            }
            done_status = {
                "data": {"extract_result": [{"state": "done"}]}
            }

            with (
                patch(
                    "src.me_finder.pdf_import_service.submit_local_pdf_segments",
                    return_value=manifest,
                ),
                patch(
                    "src.me_finder.pdf_import_service.get_batch_status",
                    return_value=done_status,
                ),
                patch(
                    "src.me_finder.pdf_import_service.download_done_results",
                    return_value=[result_dir],
                ),
            ):
                result = parse_pdf_with_mineru(
                    root,
                    pdf,
                    source_id,
                    poll_seconds=0,
                )

            self.assertEqual(result["resume"]["status"], "completed")
            config = json.loads(
                (root / "config/pdf_imports.json").read_text(encoding="utf-8")
            )
            attached = config["documents"][0]["mineru"]
            self.assertEqual(attached["resume"]["completed_pages"], [1])
            self.assertEqual(attached["resume"]["failed_pages"], [])
            self.assertNotIn("token", json.dumps(attached))


if __name__ == "__main__":
    unittest.main()

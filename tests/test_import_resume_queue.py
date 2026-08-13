from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder.import_job_journal import ImportJobJournal
from src.me_finder.import_queue import ImportTaskQueue
from src.me_finder.import_resume import sha256_file


class ImportJobJournalTests(unittest.TestCase):
    def _save(
        self,
        journal: ImportJobJournal,
        target: Path,
        *,
        job_id: str = "import-one",
        status: str = "processing",
        replaces_job_id: str | None = None,
    ) -> dict[str, object]:
        return journal.save_job(
            {
                "job_id": job_id,
                "status": status,
                "phase": "vision_processing",
                "message": "正在解析",
                "runtime_callback": lambda: None,
            },
            target=target,
            source_file_id="pdf-import-deadbeef",
            profile={"detected_pdf_type": "scanned", "pdf_page_count": 3},
            is_pdf=True,
            force_mineru=False,
            provider_id="vision-one",
            total_pages=3,
            completed_pages=[1],
            failed_pages=[{"page": 2, "error": "temporary"}],
            replaces_job_id=replaces_job_id,
        )

    def test_saves_serializable_job_context_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-test")
            journal = ImportJobJournal(root / "jobs")
            expected_hash = sha256_file(target)

            record = self._save(journal, target)
            saved = json.loads(
                (root / "jobs" / "import-one.json").read_text(encoding="utf-8")
            )

        self.assertEqual(record["file_hash"], expected_hash)
        self.assertEqual(saved["file_hash"], record["file_hash"])
        self.assertEqual(saved["total_pages"], 3)
        self.assertEqual(saved["completed_pages"], [1])
        self.assertEqual(saved["failed_pages"][0]["page"], 2)
        self.assertTrue(saved["can_resume"])
        self.assertEqual(saved["source_file_id"], "pdf-import-deadbeef")
        self.assertEqual(saved["file_type"], "pdf")
        self.assertEqual(saved["context"]["target"], str(target.resolve()))
        self.assertEqual(saved["context"]["source_file_id"], "pdf-import-deadbeef")
        self.assertEqual(saved["context"]["profile"]["pdf_page_count"], 3)
        self.assertTrue(saved["context"]["is_pdf"])
        self.assertFalse(saved["context"]["force_mineru"])
        self.assertEqual(saved["context"]["provider_id"], "vision-one")
        self.assertNotIn("runtime_callback", saved)

    def test_update_is_atomic_and_preserves_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-test")
            jobs_dir = root / "jobs"
            journal = ImportJobJournal(jobs_dir)
            original = self._save(journal, target)

            updated = journal.update_job(
                "import-one",
                status="paused",
                can_resume=True,
                completed_pages=[1, 2],
                failed_pages=[],
            )

            self.assertEqual(updated["status"], "paused")
            self.assertEqual(updated["completed_pages"], [1, 2])
            self.assertEqual(updated["context"], original["context"])
            self.assertNotEqual(updated["last_updated"], "")
            self.assertEqual(journal.get_job("import-one"), updated)
            self.assertEqual(list(jobs_dir.glob("*.tmp")), [])

    def test_retry_route_and_display_fields_are_updated_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-test")
            journal = ImportJobJournal(root / "jobs")
            self._save(journal, target)

            updated = journal.switch_parser_route(
                "import-one",
                parse_route="vision",
                force_mineru=False,
                provider_id="vision-fallback",
                provider_name="备用视觉接口",
            )

            self.assertFalse(updated["context"]["force_mineru"])
            self.assertEqual(
                updated["context"]["provider_id"],
                "vision-fallback",
            )
            self.assertEqual(updated["parse_route"], "vision")
            self.assertEqual(updated["provider_id"], "vision-fallback")
            self.assertEqual(updated["provider_name"], "备用视觉接口")
            self.assertEqual(
                journal.get_job("import-one")["context"]["provider_id"],
                "vision-fallback",
            )

    def test_retry_replacement_generations_are_monotonic_per_predecessor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-test")
            journal = ImportJobJournal(root / "jobs")

            first = self._save(
                journal,
                target,
                job_id="retry-first",
                replaces_job_id="retry-old",
            )
            second = self._save(
                journal,
                target,
                job_id="retry-second",
                replaces_job_id="retry-old",
            )

        self.assertEqual(first["replaces_job_id"], "retry-old")
        self.assertEqual(first["replacement_lineage_id"], "retry-old")
        self.assertEqual(first["replacement_generation"], 1)
        self.assertEqual(second["replacement_lineage_id"], "retry-old")
        self.assertEqual(second["replacement_generation"], 2)

    def test_startup_pauses_running_jobs_without_submitting_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-test")
            journal = ImportJobJournal(root / "jobs")
            self._save(journal, target, job_id="queued-job", status="queued")
            self._save(journal, target, job_id="running-job", status="processing")

            with patch.object(ImportTaskQueue, "submit") as submit:
                restored = journal.load_startup_jobs()

            submit.assert_not_called()
            by_id = {str(item["job_id"]): item for item in restored}
            self.assertEqual(by_id["queued-job"]["status"], "paused")
            self.assertEqual(by_id["running-job"]["status"], "paused")
            self.assertTrue(by_id["queued-job"]["can_resume"])
            self.assertTrue(by_id["running-job"]["can_resume"])
            self.assertEqual(
                journal.get_job("queued-job")["status"],
                "paused",
            )

    def test_startup_does_not_rehash_unchanged_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-test")
            journal = ImportJobJournal(root / "jobs")
            self._save(journal, target)

            with patch(
                "src.me_finder.import_job_journal.sha256_file"
            ) as digest:
                restored = journal.load_startup_jobs()

            digest.assert_not_called()
            self.assertEqual(restored[0]["status"], "paused")

    def test_startup_marks_missing_target_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-test")
            journal = ImportJobJournal(root / "jobs")
            self._save(journal, target)
            target.unlink()

            restored = journal.load_startup_jobs()

            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0]["status"], "failed")
            self.assertEqual(restored[0]["phase"], "failed")
            self.assertFalse(restored[0]["can_resume"])
            self.assertIn("不存在", str(restored[0]["error"]))
            self.assertIn("不存在", str(restored[0]["message"]))

    def test_startup_refuses_checkpoint_when_file_content_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-first")
            journal = ImportJobJournal(root / "jobs")
            self._save(journal, target)
            target.write_bytes(b"%PDF-second")

            restored = journal.load_startup_jobs()

            self.assertEqual(restored[0]["status"], "failed")
            self.assertFalse(restored[0]["can_resume"])
            self.assertIn("内容已经变化", str(restored[0]["error"]))
            self.assertIn("内容已经变化", str(restored[0]["message"]))

    def test_list_jobs_quarantines_corrupt_files_and_keeps_valid_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-test")
            jobs_dir = root / "jobs"
            journal = ImportJobJournal(jobs_dir)
            self._save(journal, target)
            damaged = jobs_dir / "broken.json"
            damaged.write_text("{not json", encoding="utf-8")

            jobs = journal.list_jobs()

            self.assertEqual([item["job_id"] for item in jobs], ["import-one"])
            self.assertFalse(damaged.exists())
            self.assertEqual(len(list(jobs_dir.glob("broken.json.corrupt-*"))), 1)

    def test_list_jobs_quarantines_semantically_invalid_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-test")
            jobs_dir = root / "jobs"
            journal = ImportJobJournal(jobs_dir)
            self._save(journal, target)
            damaged = jobs_dir / "import-one.json"
            payload = json.loads(damaged.read_text(encoding="utf-8"))
            payload["context"]["profile"] = 7
            damaged.write_text(
                json.dumps(payload, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(journal.list_jobs(), [])
            self.assertFalse(damaged.exists())
            self.assertEqual(
                len(list(jobs_dir.glob("import-one.json.corrupt-*"))),
                1,
            )

    def test_rejects_unsafe_job_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-test")
            journal = ImportJobJournal(root / "jobs")

            with self.assertRaises(ValueError):
                self._save(journal, target, job_id="../outside")

    def test_completed_or_dismissed_job_can_be_removed_from_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "paper.pdf"
            target.write_bytes(b"%PDF-test")
            journal = ImportJobJournal(root / "jobs")
            self._save(journal, target)

            self.assertTrue(journal.delete_job("import-one"))
            self.assertIsNone(journal.get_job("import-one"))
            self.assertFalse(journal.delete_job("import-one"))


if __name__ == "__main__":
    unittest.main()

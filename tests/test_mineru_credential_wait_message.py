from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.me_finder.large_document.job_ledger import JobLedger
from src.me_finder.large_document.slicing import SliceDescriptor
from src.me_finder.pdf_parser_adapters import slices_are_waiting_for_credential


class MinerUCredentialWaitMessageTests(unittest.TestCase):
    """Regression: three slices on two accounts is not a credential shortage."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name)
        source = root / "corpus" / "raw_pdf" / "hegel.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"synthetic source")
        self.ledger = JobLedger(root / "data" / "parser_jobs.sqlite3")
        job = self.ledger.create_document_job(
            source_file_id="pdf-import-hegel",
            document_id="PDF_IMPORT_HEGEL",
            source_path=source,
            source_sha256="source-sha",
            provider_id="mineru-cloud",
            parser_model="vlm",
            options_fingerprint="options",
            total_pages=489,
        )
        self.job_id = job.id
        self.slices = self.ledger.add_slices(
            job.id,
            (
                SliceDescriptor(1, 200, 0, source, "slice-1", 16, True),
                SliceDescriptor(201, 400, 200, source, "slice-2", 16, True),
                SliceDescriptor(401, 489, 400, source, "slice-3", 16, True),
            ),
            "mineru-cloud",
        )

    def _current(self):
        return self.ledger.list_slice_jobs(self.job_id)

    def test_slice_queued_behind_busy_accounts_is_not_a_credential_shortage(
        self,
    ) -> None:
        # Two accounts at concurrency 1 each: slices 1-2 are in flight while
        # slice 3 waits its turn with no credential and no remote task.
        for slice_job, account_id in zip(
            self.slices, ("mineru-account-2", "mineru-account-3")
        ):
            self.ledger.update_slice(
                slice_job.id,
                status="submitted",
                credential_id=account_id,
                remote_task_id=f"remote-{slice_job.page_start}",
            )
        self.ledger.update_slice(
            self.slices[2].id,
            status="waiting",
            last_error=(
                "all configured parser credentials are disabled, "
                "cooling down, or busy"
            ),
        )

        self.assertFalse(slices_are_waiting_for_credential(self._current()))

    def test_in_flight_slice_polled_back_to_waiting_still_counts_as_running(
        self,
    ) -> None:
        # A submitted slice returns to "waiting" between polls but keeps its
        # remote task, so the queue is still progressing.
        self.ledger.update_slice(
            self.slices[0].id,
            status="waiting",
            credential_id="mineru-account-2",
            remote_task_id="remote-1",
        )
        for slice_job in self.slices[1:]:
            self.ledger.update_slice(slice_job.id, status="waiting")

        self.assertFalse(slices_are_waiting_for_credential(self._current()))

    def test_no_submitted_slice_reports_a_credential_shortage(self) -> None:
        for slice_job in self.slices:
            self.ledger.update_slice(
                slice_job.id,
                status="waiting",
                last_error=(
                    "all configured parser credentials are disabled, "
                    "cooling down, or busy"
                ),
            )

        self.assertTrue(slices_are_waiting_for_credential(self._current()))

    def test_completed_slices_alone_report_no_shortage(self) -> None:
        for slice_job in self.slices:
            self.ledger.update_slice(
                slice_job.id,
                status="completed",
                credential_id="mineru-account-2",
                remote_task_id=f"remote-{slice_job.page_start}",
            )

        self.assertFalse(slices_are_waiting_for_credential(self._current()))


if __name__ == "__main__":
    unittest.main()

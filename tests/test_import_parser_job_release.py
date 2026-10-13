"""Abandoned imports must not keep MinerU credentials busy forever.

Regression: cancelling an import left its parser-ledger job ``waiting`` with a
remote task bound to a credential.  Nothing polled it again, yet startup
reconciliation kept counting it in flight, so with one slot per account every
later import showed "等待可用的 MinerU 账号" indefinitely.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

from src.me_finder.app_context import AppPaths
from src.me_finder.application.import_job_store import ImportJobStore
from src.me_finder.application.import_orchestrator import ImportOrchestrator
from src.me_finder.import_job_journal import ImportJobJournal
from src.me_finder.large_document.credential_pool import (
    CredentialPool,
    CredentialPoolUnavailable,
)
from src.me_finder.large_document.job_ledger import JobLedger
from src.me_finder.large_document.slicing import SliceDescriptor
from src.me_finder.lifecycle import DurableOperationGate
from tests.test_import_orchestrator import _FakeIndexRuntime, _FakeQueue


PROVIDER = "mineru-cloud"


def _open_job_holding_credential(
    ledger: JobLedger,
    root: Path,
    source_file_id: str,
    credential_id: str,
) -> str:
    """Create a ``waiting`` job whose one slice holds a remote task."""

    job = ledger.create_document_job(
        source_file_id=source_file_id,
        document_id=source_file_id.upper(),
        source_path=root / f"{source_file_id}.pdf",
        source_sha256=f"sha-{source_file_id}",
        provider_id=PROVIDER,
        parser_model="vlm",
        options_fingerprint="fp",
        total_pages=10,
    )
    ledger.add_slices(
        job.id,
        [
            SliceDescriptor(
                page_start=1,
                page_end=10,
                global_page_offset=0,
                path=root / f"{source_file_id}-slice.pdf",
                sha256="slice-sha",
                size_bytes=1,
            )
        ],
        PROVIDER,
    )
    reserved = ledger.try_reserve_credential(
        credential_id,
        provider_max_concurrency=1,
        now_iso=datetime.now(timezone.utc).isoformat(),
    )
    assert reserved is not None
    (slice_job,) = ledger.list_slice_jobs(job.id)
    ledger.update_slice(
        slice_job.id,
        status="waiting",
        remote_task_id=f"remote-{source_file_id}",
        credential_id=credential_id,
    )
    ledger.update_document(job.id, status="waiting")
    return job.id


class ParserJobReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "data").mkdir()
        self.ledger = JobLedger(self.root / "data" / "parser_jobs.sqlite3")
        for credential_id in ("account-a", "account-b"):
            self.ledger.upsert_credential(
                credential_id=credential_id,
                provider_id=PROVIDER,
                display_name=credential_id,
                secret_ref=f"secret-{credential_id}",
            )
        self.paths = AppPaths.create(
            self.root,
            index_path=self.root / "data" / "index.sqlite3",
        )
        self.journal = ImportJobJournal(
            self.root / "corpus" / "processed" / "import_jobs"
        )
        self.queue = _FakeQueue()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _pool(self) -> CredentialPool:
        return CredentialPool(
            ledger=self.ledger,
            provider_id=PROVIDER,
            secret_resolver=lambda ref: f"token-for-{ref}",
            provider_max_concurrency=1,
        )

    def _orchestrator(self, store: ImportJobStore | None = None) -> ImportOrchestrator:
        def unused(*_args: object, **_kwargs: object) -> Dict[str, object]:
            raise AssertionError("not used")

        return ImportOrchestrator(
            self.paths,
            _FakeIndexRuntime(),
            DurableOperationGate(),
            self.queue,
            self.journal,
            parse_with_mineru=unused,
            parse_with_provider=unused,
            extract_pdf=unused,
            detect_metadata=unused,
            persist_metadata=unused,
            job_store=store,
            parser_jobs=self.ledger,
        )

    def _in_flight(self, credential_id: str) -> int:
        return self.ledger.get_credential(credential_id).current_in_flight

    def test_orphaned_remote_tasks_starve_every_credential(self) -> None:
        _open_job_holding_credential(self.ledger, self.root, "orphan-a", "account-a")
        _open_job_holding_credential(self.ledger, self.root, "orphan-b", "account-b")
        pool = self._pool()
        pool.reconcile_in_flight()

        with self.assertRaises(CredentialPoolUnavailable):
            pool.acquire()

        cancelled = self.ledger.cancel_open_document_jobs(keep_source_ids=())
        pool.reconcile_in_flight()

        self.assertEqual(len(cancelled), 2)
        self.assertIn(
            pool.acquire().credential.credential_id,
            {"account-a", "account-b"},
        )

    def test_cancel_by_source_releases_only_that_source(self) -> None:
        cancelled_job = _open_job_holding_credential(
            self.ledger, self.root, "gone", "account-a"
        )
        kept_job = _open_job_holding_credential(
            self.ledger, self.root, "live", "account-b"
        )

        self.assertEqual(
            self.ledger.cancel_open_document_jobs(source_file_id="gone"),
            [cancelled_job],
        )

        self.assertEqual(
            self.ledger.get_document_job(cancelled_job).status, "cancelled"
        )
        self.assertEqual(
            [item.status for item in self.ledger.list_slice_jobs(cancelled_job)],
            ["cancelled"],
        )
        self.assertEqual(self._in_flight("account-a"), 0)
        self.assertEqual(self.ledger.get_document_job(kept_job).status, "waiting")
        self.assertEqual(self._in_flight("account-b"), 1)
        self.assertEqual(self.ledger.active_remote_counts(PROVIDER), {"account-b": 1})

    def test_cancel_leaves_finished_jobs_untouched(self) -> None:
        job_id = _open_job_holding_credential(
            self.ledger, self.root, "done", "account-a"
        )
        self.ledger.update_document(job_id, status="validated")

        self.assertEqual(self.ledger.cancel_open_document_jobs(source_file_id="done"), [])
        self.assertEqual(self.ledger.get_document_job(job_id).status, "validated")

    def test_startup_cancels_parser_jobs_without_import_journal(self) -> None:
        orphan = _open_job_holding_credential(
            self.ledger, self.root, "orphan", "account-a"
        )
        store = ImportJobStore()
        store.restore_job(
            "resumable",
            {"job_id": "resumable", "source_file_id": "resumable-src", "status": "failed"},
            {"source_file_id": "resumable-src", "target": self.root / "x.pdf"},
        )
        resumable = _open_job_holding_credential(
            self.ledger, self.root, "resumable-src", "account-b"
        )

        self._orchestrator(store)

        self.assertEqual(self.ledger.get_document_job(orphan).status, "cancelled")
        self.assertEqual(self._in_flight("account-a"), 0)
        self.assertEqual(self.ledger.get_document_job(resumable).status, "waiting")
        self.assertEqual(self._in_flight("account-b"), 1)

    def test_worker_cancellation_releases_parser_jobs(self) -> None:
        orchestrator = self._orchestrator()
        target = self.root / "cancel-me.docx"
        target.write_bytes(b"body")
        job_id = orchestrator.start_import_job(
            target, {"detected_pdf_type": "docx"}, "cancel-src", False
        )
        parser_job = _open_job_holding_credential(
            self.ledger, self.root, "cancel-src", "account-a"
        )

        self.assertEqual(orchestrator.dismiss_import_job(job_id), "cancelling")
        self.assertEqual(self.ledger.get_document_job(parser_job).status, "waiting")
        self.queue.run_next()

        self.assertIsNone(orchestrator.job_status(job_id))
        self.assertEqual(self.ledger.get_document_job(parser_job).status, "cancelled")
        self.assertEqual(self._in_flight("account-a"), 0)

    def test_dismissing_failed_import_releases_parser_jobs(self) -> None:
        store = ImportJobStore()
        orchestrator = self._orchestrator(store)
        parser_job = _open_job_holding_credential(
            self.ledger, self.root, "failed-src", "account-a"
        )
        store.restore_job(
            "failed",
            {"job_id": "failed", "source_file_id": "failed-src", "status": "failed"},
            {"source_file_id": "failed-src", "target": self.root / "f.pdf"},
        )

        self.assertEqual(orchestrator.dismiss_import_job("failed"), "dismissed")

        self.assertEqual(self.ledger.get_document_job(parser_job).status, "cancelled")
        self.assertEqual(self._in_flight("account-a"), 0)


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.me_finder.large_document.engine import LargeDocumentJobEngine
from src.me_finder.large_document.job_ledger import JobLedger
from src.me_finder.large_document.mineru_accounts import (
    MinerUAccountConfigError,
    MinerUAccountService,
)
from src.me_finder.large_document.slicing import PhysicalPDFSlicer
from src.me_finder.parser_provider import (
    NormalizedPage,
    NormalizedParseResult,
    ParserPollResult,
    ParserProvider,
    ParserSubmission,
    ParserTaskStatus,
    ProviderCapabilities,
)


def slice_writer(source, start, end, output):
    del source
    Path(output).write_bytes(f"physical-pages:{start}-{end}".encode("ascii"))


class IndependentAccountProvider(ParserProvider):
    provider_id = "mineru-cloud"

    def __init__(self):
        self.tasks = {}
        self.submitted_pages_by_account = {}

    def capabilities(self):
        return ProviderCapabilities(
            max_pages_per_file=200,
            max_bytes_per_file=None,
            max_concurrency=1,
            supports_async_jobs=True,
            supports_stream_upload=True,
        )

    def submit(self, request, *, credential=None):
        self.prepare(request)
        if credential is None:
            raise AssertionError("the 8000-page job must use an account credential")
        task_id = f"task-{len(self.tasks) + 1}"
        self.tasks[task_id] = {
            "account_id": credential.credential_id,
            "page_count": request.page_count,
        }
        self.submitted_pages_by_account[credential.credential_id] = (
            self.submitted_pages_by_account.get(credential.credential_id, 0)
            + request.page_count
        )
        return ParserSubmission(
            provider_id=self.provider_id,
            remote_task_id=task_id,
            status=ParserTaskStatus.SUBMITTED,
        )

    def poll(self, remote_task_id, *, credential=None):
        expected = self.tasks[remote_task_id]["account_id"]
        if credential is None or credential.credential_id != expected:
            raise AssertionError("remote task lost its original account affinity")
        return ParserPollResult(ParserTaskStatus.COMPLETED)

    def fetch_result(self, submission, request, *, credential=None):
        expected = self.tasks[submission.remote_task_id]["account_id"]
        if credential is None or credential.credential_id != expected:
            raise AssertionError("result fetch used a different account")
        return {"page_count": request.page_count}

    def normalize_result(self, raw_result, request):
        return NormalizedParseResult(
            provider_id=self.provider_id,
            model=request.model,
            pages=tuple(
                NormalizedPage(
                    physical_pdf_page=request.global_page_offset + index + 1,
                    text=f"page {request.global_page_offset + index + 1}",
                )
                for index in range(int(raw_result["page_count"]))
            ),
        )


class MinerUMultiAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = JobLedger(self.root / "parser_jobs.sqlite3")
        self.service = MinerUAccountService(
            ledger=self.ledger,
            config_path=self.root / "config" / "mineru_accounts.local.json",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_ten_independent_accounts_are_saved_without_returning_tokens(self):
        for index in range(1, 11):
            summary = self.service.save_account(
                account_id=f"account-{index}",
                display_name=f"MinerU 账号 {index}",
                token=f"token-private-{index}",
                expires_at="2026-12-31",
            )
            self.assertNotIn("daily_page_budget", summary.to_dict())
            self.assertNotIn("local_pages_used_today", summary.to_dict())
            self.assertNotIn("local_pages_remaining_today", summary.to_dict())

        accounts = self.service.list_accounts()
        self.assertEqual(len(accounts), 10)
        self.assertTrue(all(item.configured for item in accounts))
        safe_json = json.dumps(
            [item.to_dict() for item in accounts], ensure_ascii=False
        )
        self.assertNotIn("token-private", safe_json)
        self.assertNotIn("secret_ref", safe_json)

        records = self.ledger.list_credentials("mineru-cloud")
        self.assertEqual(len(records), 10)
        self.assertTrue(
            all(record.secret_ref == f"mineru-account:{record.id}" for record in records)
        )
        self.assertTrue(all("token-private" not in repr(record) for record in records))
        self.assertEqual(
            self.service.resolve_secret("mineru-account:account-10"),
            "token-private-10",
        )
        if os.name != "nt":
            permissions = self.service.config_path.stat().st_mode & 0o777
            self.assertEqual(permissions, 0o600)

    def test_edit_without_token_preserves_secret_without_usage_fields(self):
        self.service.save_account(
            account_id="account-1",
            display_name="Old name",
            token="Bearer original-token",
            expires_at="2026-12-31",
        )
        updated = self.service.save_account(
            account_id="account-1",
            display_name="New name",
            token="",
        )
        self.assertEqual(updated.display_name, "New name")
        self.assertEqual(updated.expires_at, "2026-12-31")
        self.assertEqual(
            self.service.resolve_secret("mineru-account:account-1"),
            "original-token",
        )

    def test_delete_account_removes_token_and_credential(self):
        self.service.save_account(
            account_id="account-1",
            display_name="Account 1",
            token="private-token",
        )

        self.service.delete_account("account-1")

        self.assertEqual(self.service.list_accounts(), [])
        private = json.loads(
            self.service.config_path.read_text(encoding="utf-8")
        )
        self.assertEqual(private["accounts"], {})
        with self.assertRaises(KeyError):
            self.ledger.get_credential("account-1")

    def test_delete_account_rejects_active_task_and_restores_token(self):
        self.service.save_account(
            account_id="account-1",
            display_name="Account 1",
            token="private-token",
        )
        self.ledger.set_credential_in_flight("account-1", 1)

        with self.assertRaisesRegex(
            MinerUAccountConfigError,
            "仍有未完成的解析任务",
        ):
            self.service.delete_account("account-1")

        self.assertEqual(
            self.service.resolve_secret("mineru-account:account-1"),
            "private-token",
        )
        self.assertEqual(len(self.service.list_accounts()), 1)

    def test_reserving_a_credential_does_not_count_as_successful_parsing(self):
        self.service.save_account(
            account_id="account-1",
            display_name="Account 1",
            token="token-1",
        )
        pool = self.service.create_pool(provider_max_concurrency=1)
        lease = pool.acquire()
        statistics = self.service.usage_statistics()
        self.assertEqual(statistics.parsed_book_count, 0)
        self.assertEqual(statistics.parsed_page_count, 0)
        self.assertEqual(statistics.credentials[0].parsed_page_count, 0)
        pool.release_unsubmitted(lease)

    def test_single_account_can_parse_beyond_1000_pages_without_cutoff(self):
        self.service.save_account(
            account_id="account-1",
            display_name="Only account",
            token="independent-token",
            max_concurrency_override=1,
        )
        provider = IndependentAccountProvider()
        source = self.root / "book-1200.pdf"
        source.write_bytes(b"synthetic PDF metadata")
        engine = LargeDocumentJobEngine(
            ledger=self.ledger,
            provider=provider,
            work_dir=self.root / "work-1200",
            slicer=PhysicalPDFSlicer(slice_writer),
            page_counter=lambda path: 1200,
            credential_pool=self.service.create_pool(provider_max_concurrency=1),
        )
        job = engine.prepare(
            source_path=source,
            source_file_id="pdf-1200",
            document_id="doc-1200",
        )
        for _ in range(20):
            current = engine.run_once(job.id)
            if current.status == "validated":
                break
        self.assertEqual(current.status, "validated")
        self.assertEqual(provider.submitted_pages_by_account, {"account-1": 1200})
        statistics = self.service.usage_statistics()
        self.assertEqual(statistics.parsed_book_count, 1)
        self.assertEqual(statistics.parsed_page_count, 1200)
        self.assertEqual(statistics.credentials[0].parsed_page_count, 1200)
        self.assertEqual(
            statistics.credentials[0].books[0].page_ranges,
            ((1, 1200),),
        )

    def test_eight_accounts_have_separate_book_and_page_attribution(self):
        for index in range(1, 9):
            self.service.save_account(
                account_id=f"account-{index}",
                display_name=f"MinerU Account {index}",
                token=f"independent-token-{index}",
                max_concurrency_override=1,
            )
        provider = IndependentAccountProvider()
        source = self.root / "book-8000.pdf"
        source.write_bytes(b"synthetic PDF metadata")
        engine = LargeDocumentJobEngine(
            ledger=self.ledger,
            provider=provider,
            work_dir=self.root / "work",
            slicer=PhysicalPDFSlicer(slice_writer),
            page_counter=lambda path: 8000,
            credential_pool=self.service.create_pool(provider_max_concurrency=1),
        )
        job = engine.prepare(
            source_path=source,
            source_file_id="pdf-8000",
            document_id="doc-8000",
        )
        self.assertEqual(job.total_slices, 40)
        self.assertTrue(
            all(item.page_count == 200 for item in self.ledger.list_slice_jobs(job.id))
        )

        statuses = []
        for _ in range(8):
            current = engine.run_once(job.id)
            statuses.append(current.status)
            if current.status == "validated":
                break
        self.assertEqual(statuses[-1], "validated")
        self.assertEqual(
            provider.submitted_pages_by_account,
            {f"account-{index}": 1000 for index in range(1, 9)},
        )
        statistics = self.service.usage_statistics()
        self.assertEqual(statistics.parsed_book_count, 1)
        self.assertEqual(statistics.parsed_page_count, 8000)
        self.assertEqual(len(statistics.credentials), 8)
        self.assertTrue(
            all(item.parsed_book_count == 1 for item in statistics.credentials)
        )
        self.assertTrue(
            all(item.parsed_page_count == 1000 for item in statistics.credentials)
        )
        self.assertTrue(
            all(
                item.books[0].source_file_name == "book-8000.pdf"
                for item in statistics.credentials
            )
        )
        safe_statistics = json.dumps(statistics.to_dict(), ensure_ascii=False)
        self.assertNotIn("independent-token", safe_statistics)
        self.assertNotIn("secret_ref", safe_statistics)
        self.assertEqual(current.completed_pages, 8000)
        self.assertEqual(current.completed_slices, 40)

    def test_damaged_private_config_is_rejected_without_exposing_content(self):
        self.service.config_path.parent.mkdir(parents=True)
        self.service.config_path.write_text("{damaged secret", encoding="utf-8")
        with self.assertRaisesRegex(Exception, "无法读取") as caught:
            self.service.list_accounts()
        self.assertNotIn("damaged secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

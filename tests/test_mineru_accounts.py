import json
import os
import tempfile
import unittest
from pathlib import Path

from src.me_finder.large_document.engine import LargeDocumentJobEngine
from src.me_finder.large_document.job_ledger import JobLedger
from src.me_finder.large_document.mineru_accounts import MinerUAccountService
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
            self.assertEqual(summary.daily_page_budget, 1000)

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

    def test_edit_without_token_preserves_secret_and_local_usage(self):
        self.service.save_account(
            account_id="account-1",
            display_name="Old name",
            token="Bearer original-token",
            expires_at="2026-12-31",
        )
        pool = self.service.create_pool(provider_max_concurrency=1)
        lease = pool.acquire(200)
        pool.finish_remote(lease.credential.credential_id)

        updated = self.service.save_account(
            account_id="account-1",
            display_name="New name",
            token="",
            daily_page_budget=1200,
        )
        self.assertEqual(updated.display_name, "New name")
        self.assertEqual(updated.local_pages_used_today, 200)
        self.assertEqual(updated.local_pages_remaining_today, 1000)
        self.assertEqual(updated.expires_at, "2026-12-31")
        self.assertEqual(
            self.service.resolve_secret("mineru-account:account-1"),
            "original-token",
        )

    def test_each_account_has_an_independent_budget_and_usage_counter(self):
        for index in range(1, 4):
            self.service.save_account(
                account_id=f"account-{index}",
                display_name=f"Account {index}",
                token=f"token-{index}",
                daily_page_budget=1000,
            )
        pool = self.service.create_pool(provider_max_concurrency=1)
        leases = [pool.acquire(200) for _ in range(3)]
        for lease in leases:
            pool.finish_remote(lease.credential.credential_id)
        used = {
            item.account_id: item.local_pages_used_today
            for item in self.service.list_accounts()
        }
        self.assertEqual(used, {"account-1": 200, "account-2": 200, "account-3": 200})

    def test_eight_accounts_exhaust_exactly_1000_pages_each_for_8000_page_book(self):
        for index in range(1, 9):
            self.service.save_account(
                account_id=f"account-{index}",
                display_name=f"MinerU Account {index}",
                token=f"independent-token-{index}",
                daily_page_budget=1000,
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
        summaries = self.service.list_accounts()
        self.assertTrue(
            all(item.local_pages_used_today == 1000 for item in summaries)
        )
        self.assertTrue(
            all(item.local_pages_remaining_today == 0 for item in summaries)
        )
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

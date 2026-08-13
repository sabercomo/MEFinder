import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.me_finder.large_document.credential_pool import (
    CredentialPool,
    CredentialPoolUnavailable,
    redact_secrets,
)
from src.me_finder.large_document.engine import LargeDocumentJobEngine
from src.me_finder.large_document.job_ledger import JobLedger, SCHEMA_V1, SCHEMA_V2
from src.me_finder.large_document.slicing import PhysicalPDFSlicer
from src.me_finder.parser_provider import (
    NormalizedPage,
    NormalizedParseResult,
    ParserPollResult,
    ParserProvider,
    ParserProviderError,
    ParserSubmission,
    ParserTaskStatus,
    ProviderCapabilities,
)


def writer(source, start, end, output):
    Path(output).write_bytes(f"slice:{start}-{end}".encode("ascii"))


class AffinityProvider(ParserProvider):
    provider_id = "mineru-cloud"

    def __init__(self, *, asynchronous=True):
        self.asynchronous = asynchronous
        self.submitted = {}
        self.polled = []

    def capabilities(self):
        return ProviderCapabilities(
            max_pages_per_file=2,
            max_bytes_per_file=None,
            max_concurrency=8,
            supports_async_jobs=self.asynchronous,
        )

    def submit(self, request, *, credential=None):
        task = f"task-{len(self.submitted) + 1}"
        self.submitted[task] = credential.credential_id if credential else None
        raw = {"count": request.page_count}
        return ParserSubmission(
            self.provider_id,
            task if self.asynchronous else None,
            ParserTaskStatus.SUBMITTED
            if self.asynchronous
            else ParserTaskStatus.COMPLETED,
            raw_result=None if self.asynchronous else raw,
        )

    def poll(self, remote_task_id, *, credential=None):
        credential_id = credential.credential_id if credential else None
        self.polled.append((remote_task_id, credential_id))
        if credential_id != self.submitted[remote_task_id]:
            raise AssertionError("remote task was polled with the wrong credential")
        return ParserPollResult(ParserTaskStatus.COMPLETED)

    def fetch_result(self, submission, request, *, credential=None):
        if submission.remote_task_id:
            expected = self.submitted[submission.remote_task_id]
            actual = credential.credential_id if credential else None
            if actual != expected:
                raise AssertionError("remote result fetched with the wrong credential")
        return submission.raw_result or {"count": request.page_count}

    def normalize_result(self, raw_result, request):
        return NormalizedParseResult(
            provider_id=self.provider_id,
            model=None,
            pages=tuple(
                NormalizedPage(request.global_page_offset + index + 1, "text")
                for index in range(int(raw_result["count"]))
            ),
        )


class CredentialPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = JobLedger(self.root / "jobs.sqlite3")
        self.secrets = {}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_credential(self, index, *, enabled=True):
        credential_id = f"credential-{index}"
        secret_ref = f"keychain:{credential_id}"
        self.secrets[secret_ref] = f"secret-token-{index}"
        return self.ledger.upsert_credential(
            credential_id=credential_id,
            provider_id="mineru-cloud",
            display_name=f"MinerU {index}",
            secret_ref=secret_ref,
            enabled=enabled,
        )

    def pool(self):
        return CredentialPool(
            ledger=self.ledger,
            provider_id="mineru-cloud",
            secret_resolver=self.secrets.__getitem__,
            provider_max_concurrency=8,
            rate_limit_cooldown_seconds=300,
        )

    def test_v1_ledger_migrates_additively_to_credentials(self) -> None:
        path = self.root / "v1.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA_V1)
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()
        migrated = JobLedger(path)
        migrated.upsert_credential(
            credential_id="one",
            provider_id="mineru-cloud",
            display_name="One",
            secret_ref="keychain:one",
        )
        with closing(sqlite3.connect(path)) as check:
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(
                check.execute(
                    "SELECT COUNT(*) FROM parser_credentials"
                ).fetchone()[0],
                1,
            )

    def test_v2_budget_state_is_cleared_when_migrating_to_v3(self) -> None:
        path = self.root / "v2.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA_V1)
        connection.executescript(SCHEMA_V2)
        connection.execute(
            """
            INSERT INTO parser_credentials(
                id, provider_id, display_name, secret_ref, daily_page_budget,
                pages_used_today, usage_date, created_at, updated_at
            ) VALUES ('one', 'mineru-cloud', 'One', 'keychain:one', 1000,
                      1000, '2026-08-10', 'now', 'now')
            """
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        connection.close()

        migrated = JobLedger(path)
        self.assertEqual(migrated.get_credential("one").display_name, "One")
        with closing(sqlite3.connect(path)) as check:
            legacy = check.execute(
                "SELECT daily_page_budget, pages_used_today, usage_date "
                "FROM parser_credentials WHERE id = 'one'"
            ).fetchone()
            self.assertEqual(legacy, (None, 0, None))
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_one_credential_completes_a_normal_job(self) -> None:
        self.add_credential(1)
        source = self.root / "source.pdf"
        source.write_bytes(b"pdf")
        provider = AffinityProvider(asynchronous=False)
        engine = LargeDocumentJobEngine(
            ledger=self.ledger,
            provider=provider,
            work_dir=self.root / "work",
            slicer=PhysicalPDFSlicer(writer),
            page_counter=lambda path: 2,
            credential_pool=self.pool(),
        )
        job = engine.prepare(source_path=source, source_file_id="pdf-1", document_id="doc")
        self.assertEqual(engine.run_once(job.id).status, "validated")
        record = self.ledger.get_credential("credential-1")
        self.assertEqual(record.current_in_flight, 0)
        self.assertEqual(
            self.ledger.completed_page_counts("mineru-cloud"),
            {"credential-1": 2},
        )

    def test_eight_credentials_balance_7000_pages_without_a_quota(self) -> None:
        for index in range(1, 9):
            self.add_credential(index)
        plan = self.pool().plan_distribution([200] * 35)
        self.assertEqual(sum(plan.pages_by_credential.values()), 7000)
        self.assertEqual(plan.unassigned_pages, 0)
        planned_pages = list(plan.pages_by_credential.values())
        self.assertLessEqual(max(planned_pages) - min(planned_pages), 200)

    def test_one_credential_plan_can_exceed_1000_pages(self) -> None:
        self.add_credential(1)
        plan = self.pool().plan_distribution([200] * 40)
        self.assertEqual(plan.assignments, ["credential-1"] * 40)
        self.assertEqual(plan.pages_by_credential, {"credential-1": 8000})
        self.assertEqual(plan.unassigned_pages, 0)

    def test_429_cools_down_credential_and_routes_new_work_elsewhere(self) -> None:
        self.add_credential(1)
        self.add_credential(2)
        pool = self.pool()
        lease = pool.acquire()
        self.assertEqual(lease.credential.credential_id, "credential-1")
        error = ParserProviderError(
            "HTTP 429", provider_id="mineru-cloud", retryable=True, rate_limited=True
        )
        pool.record_error("credential-1", error)
        pool.release_unsubmitted(lease)
        replacement = pool.acquire()
        self.assertEqual(replacement.credential.credential_id, "credential-2")
        self.assertEqual(self.ledger.get_credential("credential-1").health_status, "cooldown")

    def test_401_disables_credential(self) -> None:
        self.add_credential(1)
        pool = self.pool()
        lease = pool.acquire()
        pool.record_error(
            "credential-1",
            ParserProviderError(
                "HTTP 401",
                provider_id="mineru-cloud",
                authentication_failed=True,
            ),
        )
        pool.release_unsubmitted(lease)
        record = self.ledger.get_credential("credential-1")
        self.assertFalse(record.is_enabled)
        self.assertEqual(record.health_status, "unauthorized")
        with self.assertRaises(CredentialPoolUnavailable):
            pool.acquire()

    def test_remote_task_is_always_polled_with_original_credential_after_restart(self) -> None:
        self.add_credential(1)
        self.add_credential(2)
        source = self.root / "source.pdf"
        source.write_bytes(b"pdf")
        provider = AffinityProvider(asynchronous=True)
        engine = LargeDocumentJobEngine(
            ledger=self.ledger,
            provider=provider,
            work_dir=self.root / "work",
            slicer=PhysicalPDFSlicer(writer),
            page_counter=lambda path: 4,
            credential_pool=self.pool(),
        )
        job = engine.prepare(source_path=source, source_file_id="pdf-1", document_id="doc")
        self.assertEqual(engine.run_once(job.id).status, "waiting")
        affinities = {
            item.remote_task_id: item.credential_id
            for item in self.ledger.list_slice_jobs(job.id)
        }
        restarted_pool = self.pool()
        restarted_pool.reconcile_in_flight()
        restarted = LargeDocumentJobEngine(
            ledger=JobLedger(self.root / "jobs.sqlite3"),
            provider=provider,
            work_dir=self.root / "work",
            slicer=PhysicalPDFSlicer(writer),
            page_counter=lambda path: 4,
            credential_pool=restarted_pool,
        )
        self.assertEqual(restarted.run_once(job.id).status, "validated")
        self.assertEqual(dict(provider.polled), affinities)

    def test_recovered_credential_rejoins_pool(self) -> None:
        self.add_credential(1, enabled=False)
        pool = self.pool()
        with self.assertRaises(CredentialPoolUnavailable):
            pool.acquire()
        pool.recover("credential-1")
        self.assertEqual(pool.acquire().credential.credential_id, "credential-1")

    def test_all_credentials_unavailable_leaves_job_waiting_without_loss(self) -> None:
        self.add_credential(1, enabled=False)
        source = self.root / "source.pdf"
        source.write_bytes(b"pdf")
        provider = AffinityProvider(asynchronous=False)
        engine = LargeDocumentJobEngine(
            ledger=self.ledger,
            provider=provider,
            work_dir=self.root / "work",
            page_counter=lambda path: 2,
            credential_pool=self.pool(),
        )
        job = engine.prepare(source_path=source, source_file_id="pdf-1", document_id="doc")
        waiting = engine.run_once(job.id)
        self.assertEqual(waiting.status, "waiting")
        slice_job = self.ledger.list_slice_jobs(job.id)[0]
        self.assertEqual(slice_job.attempt_count, 0)
        self.assertIsNone(slice_job.remote_task_id)

    def test_secrets_never_enter_ledger_repr_or_redacted_output(self) -> None:
        self.add_credential(1)
        lease = self.pool().acquire()
        secret = lease.credential.secret
        self.assertNotIn(secret, repr(lease.credential))
        self.assertNotIn(secret.encode(), (self.root / "jobs.sqlite3").read_bytes())
        self.assertEqual(redact_secrets(f"failure {secret}", [secret]), "failure [REDACTED]")


if __name__ == "__main__":
    unittest.main()

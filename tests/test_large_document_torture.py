import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.me_finder.document_export import document_manifest, read_document_export
from src.me_finder.large_document.engine import LargeDocumentJobEngine
from src.me_finder.large_document.io_utils import sha256_file
from src.me_finder.large_document.job_ledger import JobLedger
from src.me_finder.large_document.merge import iter_normalized_pages
from src.me_finder.large_document.slicing import PhysicalPDFSlicer
from src.me_finder.large_document.torture import (
    GIB,
    build_dry_run_report,
    load_credential_specs,
    main,
    streaming_memory_probe,
)
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


FIXTURES = Path(__file__).parent / "fixtures"


def slice_writer(source, start, end, output):
    Path(output).write_bytes(
        b"%PDF-synthetic\n" + f"pages:{start}-{end}\n".encode("ascii")
    )


class FaultInjectingProvider(ParserProvider):
    provider_id = "synthetic-torture"

    def __init__(self, *, asynchronous=False, max_pages=1):
        self.asynchronous = asynchronous
        self.max_pages = max_pages
        self.submit_counts = {}
        self.fetch_counts = {}
        self.remote_pages = {}
        self.fail_429_once = set()
        self.fail_permanent_once = set()
        self.interrupt_download_once = set()
        self.wait_after_page = None

    def capabilities(self):
        return ProviderCapabilities(
            max_pages_per_file=self.max_pages,
            max_bytes_per_file=None,
            max_concurrency=8,
            supports_async_jobs=self.asynchronous,
            supports_stream_upload=True,
        )

    def submit(self, request, *, credential=None):
        self.prepare(request)
        page = request.global_page_offset + 1
        self.submit_counts[page] = self.submit_counts.get(page, 0) + 1
        if page in self.fail_429_once:
            self.fail_429_once.remove(page)
            raise ParserProviderError(
                "injected HTTP 429",
                provider_id=self.provider_id,
                retryable=True,
                rate_limited=True,
                status_code=429,
            )
        if page in self.fail_permanent_once:
            self.fail_permanent_once.remove(page)
            raise ParserProviderError(
                "injected permanent failure",
                provider_id=self.provider_id,
                status_code=422,
            )
        raw = {"page_count": request.page_count}
        if not self.asynchronous:
            return ParserSubmission(
                self.provider_id,
                None,
                ParserTaskStatus.COMPLETED,
                raw_result=raw,
            )
        task_id = f"task-page-{page}"
        self.remote_pages[task_id] = page
        return ParserSubmission(
            self.provider_id,
            task_id,
            ParserTaskStatus.SUBMITTED,
        )

    def poll(self, remote_task_id, *, credential=None):
        page = self.remote_pages[remote_task_id]
        if self.wait_after_page is not None and page > self.wait_after_page:
            return ParserPollResult(ParserTaskStatus.WAITING)
        return ParserPollResult(ParserTaskStatus.COMPLETED)

    def fetch_result(self, submission, request, *, credential=None):
        page = request.global_page_offset + 1
        self.fetch_counts[page] = self.fetch_counts.get(page, 0) + 1
        if page in self.interrupt_download_once:
            self.interrupt_download_once.remove(page)
            raise ParserProviderError(
                "injected interrupted download",
                provider_id=self.provider_id,
                retryable=True,
            )
        return submission.raw_result or {"page_count": request.page_count}

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


class DuplicateResultProvider(FaultInjectingProvider):
    def normalize_result(self, raw_result, request):
        page = request.global_page_offset + 1
        return NormalizedParseResult(
            provider_id=self.provider_id,
            model=None,
            pages=(NormalizedPage(page, "one"), NormalizedPage(page, "duplicate")),
        )


class LargeDocumentTortureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_offline_plans_7000_7200_and_8000_pages_with_exact_coverage(self):
        for total_pages, expected_slices in ((7000, 35), (7200, 36), (8000, 40)):
            with self.subTest(total_pages=total_pages):
                report = build_dry_run_report(
                    provider_id="synthetic",
                    total_pages=total_pages,
                    source_bytes=2 * GIB,
                    capabilities=ProviderCapabilities(200, None, max_concurrency=8),
                )
                covered = [
                    page
                    for item in report.slices
                    for page in range(item.page_start, item.page_end + 1)
                ]
                self.assertEqual(report.slice_count, expected_slices)
                self.assertEqual(covered, list(range(1, total_pages + 1)))
                self.assertTrue(report.coverage_complete)
                self.assertFalse(report.budget_insufficient)

    def test_7000_page_eight_credential_plan_stays_within_budget(self):
        specs = load_credential_specs(FIXTURES / "torture_credentials_8.json")
        report = build_dry_run_report(
            provider_id="synthetic",
            total_pages=7000,
            source_bytes=2 * GIB,
            capabilities=ProviderCapabilities(200, None, max_concurrency=8),
            credential_specs=specs,
        )
        self.assertEqual(report.slice_count, 35)
        self.assertEqual(report.unassigned_pages, 0)
        self.assertFalse(report.budget_insufficient)
        self.assertEqual(sum(report.pages_by_credential.values()), 7000)
        self.assertEqual(
            sorted(report.pages_by_credential.values()),
            [800, 800, 800, 800, 800, 1000, 1000, 1000],
        )

    def test_provider_capabilities_control_plans_without_core_constants(self):
        qwen = build_dry_run_report(
            provider_id="qwen-ocr",
            total_pages=7000,
            source_bytes=70 * 1024 * 1024,
            capabilities=ProviderCapabilities(50, 100 * 1024 * 1024),
        )
        mineru = build_dry_run_report(
            provider_id="mineru-cloud",
            total_pages=7000,
            source_bytes=70 * 1024 * 1024,
            capabilities=ProviderCapabilities(200, None),
        )
        self.assertEqual((qwen.slice_count, mineru.slice_count), (140, 35))

    def test_two_gib_memory_probe_is_bounded_by_stream_chunk_not_logical_size(self):
        result = streaming_memory_probe(
            logical_input_bytes=2 * GIB,
            sampled_bytes=24 * 1024 * 1024,
            chunk_size=1024 * 1024,
        )
        self.assertEqual(result["logical_input_bytes"], 2 * GIB)
        self.assertLess(result["peak_extra_bytes"], 4 * 1024 * 1024)
        self.assertLess(result["peak_extra_bytes"], result["logical_input_bytes"] // 100)

    def test_dry_run_cli_never_builds_or_calls_a_real_provider(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "--provider",
                    "synthetic",
                    "--dry-run",
                    "--synthetic-pages",
                    "7000",
                    "--synthetic-bytes",
                    str(2 * GIB),
                    "--max-pages",
                    "200",
                    "--max-concurrency",
                    "8",
                    "--credentials",
                    str(FIXTURES / "torture_credentials_8.json"),
                ]
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["slice_count"], 35)
        self.assertEqual(payload["coverage_first_page"], 1)
        self.assertEqual(payload["coverage_last_page"], 7000)

    def test_credential_file_rejects_plaintext_secrets(self):
        path = self.root / "credentials.json"
        path.write_text(
            json.dumps(
                {
                    "credentials": [
                        {"id": "unsafe", "secret_ref": "env:TOKEN", "token": "raw"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "references"):
            load_credential_specs(path)

    def test_slice_17_rate_limit_and_slice_23_permanent_failure_are_explainable(self):
        provider = FaultInjectingProvider()
        provider.fail_429_once.add(17)
        provider.fail_permanent_once.add(23)
        engine, job = self._engine(provider, pages=30)
        failed = engine.run_once(job.id)
        self.assertEqual(failed.status, "permanent_failure")
        slices = engine.ledger.list_slice_jobs(job.id)
        self.assertEqual(slices[16].status, "retryable_failure")
        self.assertEqual(slices[22].status, "permanent_failure")
        self.assertIn("permanently", failed.error_summary)

        engine.ledger.update_slice(slices[22].id, status="queued", last_error=None)
        recovered = engine.run_once(job.id)
        self.assertEqual(recovered.status, "validated")
        self.assertEqual(provider.submit_counts[17], 2)
        self.assertEqual(provider.submit_counts[23], 2)
        self.assertTrue(
            all(
                count == 1
                for page, count in provider.submit_counts.items()
                if page not in {17, 23}
            )
        )
        self.assertEqual(self._merged_pages(engine, job.id), list(range(1, 31)))

    def test_process_exit_halfway_resumes_without_resubmitting_completed_slices(self):
        provider = FaultInjectingProvider(asynchronous=True)
        engine, job = self._engine(provider, pages=40)
        self.assertEqual(engine.run_once(job.id).status, "waiting")
        provider.wait_after_page = 20
        self.assertEqual(engine.run_once(job.id).status, "waiting")
        self.assertEqual(engine.ledger.get_document_job(job.id).completed_pages, 20)

        provider.wait_after_page = None
        restarted = LargeDocumentJobEngine(
            ledger=JobLedger(self.root / "jobs.sqlite3"),
            provider=provider,
            work_dir=self.root / "work",
            slicer=PhysicalPDFSlicer(slice_writer),
            page_counter=lambda path: 40,
        )
        self.assertEqual(restarted.run_once(job.id).status, "validated")
        self.assertEqual(sum(provider.submit_counts.values()), 40)
        self.assertTrue(all(count == 1 for count in provider.submit_counts.values()))
        self.assertEqual(self._merged_pages(restarted, job.id), list(range(1, 41)))

    def test_interrupted_result_download_reuses_remote_task_without_resubmit(self):
        provider = FaultInjectingProvider(asynchronous=True)
        provider.interrupt_download_once.add(17)
        engine, job = self._engine(provider, pages=30)
        self.assertEqual(engine.run_once(job.id).status, "waiting")
        self.assertEqual(engine.run_once(job.id).status, "retryable_failure")
        slice_17 = engine.ledger.list_slice_jobs(job.id)[16]
        remote_task_id = slice_17.remote_task_id
        self.assertIsNotNone(remote_task_id)
        self.assertEqual(engine.run_once(job.id).status, "validated")
        restored = engine.ledger.list_slice_jobs(job.id)[16]
        self.assertEqual(restored.remote_task_id, remote_task_id)
        self.assertEqual(provider.submit_counts[17], 1)
        self.assertEqual(provider.fetch_counts[17], 2)

    def test_corrupt_slice_is_regenerated_before_provider_submission(self):
        provider = FaultInjectingProvider()
        engine, job = self._engine(provider, pages=4)
        first = engine.ledger.list_slice_jobs(job.id)[0]
        slice_path = Path(first.slice_path)
        expected_hash = first.slice_sha256
        slice_path.write_bytes(b"corrupt")
        self.assertNotEqual(sha256_file(slice_path), expected_hash)
        self.assertEqual(engine.run_once(job.id).status, "validated")
        repaired = engine.ledger.get_slice_job(first.id)
        self.assertEqual(repaired.slice_sha256, sha256_file(Path(repaired.slice_path)))
        self.assertNotEqual(repaired.slice_sha256, hashlib.sha256(b"corrupt").hexdigest())

    def test_replaced_source_and_duplicate_results_never_publish_partial_book(self):
        provider = FaultInjectingProvider()
        engine, job = self._engine(provider, pages=4)
        Path(job.source_path).write_bytes(b"replacement")
        changed = engine.run_once(job.id)
        self.assertEqual(changed.status, "permanent_failure")
        self.assertIn("source file changed", changed.error_summary)

        duplicate = DuplicateResultProvider()
        duplicate_engine, duplicate_job = self._engine(
            duplicate,
            pages=2,
            name="duplicate",
            max_attempts=1,
        )
        self.assertEqual(
            duplicate_engine.run_once(duplicate_job.id).status,
            "retryable_failure",
        )
        rejected = duplicate_engine.run_once(duplicate_job.id)
        self.assertEqual(rejected.status, "permanent_failure")
        destination = self.root / "must-not-exist.mefinder.zip"
        with self.assertRaisesRegex(RuntimeError, "pass merge validation"):
            duplicate_engine.publish(
                duplicate_job.id,
                manifest=self._manifest(2, b"source-duplicate"),
                destination=destination,
            )
        self.assertFalse(destination.exists())

    def test_published_job_is_idempotent_for_the_same_destination(self):
        provider = FaultInjectingProvider(max_pages=2)
        engine, job = self._engine(provider, pages=4, name="publish")
        self.assertEqual(engine.run_once(job.id).status, "validated")
        destination = self.root / "published.mefinder.zip"
        calls = []
        manifest = self._manifest(4, b"source-publish")
        first = engine.publish(
            job.id,
            manifest=manifest,
            destination=destination,
            publish_index=lambda: calls.append("published"),
        )
        second = engine.publish(
            job.id,
            manifest=manifest,
            destination=destination,
            publish_index=lambda: calls.append("published-again"),
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(calls, ["published"])
        self.assertEqual(
            [item["physical_pdf_page"] for item in read_document_export(destination).pages],
            [1, 2, 3, 4],
        )

    def _engine(self, provider, *, pages, name="source", max_attempts=3):
        source = self.root / f"{name}.pdf"
        source.write_bytes(f"source-{name}".encode("ascii"))
        ledger = JobLedger(self.root / "jobs.sqlite3")
        engine = LargeDocumentJobEngine(
            ledger=ledger,
            provider=provider,
            work_dir=self.root / "work",
            slicer=PhysicalPDFSlicer(slice_writer),
            page_counter=lambda path: pages,
            max_attempts=max_attempts,
        )
        job = engine.prepare(
            source_path=source,
            source_file_id=f"pdf-{name}",
            document_id=f"doc-{name}",
        )
        return engine, job

    @staticmethod
    def _merged_pages(engine, job_id):
        path = engine.work_dir / job_id / "merged.pages.ndjson"
        return [page["physical_pdf_page"] for page in iter_normalized_pages(path)]

    @staticmethod
    def _manifest(page_count, source_bytes):
        return document_manifest(
            document={"document_id": "doc", "source_file_id": "pdf"},
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            source_file={"file_name": "source.pdf", "size_bytes": len(source_bytes)},
            parser_provider="synthetic-torture",
            page_count=page_count,
        )


if __name__ == "__main__":
    unittest.main()

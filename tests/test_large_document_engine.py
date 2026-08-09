import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.document_export import document_manifest, read_document_export
from src.me_finder.large_document.engine import LargeDocumentJobEngine
from src.me_finder.large_document.io_utils import sha256_file
from src.me_finder.large_document.job_ledger import JobLedger
from src.me_finder.large_document.merge import (
    CoverageValidationError,
    iter_normalized_pages,
    merge_normalized_result_files,
    validate_slice_coverage,
    write_normalized_result,
)
from src.me_finder.large_document.slicing import (
    PhysicalPDFSlicer,
    SlicePlanner,
)
from src.me_finder.mineru_api import MinerUClient, MinerUConfig
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
from src.me_finder.pdf_extractors import PDFExtractionError, SIMPLE_PDF_MAX_BYTES, SimplePDF


class SyntheticProvider(ParserProvider):
    provider_id = "synthetic"

    def __init__(
        self,
        *,
        max_pages=2,
        max_bytes=None,
        asynchronous=False,
        fail_submissions=0,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            max_pages_per_file=max_pages,
            max_bytes_per_file=max_bytes,
            max_concurrency=2,
            supports_async_jobs=asynchronous,
            supports_stream_upload=True,
        )
        self.asynchronous = asynchronous
        self.fail_submissions = fail_submissions
        self.submit_count = 0
        self.poll_counts = {}

    def capabilities(self):
        return self._capabilities

    def submit(self, request, *, credential=None):
        self.prepare(request)
        self.submit_count += 1
        if self.submit_count <= self.fail_submissions:
            raise ParserProviderError(
                "temporary failure",
                provider_id=self.provider_id,
                retryable=True,
            )
        raw = {"page_count": request.page_count}
        if self.asynchronous:
            return ParserSubmission(
                self.provider_id,
                f"remote-{self.submit_count}",
                ParserTaskStatus.SUBMITTED,
                metadata={"page_count": request.page_count},
            )
        return ParserSubmission(
            self.provider_id,
            None,
            ParserTaskStatus.COMPLETED,
            raw_result=raw,
        )

    def poll(self, remote_task_id, *, credential=None):
        count = self.poll_counts.get(remote_task_id, 0) + 1
        self.poll_counts[remote_task_id] = count
        return ParserPollResult(
            ParserTaskStatus.COMPLETED if count >= 1 else ParserTaskStatus.WAITING
        )

    def fetch_result(self, submission, request, *, credential=None):
        return submission.raw_result or {"page_count": request.page_count}

    def normalize_result(self, raw_result, request):
        count = int(raw_result["page_count"])
        return NormalizedParseResult(
            provider_id=self.provider_id,
            model=request.model,
            pages=tuple(
                NormalizedPage(
                    physical_pdf_page=request.global_page_offset + index + 1,
                    text=f"第 {request.global_page_offset + index + 1} 頁",
                    parser_provenance={"provider": self.provider_id},
                )
                for index in range(count)
            ),
        )


def synthetic_slice_writer(source, start, end, output):
    with Path(output).open("wb") as stream:
        stream.write(b"%PDF-slice\n")
        for page in range(start, end + 1):
            stream.write((f"page-{page}\n".encode("ascii")) * 8)


class LargeDocumentCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_7000_page_metadata_plans_complete_coverage(self) -> None:
        ranges = SlicePlanner().plan(
            total_pages=7000,
            total_bytes=2_000_000_000,
            capabilities=ProviderCapabilities(200, None),
        )
        self.assertEqual(len(ranges), 35)
        self.assertEqual(validate_slice_coverage(ranges, 7000)[0], (1, 200))
        self.assertEqual(validate_slice_coverage(ranges, 7000)[-1], (6801, 7000))

    def test_provider_page_capability_changes_plan(self) -> None:
        planner = SlicePlanner()
        slow = planner.plan(
            total_pages=7000,
            total_bytes=1,
            capabilities=ProviderCapabilities(50, None),
        )
        broad = planner.plan(
            total_pages=7000,
            total_bytes=1,
            capabilities=ProviderCapabilities(700, None),
        )
        self.assertEqual((len(slow), len(broad)), (140, 10))

    def test_actual_byte_limit_recursively_splits_physical_pdf(self) -> None:
        source = self.root / "source.pdf"
        source.write_bytes(b"source")

        def sized_writer(source_path, start, end, output):
            Path(output).write_bytes(b"x" * ((end - start + 1) * 100))

        ranges = SlicePlanner().plan(
            total_pages=4,
            total_bytes=100,
            capabilities=ProviderCapabilities(4, 1_000),
        )
        descriptors = PhysicalPDFSlicer(sized_writer).create_slices(
            source, ranges, self.root / "slices", max_bytes_per_file=250
        )
        self.assertEqual([(item.page_start, item.page_end) for item in descriptors], [(1, 2), (3, 4)])
        self.assertTrue(all(item.size_bytes <= 250 for item in descriptors))

    def test_physical_slice_offsets_and_hashes_are_explicit(self) -> None:
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        descriptors = PhysicalPDFSlicer(synthetic_slice_writer).create_slices(
            source,
            SlicePlanner().plan(
                total_pages=5,
                total_bytes=100,
                capabilities=ProviderCapabilities(2, None),
            ),
            self.root / "slices",
            max_bytes_per_file=None,
        )
        self.assertEqual([item.global_page_offset for item in descriptors], [0, 2, 4])
        self.assertTrue(all(item.path.is_file() for item in descriptors))
        self.assertTrue(all(item.sha256 == sha256_file(item.path) for item in descriptors))

    def test_merge_100_slices_is_exactly_1_to_n(self) -> None:
        slices = []
        for page in range(1, 101):
            path = self.root / f"result-{page}.ndjson"
            write_normalized_result(
                path,
                NormalizedParseResult(
                    provider_id="fake",
                    model=None,
                    pages=(NormalizedPage(page, str(page)),),
                ),
            )
            slices.append({"page_start": page, "page_end": page, "result_path": str(path)})
        merged = merge_normalized_result_files(
            list(reversed(slices)), self.root / "merged.ndjson", total_pages=100
        )
        self.assertEqual(merged.page_count, 100)
        self.assertEqual(
            [page["physical_pdf_page"] for page in iter_normalized_pages(merged.path)],
            list(range(1, 101)),
        )

    def test_missing_duplicate_overlap_and_out_of_order_validation(self) -> None:
        cases = [
            ([(1, 2), (4, 5)], "missing"),
            ([(1, 2), (1, 2), (3, 5)], "duplicate"),
            ([(1, 3), (3, 5)], "overlap"),
        ]
        for ranges, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(CoverageValidationError) as caught:
                    validate_slice_coverage(ranges, 5)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(
            validate_slice_coverage([(3, 5), (1, 2)], 5),
            [(1, 2), (3, 5)],
        )
        with self.assertRaises(CoverageValidationError) as caught:
            validate_slice_coverage([(3, 5), (1, 2)], 5, require_input_order=True)
        self.assertEqual(caught.exception.code, "out_of_order")

    def test_ledger_persists_required_document_and_slice_fields(self) -> None:
        ledger = JobLedger(self.root / "jobs.sqlite3")
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        engine = LargeDocumentJobEngine(
            ledger=ledger,
            provider=SyntheticProvider(max_pages=2),
            work_dir=self.root / "work",
            slicer=PhysicalPDFSlicer(synthetic_slice_writer),
            page_counter=lambda path: 4,
        )
        job = engine.prepare(
            source_path=source,
            source_file_id="pdf-1",
            document_id="doc-1",
        )
        restored = JobLedger(self.root / "jobs.sqlite3").get_document_job(job.id)
        slices = JobLedger(self.root / "jobs.sqlite3").list_slice_jobs(job.id)
        self.assertEqual((restored.total_pages, restored.total_slices), (4, 2))
        self.assertEqual([(s.page_start, s.page_end, s.status) for s in slices], [(1, 2, "queued"), (3, 4, "queued")])

    def test_crash_resume_does_not_resubmit_completed_slices(self) -> None:
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        provider = SyntheticProvider(max_pages=2)
        ledger = JobLedger(self.root / "jobs.sqlite3")
        engine = LargeDocumentJobEngine(
            ledger=ledger,
            provider=provider,
            work_dir=self.root / "work",
            slicer=PhysicalPDFSlicer(synthetic_slice_writer),
            page_counter=lambda path: 4,
        )
        job = engine.prepare(source_path=source, source_file_id="pdf-1", document_id="doc")
        self.assertEqual(engine.run_once(job.id).status, "validated")
        self.assertEqual(provider.submit_count, 2)
        restarted = LargeDocumentJobEngine(
            ledger=JobLedger(self.root / "jobs.sqlite3"),
            provider=provider,
            work_dir=self.root / "work",
            slicer=PhysicalPDFSlicer(synthetic_slice_writer),
            page_counter=lambda path: 4,
        )
        resumed = restarted.prepare(source_path=source, source_file_id="pdf-1", document_id="doc")
        restarted.run_once(resumed.id)
        self.assertEqual(resumed.id, job.id)
        self.assertEqual(provider.submit_count, 2)

    def test_existing_remote_task_is_polled_after_restart(self) -> None:
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        provider = SyntheticProvider(max_pages=4, asynchronous=True)
        ledger = JobLedger(self.root / "jobs.sqlite3")
        engine = LargeDocumentJobEngine(
            ledger=ledger,
            provider=provider,
            work_dir=self.root / "work",
            page_counter=lambda path: 4,
        )
        job = engine.prepare(source_path=source, source_file_id="pdf-1", document_id="doc")
        self.assertEqual(engine.run_once(job.id).status, "waiting")
        self.assertEqual(provider.submit_count, 1)
        self.assertEqual(engine.run_once(job.id).status, "validated")
        self.assertEqual(provider.submit_count, 1)

    def test_changed_source_hash_never_reuses_old_job(self) -> None:
        source = self.root / "source.pdf"
        source.write_bytes(b"first")
        provider = SyntheticProvider(max_pages=4)
        engine = LargeDocumentJobEngine(
            ledger=JobLedger(self.root / "jobs.sqlite3"),
            provider=provider,
            work_dir=self.root / "work",
            page_counter=lambda path: 4,
        )
        first = engine.prepare(source_path=source, source_file_id="pdf-1", document_id="doc")
        source.write_bytes(b"second")
        second = engine.prepare(source_path=source, source_file_id="pdf-1", document_id="doc")
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.source_sha256, second.source_sha256)

    def test_publish_failure_preserves_previous_export_and_index(self) -> None:
        engine, job = self._completed_engine()
        destination = self.root / "published.zip"
        destination.write_bytes(b"previous-export")
        index = {"version": "previous"}

        def fail_index():
            raise RuntimeError("index write failed")

        with self.assertRaisesRegex(RuntimeError, "index write failed"):
            engine.publish(
                job.id,
                manifest=self._manifest(4),
                destination=destination,
                publish_index=fail_index,
            )
        self.assertEqual(destination.read_bytes(), b"previous-export")
        self.assertEqual(index["version"], "previous")
        self.assertEqual(engine.ledger.get_document_job(job.id).publish_status, "failed")

    def test_publish_success_switches_once_after_full_coverage(self) -> None:
        engine, job = self._completed_engine()
        destination = self.root / "published.zip"
        index = {"version": "previous", "calls": 0}

        def publish_index():
            index.update(version="new", calls=index["calls"] + 1)

        published = engine.publish(
            job.id,
            manifest=self._manifest(4),
            destination=destination,
            publish_index=publish_index,
        )
        self.assertEqual(published.status, "published")
        self.assertEqual(index, {"version": "new", "calls": 1})
        self.assertEqual(
            [p["physical_pdf_page"] for p in read_document_export(destination).pages],
            [1, 2, 3, 4],
        )

    def test_multiple_retry_attempts_reach_explainable_final_state(self) -> None:
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        provider = SyntheticProvider(max_pages=4, fail_submissions=2)
        engine = LargeDocumentJobEngine(
            ledger=JobLedger(self.root / "jobs.sqlite3"),
            provider=provider,
            work_dir=self.root / "work",
            page_counter=lambda path: 4,
            max_attempts=3,
        )
        job = engine.prepare(source_path=source, source_file_id="pdf-1", document_id="doc")
        self.assertEqual(engine.run_once(job.id).status, "retryable_failure")
        self.assertEqual(engine.run_once(job.id).status, "retryable_failure")
        self.assertEqual(engine.run_once(job.id).status, "validated")
        self.assertEqual(engine.ledger.list_slice_jobs(job.id)[0].attempt_count, 3)

    def test_streaming_hash_uses_bounded_reads_for_2gb_metadata(self) -> None:
        class FakeLargeStream(io.BytesIO):
            logical_size = 2 * 1024 * 1024 * 1024

            def __init__(self):
                super().__init__(b"x" * (3 * 1024 * 1024))
                self.requested = []

            def read(self, size=-1):
                self.requested.append(size)
                if size < 0 or size > 1024 * 1024:
                    raise AssertionError("unbounded large-file read")
                return super().read(size)

        stream = FakeLargeStream()
        with mock.patch("pathlib.Path.open", return_value=stream):
            digest = sha256_file(self.root / "synthetic-2gb.pdf")
        self.assertEqual(digest, hashlib.sha256(b"x" * (3 * 1024 * 1024)).hexdigest())
        self.assertTrue(stream.requested)
        self.assertTrue(all(size == 1024 * 1024 for size in stream.requested))

    def test_mineru_upload_and_download_are_chunked(self) -> None:
        source = self.root / "upload.pdf"
        source.write_bytes(b"x" * (3 * 1024 * 1024))

        class Response:
            status = 200

            def __init__(self, body=b""):
                self.body = io.BytesIO(body)
                self.read_sizes = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                self.read_sizes.append(size)
                if size < 0:
                    raise AssertionError("download attempted an unbounded read")
                return self.body.read(size)

        download_response = Response(b"z" * (3 * 1024 * 1024))

        class Opener:
            def __init__(self):
                self.upload_read_sizes = []

            def open(self, request, timeout):
                if request.get_method() == "PUT":
                    while True:
                        chunk = request.data.read(1024 * 1024)
                        self.upload_read_sizes.append(1024 * 1024)
                        if not chunk:
                            break
                    return Response()
                return download_response

        client = MinerUClient(MinerUConfig(token="secret"))
        opener = Opener()
        client.opener = opener
        self.assertEqual(client.upload_file("https://upload", source), 200)
        output = self.root / "download.bin"
        client.download_url("https://download", output)
        self.assertEqual(output.stat().st_size, 3 * 1024 * 1024)
        self.assertTrue(all(size == 1024 * 1024 for size in download_response.read_sizes))

    def test_large_simple_pdf_fallback_refuses_full_memory_load(self) -> None:
        path = self.root / "sparse.pdf"
        with path.open("wb") as stream:
            stream.seek(SIMPLE_PDF_MAX_BYTES)
            stream.write(b"x")
        with self.assertRaises(PDFExtractionError):
            SimplePDF(path)

    def _completed_engine(self):
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        engine = LargeDocumentJobEngine(
            ledger=JobLedger(self.root / "jobs.sqlite3"),
            provider=SyntheticProvider(max_pages=2),
            work_dir=self.root / "work",
            slicer=PhysicalPDFSlicer(synthetic_slice_writer),
            page_counter=lambda path: 4,
        )
        job = engine.prepare(source_path=source, source_file_id="pdf-1", document_id="doc")
        completed = engine.run_once(job.id)
        self.assertEqual(completed.status, "validated")
        return engine, completed

    @staticmethod
    def _manifest(page_count):
        return document_manifest(
            document={"document_id": "doc", "source_file_id": "pdf-1"},
            source_sha256=hashlib.sha256(b"source").hexdigest(),
            source_file={"file_name": "source.pdf"},
            parser_provider="synthetic",
            page_count=page_count,
        )


if __name__ == "__main__":
    unittest.main()

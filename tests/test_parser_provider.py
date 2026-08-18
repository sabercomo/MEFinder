import hashlib
import tempfile
import unittest
from pathlib import Path

from src.me_finder.mineru_api import MinerUError
from src.me_finder.mineru_provider import MinerUCloudProvider
from src.me_finder.parser_provider import (
    NormalizedPage,
    NormalizedParseResult,
    ParserPollResult,
    ParserProvider,
    ParserProviderError,
    ParserRequest,
    ParserSubmission,
    ParserTaskStatus,
    ProviderCapabilities,
)


class FakeParserProvider(ParserProvider):
    provider_id = "fake"

    def __init__(self, *, asynchronous: bool) -> None:
        self.asynchronous = asynchronous
        self.poll_count = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            max_pages_per_file=10,
            max_bytes_per_file=1024,
            supports_async_jobs=self.asynchronous,
            supports_stream_upload=True,
        )

    def submit(self, request, *, credential=None):
        self.prepare(request)
        if self.asynchronous:
            return ParserSubmission("fake", "remote-1", ParserTaskStatus.SUBMITTED)
        return ParserSubmission(
            "fake",
            None,
            ParserTaskStatus.COMPLETED,
            raw_result={"pages": ["ok"]},
        )

    def poll(self, remote_task_id, *, credential=None):
        self.poll_count += 1
        return ParserPollResult(
            ParserTaskStatus.COMPLETED
            if self.poll_count > 1
            else ParserTaskStatus.WAITING
        )

    def fetch_result(self, submission, request, *, credential=None):
        return submission.raw_result or {"pages": ["ok"]}

    def normalize_result(self, raw_result, request):
        if not isinstance(raw_result, dict):
            raise ParserProviderError("malformed", provider_id=self.provider_id)
        return NormalizedParseResult(
            provider_id="fake",
            model=None,
            pages=(
                NormalizedPage(
                    physical_pdf_page=request.global_page_offset + 1,
                    text=str(raw_result["pages"][0]),
                ),
            ),
        )


class FakeMinerUClient:
    def __init__(self) -> None:
        self.applied = None
        self.uploaded = None
        self.status_calls = []

    def apply_upload_urls(self, files, **options):
        self.applied = (files, options)
        return {"data": {"batch_id": "batch-1", "file_urls": ["https://upload"]}}

    def upload_file(self, url, path):
        self.uploaded = (url, Path(path))
        return 200

    def batch_status(self, task_id):
        self.status_calls.append(task_id)
        return {
            "code": 0,
            "data": {
                "extract_result": [
                    {
                        "state": "done",
                        "content_list": [
                            {"page_idx": 0, "type": "text", "text": "第一頁", "text_level": 1, "bbox": [1, 2, 3, 4]},
                            {"page_idx": 1, "type": "text", "text": "第二页", "text_level": 2},
                        ],
                    }
                ]
            },
        }


class ParserProviderContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "slice.pdf"
        self.path.write_bytes(b"%PDF-small")
        self.request = ParserRequest(
            source_path=self.path,
            source_sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),
            document_id="doc",
            page_start=1,
            page_end=2,
            global_page_offset=20,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_capability_is_returned_and_enforced(self) -> None:
        provider = FakeParserProvider(asynchronous=False)
        self.assertEqual(provider.capabilities().max_pages_per_file, 10)
        too_large = ParserRequest(
            source_path=self.path,
            source_sha256=self.request.source_sha256,
            document_id="doc",
            page_start=1,
            page_end=11,
            global_page_offset=0,
        )
        with self.assertRaises(ParserProviderError):
            provider.prepare(too_large)

    def test_async_provider_lifecycle(self) -> None:
        provider = FakeParserProvider(asynchronous=True)
        submission = provider.submit(self.request)
        self.assertEqual(submission.status, ParserTaskStatus.SUBMITTED)
        self.assertEqual(provider.poll("remote-1").status, ParserTaskStatus.WAITING)
        self.assertEqual(provider.poll("remote-1").status, ParserTaskStatus.COMPLETED)
        result = provider.normalize_result(
            provider.fetch_result(submission, self.request), self.request
        )
        self.assertEqual(result.pages[0].physical_pdf_page, 21)

    def test_sync_provider_lifecycle(self) -> None:
        provider = FakeParserProvider(asynchronous=False)
        submission = provider.submit(self.request)
        self.assertEqual(submission.status, ParserTaskStatus.COMPLETED)
        normalized = provider.normalize_result(submission.raw_result, self.request)
        self.assertEqual(normalized.pages[0].text, "ok")

    def test_provider_errors_are_normalized(self) -> None:
        with self.assertRaises(ParserProviderError):
            FakeParserProvider(asynchronous=False).normalize_result([], self.request)

    def test_normalized_result_enters_common_page_shape(self) -> None:
        page = FakeParserProvider(asynchronous=False).normalize_result(
            {"pages": ["正文"]}, self.request
        ).pages[0]
        self.assertEqual(page.to_dict()["physical_pdf_page"], 21)
        self.assertEqual(page.to_dict()["text"], "正文")

    def test_mineru_cloud_matches_existing_submit_poll_fetch_behavior(self) -> None:
        client = FakeMinerUClient()
        provider = MinerUCloudProvider(client=client)
        self.assertEqual(
            provider.capabilities().max_bytes_per_file,
            200 * 1024 * 1024,
        )
        submission = provider.submit(self.request)
        self.assertEqual(submission.remote_task_id, "batch-1")
        self.assertEqual(client.applied[0][0]["name"], "slice.pdf")
        self.assertNotIn("page_ranges", client.applied[0][0])
        self.assertEqual(client.uploaded, ("https://upload", self.path))
        self.assertEqual(provider.poll("batch-1").status, ParserTaskStatus.COMPLETED)
        raw = provider.fetch_result(submission, self.request)
        normalized = provider.normalize_result(raw, self.request)
        self.assertEqual([p.physical_pdf_page for p in normalized.pages], [21, 22])
        self.assertEqual(normalized.pages[0].blocks[0].bbox, (1, 2, 3, 4))
        self.assertEqual(
            [block.text_level for page in normalized.pages for block in page.blocks],
            [1, 2],
        )
        self.assertEqual(
            normalized.pages[0].blocks[0].to_dict()["text_level"],
            1,
        )

    def test_mineru_text_level_is_passthrough_and_missing_stays_none(self) -> None:
        provider = MinerUCloudProvider(client=FakeMinerUClient())
        normalized = provider.normalize_result(
            {
                "content_list": [
                    {"page_idx": 0, "type": "text", "text": "章", "text_level": 1},
                    {"page_idx": 0, "type": "text", "text": "节", "text_level": 2},
                    {"page_idx": 0, "type": "text", "text": "小节", "text_level": 3},
                    {"page_idx": 0, "type": "text", "text": "无层级正文"},
                ]
            },
            self.request,
        )
        self.assertEqual(
            [block.text_level for block in normalized.pages[0].blocks],
            [1, 2, 3, None],
        )

    def test_mineru_header_type_without_text_level_gets_no_level(self) -> None:
        provider = MinerUCloudProvider(client=FakeMinerUClient())
        normalized = provider.normalize_result(
            {
                "content_list": [
                    {"page_idx": 0, "type": "header", "text": "无层级标题"},
                ]
            },
            self.request,
        )
        block = normalized.pages[0].blocks[0]
        self.assertEqual(block.block_type, "header")
        self.assertIsNone(block.text_level)

    def test_mineru_error_does_not_leak_outside_provider_contract(self) -> None:
        class FailingClient(FakeMinerUClient):
            def apply_upload_urls(self, files, **options):
                raise MinerUError("MinerU request failed (HTTP 429)")

        provider = MinerUCloudProvider(client=FailingClient())
        with self.assertRaises(ParserProviderError) as caught:
            provider.submit(self.request)
        self.assertTrue(caught.exception.rate_limited)
        self.assertTrue(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()

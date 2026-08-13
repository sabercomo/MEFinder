import hashlib
import tempfile
import unittest
from pathlib import Path

from src.me_finder.large_document.slicing import SlicePlanner
from src.me_finder.parser_provider import ParserProviderError, ParserRequest, ParserTaskStatus
from src.me_finder.qwen_ocr_provider import QwenOCRConfig, QwenOCRProvider
from src.me_finder.vision_api import VisionAPIError


class FakeQwenClient:
    def __init__(self, outputs=None, error=None):
        self.outputs = list(outputs or [])
        self.error = error
        self.calls = []

    def extract_page(self, image, mime_type="image/png"):
        self.calls.append((image, mime_type))
        if self.error:
            raise self.error
        return self.outputs.pop(0)


class QwenOCRProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "slice.pdf"
        self.path.write_bytes(b"%PDF-qwen")
        self.config = QwenOCRConfig(
            api_base="https://workspace.example/compatible-mode/v1",
            api_key="dashscope-super-secret",
            model="qwen3.5-ocr",
        )
        self.request = ParserRequest(
            source_path=self.path,
            source_sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),
            document_id="doc",
            page_start=1,
            page_end=2,
            global_page_offset=50,
            model="qwen3.5-ocr",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def provider(self, client):
        return QwenOCRProvider(
            self.config,
            client=client,
            page_renderer=lambda path, edge: iter([b"page-one", b"page-two"]),
        )

    def test_capability_and_50_page_planner_limit(self) -> None:
        provider = self.provider(FakeQwenClient())
        capability = provider.capabilities()
        self.assertEqual(capability.max_pages_per_file, 50)
        self.assertEqual(capability.max_bytes_per_file, 100 * 1024 * 1024)
        ranges = SlicePlanner().plan(
            total_pages=120,
            total_bytes=1,
            capabilities=capability,
        )
        self.assertEqual([(r.page_start, r.page_end) for r in ranges], [(1, 50), (51, 100), (101, 120)])

    def test_normal_scanned_traditional_chinese_parse(self) -> None:
        client = FakeQwenClient(["學而時習之", "測試繁體中文"])
        provider = self.provider(client)
        submission = provider.submit(self.request)
        self.assertEqual(submission.status, ParserTaskStatus.COMPLETED)
        normalized = provider.normalize_result(
            provider.fetch_result(submission, self.request), self.request
        )
        self.assertEqual([page.physical_pdf_page for page in normalized.pages], [51, 52])
        self.assertEqual(normalized.pages[1].text, "測試繁體中文")
        self.assertEqual(client.calls, [(b"page-one", "image/png"), (b"page-two", "image/png")])

    def test_timeout_and_rate_limit_are_classified(self) -> None:
        cases = [
            (VisionAPIError("Qwen network timeout"), True, False),
            (VisionAPIError("Qwen 返回 HTTP 429：rate limit"), True, True),
        ]
        for error, retryable, rate_limited in cases:
            with self.subTest(error=str(error)):
                provider = self.provider(FakeQwenClient(error=error))
                with self.assertRaises(ParserProviderError) as caught:
                    provider.submit(self.request)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertEqual(caught.exception.rate_limited, rate_limited)

    def test_malformed_response_is_rejected(self) -> None:
        provider = self.provider(FakeQwenClient(["one", "two"]))
        with self.assertRaisesRegex(ParserProviderError, "malformed"):
            provider.normalize_result({"unexpected": []}, self.request)
        with self.assertRaisesRegex(ParserProviderError, "page text"):
            provider.normalize_result(
                {"pages": [{"page_idx": 0, "text": None}, {"page_idx": 1, "text": "ok"}]},
                self.request,
            )

    def test_bbox_present_and_absent_are_both_normalized(self) -> None:
        provider = self.provider(FakeQwenClient())
        result = provider.normalize_result(
            {
                "model": "qwen3.5-ocr",
                "pages": [
                    {
                        "page_idx": 0,
                        "text": "with bbox",
                        "blocks": [{"text": "with bbox", "bbox": [0.1, 0.2, 0.3, 0.4]}],
                    },
                    {"page_idx": 1, "text": "without bbox"},
                ],
            },
            self.request,
        )
        self.assertEqual(result.pages[0].blocks[0].bbox, (0.1, 0.2, 0.3, 0.4))
        self.assertEqual(result.pages[1].blocks, ())

    def test_page_offset_and_provider_provenance_are_preserved(self) -> None:
        result = self.provider(FakeQwenClient()).normalize_result(
            {"pages": [{"page_idx": 0, "text": "a"}, {"page_idx": 1, "text": "b"}]},
            self.request,
        )
        self.assertEqual(result.pages[0].physical_pdf_page, 51)
        self.assertEqual(result.pages[0].parser_provenance["global_page_offset"], 50)
        self.assertEqual(result.provider_id, "qwen-ocr")
        self.assertEqual(result.model, "qwen3.5-ocr")

    def test_api_key_is_not_exposed_by_repr_or_errors(self) -> None:
        self.assertNotIn("dashscope-super-secret", repr(self.config))
        provider = self.provider(FakeQwenClient(error=VisionAPIError("HTTP 401")))
        with self.assertRaises(ParserProviderError) as caught:
            provider.submit(self.request)
        self.assertNotIn("dashscope-super-secret", str(caught.exception))
        self.assertTrue(caught.exception.authentication_failed)


if __name__ == "__main__":
    unittest.main()

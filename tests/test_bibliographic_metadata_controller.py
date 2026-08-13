from __future__ import annotations

import threading
import unittest

from src.me_finder.application.bibliographic_metadata_coordinator import (
    BibliographicMetadataQueueError,
)
from src.me_finder.application.document_query_service import (
    DocumentQueryError,
    DocumentQueryUnavailable,
)
from src.me_finder.bibliographic_metadata_controller import (
    BibliographicMetadataController,
)
from src.me_finder.crossref_lookup import CrossrefLookupError
from src.me_finder.foreign_book_lookup import BookLookupError
from src.me_finder.journal_metadata_lookup import CNKILookupError
from src.me_finder.mineru_api import MinerUError


class FakeQueries:
    def __init__(self) -> None:
        self.metadata_result = {"title": "题名"}
        self.metadata_error: Exception | None = None
        self.detect_result = {"title": "识别题名"}
        self.detect_error: Exception | None = None
        self.detect_calls: list[tuple[str, bool]] = []

    def bibliographic_metadata(self, source_file_id: str):
        if self.metadata_error is not None:
            raise self.metadata_error
        return {**self.metadata_result, "source_file_id": source_file_id}

    def detect_bibliographic_metadata(
        self,
        source_file_id: str,
        *,
        force: bool = False,
    ):
        self.detect_calls.append((source_file_id, force))
        if self.detect_error is not None:
            raise self.detect_error
        return dict(self.detect_result)


class FakeMetadataCoordinator:
    def __init__(self) -> None:
        self.batch_result = {"job_id": "batch-1", "candidates": 2}
        self.batch_error: Exception | None = None
        self.batch_active_ids: set[str] | None = None
        self.save_result = {"title": "已保存"}
        self.save_error: Exception | None = None
        self.save_calls: list[tuple[str, dict[str, object]]] = []

    def start_batch(self, *, additional_active_source_ids=()):
        self.batch_active_ids = set(additional_active_source_ids)
        if self.batch_error is not None:
            raise self.batch_error
        return dict(self.batch_result)

    def save_manual(self, source_file_id: str, payload):
        self.save_calls.append((source_file_id, dict(payload)))
        if self.save_error is not None:
            raise self.save_error
        return dict(self.save_result)


class BibliographicMetadataControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queries = FakeQueries()
        self.metadata = FakeMetadataCoordinator()
        self.lookup_lock = threading.Lock()
        self.lookup_calls: list[tuple[str, dict[str, object]]] = []
        self.lookup_errors: dict[str, Exception] = {}
        self.controller = BibliographicMetadataController(
            self.queries,
            self.metadata,
            additional_active_source_ids=lambda: {"mapping-active"},
            lookup_lock=self.lookup_lock,
            parse_cnki_citation=self._parse_citation,
            lookup_cnki=lambda payload: self._lookup("cnki", payload),
            fetch_cnki_candidate=(
                lambda payload: self._lookup("candidate", payload)
            ),
            lookup_google_books=(
                lambda payload: self._lookup("books", payload)
            ),
            lookup_crossref=(
                lambda payload: self._lookup("crossref", payload)
            ),
        )

    @staticmethod
    def _parse_citation(value: object):
        if value == "bad":
            raise ValueError("引文无效")
        return {"title": str(value)}

    def _lookup(self, name: str, payload):
        self.lookup_calls.append((name, dict(payload)))
        error = self.lookup_errors.get(name)
        if error is not None:
            raise error
        return {"provider": name, "received": dict(payload)}

    def test_metadata_get_preserves_success_and_not_found_contract(self) -> None:
        self.assertEqual(
            self.controller.metadata("pdf-one"),
            (
                200,
                {
                    "ok": True,
                    "metadata": {
                        "title": "题名",
                        "source_file_id": "pdf-one",
                    },
                },
            ),
        )
        self.assertEqual(
            self.controller.metadata(""),
            (400, {"error": "invalid request"}),
        )
        self.queries.metadata_error = DocumentQueryError("文献不存在")
        self.assertEqual(
            self.controller.metadata("missing"),
            (404, {"error": "文献不存在"}),
        )

    def test_batch_detect_passes_mapping_activity_and_maps_queue_errors(self) -> None:
        self.assertEqual(
            self.controller.batch_detect({}),
            (
                200,
                {
                    "ok": True,
                    "job_id": "batch-1",
                    "candidates": 2,
                },
            ),
        )
        self.assertEqual(self.metadata.batch_active_ids, {"mapping-active"})

        self.metadata.batch_error = BibliographicMetadataQueueError(
            "batch-full",
            RuntimeError("队列已满"),
        )
        self.assertEqual(
            self.controller.batch_detect(None),
            (
                503,
                {"error": "队列已满", "job_id": "batch-full"},
            ),
        )
        self.metadata.batch_error = DocumentQueryUnavailable("索引正在重建")
        self.assertEqual(
            self.controller.batch_detect([]),
            (503, {"error": "索引正在重建"}),
        )
        self.metadata.batch_error = OSError("读取失败")
        self.assertEqual(
            self.controller.batch_detect({}),
            (500, {"error": "筛选待识别文献失败：读取失败"}),
        )

    def test_parse_cnki_citation_requires_exact_payload(self) -> None:
        self.assertEqual(
            self.controller.parse_cnki_citation({"citation_text": "引文"}),
            (200, {"ok": True, "metadata": {"title": "引文"}}),
        )
        self.assertEqual(
            self.controller.parse_cnki_citation(
                {"citation_text": "引文", "extra": True}
            ),
            (400, {"error": "请求必须只包含 citation_text。"}),
        )
        self.assertEqual(
            self.controller.parse_cnki_citation({"citation_text": "bad"}),
            (400, {"error": "引文无效"}),
        )

    def test_cnki_lookup_validates_fields_and_maps_provider_error(self) -> None:
        payload = {"metadata": {"title": "题名", "doi": "10/x"}}
        self.assertEqual(
            self.controller.lookup_cnki(payload),
            (
                200,
                {
                    "ok": True,
                    "provider": "cnki",
                    "received": payload["metadata"],
                },
            ),
        )
        self.assertEqual(
            self.controller.lookup_cnki({"metadata": {"isbn": "x"}}),
            (400, {"error": "知网查询字段无效。"}),
        )

        self.lookup_errors["cnki"] = CNKILookupError(
            "verification_required",
            "需要验证",
            open_url="https://example.test/verify",
        )
        self.assertEqual(
            self.controller.lookup_cnki(payload),
            (
                403,
                {
                    "error": "需要验证",
                    "code": "verification_required",
                    "open_url": "https://example.test/verify",
                },
            ),
        )
        self.assertTrue(self.lookup_lock.acquire(blocking=False))
        self.lookup_lock.release()

    def test_lookup_busy_uses_cnki_and_network_specific_messages(self) -> None:
        self.lookup_lock.acquire()
        try:
            self.assertEqual(
                self.controller.cnki_candidate(
                    {"candidate": {"record_url": "https://example.test"}}
                ),
                (
                    409,
                    {
                        "error": "已有知网查询正在进行，请稍候。",
                        "code": "lookup_busy",
                    },
                ),
            )
            self.assertEqual(
                self.controller.lookup_google_books(
                    {"metadata": {"title": "Book"}}
                ),
                (
                    409,
                    {
                        "error": "已有联网查询正在进行，请稍候。",
                        "code": "lookup_busy",
                    },
                ),
            )
        finally:
            self.lookup_lock.release()

    def test_candidate_books_and_crossref_preserve_error_mappings(self) -> None:
        cases = [
            (
                "candidate",
                self.controller.cnki_candidate,
                {"candidate": {"record_url": "https://example.test"}},
                CNKILookupError("invalid_candidate", "候选无效"),
                400,
            ),
            (
                "books",
                self.controller.lookup_google_books,
                {"metadata": {"title": "Book", "isbn": "123"}},
                BookLookupError("timeout", "图书查询超时", "books-url"),
                504,
            ),
            (
                "crossref",
                self.controller.lookup_crossref,
                {"metadata": {"title": "Paper", "doi": "10/x"}},
                CrossrefLookupError("rate_limited", "Crossref 限流", "doi-url"),
                429,
            ),
        ]
        for name, operation, payload, error, expected_status in cases:
            with self.subTest(name=name):
                self.lookup_errors[name] = error
                status, response = operation(payload)
                self.assertEqual(status, expected_status)
                self.assertEqual(response["error"], str(error))
                self.assertEqual(response["code"], error.code)
                self.assertEqual(response["open_url"], error.open_url)
                self.lookup_errors.pop(name)

    def test_detect_maps_application_and_runtime_failures(self) -> None:
        self.assertEqual(
            self.controller.detect({"source_id": "pdf-one", "force": 1}),
            (200, {"ok": True, "metadata": {"title": "识别题名"}}),
        )
        self.assertEqual(self.queries.detect_calls, [("pdf-one", True)])
        self.assertEqual(
            self.controller.detect([]),
            (400, {"error": "invalid request"}),
        )

        self.queries.detect_error = DocumentQueryError("无法读取文献")
        self.assertEqual(
            self.controller.detect({"source_id": "pdf-one"}),
            (400, {"error": "无法读取文献"}),
        )
        self.queries.detect_error = DocumentQueryUnavailable("索引不可用")
        self.assertEqual(
            self.controller.detect({"source_id": "pdf-one"}),
            (503, {"error": "索引不可用"}),
        )
        self.queries.detect_error = OSError("磁盘错误")
        self.assertEqual(
            self.controller.detect({"source_id": "pdf-one"}),
            (500, {"error": "书目信息识别失败：磁盘错误"}),
        )

    def test_save_validates_payload_and_maps_application_failures(self) -> None:
        payload = {"source_id": "pdf-one", "metadata": {"title": "新题名"}}
        self.assertEqual(
            self.controller.save(payload),
            (200, {"ok": True, "metadata": {"title": "已保存"}}),
        )
        self.assertEqual(
            self.metadata.save_calls,
            [("pdf-one", {"title": "新题名"})],
        )
        self.assertEqual(
            self.controller.save({"source_id": "pdf-one", "metadata": [1]}),
            (400, {"error": "invalid request"}),
        )

        self.metadata.save_error = MinerUError("配置不存在")
        self.assertEqual(
            self.controller.save(payload),
            (400, {"error": "配置不存在"}),
        )
        self.metadata.save_error = OSError("保存失败")
        self.assertEqual(
            self.controller.save(payload),
            (500, {"error": "书目信息保存失败：保存失败"}),
        )


if __name__ == "__main__":
    unittest.main()

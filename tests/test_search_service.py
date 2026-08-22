from __future__ import annotations

import unittest

from src.me_finder.application import SearchRequest, SearchService


class RecordingSearchEngine:
    def __init__(self) -> None:
        self.arguments: tuple[object, ...] | None = None

    def search(self, *arguments) -> dict[str, object]:
        self.arguments = arguments
        return {"total": 0, "results": []}


class SearchServiceTests(unittest.TestCase):
    def test_request_dto_preserves_legacy_all_limit(self) -> None:
        request = SearchRequest.from_payload(
            {
                "query": "原句",
                "mode": "punctuation",
                "limit": "0",
                "source_type": "pdf",
                "source_file_id": " pdf-one ",
            }
        )

        self.assertEqual(request.limit, "all")
        self.assertEqual(request.source_file_id, "pdf-one")

    def test_invalid_limit_keeps_existing_default(self) -> None:
        self.assertEqual(SearchRequest.from_payload({"limit": "many"}).limit, 10)

    def test_non_object_payload_is_rejected_at_application_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON 对象"):
            SearchRequest.from_payload([])

    def test_service_delegates_normalized_request_to_search_port(self) -> None:
        engine = RecordingSearchEngine()
        request = SearchRequest.from_payload(
            {"query": "原句", "limit": 5, "source_type": "word"}
        )

        result = SearchService.execute(engine, request)

        self.assertEqual(result, {"total": 0, "results": []})
        self.assertEqual(
            engine.arguments,
            ("原句", "auto", 5, "word", None, None),
        )


if __name__ == "__main__":
    unittest.main()

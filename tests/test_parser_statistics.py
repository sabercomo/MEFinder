from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import build_database
from src.me_finder.parser_statistics import build_parser_statistics


def _pdf_source(
    source_id: str,
    title: str,
    *,
    parser: str,
    provider_id: str | None = None,
    provider_name: str | None = None,
) -> dict[str, object]:
    profile: dict[str, object] = {"parser": parser}
    if provider_id:
        profile["provider_id"] = provider_id
    if provider_name:
        profile["provider_name"] = provider_name
    return {
        "source_file_id": source_id,
        "source_type": "pdf",
        "document_id": f"document-{source_id}",
        "file_name": f"{title}.pdf",
        "display_title": title,
        "pdf_profile": profile,
    }


def _pages(source_id: str, count: int, parser: str) -> list[dict[str, object]]:
    return [
        {
            "source_file_id": source_id,
            "pdf_page_index": index,
            "text_raw": f"{source_id}-{index}",
            "parser": parser,
        }
        for index in range(count)
    ]


class ParserStatisticsTests(unittest.TestCase):
    def test_totals_group_all_real_parser_providers_and_enrich_mineru_accounts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "index.sqlite3"
            build_database(
                {
                    "metadata": {},
                    "source_files": [
                        _pdf_source(
                            "mineru-book",
                            "MinerU 之书",
                            parser="mineru",
                            provider_id="mineru-cloud",
                            provider_name="MinerU",
                        ),
                        _pdf_source(
                            "vision-book",
                            "视觉 API 之书",
                            parser="openai_compatible",
                            provider_id="provider-qwen",
                            provider_name="通义千问",
                        ),
                        _pdf_source(
                            "native-book",
                            "原生 PDF",
                            parser="pymupdf",
                        ),
                        _pdf_source(
                            "local-ocr-book",
                            "本地 OCR 之书",
                            parser="ndlocr-lite",
                            provider_id="ndlocr-lite",
                        ),
                        {
                            "source_file_id": "word-book",
                            "source_type": "word",
                            "file_name": "word.docx",
                        },
                    ],
                    "pdf_pages": (
                        _pages("mineru-book", 3, "mineru")
                        + _pages("vision-book", 2, "openai_compatible")
                        + _pages("native-book", 1, "pymupdf")
                        + _pages("local-ocr-book", 4, "ndlocr-lite")
                    ),
                    "pdf_import_runs": [
                        {
                            "run_id": "run-1",
                            "source_file_id": "vision-book",
                            "status": "success",
                            "finished_at": "2026-08-11T08:00:00+00:00",
                        }
                    ],
                },
                database,
            )
            result = build_parser_statistics(
                database,
                mineru_statistics={
                    "credentials": [
                        {
                            "account_id": "account-1",
                            "display_name": "MinerU 工作账号",
                            "books": [
                                {
                                    "source_file_id": "mineru-book",
                                    "source_file_name": "MinerU 之书.pdf",
                                    "parsed_page_count": 3,
                                    "page_ranges": [[1, 3]],
                                },
                                {
                                    "source_file_id": "deleted-book",
                                    "source_file_name": "已删除.pdf",
                                    "parsed_page_count": 9,
                                    "page_ranges": [[1, 9]],
                                },
                            ],
                        }
                    ]
                },
            )

        self.assertEqual(
            result["total"],
            {
                "parsed_book_count": 4,
                "parsed_page_count": 10,
                "provider_count": 4,
            },
        )
        providers = {
            item["provider_id"]: item for item in result["providers"]
        }
        self.assertEqual(providers["provider-qwen"]["provider_name"], "通义千问")
        self.assertEqual(providers["provider-qwen"]["parsed_page_count"], 2)
        self.assertEqual(providers["pymupdf"]["provider_kind"], "local")
        self.assertEqual(providers["ndlocr-lite"]["provider_kind"], "local")
        self.assertEqual(providers["ndlocr-lite"]["provider_name"], "NDL 日文 OCR")
        self.assertEqual(
            providers["mineru-cloud"]["credentials"][0]["parsed_page_count"],
            3,
        )
        self.assertEqual(
            len(providers["mineru-cloud"]["credentials"][0]["books"]),
            1,
        )

    def test_missing_database_returns_an_empty_stable_contract(self) -> None:
        result = build_parser_statistics(Path("/not/a/real/index.sqlite3"))
        self.assertEqual(result["total"]["parsed_book_count"], 0)
        self.assertEqual(result["providers"], [])


if __name__ == "__main__":
    unittest.main()

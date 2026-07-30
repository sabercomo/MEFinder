from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.me_finder.pdf_extractors import file_sha256
from src.me_finder.pdf_import_service import (
    attach_mineru_manifest,
    load_import_config,
    rebuild_local_index,
    register_pdf,
    save_import_config,
)


class PDFImportConfigTests(unittest.TestCase):
    def _root(self, temporary: str) -> Path:
        root = Path(temporary) / "MEFinder"
        (root / "config").mkdir(parents=True)
        (root / "corpus" / "raw_pdf").mkdir(parents=True)
        return root

    @staticmethod
    def _pdf(root: Path, name: str, content: bytes) -> Path:
        path = root / "corpus" / "raw_pdf" / name
        path.write_bytes(content)
        return path

    @staticmethod
    def _source_id(path: Path) -> str:
        return f"pdf-import-{file_sha256(path)[:16]}"

    def test_retry_under_a_new_file_name_reuses_the_content_id(self) -> None:
        with TemporaryDirectory() as temporary:
            root = self._root(temporary)
            original = self._pdf(root, "paper.pdf", b"%PDF identical bytes")
            retry = self._pdf(
                root,
                "paper (imported-deadbeef).pdf",
                b"%PDF identical bytes",
            )

            first = register_pdf(root, original)
            second = register_pdf(root, retry)

            self.assertEqual(first["source_file_id"], second["source_file_id"])
            config = json.loads(
                (root / "config" / "pdf_imports.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(config["documents"]), 1)
            self.assertEqual(config["documents"][0]["file_name"], original.name)
            # Registration only repairs configuration; neither the copied retry
            # nor any user-owned source file is deleted.
            self.assertTrue(original.exists())
            self.assertTrue(retry.exists())

    def test_legacy_duplicates_merge_metadata_without_removing_pdf_files(self) -> None:
        with TemporaryDirectory() as temporary:
            root = self._root(temporary)
            original = self._pdf(root, "paper.pdf", b"%PDF legacy duplicate")
            retry = self._pdf(
                root,
                "paper (imported-cafebabe).pdf",
                b"%PDF legacy duplicate",
            )
            source_id = self._source_id(original)
            config_path = root / "config" / "pdf_imports.json"
            config_path.write_text(
                json.dumps(
                    {
                        "custom_setting": "preserved",
                        "documents": [
                            {
                                "enabled": True,
                                "source_file_id": source_id,
                                "document_id": "DOCUMENT_FIRST",
                                "file_name": original.name,
                                "title": "人工标题",
                                "author": None,
                                "page_mapping": {
                                    "validated_by": "manual",
                                    "segments": [{"pdf_page_start": 1}],
                                },
                            },
                            {
                                "enabled": True,
                                "source_file_id": source_id,
                                "document_id": "DOCUMENT_SECOND",
                                "file_name": retry.name,
                                "author": "作者",
                                "mineru": {"manifest": "parsed/manifest.json"},
                                "bibliographic_metadata": {"publisher": "出版社"},
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            reused = register_pdf(root, retry)

            self.assertEqual(reused["source_file_id"], source_id)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["custom_setting"], "preserved")
            self.assertEqual(len(saved["documents"]), 1)
            merged = saved["documents"][0]
            self.assertEqual(merged["file_name"], original.name)
            self.assertEqual(merged["document_id"], "DOCUMENT_FIRST")
            self.assertEqual(merged["title"], "人工标题")
            self.assertEqual(merged["author"], "作者")
            self.assertEqual(merged["mineru"]["manifest"], "parsed/manifest.json")
            self.assertEqual(
                merged["bibliographic_metadata"]["publisher"],
                "出版社",
            )
            self.assertEqual(
                merged["page_mapping"]["segments"],
                [{"pdf_page_start": 1}],
            )
            self.assertTrue(original.exists())
            self.assertTrue(retry.exists())

    def test_legacy_duplicate_keeps_the_more_complete_parser_result(self) -> None:
        with TemporaryDirectory() as temporary:
            root = self._root(temporary)
            pdf = self._pdf(root, "paper.pdf", b"%PDF parser retry")
            source_id = self._source_id(pdf)
            parsed_dir = root / "corpus" / "parsed"
            parsed_dir.mkdir(parents=True)
            partial_manifest = parsed_dir / "partial.json"
            complete_manifest = parsed_dir / "complete.json"
            partial_manifest.write_text(
                json.dumps(
                    {
                        "total_pages": 4,
                        "completed_pages": [1],
                        "completed_page_count": 1,
                        "failed_pages": [{"page": 2}],
                        "failed_page_count": 1,
                        "status": "failed",
                        "last_updated": "2026-07-29T10:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            complete_manifest.write_text(
                json.dumps(
                    {
                        "total_pages": 4,
                        "completed_pages": [1, 2, 3, 4],
                        "completed_page_count": 4,
                        "failed_pages": [],
                        "failed_page_count": 0,
                        "status": "completed",
                        "last_updated": "2026-07-29T11:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config" / "pdf_imports.json"

            save_import_config(
                config_path,
                {
                    "documents": [
                        {
                            "source_file_id": source_id,
                            "file_name": pdf.name,
                            "mineru": {
                                "manifest": partial_manifest.relative_to(root).as_posix(),
                                "resume": {
                                    "completed_page_count": 1,
                                    "failed_page_count": 1,
                                    "status": "failed",
                                },
                            },
                        },
                        {
                            "source_file_id": source_id,
                            "file_name": "paper (imported-retry).pdf",
                            "parser_results": {
                                "manifest": complete_manifest.relative_to(root).as_posix(),
                                "parser": "openai_compatible",
                                "resume": {
                                    "completed_page_count": 4,
                                    "failed_page_count": 0,
                                    "status": "completed",
                                },
                            },
                        },
                    ]
                },
            )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["documents"]), 1)
            merged = saved["documents"][0]
            self.assertNotIn("mineru", merged)
            self.assertEqual(
                merged["parser_results"]["manifest"],
                complete_manifest.relative_to(root).as_posix(),
            )
            self.assertEqual(
                merged["parser_results"]["resume"]["completed_page_count"],
                4,
            )

    def test_duplicate_repair_uses_a_still_existing_copy_if_first_is_missing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = self._root(temporary)
            existing = self._pdf(root, "still-here.pdf", b"%PDF still here")
            source_id = self._source_id(existing)
            config_path = root / "config" / "pdf_imports.json"

            save_import_config(
                config_path,
                {
                    "documents": [
                        {
                            "source_file_id": source_id,
                            "file_name": "missing.pdf",
                            "title": "保留标题",
                        },
                        {
                            "source_file_id": source_id,
                            "file_name": existing.name,
                            "author": "保留作者",
                        },
                    ]
                },
            )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["documents"]), 1)
            self.assertEqual(saved["documents"][0]["file_name"], existing.name)
            self.assertEqual(saved["documents"][0]["title"], "保留标题")
            self.assertEqual(saved["documents"][0]["author"], "保留作者")
            self.assertTrue(existing.exists())

    def test_concurrent_same_content_registration_keeps_one_document(self) -> None:
        with TemporaryDirectory() as temporary:
            root = self._root(temporary)
            paths = [
                self._pdf(root, f"retry-{index}.pdf", b"%PDF concurrent duplicate")
                for index in range(12)
            ]

            with ThreadPoolExecutor(max_workers=8) as executor:
                documents = list(executor.map(lambda path: register_pdf(root, path), paths))

            self.assertEqual(
                len({str(document["source_file_id"]) for document in documents}),
                1,
            )
            config = load_import_config(root / "config" / "pdf_imports.json")
            self.assertEqual(len(config["documents"]), 1)
            self.assertEqual(
                list((root / "config").glob(".pdf_imports.json.*.tmp")),
                [],
            )
            self.assertTrue(all(path.exists() for path in paths))

    def test_concurrent_manifest_attachments_do_not_lose_an_update(self) -> None:
        with TemporaryDirectory() as temporary:
            root = self._root(temporary)
            first = register_pdf(
                root,
                self._pdf(root, "first.pdf", b"%PDF first"),
            )
            second = register_pdf(
                root,
                self._pdf(root, "second.pdf", b"%PDF second"),
            )
            manifests = []
            for index in range(2):
                manifest = root / "corpus" / "parsed" / f"manifest-{index}.json"
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(
                    json.dumps(
                        {
                            "segments": [],
                            "total_pages": 0,
                            "completed_pages": [],
                            "failed_pages": [],
                        }
                    ),
                    encoding="utf-8",
                )
                manifests.append(manifest)

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        attach_mineru_manifest,
                        root,
                        str(first["source_file_id"]),
                        manifests[0],
                    ),
                    executor.submit(
                        attach_mineru_manifest,
                        root,
                        str(second["source_file_id"]),
                        manifests[1],
                    ),
                ]
                for future in futures:
                    future.result()

            config = load_import_config(root / "config" / "pdf_imports.json")
            by_id = {
                str(document["source_file_id"]): document
                for document in config["documents"]
            }
            self.assertIn("mineru", by_id[str(first["source_file_id"])])
            self.assertIn("mineru", by_id[str(second["source_file_id"])])

    def test_rebuild_persists_legacy_deduplication_before_indexing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = self._root(temporary)
            pdf = self._pdf(root, "paper.pdf", b"%PDF repair before rebuild")
            source_id = self._source_id(pdf)
            config_path = root / "config" / "pdf_imports.json"
            config_path.write_text(
                json.dumps(
                    {
                        "documents": [
                            {
                                "source_file_id": source_id,
                                "file_name": pdf.name,
                            },
                            {
                                "source_file_id": source_id,
                                "file_name": "retry.pdf",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "src.me_finder.pdf_import_service.build_index",
                return_value={"source_files": []},
            ) as build:
                rebuild_local_index(root)

            build.assert_called_once()
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["documents"]), 1)


if __name__ == "__main__":
    unittest.main()

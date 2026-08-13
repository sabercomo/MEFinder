from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder.large_document.job_ledger import JobLedger
from src.me_finder.large_document.merge import write_normalized_result
from src.me_finder.large_document.mineru_accounts import MinerUAccountService
from src.me_finder.large_document.slicing import SliceDescriptor
from src.me_finder.parser_provider import (
    NormalizedBlock,
    NormalizedPage,
    NormalizedParseResult,
)
from src.me_finder.pdf_import_service import (
    _publish_mineru_engine_results,
    load_import_config,
    parse_pdf_with_mineru,
)


class MinerUEngineImportBridgeTests(unittest.TestCase):
    def test_validated_slices_are_published_into_existing_indexer_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_id = "pdf-import-bridge"
            source = root / "corpus" / "raw_pdf" / "bridge.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"synthetic source")
            config_path = root / "config" / "pdf_imports.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "documents": [
                            {
                                "source_file_id": source_id,
                                "document_id": "PDF_IMPORT_BRIDGE",
                                "file_name": "bridge.pdf",
                                "enabled": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            ledger = JobLedger(root / "data" / "parser_jobs.sqlite3")
            job = ledger.create_document_job(
                source_file_id=source_id,
                document_id="PDF_IMPORT_BRIDGE",
                source_path=source,
                source_sha256="source-sha",
                provider_id="mineru-cloud",
                parser_model="vlm",
                options_fingerprint="options",
                total_pages=4,
            )
            descriptors = (
                SliceDescriptor(1, 2, 0, source, "slice-1", 16, False),
                SliceDescriptor(3, 4, 2, source, "slice-2", 16, False),
            )
            slices = ledger.add_slices(job.id, descriptors, "mineru-cloud")
            for slice_job, account_id in zip(slices, ("account-1", "account-2")):
                result_path = root / "normalized" / f"{slice_job.id}.ndjson"
                pages = tuple(
                    NormalizedPage(
                        physical_pdf_page=page_number,
                        text=f"page {page_number}",
                        blocks=(
                            NormalizedBlock(
                                text=f"block {page_number}",
                                block_type="text",
                                reading_order=0,
                            ),
                        ),
                    )
                    for page_number in range(
                        slice_job.page_start, slice_job.page_end + 1
                    )
                )
                digest = write_normalized_result(
                    result_path,
                    NormalizedParseResult(
                        provider_id="mineru-cloud", model="vlm", pages=pages
                    ),
                )
                ledger.update_slice(
                    slice_job.id,
                    status="completed",
                    credential_id=account_id,
                    result_path=str(result_path),
                    result_sha256=digest,
                )
            ledger.refresh_progress(job.id)
            ledger.update_document(job.id, status="validated")

            result = _publish_mineru_engine_results(
                root,
                source_id,
                ledger=ledger,
                document_job_id=job.id,
            )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["segments"], 2)
            manifest_path = Path(result["manifest_path"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["total_pages"], 4)
            self.assertEqual(manifest["provider_id"], "mineru-cloud")
            self.assertEqual(
                [item["page_ranges"] for item in manifest["segments"]],
                ["1-2", "3-4"],
            )
            self.assertEqual(
                [item["credential_id"] for item in manifest["segments"]],
                ["account-1", "account-2"],
            )
            self.assertEqual(
                [item["page_index_offset"] for item in manifest["segments"]],
                [0, 2],
            )
            for segment in manifest["segments"]:
                content = json.loads(
                    (
                        Path(segment["result_dir"]) / "content_list.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    [item["page_idx"] for item in content], [0, 1]
                )

            imported = load_import_config(config_path)
            attachment = imported["documents"][0]["mineru"]
            self.assertEqual(attachment["resume"]["completed_page_count"], 4)
            self.assertFalse(Path(str(attachment["manifest"])).is_absolute())

            local_result = _publish_mineru_engine_results(
                root,
                source_id,
                ledger=ledger,
                document_job_id=job.id,
                provider_id="mineru-local",
                provider_name="本地 MinerU",
            )
            local_manifest = json.loads(
                Path(local_result["manifest_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(local_manifest["provider_id"], "mineru-local")
            self.assertEqual(local_manifest["provider_name"], "本地 MinerU")

    def test_multi_account_config_switches_existing_import_to_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "book.pdf"
            source.write_bytes(b"synthetic PDF")
            ledger = JobLedger(root / "data" / "parser_jobs.sqlite3")
            service = MinerUAccountService(
                ledger=ledger,
                config_path=root / "config" / "mineru_accounts.local.json",
            )
            service.save_account(
                account_id="account-1",
                display_name="Account 1",
                token="private-token",
            )
            expected = {"status": "completed", "document_job_id": "job-1"}

            with patch(
                "src.me_finder.pdf_import_service._parse_pdf_with_mineru_accounts",
                return_value=expected,
            ) as engine_import:
                result = parse_pdf_with_mineru(
                    root,
                    source,
                    "source-1",
                    poll_seconds=3,
                    timeout_minutes=9,
                )

            self.assertEqual(result, expected)
            self.assertEqual(engine_import.call_args.kwargs["poll_seconds"], 3)
            self.assertEqual(engine_import.call_args.kwargs["timeout_minutes"], 9)
            self.assertEqual(
                engine_import.call_args.kwargs["account_service"].list_accounts()[
                    0
                ].account_id,
                "account-1",
            )


if __name__ == "__main__":
    unittest.main()

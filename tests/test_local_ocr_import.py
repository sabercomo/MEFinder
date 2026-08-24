from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import fitz

from src.me_finder.local_ocr_settings import save_local_ocr_config
from src.me_finder.pdf_import_service import (
    load_import_config,
    parse_pdf_with_local_ocr,
)


class LocalOCRImportIntegrationTests(unittest.TestCase):
    def test_page_image_runner_publishes_structured_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_id = "pdf-local-ocr"
            pdf = root / "corpus" / "raw_pdf" / "scan.pdf"
            pdf.parent.mkdir(parents=True)
            document = fitz.open()
            page = document.new_page(width=200, height=300)
            page.insert_text((30, 60), "scan")
            document.save(str(pdf))
            document.close()
            config_path = root / "config" / "pdf_imports.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "documents": [
                            {
                                "source_file_id": source_id,
                                "document_id": "PDF_LOCAL_OCR",
                                "file_name": "scan.pdf",
                                "enabled": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            runner = root / "ocr.py"
            runner.write_text(
                """
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--sourceimg', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--json-only', action='store_true')
args = parser.parse_args()
source = Path(args.sourceimg)
output = Path(args.output)
(output / f'{source.stem}.json').write_text(json.dumps({
    'contents': [[{
        'id': 1,
        'text': 'これは識別本文です',
        'boundingBox': [[20, 30], [20, 80], [100, 80], [100, 30]],
        'isVertical': 'false',
        'confidence': 0.9,
    }]]
}), encoding='utf-8')
""".strip()
                + "\n",
                encoding="utf-8",
            )
            save_local_ocr_config(
                {
                    "engines": {
                        "ndlocr-lite": {
                            "enabled": True,
                            "python_path": sys.executable,
                            "script_path": str(runner),
                        },
                        "ndlkotenocr-lite": {
                            "enabled": False,
                            "python_path": "",
                            "script_path": "",
                        },
                    }
                },
                root / "config" / "local_ocr.json",
            )
            progress: list[dict[str, object]] = []

            result = parse_pdf_with_local_ocr(
                root,
                pdf,
                source_id,
                on_progress=progress.append,
            )

            self.assertEqual(result["provider_id"], "ndlocr-lite")
            self.assertEqual(result["selection"]["strategy"], "japanese_script")
            self.assertEqual(progress[-1]["completed"], 1)
            attachment = load_import_config(config_path)["documents"][0][
                "parser_results"
            ]
            self.assertEqual(attachment["parser"], "ndlocr-lite")
            self.assertFalse(Path(attachment["manifest"]).is_absolute())
            manifest = json.loads(
                (root / attachment["manifest"]).read_text(encoding="utf-8")
            )
            content = json.loads(
                (
                    Path(manifest["segments"][0]["result_dir"])
                    / "content_list.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(content[0]["text"], "これは識別本文です")
            self.assertEqual(content[0]["page_idx"], 0)


if __name__ == "__main__":
    unittest.main()

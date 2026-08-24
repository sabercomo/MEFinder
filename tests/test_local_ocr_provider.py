from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.local_ocr_provider import (
    LocalOCRProvider,
    LocalOCRProbeEvidence,
    RenderedOCRPage,
    _text_band_counts,
    choose_local_ocr_engine,
    normalize_ndl_page,
    sample_page_indices,
)
from src.me_finder.local_ocr_settings import LocalOCREngineConfig
from src.me_finder.parser_provider import (
    ParserProviderError,
    ParserRequest,
    ParserTaskStatus,
)


class LocalOCRProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "slice.pdf"
        self.source.write_bytes(b"synthetic PDF slice")
        self.runner = self.root / "ocr.py"
        self.runner.write_text(
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
(output / 'arguments.json').write_text(json.dumps(vars(args)), encoding='utf-8')
(output / f'{source.stem}.json').write_text(json.dumps({
    'contents': [[{
        'id': 7,
        'text': '縦書き',
        'boundingBox': [[20, 10], [20, 90], [40, 90], [40, 10]],
        'isVertical': 'false',
        'confidence': 0.8,
    }]]
}), encoding='utf-8')
""".strip()
            + "\n",
            encoding="utf-8",
        )

    def _engine(self, provider_id: str = "ndlocr-lite") -> LocalOCREngineConfig:
        return LocalOCREngineConfig(
            provider_id=provider_id,
            display_name="NDL test OCR",
            version="test-version",
            python_path=Path(sys.executable),
            script_path=self.runner,
            enabled=True,
        )

    def _request(self, output_dir: Path | None = None) -> ParserRequest:
        return ParserRequest(
            source_path=self.source,
            source_sha256="source-sha",
            document_id="DOC",
            page_start=1,
            page_end=1,
            global_page_offset=4,
            output_dir=output_dir or self.root / "output",
        )

    def _renderer(self, _source, output, _dpi, _indices):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        image = output / "page-000001.png"
        image.write_bytes(b"png")
        return (
            RenderedOCRPage(0, image, 100, 100, 50.0, 100.0, 0.2),
        )

    def test_runner_receives_only_page_image_and_modern_json_flag(self) -> None:
        output = self.root / "job"
        provider = LocalOCRProvider(self._engine(), page_renderer=self._renderer)

        submission = provider.submit(self._request(output))
        normalized = provider.normalize_result(submission.raw_result, self._request(output))

        arguments_path = next(output.glob("run-*/raw/page-*/arguments.json"))
        arguments = json.loads(arguments_path.read_text(encoding="utf-8"))
        self.assertIn("page-000001.png", arguments["sourceimg"])
        self.assertNotIn("sourcepdf", arguments)
        self.assertTrue(arguments["json_only"])
        self.assertEqual(normalized.pages[0].physical_pdf_page, 5)
        self.assertEqual(normalized.pages[0].blocks[0].bbox, (10.0, 10.0, 20.0, 90.0))
        self.assertTrue(normalized.pages[0].blocks[0].provenance["is_vertical"])
        self.assertEqual(normalized.pages[0].blocks[0].provenance["raw_is_vertical"], "false")

    def test_ancient_runner_does_not_receive_unsupported_json_flag(self) -> None:
        output = self.root / "ancient-job"
        provider = LocalOCRProvider(
            self._engine("ndlkotenocr-lite"),
            page_renderer=self._renderer,
        )

        provider.submit(self._request(output))

        arguments = json.loads(
            next(output.glob("run-*/raw/page-*/arguments.json")).read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(arguments["json_only"])

    def test_blank_page_skips_runner_and_preserves_page_coverage(self) -> None:
        def blank_renderer(_source, output, _dpi, _indices):
            output = Path(output)
            output.mkdir(parents=True, exist_ok=True)
            image = output / "page-000001.png"
            image.write_bytes(b"png")
            return (RenderedOCRPage(0, image, 100, 100, 50.0, 100.0, 0.0),)

        provider = LocalOCRProvider(self._engine(), page_renderer=blank_renderer)
        submission = provider.submit(self._request())
        normalized = provider.normalize_result(submission.raw_result, self._request())

        self.assertEqual(normalized.pages[0].text, "")
        self.assertEqual(normalized.pages[0].warnings, ("blank_page",))
        self.assertFalse(list((self.root / "output").glob("run-*/raw/page-*")))

    def test_pre_cancel_returns_synchronous_cancelled_submission(self) -> None:
        provider = LocalOCRProvider(
            self._engine(),
            page_renderer=self._renderer,
            cancel_requested=lambda: True,
        )

        submission = provider.submit(self._request())

        self.assertEqual(submission.status, ParserTaskStatus.CANCELLED)

    def test_cancel_terminates_current_runner_without_waiting_for_exit(self) -> None:
        self.runner.write_text(
            "import time\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        checks = 0

        def cancel_after_start() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 3

        provider = LocalOCRProvider(
            self._engine(),
            page_renderer=self._renderer,
            cancel_requested=cancel_after_start,
        )
        started = time.monotonic()

        submission = provider.submit(self._request())

        self.assertEqual(submission.status, ParserTaskStatus.CANCELLED)
        self.assertLess(time.monotonic() - started, 3)

    def test_timeout_is_retryable_and_terminates_runner(self) -> None:
        self.runner.write_text(
            "import time\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        provider = LocalOCRProvider(
            self._engine(),
            page_renderer=self._renderer,
            timeout_seconds_per_page=0,
        )

        with self.assertRaises(ParserProviderError) as caught:
            provider.submit(self._request())

        self.assertTrue(caught.exception.retryable)
        self.assertIn("timed out", str(caught.exception))

    def test_nonzero_exit_and_invalid_json_fail_explicitly(self) -> None:
        self.runner.write_text("raise SystemExit(3)\n", encoding="utf-8")
        provider = LocalOCRProvider(self._engine(), page_renderer=self._renderer)
        with self.assertRaisesRegex(ParserProviderError, "exited with 3"):
            provider.submit(self._request(self.root / "nonzero"))

        self.runner.write_text(
            """
import argparse
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--sourceimg', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--json-only', action='store_true')
args = parser.parse_args()
source = Path(args.sourceimg)
(Path(args.output) / f'{source.stem}.json').write_text('{broken', encoding='utf-8')
""".strip()
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ParserProviderError, "valid page JSON"):
            provider.submit(self._request(self.root / "invalid-json"))

    def test_normalization_ignores_malformed_lines_and_keeps_geometry(self) -> None:
        request = self._request()
        page = normalize_ndl_page(
            {
                "local_page_index": 0,
                "image_width": 200,
                "image_height": 400,
                "pdf_width": 100,
                "pdf_height": 200,
                "ink_ratio": 0.1,
                "payload": {
                    "contents": [[
                        {"text": "", "boundingBox": [[0, 0]] * 4},
                        {
                            "text": "本文",
                            "boundingBox": [[10, 20], [10, 60], [50, 60], [50, 20]],
                            "isVertical": "true",
                        },
                    ]]
                },
            },
            request=request,
            provider_id="ndlocr-lite",
            version="1.2.3",
        )

        self.assertEqual(page.blocks[0].bbox, (5.0, 10.0, 25.0, 30.0))
        self.assertFalse(page.blocks[0].provenance["is_vertical"])
        self.assertEqual(page.blocks[0].provenance["raw_is_vertical"], "true")
        self.assertEqual(sample_page_indices(10, 3), (0, 4, 9))

    def test_dual_engine_selector_uses_geometry_then_coverage(self) -> None:
        modern = self._engine("ndlocr-lite")
        ancient = self._engine("ndlkotenocr-lite")
        fake_document = mock.MagicMock()
        fake_document.__len__.return_value = 9

        def strong_ancient(provider, *_args, **_kwargs):
            if provider.provider_id == "ndlocr-lite":
                return LocalOCRProbeEvidence(
                    "ndlocr-lite", 10, 100, 8, 3, 20, 1, 20
                )
            return LocalOCRProbeEvidence(
                "ndlkotenocr-lite", 8, 80, 8, 3, 0, 1, 20
            )

        with (
            mock.patch("fitz.open", return_value=fake_document),
            mock.patch.object(
                LocalOCRProvider,
                "probe",
                autospec=True,
                side_effect=strong_ancient,
            ),
        ):
            selected, evidence = choose_local_ocr_engine(
                (modern, ancient),
                pdf_path=self.source,
                work_dir=self.root / "probe",
                render_dpi=200,
                probe_pages=3,
                timeout_seconds_per_page=30,
                blank_ink_ratio=0.001,
            )

        self.assertEqual(selected.provider_id, "ndlkotenocr-lite")
        self.assertEqual(evidence["strategy"], "vertical_geometry")
        self.assertEqual(evidence["sample_pages"], [1, 5, 9])

        def weak_ancient(provider, *_args, **_kwargs):
            if provider.provider_id == "ndlocr-lite":
                return LocalOCRProbeEvidence(
                    "ndlocr-lite", 10, 100, 8, 3, 20, 1, 20
                )
            return LocalOCRProbeEvidence(
                "ndlkotenocr-lite", 2, 10, 2, 3, 0, 1, 20
            )

        with (
            mock.patch("fitz.open", return_value=fake_document),
            mock.patch.object(
                LocalOCRProvider,
                "probe",
                autospec=True,
                side_effect=weak_ancient,
            ),
        ):
            selected, evidence = choose_local_ocr_engine(
                (modern, ancient),
                pdf_path=self.source,
                work_dir=self.root / "probe-weak",
                render_dpi=200,
                probe_pages=3,
                timeout_seconds_per_page=30,
                blank_ink_ratio=0.001,
            )

        self.assertEqual(selected.provider_id, "ndlocr-lite")
        self.assertEqual(evidence["strategy"], "japanese_script")

    def test_single_ancient_engine_rejects_horizontal_modern_book(self) -> None:
        ancient = self._engine("ndlkotenocr-lite")
        fake_document = mock.MagicMock()
        fake_document.__len__.return_value = 184

        with (
            mock.patch("fitz.open", return_value=fake_document),
            mock.patch.object(
                LocalOCRProvider,
                "probe",
                autospec=True,
                return_value=LocalOCRProbeEvidence(
                    "ndlkotenocr-lite", 120, 2400, 4, 3, 0, 20, 1
                ),
            ),
        ):
            with self.assertRaisesRegex(
                ParserProviderError,
                "抽样页不是日文文本，也不是竖排古籍版式",
            ):
                choose_local_ocr_engine(
                    (ancient,),
                    pdf_path=self.source,
                    work_dir=self.root / "probe-horizontal",
                    render_dpi=200,
                    probe_pages=3,
                    timeout_seconds_per_page=30,
                    blank_ink_ratio=0.001,
                )

    def test_single_ancient_engine_accepts_vertical_book(self) -> None:
        ancient = self._engine("ndlkotenocr-lite")
        fake_document = mock.MagicMock()
        fake_document.__len__.return_value = 120

        with (
            mock.patch("fitz.open", return_value=fake_document),
            mock.patch.object(
                LocalOCRProvider,
                "probe",
                autospec=True,
                return_value=LocalOCRProbeEvidence(
                    "ndlkotenocr-lite", 80, 1600, 70, 3, 0, 1, 20
                ),
            ),
        ):
            selected, evidence = choose_local_ocr_engine(
                (ancient,),
                pdf_path=self.source,
                work_dir=self.root / "probe-vertical",
                render_dpi=200,
                probe_pages=3,
                timeout_seconds_per_page=30,
                blank_ink_ratio=0.001,
            )

        self.assertEqual(selected.provider_id, "ndlkotenocr-lite")
        self.assertEqual(evidence["strategy"], "vertical_geometry")

    def test_single_modern_engine_rejects_horizontal_non_japanese_book(self) -> None:
        modern = self._engine("ndlocr-lite")
        fake_document = mock.MagicMock()
        fake_document.__len__.return_value = 184

        with (
            mock.patch("fitz.open", return_value=fake_document),
            mock.patch.object(
                LocalOCRProvider,
                "probe",
                autospec=True,
                return_value=LocalOCRProbeEvidence(
                    "ndlocr-lite", 120, 2400, 4, 3, 0, 20, 1
                ),
            ),
        ):
            with self.assertRaisesRegex(
                ParserProviderError,
                "抽样页不是日文文本，也不是竖排古籍版式",
            ):
                choose_local_ocr_engine(
                    (modern,),
                    pdf_path=self.source,
                    work_dir=self.root / "probe-non-japanese",
                    render_dpi=200,
                    probe_pages=3,
                    timeout_seconds_per_page=30,
                    blank_ink_ratio=0.001,
                )

    def test_text_band_counts_distinguish_horizontal_and_vertical_lines(self) -> None:
        width = 30
        height = 30
        horizontal = bytearray([255] * (width * height))
        vertical = bytearray([255] * (width * height))
        for anchor in (5, 15, 25):
            for offset in (0, 1):
                for position in range(2, 28):
                    horizontal[(anchor + offset) * width + position] = 0
                    vertical[position * width + anchor + offset] = 0

        self.assertEqual(
            _text_band_counts(bytes(horizontal), width, height, 1),
            (3, 1),
        )
        self.assertEqual(
            _text_band_counts(bytes(vertical), width, height, 1),
            (1, 3),
        )


if __name__ == "__main__":
    unittest.main()

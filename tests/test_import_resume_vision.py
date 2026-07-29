from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.me_finder.vision_api import (
    DEFAULT_VISION_MANIFEST_DIR,
    VisionAPIError,
    parse_pdf_with_vision_provider,
    save_vision_provider,
)


class _FakePixmap:
    def __init__(self, page_idx: int) -> None:
        self.page_idx = page_idx

    def tobytes(self, output: str) -> bytes:
        if output != "png":
            raise AssertionError(output)
        return f"page-{self.page_idx}".encode("ascii")


class _FakeRect:
    width = 600
    height = 900


class _FakePage:
    rect = _FakeRect()

    def __init__(self, page_idx: int) -> None:
        self.page_idx = page_idx

    def get_pixmap(self, **_kwargs):
        return _FakePixmap(self.page_idx)


class _FakeDocument:
    def __init__(self, page_count: int) -> None:
        self.page_count = page_count
        self.closed = False

    def __len__(self) -> int:
        return self.page_count

    def load_page(self, page_idx: int):
        return _FakePage(page_idx)

    def close(self) -> None:
        self.closed = True


class _FakeFitz:
    def __init__(self, page_count: int) -> None:
        self.page_count = page_count

    @staticmethod
    def Matrix(x: float, y: float):
        return (x, y)

    def open(self, _path: str):
        return _FakeDocument(self.page_count)


def _page_idx(image_bytes: bytes) -> int:
    return int(image_bytes.decode("ascii").split("-", 1)[1])


class VisionImportResumeTests(unittest.TestCase):
    def _fixture(
        self,
        root: Path,
        *,
        model: str = "vision-model",
        provider_id: str = "",
    ) -> tuple[Path, Path, str]:
        pdf_path = root / "corpus" / "raw_pdf" / "sample.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        if not pdf_path.exists():
            pdf_path.write_bytes(b"%PDF-resume-test")
        config_path = root / "config" / "vision_api.local.json"
        summary = save_vision_provider(
            {
                "id": provider_id,
                "name": "测试视觉接口",
                "api_base": "https://example.test/v1",
                "model": model,
                "api_key": "secret",
                "enabled": True,
            },
            config_path,
        )
        selected = (
            next(
                item
                for item in summary["providers"]
                if item["id"] == provider_id
            )
            if provider_id
            else summary["providers"][-1]
        )
        return pdf_path, config_path, str(selected["id"])

    def _parse(
        self,
        root: Path,
        pdf_path: Path,
        config_path: Path,
        provider_id: str,
        page_count: int,
        side_effect,
        on_progress=None,
    ):
        with patch(
            "src.me_finder.vision_api.load_pymupdf",
            return_value=_FakeFitz(page_count),
        ), patch(
            "src.me_finder.vision_api.OpenAICompatibleVisionClient.extract_page",
            side_effect=side_effect,
        ) as extract:
            result = parse_pdf_with_vision_provider(
                root,
                pdf_path,
                "pdf-import-resume-test",
                provider_id,
                config_path=config_path,
                on_progress=on_progress,
            )
        return result, extract

    def test_failed_run_keeps_published_manifest_and_retries_only_failed_page(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path, config_path, provider_id = self._fixture(root)
            final_manifest = (
                root
                / DEFAULT_VISION_MANIFEST_DIR
                / "vision-pdf-import-resume-test.json"
            )
            final_manifest.parent.mkdir(parents=True, exist_ok=True)
            final_manifest.write_text('{"published": "old"}\n', encoding="utf-8")
            first_calls: list[int] = []

            def first_attempt(image_bytes: bytes) -> str:
                page_idx = _page_idx(image_bytes)
                first_calls.append(page_idx)
                if page_idx == 1:
                    raise VisionAPIError("第二页暂时失败")
                return f"first-{page_idx}"

            with self.assertRaisesRegex(VisionAPIError, "1 个失败页"):
                self._parse(
                    root,
                    pdf_path,
                    config_path,
                    provider_id,
                    3,
                    first_attempt,
                )
            self.assertEqual(first_calls, [0, 1, 2])
            self.assertEqual(
                json.loads(final_manifest.read_text(encoding="utf-8")),
                {"published": "old"},
            )
            work_manifest = next(
                (root / DEFAULT_VISION_MANIFEST_DIR / "work").glob("*.json")
            )
            failed = json.loads(work_manifest.read_text(encoding="utf-8"))
            self.assertEqual(failed["completed_pages"], [1, 3])
            self.assertEqual(
                [item["page"] for item in failed["failed_pages"]],
                [2],
            )

            retry_calls: list[int] = []

            def retry(image_bytes: bytes) -> str:
                page_idx = _page_idx(image_bytes)
                retry_calls.append(page_idx)
                return "second-page-recovered"

            result, _extract = self._parse(
                root,
                pdf_path,
                config_path,
                provider_id,
                3,
                retry,
            )
            self.assertEqual(retry_calls, [1])
            published = json.loads(
                Path(str(result["manifest_path"])).read_text(encoding="utf-8")
            )
            result_dir = root / published["segments"][0]["result_dir"]
            content = json.loads(
                (result_dir / "content_list.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["text"] for item in content],
                ["first-0", "second-page-recovered", "first-2"],
            )
            self.assertEqual(result["resume"]["completed_pages"], [1, 2, 3])

    def test_keyboard_interrupt_reuses_pages_already_checkpointed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path, config_path, provider_id = self._fixture(root)
            first_calls: list[int] = []

            def interrupted(image_bytes: bytes) -> str:
                page_idx = _page_idx(image_bytes)
                first_calls.append(page_idx)
                if page_idx == 1:
                    raise KeyboardInterrupt
                return f"before-crash-{page_idx}"

            with self.assertRaises(KeyboardInterrupt):
                self._parse(
                    root,
                    pdf_path,
                    config_path,
                    provider_id,
                    3,
                    interrupted,
                )
            self.assertEqual(first_calls, [0, 1])

            retry_calls: list[int] = []

            def resumed(image_bytes: bytes) -> str:
                page_idx = _page_idx(image_bytes)
                retry_calls.append(page_idx)
                return f"after-crash-{page_idx}"

            result, _extract = self._parse(
                root,
                pdf_path,
                config_path,
                provider_id,
                3,
                resumed,
            )
            self.assertEqual(retry_calls, [1, 2])
            self.assertEqual(result["resume"]["completed_page_count"], 3)

    def test_checkpoint_scan_repairs_a_lagging_manifest_without_api_calls(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path, config_path, provider_id = self._fixture(root)
            result, _extract = self._parse(
                root,
                pdf_path,
                config_path,
                provider_id,
                2,
                lambda image: f"text-{_page_idx(image)}",
            )
            work_path = Path(str(result["work_manifest_path"]))
            work = json.loads(work_path.read_text(encoding="utf-8"))
            work["pages"][0]["status"] = "running"
            work["completed_pages"] = [2]
            work["completed_page_count"] = 1
            work_path.write_text(
                json.dumps(work, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            Path(str(result["manifest_path"])).unlink()

            repaired, extract = self._parse(
                root,
                pdf_path,
                config_path,
                provider_id,
                2,
                lambda _image: "should-not-run",
            )
            self.assertEqual(extract.call_count, 0)
            self.assertEqual(repaired["resume"]["completed_pages"], [1, 2])
            self.assertTrue(Path(str(repaired["manifest_path"])).is_file())

    def test_empty_page_is_complete_and_completed_rerun_makes_zero_calls(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path, config_path, provider_id = self._fixture(root)
            progress: list[dict[str, object]] = []
            result, _extract = self._parse(
                root,
                pdf_path,
                config_path,
                provider_id,
                2,
                lambda image: "" if _page_idx(image) == 0 else "第二页",
                on_progress=progress.append,
            )
            self.assertTrue(progress)
            self.assertTrue(all("resume" in update for update in progress))
            self.assertEqual(progress[-1]["resume"]["completed_pages"], [1, 2])
            published = json.loads(
                Path(str(result["manifest_path"])).read_text(encoding="utf-8")
            )
            content = json.loads(
                (
                    root
                    / published["segments"][0]["result_dir"]
                    / "content_list.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual([item["text"] for item in content], ["", "第二页"])

            rerun, extract = self._parse(
                root,
                pdf_path,
                config_path,
                provider_id,
                2,
                lambda _image: "should-not-run",
            )
            self.assertEqual(extract.call_count, 0)
            self.assertEqual(rerun["resume"]["completed_page_count"], 2)

    def test_file_provider_and_model_changes_do_not_reuse_old_pages(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path, config_path, first_provider = self._fixture(root)
            self._parse(
                root,
                pdf_path,
                config_path,
                first_provider,
                1,
                lambda _image: "first",
            )

            _pdf, _config, first_provider = self._fixture(
                root,
                model="vision-model-v2",
                provider_id=first_provider,
            )
            _result, model_extract = self._parse(
                root,
                pdf_path,
                config_path,
                first_provider,
                1,
                lambda _image: "new-model",
            )
            self.assertEqual(model_extract.call_count, 1)

            _pdf, _config, second_provider = self._fixture(
                root,
                model="other-model",
            )
            _result, provider_extract = self._parse(
                root,
                pdf_path,
                config_path,
                second_provider,
                1,
                lambda _image: "new-provider",
            )
            self.assertEqual(provider_extract.call_count, 1)

            pdf_path.write_bytes(b"%PDF-resume-test-modified")
            _result, file_extract = self._parse(
                root,
                pdf_path,
                config_path,
                second_provider,
                1,
                lambda _image: "new-file",
            )
            self.assertEqual(file_extract.call_count, 1)

    def test_document_wide_api_failure_stops_further_paid_requests(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path, config_path, provider_id = self._fixture(root)
            calls: list[int] = []

            def quota_failure(image_bytes: bytes) -> str:
                calls.append(_page_idx(image_bytes))
                raise VisionAPIError(
                    "账号额度不足",
                    stop_document=True,
                )

            with self.assertRaisesRegex(VisionAPIError, "已停止继续请求"):
                self._parse(
                    root,
                    pdf_path,
                    config_path,
                    provider_id,
                    5,
                    quota_failure,
                )
            self.assertEqual(calls, [0])
            work_manifest = next(
                (root / DEFAULT_VISION_MANIFEST_DIR / "work").glob("*.json")
            )
            work = json.loads(work_manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["status"] for item in work["pages"]],
                ["failed", "pending", "pending", "pending", "pending"],
            )

    def test_semantically_damaged_work_manifest_is_quarantined(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path, config_path, provider_id = self._fixture(root)
            result, _extract = self._parse(
                root,
                pdf_path,
                config_path,
                provider_id,
                1,
                lambda _image: "kept checkpoint",
            )
            work_path = Path(str(result["work_manifest_path"]))
            work = json.loads(work_path.read_text(encoding="utf-8"))
            work["pages"] = 7
            work_path.write_text(
                json.dumps(work, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            Path(str(result["manifest_path"])).unlink()

            repaired, extract = self._parse(
                root,
                pdf_path,
                config_path,
                provider_id,
                1,
                lambda _image: "should-not-run",
            )
            self.assertEqual(extract.call_count, 0)
            self.assertEqual(repaired["resume"]["completed_pages"], [1])
            self.assertTrue(
                list(work_path.parent.glob(work_path.name + ".corrupt-*"))
            )


if __name__ == "__main__":
    unittest.main()

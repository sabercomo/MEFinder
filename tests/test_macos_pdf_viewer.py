from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder import macos_pdf_viewer


class MacOSPDFViewerTests(unittest.TestCase):
    def test_page_number_normalization_is_one_based(self) -> None:
        self.assertEqual(macos_pdf_viewer.normalize_page_number("12"), 12)
        self.assertEqual(macos_pdf_viewer.normalize_page_number(1), 1)
        for invalid in (None, "", 0, -1, "page 3", object()):
            self.assertIsNone(macos_pdf_viewer.normalize_page_number(invalid))

    def test_open_only_schedules_ui_work_on_the_main_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "中文 文献.pdf"
            target.write_bytes(b"%PDF-1.4\n%%EOF\n")
            viewer = macos_pdf_viewer.MacOSPDFViewer()
            scheduled = []

            def run_scheduled(callback, *args):
                scheduled.append((callback, args))
                callback(*args)

            with (
                mock.patch.object(macos_pdf_viewer.sys, "platform", "darwin"),
                mock.patch.object(macos_pdf_viewer, "_is_main_thread", return_value=False),
                mock.patch.object(
                    viewer,
                    "_open_on_main",
                    return_value={"page": 7, "page_count": 20},
                ) as open_on_main,
                mock.patch.object(
                    macos_pdf_viewer,
                    "_dispatch_to_main",
                    side_effect=run_scheduled,
                ) as dispatch,
            ):
                result = viewer.open(target, "7")

        dispatch.assert_called_once()
        callback, args = scheduled[0]
        self.assertEqual(callback, viewer._open_with_result_on_main)
        path, page = args[:2]
        self.assertEqual(path, str(target.resolve()))
        self.assertEqual(page, 7)
        open_on_main.assert_called_once_with(str(target.resolve()), 7)
        self.assertEqual(result, {"page": 7, "page_count": 20})

    def test_open_rejects_non_pdf_and_non_macos_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "source.txt"
            target.write_text("not a pdf", encoding="utf-8")
            viewer = macos_pdf_viewer.MacOSPDFViewer()
            with mock.patch.object(macos_pdf_viewer.sys, "platform", "darwin"):
                with self.assertRaises(ValueError):
                    viewer.open(target)
            with mock.patch.object(macos_pdf_viewer.sys, "platform", "win32"):
                with self.assertRaises(RuntimeError):
                    viewer.open(target)

    def test_main_thread_pdfkit_failure_is_returned_to_the_request_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "damaged.pdf"
            target.write_bytes(b"not a pdf")
            viewer = macos_pdf_viewer.MacOSPDFViewer()

            def run_scheduled(callback, *args):
                callback(*args)

            with (
                mock.patch.object(macos_pdf_viewer.sys, "platform", "darwin"),
                mock.patch.object(macos_pdf_viewer, "_is_main_thread", return_value=False),
                mock.patch.object(
                    viewer,
                    "_open_on_main",
                    side_effect=ValueError("文件已经损坏"),
                ),
                mock.patch.object(
                    macos_pdf_viewer,
                    "_dispatch_to_main",
                    side_effect=run_scheduled,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "文件已经损坏"):
                    viewer.open(target, 2)

    def test_timed_out_open_closes_late_window_but_keeps_controller_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "slow.pdf"
            target.write_bytes(b"%PDF-1.4\n%%EOF\n")
            controller = mock.Mock()
            controller.open_document.side_effect = [
                {"page": 2, "page_count": 10},
                {"page": 3, "page_count": 10},
            ]
            viewer = macos_pdf_viewer.MacOSPDFViewer()
            viewer._controller = controller
            scheduled = []

            with (
                mock.patch.object(macos_pdf_viewer.sys, "platform", "darwin"),
                mock.patch.object(macos_pdf_viewer, "_is_main_thread", return_value=False),
                mock.patch.object(
                    macos_pdf_viewer,
                    "MAIN_THREAD_OPEN_TIMEOUT_SECONDS",
                    0.001,
                ),
                mock.patch.object(
                    macos_pdf_viewer,
                    "_dispatch_to_main",
                    side_effect=lambda callback, *args: scheduled.append((callback, args)),
                ),
            ):
                with self.assertRaises(TimeoutError):
                    viewer.open(target, 2)

            callback, args = scheduled[0]
            callback(*args)
            controller.close_window.assert_called_once_with()
            self.assertIs(viewer._controller, controller)
            self.assertEqual(
                viewer._open_on_main(str(target), 3),
                {"page": 3, "page_count": 10},
            )

    def test_controller_is_retained_and_reused(self) -> None:
        controller = mock.Mock()
        controller.open_document.side_effect = [
            {"page": 4, "page_count": 100},
            {"page": 9, "page_count": 100},
        ]
        viewer = macos_pdf_viewer.MacOSPDFViewer()
        with mock.patch.object(
            macos_pdf_viewer,
            "_make_pdf_viewer_controller",
            return_value=controller,
        ) as factory:
            viewer._open_on_main("/tmp/one.pdf", 4)
            viewer._open_on_main("/tmp/two.pdf", 9)

        factory.assert_called_once_with()
        self.assertIs(viewer._controller, controller)
        self.assertEqual(
            controller.open_document.call_args_list,
            [
                mock.call("/tmp/one.pdf", 4),
                mock.call("/tmp/two.pdf", 9),
            ],
        )

    def test_close_on_main_closes_and_releases_controller(self) -> None:
        controller = mock.Mock()
        viewer = macos_pdf_viewer.MacOSPDFViewer()
        viewer._controller = controller
        with (
            mock.patch.object(macos_pdf_viewer.sys, "platform", "darwin"),
            mock.patch.object(macos_pdf_viewer, "_is_main_thread", return_value=True),
        ):
            viewer.close()

        controller.close_window.assert_called_once_with()
        self.assertTrue(viewer._closing)
        self.assertIsNone(viewer._controller)

    def test_packaging_collects_pdfkit_bridge_and_main_thread_helper(self) -> None:
        spec = Path("desktop_macos.spec").read_text(encoding="utf-8")
        build_script = Path("build_macos.sh").read_text(encoding="utf-8")
        self.assertIn('collect_submodules("Quartz.PDFKit")', spec)
        self.assertIn('"src.me_finder.macos_pdf_viewer"', spec)
        self.assertIn('"PyObjCTools.AppHelper"', spec)
        self.assertIn(
            "from Quartz.PDFKit import PDFDocument, PDFView",
            build_script,
        )
        self.assertIn("Quartz/PDFKit/_PDFKit*.so", build_script)

    @unittest.skipUnless(
        importlib.util.find_spec("Quartz") is not None,
        "PyObjC is only installed in the macOS build environment",
    )
    def test_pdfkit_controller_class_can_be_constructed(self) -> None:
        controller = macos_pdf_viewer._make_pdf_viewer_controller()
        self.assertIsNotNone(controller)
        self.assertIsNone(controller.window)


if __name__ == "__main__":
    unittest.main()

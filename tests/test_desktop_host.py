"""Desktop host: one native capability entry, no platform in business pages."""

from __future__ import annotations

import ast
import unittest
from unittest import mock

from src.me_finder.desktop_host import PywebviewDesktopHost


class FakeFileDialog:
    FOLDER = "folder-dialog"
    OPEN = "open-dialog"


class FakeWebViewErrors:
    class WebViewException(Exception):
        pass


class FakeWebView:
    FileDialog = FakeFileDialog
    errors = FakeWebViewErrors


class FakeWindow:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.result = None
        self.raise_webview_error = False

    def create_file_dialog(self, dialog_kind, **kwargs):
        self.calls.append((dialog_kind, kwargs))
        if self.raise_webview_error:
            raise FakeWebViewErrors.WebViewException("对话框崩溃")
        return self.result


class PywebviewDesktopHostTests(unittest.TestCase):
    def build_host(self, **kwargs) -> tuple[PywebviewDesktopHost, FakeWindow]:
        window = FakeWindow()
        host = PywebviewDesktopHost(
            window, FakeWebView(), app_data_root=kwargs.pop("app_data_root", None), **kwargs
        )
        return host, window

    def test_scan_picker_uses_documents_and_multiple_selection(self) -> None:
        host, window = self.build_host()
        window.result = ["/tmp/文献", "/tmp/books"]
        folders = host.choose_scan_directories()
        self.assertEqual(folders, ["/tmp/文献", "/tmp/books"])
        kind, options = window.calls[0]
        self.assertEqual(kind, FakeFileDialog.FOLDER)
        self.assertTrue(options["allow_multiple"])
        self.assertIn("Documents", options["directory"])

    def test_data_directory_picker_requires_installed_app_root(self) -> None:
        host, window = self.build_host()
        self.assertIsNone(host.choose_data_directory())
        self.assertEqual(window.calls, [])

        host, window = self.build_host(app_data_root="/tmp/MEFinder/data")
        window.result = "/tmp/MEFinder"
        self.assertEqual(host.choose_data_directory(), "/tmp/MEFinder")
        kind, options = window.calls[0]
        self.assertEqual(kind, FakeFileDialog.FOLDER)
        self.assertFalse(options["allow_multiple"])

    def test_backup_picker_requests_single_zip_and_maps_dialog_errors(self) -> None:
        host, window = self.build_host()
        window.result = "/tmp/backup.zip"
        self.assertEqual(host.choose_backup_file(), "/tmp/backup.zip")
        kind, options = window.calls[0]
        self.assertEqual(kind, FakeFileDialog.OPEN)
        self.assertFalse(options["allow_multiple"])
        self.assertEqual(options["file_types"], ("MEFinder 备份 (*.zip)",))

        window.raise_webview_error = True
        with self.assertRaisesRegex(RuntimeError, "对话框崩溃"):
            host.choose_backup_file()

    def test_export_picker_maps_dialog_errors(self) -> None:
        host, window = self.build_host()
        window.result = "/tmp/exports"
        self.assertEqual(host.choose_export_directory(), "/tmp/exports")
        window.raise_webview_error = True
        with self.assertRaisesRegex(RuntimeError, "对话框崩溃"):
            host.choose_export_directory()

    def test_capabilities_reflect_installed_native_surfaces(self) -> None:
        host, _window = self.build_host()
        capabilities = host.capabilities()
        self.assertIsNone(capabilities.pdf_opener)
        self.assertIsNone(capabilities.theme_setter)
        self.assertIsNone(capabilities.directory_chooser)
        self.assertIsNotNone(capabilities.scan_directory_chooser)
        self.assertIsNotNone(capabilities.backup_file_chooser)
        self.assertIsNotNone(capabilities.export_directory_chooser)

        class FakeViewer:
            def open(self, *args):
                return {"opened": args}

        host, _window = self.build_host(
            pdf_viewer=FakeViewer(), native_theme_setter=lambda theme: None,
            app_data_root="/tmp/MEFinder/data",
        )
        capabilities = host.capabilities()
        self.assertIsNotNone(capabilities.pdf_opener)
        self.assertIsNotNone(capabilities.theme_setter)
        self.assertIsNotNone(capabilities.directory_chooser)
        self.assertEqual(
            capabilities.pdf_opener("/tmp/book.pdf", 3), {"opened": ("/tmp/book.pdf", 3)}
        )

    def test_theme_setter_is_a_safe_noop_without_native_surface(self) -> None:
        host, _window = self.build_host()
        host.set_native_theme("midnight")


class BusinessPagesStayPlatformFreeTests(unittest.TestCase):
    """The web business layer must not branch on the platform."""

    BUSINESS_MODULES = (
        "web.py",
        "web_http.py",
        "web_runtime.py",
        "http_routes.py",
        "application/search_service.py",
        "application/script_search.py",
        "document_export_service.py",
        "markdown_export.py",
        "epub_export.py",
    )

    def test_business_modules_never_reference_sys_platform(self) -> None:
        from pathlib import Path

        package = Path("src/me_finder")
        for relative in self.BUSINESS_MODULES:
            tree = ast.parse((package / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "platform":
                    self.fail(f"{relative} references sys.platform")
                if (
                    isinstance(node, ast.Name)
                    and node.id == "platform"
                    and isinstance(node.ctx, ast.Load)
                ):
                    self.fail(f"{relative} imports or uses the platform module")


if __name__ == "__main__":
    unittest.main()

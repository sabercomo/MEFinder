from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.archive_transfer_controller import (
    ArchiveTransferController,
)
from src.me_finder.document_export import DocumentExportError
from src.me_finder.mineru_api import MinerUError


class FakeBackup:
    def __init__(self) -> None:
        self.export_result = {
            "ok": True,
            "path": "/data/backups/library.zip",
            "size_bytes": 12,
        }
        self.export_error: Exception | None = None
        self.export_output_dirs: list[Path | None] = []
        self.restore_error: Exception | None = None
        self.restore_paths: list[str] = []

    def export(self, *, output_dir=None):
        self.export_output_dirs.append(output_dir)
        if self.export_error is not None:
            raise self.export_error
        return dict(self.export_result)

    def start_restore(self, source_path):
        self.restore_paths.append(source_path)
        if self.restore_error is not None:
            raise self.restore_error
        return "restore-123"


class ArchiveTransferControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backup = FakeBackup()
        self.export_calls: list[dict[str, object]] = []

        def export_document(**kwargs):
            self.export_calls.append(dict(kwargs))
            return {
                "ok": True,
                "path": "/data/exports/book.mefinder.zip",
                "source_file_id": kwargs["source_file_id"],
            }

        self.markdown_calls: list[dict[str, object]] = []

        def export_markdown(**kwargs):
            self.markdown_calls.append(dict(kwargs))
            return {
                "ok": True,
                "path": "/data/exports/book.md",
                "source_file_id": kwargs["source_file_id"],
            }

        self.controller = ArchiveTransferController(
            self.backup,
            database_path=Path("/runtime/data/index.sqlite3"),
            runtime_root=Path("/runtime"),
            document_output_dir=Path("/app-data/exports"),
            export_document=export_document,
            export_document_markdown=export_markdown,
        )

    def test_success_responses_preserve_archive_arguments(self) -> None:
        self.assertEqual(
            self.controller.export_backup(None),
            (200, self.backup.export_result),
        )
        self.assertEqual(
            self.controller.export_document({"source_id": " pdf-one "}),
            (
                200,
                {
                    "ok": True,
                    "path": "/data/exports/book.mefinder.zip",
                    "source_file_id": " pdf-one ",
                },
            ),
        )
        self.assertEqual(
            self.export_calls,
            [
                {
                    "database_path": Path("/runtime/data/index.sqlite3"),
                    "runtime_root": Path("/runtime"),
                    "source_file_id": " pdf-one ",
                    "output_dir": Path("/app-data/exports"),
                    "include_source_pdf": False,
                }
            ],
        )

        self.controller.export_document(
            {"source_id": "pdf-two", "include_source_pdf": True}
        )
        self.assertTrue(self.export_calls[-1]["include_source_pdf"])
        self.assertEqual(
            self.controller.restore_backup({"path": " /tmp/backup.zip "}),
            (200, {"ok": True, "job_id": "restore-123"}),
        )
        self.assertEqual(self.backup.restore_paths, ["/tmp/backup.zip"])

    def test_selected_output_directory_is_forwarded_to_both_exporters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)

            self.assertEqual(
                self.controller.export_backup({"output_dir": str(output_dir)}),
                (200, self.backup.export_result),
            )
            self.controller.export_document(
                {"source_id": "pdf-one", "output_dir": str(output_dir)}
            )

        self.assertEqual(self.backup.export_output_dirs[-1], output_dir)
        self.assertEqual(self.export_calls[-1]["output_dir"], output_dir)

    def test_markdown_export_passes_arguments_and_uses_default_directory(self) -> None:
        self.assertEqual(
            self.controller.export_document_markdown({"source_id": " pdf-one "}),
            (
                200,
                {
                    "ok": True,
                    "path": "/data/exports/book.md",
                    "source_file_id": " pdf-one ",
                },
            ),
        )
        self.assertEqual(
            self.markdown_calls,
            [
                {
                    "database_path": Path("/runtime/data/index.sqlite3"),
                    "source_file_id": " pdf-one ",
                    "output_dir": Path("/app-data/exports"),
                }
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self.controller.export_document_markdown(
                {"source_id": "pdf-two", "output_dir": str(output_dir)}
            )
        self.assertEqual(self.markdown_calls[-1]["output_dir"], output_dir)

    def test_markdown_export_invalid_payload_fails_before_service(self) -> None:
        self.assertEqual(
            self.controller.export_document_markdown(None),
            (400, {"error": "单书 Markdown 导出请求必须是 JSON 对象。"}),
        )
        self.assertEqual(self.markdown_calls, [])

    def test_invalid_payloads_fail_before_archive_services(self) -> None:
        for payload in (None, [], "pdf-one"):
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.controller.export_document(payload),
                    (400, {"error": "单书导出请求必须是 JSON 对象。"}),
                )
        self.assertEqual(
            self.controller.export_document(
                {"source_id": "pdf-one", "include_source_pdf": "yes"}
            ),
            (400, {"error": "文档包原 PDF 选项必须是布尔值。"}),
        )
        for payload in (
            {"source_id": "pdf-one", "output_dir": ""},
            {"source_id": "pdf-one", "output_dir": "relative"},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.controller.export_document(payload)[0], 400)
        for payload in (None, [], {}, {"path": "  "}):
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.controller.restore_backup(payload),
                    (400, {"error": "请填写备份文件路径。"}),
                )
        self.assertEqual(self.export_calls, [])
        self.assertEqual(self.backup.restore_paths, [])

    def test_expected_archive_errors_keep_public_statuses(self) -> None:
        self.backup.export_error = ValueError("invalid root")
        self.assertEqual(
            self.controller.export_backup({}),
            (500, {"error": "导出备份失败：invalid root"}),
        )
        self.backup.restore_error = MinerUError("备份文件不存在。")
        self.assertEqual(
            self.controller.restore_backup({"path": "/tmp/missing.zip"}),
            (400, {"error": "备份文件不存在。"}),
        )

        def invalid_document(**_kwargs):
            raise DocumentExportError("这份文献不支持导出。")

        controller = ArchiveTransferController(
            self.backup,
            database_path=Path("/runtime/index.sqlite3"),
            runtime_root=Path("/runtime"),
            document_output_dir=Path("/exports"),
            export_document=invalid_document,
        )
        self.assertEqual(
            controller.export_document({"source_id": "word-one"}),
            (400, {"error": "这份文献不支持导出。"}),
        )

    def test_filesystem_and_database_errors_are_safe(self) -> None:
        self.backup.restore_error = OSError("permission denied")
        self.assertEqual(
            self.controller.restore_backup({"path": "/tmp/backup.zip"}),
            (500, {"error": "读取备份失败：permission denied"}),
        )

        for error in (OSError("disk full"), sqlite3.OperationalError("locked")):
            def failed_export(**_kwargs):
                raise error

            controller = ArchiveTransferController(
                self.backup,
                database_path=Path("/runtime/index.sqlite3"),
                runtime_root=Path("/runtime"),
                document_output_dir=Path("/exports"),
                export_document=failed_export,
            )
            with self.subTest(error=error), self.assertLogs(level="ERROR"):
                response = controller.export_document({"source_id": "pdf-one"})
            self.assertEqual(
                response,
                (
                    500,
                    {
                        "error": (
                            "单书导出失败，请检查应用数据目录"
                            "和可用磁盘空间。"
                        )
                    },
                ),
            )


if __name__ == "__main__":
    unittest.main()

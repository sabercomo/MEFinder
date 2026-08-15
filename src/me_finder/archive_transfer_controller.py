"""Transport-neutral JSON responses for backup and document archives."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Callable, Dict, Mapping, Protocol, Tuple

from .document_export import DocumentExportError
from .document_export_service import export_indexed_pdf
from .mineru_api import MinerUError


ArchiveTransferResponse = Tuple[int, Dict[str, object]]
DocumentExporter = Callable[..., Dict[str, object]]


class BackupTransferPort(Protocol):
    def export(self, *, output_dir: Path | None = None) -> Dict[str, object]:
        ...

    def start_restore(self, source_path: str) -> str:
        ...


class ArchiveTransferController:
    """Validate archive commands while leaving file framing to HTTP."""

    def __init__(
        self,
        backup: BackupTransferPort,
        *,
        database_path: Path,
        runtime_root: Path,
        document_output_dir: Path,
        export_document: DocumentExporter = export_indexed_pdf,
    ) -> None:
        self._backup = backup
        self._database_path = Path(database_path)
        self._runtime_root = Path(runtime_root)
        self._document_output_dir = Path(document_output_dir)
        self._export_document = export_document

    def export_backup(self, payload: object) -> ArchiveTransferResponse:
        try:
            output_dir = _requested_output_directory(payload)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        try:
            return 200, self._backup.export(output_dir=output_dir)
        except (OSError, ValueError) as exc:
            return 500, {"error": f"导出备份失败：{exc}"}

    def export_document(self, payload: object) -> ArchiveTransferResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "单书导出请求必须是 JSON 对象。"}
        include_source_pdf = payload.get("include_source_pdf", False)
        if not isinstance(include_source_pdf, bool):
            return 400, {"error": "文档包原 PDF 选项必须是布尔值。"}
        try:
            output_dir = (
                _requested_output_directory(payload) or self._document_output_dir
            )
        except ValueError as exc:
            return 400, {"error": str(exc)}
        try:
            result = self._export_document(
                database_path=self._database_path,
                runtime_root=self._runtime_root,
                source_file_id=str(payload.get("source_id") or ""),
                output_dir=output_dir,
                include_source_pdf=include_source_pdf,
            )
        except DocumentExportError as exc:
            return 400, {"error": str(exc)}
        except (OSError, sqlite3.Error):
            logging.exception("single-document export failed")
            return 500, {
                "error": "单书导出失败，请检查应用数据目录和可用磁盘空间。"
            }
        return 200, result

    def restore_backup(self, payload: object) -> ArchiveTransferResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "请填写备份文件路径。"}
        source_path = str(payload.get("path") or "").strip()
        if not source_path:
            return 400, {"error": "请填写备份文件路径。"}
        try:
            job_id = self._backup.start_restore(source_path)
        except (MinerUError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except OSError as exc:
            return 500, {"error": f"读取备份失败：{exc}"}
        return 200, {"ok": True, "job_id": job_id}


def _requested_output_directory(payload: object) -> Path | None:
    if not isinstance(payload, Mapping) or "output_dir" not in payload:
        return None
    value = payload.get("output_dir")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请选择导出文件夹。")
    path = Path(value.strip())
    if not path.is_absolute():
        raise ValueError("导出文件夹必须是绝对路径。")
    if not path.is_dir():
        raise ValueError("所选导出文件夹不存在。")
    return path

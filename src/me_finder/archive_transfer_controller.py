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
    def export(self) -> Dict[str, object]:
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

    def export_backup(self, _payload: object) -> ArchiveTransferResponse:
        try:
            return 200, self._backup.export()
        except (OSError, ValueError) as exc:
            return 500, {"error": f"导出备份失败：{exc}"}

    def export_document(self, payload: object) -> ArchiveTransferResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "单书导出请求必须是 JSON 对象。"}
        try:
            result = self._export_document(
                database_path=self._database_path,
                runtime_root=self._runtime_root,
                source_file_id=str(payload.get("source_id") or ""),
                output_dir=self._document_output_dir,
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

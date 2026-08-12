"""Bounded-memory staging for browser uploads.

WebView2 may buffer a ``File`` request body before the local HTTP server sees
it.  This store keeps every browser request small and assembles the document
in a private staging directory before the normal import pipeline takes over.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Dict, Mapping


MAX_UPLOAD_BYTES = 600 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024
MAX_CHUNK_BYTES = 8 * 1024 * 1024
STALE_UPLOAD_SECONDS = 6 * 60 * 60
_UPLOAD_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class ChunkedUploadError(ValueError):
    """A client-visible chunk upload protocol error."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class _UploadSession:
    upload_id: str
    filename: str
    total_size: int
    metadata: Dict[str, str]
    temp_path: Path
    received_size: int = 0
    touched_at: float = 0.0


@dataclass(frozen=True)
class CompletedUpload:
    upload_id: str
    filename: str
    total_size: int
    metadata: Mapping[str, str]
    temp_path: Path


class ChunkedUploadStore:
    """Thread-safe owner of incomplete local upload files."""

    def __init__(
        self,
        directory: Path,
        *,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        max_chunk_bytes: int = MAX_CHUNK_BYTES,
        stale_after_seconds: int = STALE_UPLOAD_SECONDS,
    ) -> None:
        self.directory = Path(directory)
        self.max_upload_bytes = int(max_upload_bytes)
        self.chunk_bytes = int(chunk_bytes)
        self.max_chunk_bytes = int(max_chunk_bytes)
        self.stale_after_seconds = int(stale_after_seconds)
        self._lock = threading.RLock()
        self._sessions: Dict[str, _UploadSession] = {}
        if self.chunk_bytes <= 0 or self.chunk_bytes > self.max_chunk_bytes:
            raise ValueError("chunk_bytes must fit within max_chunk_bytes")
        self._cleanup_stale_files()

    def start(
        self,
        filename: str,
        total_size: int,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> Dict[str, object]:
        total_size = int(total_size)
        if total_size <= 0 or total_size > self.max_upload_bytes:
            raise ChunkedUploadError("文件为空或超过 600 MB 限制。", status=413)
        safe_name = Path(str(filename)).name
        if not safe_name or safe_name in {".", ".."}:
            raise ChunkedUploadError("无法识别文件名。")

        with self._lock:
            self._cleanup_expired_sessions_locked()
            self.directory.mkdir(parents=True, exist_ok=True)
            upload_id = uuid.uuid4().hex
            temp_path = self.directory / f".mefinder-upload-{upload_id}.part"
            try:
                temp_path.open("xb").close()
            except OSError as exc:
                raise ChunkedUploadError(f"无法创建上传临时文件：{exc}") from exc
            now = time.time()
            self._sessions[upload_id] = _UploadSession(
                upload_id=upload_id,
                filename=safe_name,
                total_size=total_size,
                metadata={str(key): str(value) for key, value in (metadata or {}).items()},
                temp_path=temp_path,
                touched_at=now,
            )
        return {
            "upload_id": upload_id,
            "chunk_size": self.chunk_bytes,
            "total_size": total_size,
        }

    def append(
        self,
        upload_id: str,
        offset: int,
        content_length: int,
        reader: BinaryIO,
    ) -> Dict[str, object]:
        upload_id = self._validated_upload_id(upload_id)
        try:
            offset = int(offset)
            content_length = int(content_length)
        except (TypeError, ValueError) as exc:
            raise ChunkedUploadError("上传分块位置或长度无效。") from exc
        if offset < 0:
            raise ChunkedUploadError("上传分块位置无效。")
        if content_length <= 0:
            raise ChunkedUploadError("上传分块为空。")
        if content_length > self.max_chunk_bytes:
            raise ChunkedUploadError("上传分块超过 8 MB 限制。", status=413)

        with self._lock:
            self._cleanup_expired_sessions_locked(exclude=upload_id)
            session = self._sessions.get(upload_id)
            if session is None:
                raise ChunkedUploadError("上传任务不存在或已过期。", status=404)
            if offset != session.received_size:
                raise ChunkedUploadError(
                    f"上传分块顺序错误，应从 {session.received_size} 字节继续。",
                    status=409,
                )
            if offset + content_length > session.total_size:
                raise ChunkedUploadError("上传数据超过声明的文件大小。", status=413)

            try:
                with session.temp_path.open("r+b") as stream:
                    stream.seek(offset)
                    remaining = content_length
                    while remaining > 0:
                        chunk = reader.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ChunkedUploadError("上传分块数据不完整。")
                        stream.write(chunk)
                        remaining -= len(chunk)
            except Exception:
                self._truncate_after_failed_append(session, offset)
                raise

            session.received_size = offset + content_length
            session.touched_at = time.time()
            return {
                "upload_id": upload_id,
                "received_size": session.received_size,
                "total_size": session.total_size,
                "complete": session.received_size == session.total_size,
                "first_chunk": offset == 0,
            }

    def finish(self, upload_id: str) -> CompletedUpload:
        upload_id = self._validated_upload_id(upload_id)
        with self._lock:
            self._cleanup_expired_sessions_locked(exclude=upload_id)
            session = self._sessions.get(upload_id)
            if session is None:
                raise ChunkedUploadError("上传任务不存在或已过期。", status=404)
            if session.received_size != session.total_size:
                raise ChunkedUploadError(
                    f"文件尚未上传完整：{session.received_size}/{session.total_size} 字节。",
                    status=409,
                )
            try:
                actual_size = session.temp_path.stat().st_size
            except OSError as exc:
                self._sessions.pop(upload_id, None)
                raise ChunkedUploadError("上传临时文件已丢失。", status=409) from exc
            if actual_size != session.total_size:
                self._sessions.pop(upload_id, None)
                self._unlink(session.temp_path)
                raise ChunkedUploadError("上传文件大小校验失败。", status=409)
            self._sessions.pop(upload_id, None)
            return CompletedUpload(
                upload_id=session.upload_id,
                filename=session.filename,
                total_size=session.total_size,
                metadata=dict(session.metadata),
                temp_path=session.temp_path,
            )

    def cancel(self, upload_id: str) -> bool:
        upload_id = self._validated_upload_id(upload_id)
        with self._lock:
            session = self._sessions.pop(upload_id, None)
            if session is None:
                return False
            self._unlink(session.temp_path)
            return True

    def active_session_count(self) -> int:
        """Return live upload sessions after expiring abandoned ones."""

        with self._lock:
            self._cleanup_expired_sessions_locked()
            return len(self._sessions)

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            for session in sessions:
                self._unlink(session.temp_path)

    def _validated_upload_id(self, upload_id: str) -> str:
        value = str(upload_id or "").strip().lower()
        if not _UPLOAD_ID_PATTERN.fullmatch(value):
            raise ChunkedUploadError("上传任务编号无效。")
        return value

    def _cleanup_expired_sessions_locked(self, *, exclude: str = "") -> None:
        cutoff = time.time() - max(0, self.stale_after_seconds)
        expired = [
            upload_id
            for upload_id, session in self._sessions.items()
            if upload_id != exclude and session.touched_at < cutoff
        ]
        for upload_id in expired:
            session = self._sessions.pop(upload_id)
            self._unlink(session.temp_path)
        self._cleanup_stale_files(active_paths={s.temp_path for s in self._sessions.values()})

    def _cleanup_stale_files(self, *, active_paths: set[Path] | None = None) -> None:
        active_paths = active_paths or set()
        if not self.directory.exists():
            return
        cutoff = time.time() - max(0, self.stale_after_seconds)
        for path in self.directory.glob(".mefinder-upload-*.part"):
            if path in active_paths:
                continue
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    @staticmethod
    def _truncate_after_failed_append(session: _UploadSession, offset: int) -> None:
        try:
            with session.temp_path.open("r+b") as stream:
                stream.truncate(offset)
        except OSError:
            pass

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

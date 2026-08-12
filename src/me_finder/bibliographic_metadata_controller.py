"""Transport-neutral JSON responses for bibliographic metadata workflows."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Callable, Dict, Iterable, Mapping, Tuple

from .application.bibliographic_metadata_coordinator import (
    BibliographicMetadataCoordinator,
    BibliographicMetadataQueueError,
)
from .application.document_query_service import (
    DocumentQueryError,
    DocumentQueryService,
    DocumentQueryUnavailable,
)
from .foreign_book_lookup import BookLookupError
from .journal_metadata_lookup import CNKILookupError
from .crossref_lookup import CrossrefLookupError
from .mineru_api import MinerUError


MetadataResponse = Tuple[int, Dict[str, object]]
MetadataOperation = Callable[[Mapping[str, object]], Dict[str, object]]
CitationParser = Callable[[object], Dict[str, object]]
ActiveSourceIds = Callable[[], Iterable[str]]


class BibliographicMetadataController:
    """Map metadata requests onto application services without HTTP coupling."""

    def __init__(
        self,
        document_queries: DocumentQueryService,
        metadata: BibliographicMetadataCoordinator,
        *,
        additional_active_source_ids: ActiveSourceIds,
        lookup_lock: threading.Lock,
        parse_cnki_citation: CitationParser,
        lookup_cnki: MetadataOperation,
        fetch_cnki_candidate: MetadataOperation,
        lookup_google_books: MetadataOperation,
        lookup_crossref: MetadataOperation,
    ) -> None:
        self._document_queries = document_queries
        self._metadata = metadata
        self._additional_active_source_ids = additional_active_source_ids
        self._lookup_lock = lookup_lock
        self._parse_cnki_citation = parse_cnki_citation
        self._lookup_cnki = lookup_cnki
        self._fetch_cnki_candidate = fetch_cnki_candidate
        self._lookup_google_books = lookup_google_books
        self._lookup_crossref = lookup_crossref

    def metadata(self, source_file_id: object) -> MetadataResponse:
        if not source_file_id:
            return 400, {"error": "invalid request"}
        try:
            metadata = self._document_queries.bibliographic_metadata(
                str(source_file_id)
            )
        except (MinerUError, DocumentQueryError) as exc:
            return 404, {"error": str(exc)}
        return 200, {"ok": True, "metadata": metadata}

    def batch_detect(self, _payload: object) -> MetadataResponse:
        try:
            result = self._metadata.start_batch(
                additional_active_source_ids=(
                    self._additional_active_source_ids()
                )
            )
        except DocumentQueryUnavailable as exc:
            return 503, {"error": str(exc)}
        except BibliographicMetadataQueueError as exc:
            return 503, {"error": str(exc), "job_id": exc.job_id}
        except (
            MinerUError,
            OSError,
            ValueError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            return 500, {"error": f"筛选待识别文献失败：{exc}"}
        return 200, {"ok": True, **result}

    def parse_cnki_citation(self, payload: object) -> MetadataResponse:
        if not isinstance(payload, dict) or set(payload) != {"citation_text"}:
            return 400, {"error": "请求必须只包含 citation_text。"}
        try:
            metadata = self._parse_cnki_citation(payload["citation_text"])
        except ValueError as exc:
            return 400, {"error": str(exc)}
        return 200, {"ok": True, "metadata": metadata}

    def lookup_cnki(self, payload: object) -> MetadataResponse:
        if not isinstance(payload, dict) or set(payload) != {"metadata"}:
            return 400, {"error": "请求必须只包含 metadata。"}
        query_metadata = payload.get("metadata")
        allowed_fields = {
            "title",
            "author",
            "publish_year",
            "journal_name",
            "doi",
            "issn",
        }
        if (
            not isinstance(query_metadata, dict)
            or set(query_metadata) - allowed_fields
        ):
            return 400, {"error": "知网查询字段无效。"}
        if not self._lookup_lock.acquire(blocking=False):
            return 409, {
                "error": "已有知网查询正在进行，请稍候。",
                "code": "lookup_busy",
            }
        try:
            result = self._lookup_cnki(query_metadata)
        except CNKILookupError as exc:
            status = {
                "invalid_query": 400,
                "verification_required": 403,
                "rate_limited": 429,
                "timeout": 504,
            }.get(exc.code, 502)
            return status, {
                "error": str(exc),
                "code": exc.code,
                "open_url": exc.open_url,
            }
        finally:
            self._lookup_lock.release()
        return 200, {"ok": True, **result}

    def cnki_candidate(self, payload: object) -> MetadataResponse:
        if not isinstance(payload, dict) or set(payload) != {"candidate"}:
            return 400, {"error": "请求必须只包含 candidate。"}
        candidate = payload.get("candidate")
        if not isinstance(candidate, dict) or set(candidate) != {"record_url"}:
            return 400, {"error": "知网候选字段无效。"}
        if not self._lookup_lock.acquire(blocking=False):
            return 409, {
                "error": "已有知网查询正在进行，请稍候。",
                "code": "lookup_busy",
            }
        try:
            result = self._fetch_cnki_candidate(candidate)
        except CNKILookupError as exc:
            status = {
                "invalid_candidate": 400,
                "verification_required": 403,
                "rate_limited": 429,
                "timeout": 504,
            }.get(exc.code, 502)
            return status, {
                "error": str(exc),
                "code": exc.code,
                "open_url": exc.open_url,
            }
        finally:
            self._lookup_lock.release()
        return 200, {"ok": True, **result}

    def lookup_google_books(self, payload: object) -> MetadataResponse:
        if not isinstance(payload, dict) or set(payload) != {"metadata"}:
            return 400, {"error": "请求必须只包含 metadata。"}
        query_metadata = payload.get("metadata")
        allowed_fields = {"title", "author", "publish_year", "isbn"}
        if (
            not isinstance(query_metadata, dict)
            or set(query_metadata) - allowed_fields
        ):
            return 400, {"error": "图书查询字段无效。"}
        if not self._lookup_lock.acquire(blocking=False):
            return 409, {
                "error": "已有联网查询正在进行，请稍候。",
                "code": "lookup_busy",
            }
        try:
            result = self._lookup_google_books(query_metadata)
        except BookLookupError as exc:
            status = {
                "invalid_query": 400,
                "rate_limited": 429,
                "timeout": 504,
            }.get(exc.code, 502)
            return status, {
                "error": str(exc),
                "code": exc.code,
                "open_url": exc.open_url,
            }
        finally:
            self._lookup_lock.release()
        return 200, {"ok": True, **result}

    def lookup_crossref(self, payload: object) -> MetadataResponse:
        if not isinstance(payload, dict) or set(payload) != {"metadata"}:
            return 400, {"error": "请求必须只包含 metadata。"}
        query_metadata = payload.get("metadata")
        allowed_fields = {"title", "author", "publish_year", "doi"}
        if (
            not isinstance(query_metadata, dict)
            or set(query_metadata) - allowed_fields
        ):
            return 400, {"error": "Crossref 查询字段无效。"}
        if not self._lookup_lock.acquire(blocking=False):
            return 409, {
                "error": "已有联网查询正在进行，请稍候。",
                "code": "lookup_busy",
            }
        try:
            result = self._lookup_crossref(query_metadata)
        except CrossrefLookupError as exc:
            status = {
                "invalid_query": 400,
                "rate_limited": 429,
                "timeout": 504,
            }.get(exc.code, 502)
            return status, {
                "error": str(exc),
                "code": exc.code,
                "open_url": exc.open_url,
            }
        finally:
            self._lookup_lock.release()
        return 200, {"ok": True, **result}

    def detect(self, payload: object) -> MetadataResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "invalid request"}
        source_file_id = str(payload.get("source_id") or "")
        if not source_file_id:
            return 400, {"error": "invalid request"}
        try:
            metadata = self._document_queries.detect_bibliographic_metadata(
                source_file_id,
                force=bool(payload.get("force")),
            )
        except (MinerUError, DocumentQueryError) as exc:
            return 400, {"error": str(exc)}
        except DocumentQueryUnavailable as exc:
            return 503, {"error": str(exc)}
        except (
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            return 500, {"error": f"书目信息识别失败：{exc}"}
        return 200, {"ok": True, "metadata": metadata}

    def save(self, payload: object) -> MetadataResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "invalid request"}
        source_file_id = str(payload.get("source_id") or "")
        metadata_payload = payload.get("metadata") or {}
        if not source_file_id or not isinstance(metadata_payload, dict):
            return 400, {"error": "invalid request"}
        try:
            metadata = self._metadata.save_manual(
                source_file_id,
                metadata_payload,
            )
        except (MinerUError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except (
            OSError,
            RuntimeError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            return 500, {"error": f"书目信息保存失败：{exc}"}
        return 200, {"ok": True, "metadata": metadata}

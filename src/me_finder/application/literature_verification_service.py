"""Application entry point for local literature-verification use cases."""

from __future__ import annotations

import difflib
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from ..runtime_location import runtime_root
from .search_service import SearchRequest, SearchService


RuntimeRootProvider = Callable[[], Path]
SCHEMA_VERSION = "1"
SOURCE_TYPES = {"all", "pdf", "word", "epub"}
SEARCH_MODES = {"auto", "exact", "compact", "punctuation", "fuzzy"}
SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class LiteratureVerificationService:
    """Resolve the current index for each use case without retaining resources."""

    def __init__(
        self,
        runtime_root_provider: RuntimeRootProvider = runtime_root,
    ) -> None:
        self._runtime_root_provider = runtime_root_provider

    @property
    def index_path(self) -> Path:
        return Path(self._runtime_root_provider()) / "data" / "index.sqlite3"

    def list_documents(
        self,
        *,
        query: str | None = None,
        source_type: str = "all",
        limit: int = 20,
    ) -> dict[str, object]:
        from ..database import load_database_index

        if query is not None and not isinstance(query, str):
            raise ValueError("query 必须是字符串或 null")
        _validate_source_type(source_type)
        validated_limit = _bounded_integer("limit", limit, minimum=1, maximum=100)
        catalog = load_database_index(self._existing_index_path())
        volumes_by_source = {
            str(item.get("source_file_id")): item
            for item in catalog.get("volumes", [])
            if isinstance(item, Mapping) and item.get("source_file_id")
        }
        works_by_volume = {
            str(item.get("volume_id")): item
            for item in catalog.get("works", [])
            if isinstance(item, Mapping) and item.get("volume_id")
        }
        normalized_query = (query or "").strip().casefold()
        documents = []
        sources = sorted(
            (
                item
                for item in catalog.get("source_files", [])
                if isinstance(item, Mapping)
            ),
            key=lambda item: str(item.get("source_file_id") or ""),
        )
        for source in sources:
            document_type = str(source.get("source_type") or "")
            if source_type != "all" and document_type != source_type:
                continue
            volume = volumes_by_source.get(str(source.get("source_file_id") or ""), {})
            work = works_by_volume.get(str(volume.get("volume_id") or ""), {})
            bibliographic = source.get("bibliographic_metadata")
            if not isinstance(bibliographic, Mapping):
                bibliographic = {}
            title = _first_text(
                bibliographic.get("title"),
                source.get("document_title"),
                source.get("display_title"),
                source.get("title"),
                volume.get("display_title"),
                volume.get("document_title"),
                work.get("title"),
                source.get("file_name"),
            )
            author = _first_text(
                bibliographic.get("author"),
                bibliographic.get("authors"),
                source.get("author"),
                source.get("author_label"),
                volume.get("author_label"),
                work.get("author_label"),
            )
            original_file_name = _first_text(
                source.get("original_file_name"),
                source.get("file_name"),
            )
            if normalized_query and normalized_query not in "\n".join(
                value.casefold()
                for value in (title, author, original_file_name)
                if value is not None
            ):
                continue
            documents.append(
                {
                    "source_file_id": str(source["source_file_id"]),
                    "source_type": document_type,
                    "title": title,
                    "author": author,
                    "original_file_name": original_file_name,
                }
            )

        total = len(documents)
        return {
            "schema_version": SCHEMA_VERSION,
            "total": total,
            "has_more": total > validated_limit,
            "documents": documents[:validated_limit],
        }

    def locate_quote(
        self,
        quote: str,
        *,
        mode: str = "auto",
        source_file_id: str | None = None,
        source_type: str = "all",
        limit: int = 5,
    ) -> dict[str, object]:
        _validate_quote(quote)
        validated_mode = _validate_mode(mode)
        _validate_source_type(source_type)
        validated_source_id = _validate_optional_source_id(source_file_id)
        validated_limit = _bounded_integer("limit", limit, minimum=1, maximum=20)

        with self._open_engine() as engine:
            return self._search_one(
                engine,
                quote,
                mode=validated_mode,
                source_file_id=validated_source_id,
                source_type=source_type,
                limit=validated_limit,
            )

    def verify_quotes(
        self,
        quotes: object,
        *,
        mode: str = "auto",
        source_file_id: str | None = None,
        source_type: str = "all",
        matches_per_quote: int = 1,
    ) -> dict[str, object]:
        validated_quotes = _validate_quotes(quotes)
        validated_mode = _validate_mode(mode)
        _validate_source_type(source_type)
        validated_source_id = _validate_optional_source_id(source_file_id)
        validated_limit = _bounded_integer(
            "matches_per_quote", matches_per_quote, minimum=1, maximum=5
        )

        # Resolve source existence once so every quote reports the same error.
        with self._open_engine(validated_source_id) as engine:
            results = [
                _verify_result(
                    index,
                    self._search_one(
                        engine,
                        quote,
                        mode=validated_mode,
                        source_file_id=validated_source_id,
                        source_type=source_type,
                        limit=validated_limit,
                    ),
                )
                for index, quote in enumerate(validated_quotes)
            ]

        counts = {"verified": 0, "approximate": 0, "not_found": 0}
        for result in results:
            counts[str(result["status"])] += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "total": len(results),
            "verified_count": counts["verified"],
            "approximate_count": counts["approximate"],
            "not_found_count": counts["not_found"],
            "results": results,
        }

    def diff_quote(
        self,
        quote: str,
        *,
        source_file_id: str | None = None,
        source_type: str = "all",
        mode: str = "fuzzy",
    ) -> dict[str, object]:
        _validate_quote(quote)
        validated_mode = _validate_mode(mode)
        _validate_source_type(source_type)
        validated_source_id = _validate_optional_source_id(source_file_id)

        with self._open_engine() as engine:
            located = self._search_one(
                engine,
                quote,
                mode=validated_mode,
                source_file_id=validated_source_id,
                source_type=source_type,
                limit=1,
            )

        matches = located["matches"]
        if not matches:
            return {
                "schema_version": SCHEMA_VERSION,
                "quote": quote,
                "status": "not_found",
                "match": None,
                "similarity": None,
                "diff": [],
                "stats": None,
            }
        best = matches[0]
        segments, stats, identical = _character_diff(
            quote, str(best["matched_text"])
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "quote": quote,
            "status": "identical" if identical else "different",
            "match": best,
            "similarity": _similarity(quote, str(best["matched_text"])),
            "diff": segments,
            "stats": stats,
        }

    def search_passages(
        self,
        query: str,
        *,
        source_file_id: str | None = None,
        source_type: str = "all",
        limit: int = 10,
    ) -> dict[str, object]:
        _validate_query(query)
        _validate_source_type(source_type)
        validated_source_id = _validate_optional_source_id(source_file_id)
        validated_limit = _bounded_integer("limit", limit, minimum=1, maximum=20)

        with self._open_engine(validated_source_id) as engine:
            raw_result = engine.search_passages(
                query,
                limit=validated_limit,
                source_type=source_type,
                source_file_id=validated_source_id,
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "query": str(raw_result["query"]),
            "total": int(raw_result["total"]),
            "total_is_exact": bool(raw_result["total_is_exact"]),
            "has_more": bool(raw_result["has_more"]),
            "passages": [
                _passage_match(item)
                for item in raw_result["results"]
                if isinstance(item, Mapping)
            ],
        }

    def find_parallel_passages(
        self,
        quote: str,
        *,
        mode: str = "auto",
        source_file_id: str | None = None,
        target_source_file_id: str | None = None,
        target_language_code: str | None = None,
        limit: int = 5,
    ) -> dict[str, object]:
        """Find a quote, then read its persisted cross-version alignment."""

        from .parallel_passage_service import find_parallel_passages

        _validate_quote(quote)
        validated_mode = _validate_mode(mode)
        validated_source_id = _validate_optional_source_id(source_file_id)
        validated_target_id = _validate_optional_source_id(
            target_source_file_id, name="target_source_file_id"
        )
        validated_language = _validate_optional_language_code(target_language_code)
        validated_limit = _bounded_integer("limit", limit, minimum=1, maximum=20)
        index_path = self._existing_index_path()

        with self._open_engine(validated_source_id) as engine:
            return find_parallel_passages(
                index_path,
                engine,
                quote,
                mode=validated_mode,
                source_file_id=validated_source_id,
                target_source_file_id=validated_target_id,
                target_language_code=validated_language,
                limit=validated_limit,
                schema_version=SCHEMA_VERSION,
                source_formatter=_search_match,
            )

    def propose_alignment_correction(
        self,
        *,
        source_file_id: str,
        target_source_file_id: str,
        source_segment_ids: Sequence[object],
        target_segment_ids: Sequence[object],
        evidence: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        from .alignment_override_service import propose_alignment_correction

        return propose_alignment_correction(
            self._existing_index_path(),
            source_file_id=source_file_id,
            target_source_file_id=target_source_file_id,
            source_segment_ids=source_segment_ids,
            target_segment_ids=target_segment_ids,
            evidence=evidence,
        )

    def confirm_alignment_correction(
        self, *, override_id: str, confirmation_token: str
    ) -> dict[str, object]:
        from .alignment_override_service import confirm_alignment_correction

        return confirm_alignment_correction(
            self._existing_index_path(),
            override_id=override_id,
            confirmation_token=confirmation_token,
        )

    def revoke_alignment_correction(self, *, override_id: str) -> dict[str, object]:
        from .alignment_override_service import revoke_alignment_correction

        return revoke_alignment_correction(
            self._existing_index_path(), override_id=override_id
        )

    def list_alignment_corrections(
        self,
        *,
        source_file_id: str | None = None,
        target_source_file_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        from .alignment_override_service import list_alignment_corrections

        return list_alignment_corrections(
            self._existing_index_path(),
            source_file_id=source_file_id,
            target_source_file_id=target_source_file_id,
            status=status,
            limit=limit,
        )

    def _search_one(
        self,
        engine: object,
        quote: str,
        *,
        mode: str,
        source_file_id: str | None,
        source_type: str,
        limit: int,
    ) -> dict[str, object]:
        from ..structured_reader import SourceNotFound

        if (
            source_file_id is not None
            and source_file_id not in engine.sources_by_id
        ):
            raise SourceNotFound(f"未找到文献：{source_file_id}")
        raw_result = SearchService.execute(
            engine,
            SearchRequest(
                query=quote,
                mode=mode,
                limit=limit,
                source_type=source_type,
                source_file_id=source_file_id,
            ),
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "query": str(raw_result["query"]),
            "total": int(raw_result["total"]),
            "total_is_exact": bool(raw_result["total_is_exact"]),
            "has_more": bool(raw_result["has_more"]),
            "matches": [
                _search_match(item)
                for item in raw_result["results"]
                if isinstance(item, Mapping)
            ],
        }

    def read_document_window(
        self,
        source_file_id: str,
        *,
        start: int = 0,
        count: int = 10,
    ) -> dict[str, object]:
        from ..structured_reader import get_document_window

        validated_source_id = _validate_source_id(source_file_id)
        validated_start = _bounded_integer("start", start, minimum=0)
        validated_count = _bounded_integer("count", count, minimum=1, maximum=50)
        raw_result = get_document_window(
            self._existing_index_path(),
            validated_source_id,
            start=validated_start,
            count=validated_count,
        )
        source = raw_result["source"]
        if not isinstance(source, Mapping):
            raise ValueError("结构化阅读器返回了无效的 source")
        source_type = str(source["source_type"])
        return {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "source_file_id": str(source["source_file_id"]),
                "source_type": source_type,
                "title": _first_text(
                    source.get("display_title"),
                    source.get("document_title"),
                    source.get("file_name"),
                ),
                "original_file_name": _first_text(
                    source.get("original_file_name"),
                    source.get("file_name"),
                ),
            },
            "start": int(raw_result["start"]),
            "count": int(raw_result["count"]),
            "total": int(raw_result["total"]),
            "last_position": raw_result["last_position"],
            "has_more": bool(raw_result["has_more"]),
            "previous_start": raw_result["previous_start"],
            "next_start": raw_result["next_start"],
            "items": [
                _reader_item(item, source_type)
                for item in raw_result["items"]
                if isinstance(item, Mapping)
            ],
        }

    def read_bibliographic_pages(
        self,
        source_file_id: str,
        *,
        front: int = 5,
        back: int = 5,
    ) -> dict[str, object]:
        from ..structured_reader import get_document_window

        validated_source_id = _validate_source_id(source_file_id)
        front_count = _bounded_integer("front", front, minimum=0, maximum=20)
        back_count = _bounded_integer("back", back, minimum=0, maximum=20)
        if front_count == 0 and back_count == 0:
            raise ValueError("front 与 back 不能同时为 0")
        index_path = self._existing_index_path()

        # One probe from the start resolves total pages and the source record even
        # when only the tail is requested (count must be at least 1).
        probe = get_document_window(
            index_path,
            validated_source_id,
            start=0,
            count=max(front_count, 1),
        )
        source = probe["source"]
        if not isinstance(source, Mapping):
            raise ValueError("结构化阅读器返回了无效的 source")
        source_type = str(source["source_type"])
        total = int(probe["total"])

        front_pages: list[dict[str, object]] = []
        front_positions: set[int] = set()
        if front_count > 0:
            for item in probe["items"]:
                if not isinstance(item, Mapping):
                    continue
                page = _bibliographic_page(item, source_type)
                front_pages.append(page)
                front_positions.add(int(page["position"]))

        back_pages: list[dict[str, object]] = []
        if back_count > 0 and total > 0:
            back_start = max(0, total - back_count)
            tail = get_document_window(
                index_path,
                validated_source_id,
                start=back_start,
                count=back_count,
            )
            for item in tail["items"]:
                if not isinstance(item, Mapping):
                    continue
                page = _bibliographic_page(item, source_type)
                # A short document can make the tail window overlap the front.
                if int(page["position"]) in front_positions:
                    continue
                back_pages.append(page)

        return {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "source_file_id": str(source["source_file_id"]),
                "source_type": source_type,
                "title": _first_text(
                    source.get("display_title"),
                    source.get("document_title"),
                    source.get("file_name"),
                ),
                "original_file_name": _first_text(
                    source.get("original_file_name"),
                    source.get("file_name"),
                ),
            },
            "total": total,
            "front": front_pages,
            "back": back_pages,
        }

    def read_bibliographic_metadata(
        self,
        source_file_id: str,
    ) -> dict[str, object]:
        from ..bibliographic_metadata import (
            METADATA_FIELDS,
            canonical_metadata,
            invalid_metadata_fields,
            is_valid_bibliographic_value,
        )
        from ..database import load_database_index
        from ..structured_reader import SourceNotFound

        validated_source_id = _validate_source_id(source_file_id)
        catalog = load_database_index(self._existing_index_path())
        source = next(
            (
                item
                for item in catalog.get("source_files", [])
                if isinstance(item, Mapping)
                and str(item.get("source_file_id") or "") == validated_source_id
            ),
            None,
        )
        if source is None:
            raise SourceNotFound(f"未找到文献：{validated_source_id}")

        record = canonical_metadata(source)
        invalid = set(invalid_metadata_fields(record))
        fields: list[dict[str, object]] = []
        missing_fields: list[str] = []
        invalid_fields: list[str] = []
        for name in METADATA_FIELDS:
            value = record.get(name)
            if name in invalid:
                status = "invalid"
                invalid_fields.append(name)
            elif value not in (None, "") and is_valid_bibliographic_value(value):
                status = "present"
            else:
                status = "missing"
                missing_fields.append(name)
            fields.append(
                {"field": name, "value": _first_text(value), "status": status}
            )

        return {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "source_file_id": validated_source_id,
                "source_type": str(source.get("source_type") or ""),
                "title": _first_text(
                    record.get("title"),
                    source.get("document_title"),
                    source.get("display_title"),
                    source.get("file_name"),
                ),
                "original_file_name": _first_text(
                    source.get("original_file_name"),
                    source.get("file_name"),
                ),
            },
            "document_type": _first_text(record.get("document_type")),
            "metadata_source": _first_text(record.get("metadata_source")),
            "metadata_status": _first_text(record.get("metadata_status")),
            "fields": fields,
            "missing_fields": missing_fields,
            "invalid_fields": invalid_fields,
        }

    def _existing_index_path(self) -> Path:
        path = self.index_path
        if not path.is_file():
            raise FileNotFoundError(f"Index not found: {path}")
        return path

    @contextmanager
    def _open_engine(self, source_file_id: str | None = None) -> Iterator[object]:
        """Open the index for one tool call, and always close it.

        Each MCP call resolves the index afresh (the sidecar holds no long-lived
        handle), so every read-only tool needs the same open/validate/close
        dance.  Passing ``source_file_id`` also checks it exists up front, which
        every scoped tool must do before searching.
        """

        from ..search import SearchEngine
        from ..structured_reader import SourceNotFound

        engine = SearchEngine(self._existing_index_path())
        try:
            if (
                source_file_id is not None
                and source_file_id not in engine.sources_by_id
            ):
                raise SourceNotFound(f"未找到文献：{source_file_id}")
            yield engine
        finally:
            engine.close()


def _validate_quote(quote: object) -> None:
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("quote 必须是非空字符串")
    if len(quote) > 10_000:
        raise ValueError("quote 不能超过 10000 个 Unicode codepoint")


def _validate_query(query: object) -> None:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    if len(query) > 1_000:
        raise ValueError("query 不能超过 1000 个 Unicode codepoint")


def _validate_optional_language_code(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", value
    ):
        raise ValueError("target_language_code 必须是 BCP 47 语言代码")
    return value


def _validate_quotes(quotes: object) -> list[str]:
    if not isinstance(quotes, (list, tuple)):
        raise ValueError("quotes 必须是字符串数组")
    if not 1 <= len(quotes) <= 50:
        raise ValueError("quotes 必须包含 1 到 50 条引文")
    validated: list[str] = []
    for quote in quotes:
        _validate_quote(quote)
        validated.append(quote)
    return validated


def _validate_mode(mode: object) -> str:
    if not isinstance(mode, str) or mode not in SEARCH_MODES:
        raise ValueError("mode 不受支持")
    return mode


def _verify_status(located: Mapping[str, object]) -> str:
    matches = located["matches"]
    if not isinstance(matches, list) or not matches:
        return "not_found"
    best = matches[0]
    if str(best["match_type"]) != "fuzzy" or float(best["match_score"]) >= 1.0:
        return "verified"
    return "approximate"


def _verify_result(index: int, located: Mapping[str, object]) -> dict[str, object]:
    return {
        "index": index,
        "quote": str(located["query"]),
        "status": _verify_status(located),
        "total": int(located["total"]),
        "has_more": bool(located["has_more"]),
        "matches": located["matches"],
    }


def _similarity(quote: str, source: str) -> float:
    ratio = difflib.SequenceMatcher(None, quote, source, autojunk=False).ratio()
    return round(float(ratio), 4)


def _character_diff(
    quote: str,
    source: str,
) -> tuple[list[dict[str, object]], dict[str, object], bool]:
    """Character-level alignment of the quote against the original passage."""

    matcher = difflib.SequenceMatcher(None, quote, source, autojunk=False)
    segments: list[dict[str, object]] = []
    stats = {
        "equal": 0,
        "added": 0,
        "missing": 0,
        "changed_quote": 0,
        "changed_source": 0,
    }
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        quote_text = quote[i1:i2]
        source_text = source[j1:j2]
        if tag == "equal":
            segments.append({"op": "equal", "quote": quote_text, "source": source_text})
            stats["equal"] += i2 - i1
        elif tag == "delete":
            # Present in the quote but not the source: the user added characters.
            segments.append({"op": "added", "quote": quote_text, "source": ""})
            stats["added"] += i2 - i1
        elif tag == "insert":
            # Present in the source but not the quote: the user omitted characters.
            segments.append({"op": "missing", "quote": "", "source": source_text})
            stats["missing"] += j2 - j1
        else:
            segments.append(
                {"op": "changed", "quote": quote_text, "source": source_text}
            )
            stats["changed_quote"] += i2 - i1
            stats["changed_source"] += j2 - j1
    identical = (
        stats["added"] == 0
        and stats["missing"] == 0
        and stats["changed_quote"] == 0
        and stats["changed_source"] == 0
    )
    return segments, stats, identical


def _validate_source_type(source_type: object) -> None:
    if not isinstance(source_type, str) or source_type not in SOURCE_TYPES:
        raise ValueError("source_type 必须是 all、pdf、word 或 epub")


def _validate_source_id(
    source_file_id: object, *, name: str = "source_file_id"
) -> str:
    if not isinstance(source_file_id, str) or not SOURCE_ID_PATTERN.fullmatch(
        source_file_id
    ):
        raise ValueError(
            f"{name} 只能包含 ASCII 字母、数字、点、下划线和连字符，且长度不超过 128"
        )
    return source_file_id


def _validate_optional_source_id(
    source_file_id: object, *, name: str = "source_file_id"
) -> str | None:
    if source_file_id is None:
        return None
    return _validate_source_id(source_file_id, name=name)


def _bounded_integer(
    name: str,
    value: object,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} 不能大于 {maximum}")
    return value


def _first_text(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            text = "、".join(str(item).strip() for item in value if str(item).strip())
        else:
            text = str(value).strip()
        if text:
            return text
    return None


def _first_present(fields: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        value = fields.get(key)
        if value is not None:
            return value
    return None


def _physical_page(
    fields: Mapping[str, object],
    source_type: str,
) -> dict[str, object]:
    if source_type != "pdf":
        return {
            "start_index": None,
            "end_index": None,
            "start_label": None,
            "end_label": None,
        }
    start_index = _first_present(fields, "pdf_page_start_index", "pdf_page_index")
    end_index = _first_present(fields, "pdf_page_end_index", "pdf_page_index")
    start_label = _first_text(
        _first_present(fields, "pdf_page_start_label", "pdf_page_label")
    )
    end_label = _first_text(
        _first_present(fields, "pdf_page_end_label", "pdf_page_label")
    )
    return {
        "start_index": int(start_index) if start_index is not None else None,
        "end_index": int(end_index) if end_index is not None else None,
        "start_label": start_label,
        "end_label": end_label or start_label,
    }


def _citation_page(
    fields: Mapping[str, object],
    source_type: str,
    physical_page: Mapping[str, object],
) -> dict[str, object]:
    verified_value = _first_present(
        fields,
        "citation_page_verified",
        "page_verified",
    )
    verified = bool(verified_value)
    start = _first_text(fields.get("citation_page_start")) if verified else None
    end = _first_text(fields.get("citation_page_end")) if verified else None
    if verified:
        status = "calibrated" if source_type == "pdf" else "verified"
    elif source_type == "pdf" and physical_page.get("start_index") is not None:
        status = "uncalibrated"
    else:
        status = "unavailable"
    return {
        "start": start,
        "end": end or start,
        "status": status,
    }


def _page_mapping(fields: Mapping[str, object]) -> dict[str, object]:
    confidence = _first_present(
        fields,
        "page_mapping_confidence",
        "mapping_confidence",
        "page_confidence",
    )
    return {
        "method": _first_text(
            _first_present(
                fields,
                "page_mapping_method",
                "mapping_method",
                "page_source_type",
            )
        ),
        "confidence": float(confidence) if confidence is not None else None,
        "confidence_level": _first_text(fields.get("mapping_confidence_level")),
        "note": _first_text(fields.get("page_note")),
    }


def _search_match(fields: Mapping[str, object]) -> dict[str, object]:
    source_type = str(fields["source_type"])
    internal_match_type = str(fields["match_type"])
    physical_page = _physical_page(fields, source_type)
    reader_start = _first_present(
        fields,
        "pdf_page_start_index" if source_type == "pdf" else "paragraph_index",
    )
    return {
        "paragraph_id": str(fields["paragraph_id"]),
        "source_file_id": str(fields["source_file_id"]),
        "source_type": source_type,
        "document_title": _first_text(fields.get("document_title")),
        "work_title": _first_text(fields.get("work_title")),
        "author": _first_text(fields.get("author_label")),
        "matched_text": str(fields["matched_text"]),
        "paragraph_text": str(fields["paragraph_text"]),
        "context_before": fields["context_before"],
        "context_after": fields["context_after"],
        "match_type": (
            "fuzzy" if internal_match_type == "ngram_fuzzy" else internal_match_type
        ),
        "match_score": float(fields["match_score"]),
        "physical_page": physical_page,
        "citation_page": _citation_page(fields, source_type, physical_page),
        "page_mapping": _page_mapping(fields),
        "reader": {
            "unit": "pdf_page" if source_type == "pdf" else "word_paragraph",
            "start": int(reader_start),
        },
    }


def _passage_match(fields: Mapping[str, object]) -> dict[str, object]:
    source_type = str(fields["source_type"])
    physical_page = _physical_page(fields, source_type)
    reader_start = _first_present(
        fields,
        "pdf_page_start_index" if source_type == "pdf" else "paragraph_index",
    )
    relevance = fields["relevance"]
    if not isinstance(relevance, Mapping):
        raise ValueError("检索引擎返回了无效的 relevance")
    citation_formats = fields["citation_formats"]
    if not isinstance(citation_formats, Mapping):
        raise ValueError("检索引擎返回了无效的 citation_formats")
    return {
        "paragraph_id": str(fields["paragraph_id"]),
        "source_file_id": str(fields["source_file_id"]),
        "source_type": source_type,
        "document_title": _first_text(fields.get("document_title")),
        "work_title": _first_text(fields.get("work_title")),
        "author": _first_text(fields.get("author_label")),
        "paragraph_text": str(fields["paragraph_text"]),
        "preview": str(fields["matched_text"]),
        "context_before": fields["context_before"],
        "context_after": fields["context_after"],
        "relevance": {
            "rank": int(relevance["rank"]),
            "method": str(relevance["method"]),
            "score": float(relevance["score"]),
        },
        "physical_page": physical_page,
        "citation_page": _citation_page(fields, source_type, physical_page),
        "page_mapping": _page_mapping(fields),
        "citation": {
            "chinese": str(citation_formats["chinese"]),
            "gb": str(citation_formats["gb"]),
            "chinese_status": str(citation_formats["chinese_status"]),
            "gb_status": str(citation_formats["gb_status"]),
        },
        "reader": {
            "unit": "pdf_page" if source_type == "pdf" else "word_paragraph",
            "start": int(reader_start),
        },
    }


_BIBLIOGRAPHIC_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("isbn", ("isbn",)),
    ("cip", ("图书在版编目", "(cip)", "（cip）", "cip数据", "cip 数据")),
    ("publisher", ("出版社", "出版发行", "出版發行", "press", "verlag", "éditions")),
    ("price", ("定价", "定 价", "售价")),
    ("responsibility", ("责任编辑", "責任編輯", "责任印制", "装帧设计", "封面设计")),
)
_YEAR_RE = re.compile(r"(?:1[89]\d{2}|20\d{2})\s*年")


def _bibliographic_hints(text: str) -> list[str]:
    """Flag copyright-page cues found verbatim in already-parsed page text."""

    lowered = text.casefold()
    hints = [
        name
        for name, tokens in _BIBLIOGRAPHIC_CUES
        if any(token in lowered for token in tokens)
    ]
    if _YEAR_RE.search(text):
        hints.append("year")
    return hints


def _is_likely_copyright_page(hints: Sequence[str]) -> bool:
    if {"isbn", "cip"} & set(hints):
        return True
    return len(hints) >= 3


def _bibliographic_page(
    fields: Mapping[str, object],
    source_type: str,
) -> dict[str, object]:
    physical_page = _physical_page(fields, source_type)
    position = _first_present(
        fields,
        "pdf_page_index" if source_type == "pdf" else "paragraph_index",
    )
    text = str(fields["text_raw"])
    hints = _bibliographic_hints(text)
    return {
        "item_type": str(fields["item_type"]),
        "position": int(position),
        "text": text,
        "is_empty": bool(fields.get("is_empty", not text.strip())),
        "physical_page": physical_page,
        "citation_page": _citation_page(fields, source_type, physical_page),
        "hints": hints,
        "likely_copyright_page": _is_likely_copyright_page(hints),
    }


def _reader_item(
    fields: Mapping[str, object],
    source_type: str,
) -> dict[str, object]:
    physical_page = _physical_page(fields, source_type)
    position = _first_present(
        fields,
        "pdf_page_index" if source_type == "pdf" else "paragraph_index",
    )
    text = str(fields["text_raw"])
    citation_formats = fields["citation_formats"]
    if not isinstance(citation_formats, Mapping):
        raise ValueError("结构化阅读器返回了无效的 citation_formats")
    return {
        "item_type": str(fields["item_type"]),
        "anchor_id": _first_text(fields.get("anchor_id")),
        "position": int(position),
        "text": text,
        "is_empty": bool(fields.get("is_empty", not text.strip())),
        "physical_page": physical_page,
        "citation_page": _citation_page(fields, source_type, physical_page),
        "page_mapping": _page_mapping(fields),
        "page_display": str(fields["page_display"]),
        "page_verified": bool(fields["page_verified"]),
        "citation_formats": {
            "chinese": str(citation_formats["chinese"]),
            "gb": str(citation_formats["gb"]),
            "page_verified": bool(citation_formats["page_verified"]),
            "can_copy": bool(citation_formats["can_copy"]),
        },
    }

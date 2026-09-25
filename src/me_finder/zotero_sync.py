"""Zotero → MEFinder source sync: snapshot, diff and execution.

Zotero is the source of truth for collections, items and files. MEFinder
imports the PDF/EPUB attachments of the selected collections through its own
import pipeline and keeps a link table (``persistence.zotero_sync_store``).
Zotero's own full-text index is never read.

Safety rule (docs/issues/zotero-source-sync.md): a removal is only ever planned
from a complete, successful read of every selected collection. Any failure —
Zotero not running, Local API off, a request error, an incomplete page, a
selected collection that vanished, or a listing that contradicts Zotero's own
item count — pauses the sync without removing anything.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .bibliographic_metadata import metadata_missing_fields
from .persistence.zotero_sync_store import StoredZoteroState, ZoteroSyncStore
from .zotero_local_api import (
    ZoteroIncomplete,
    ZoteroLocalClient,
    ZoteroProbe,
    ZoteroUnavailable,
)


ATTACHMENT_FORMATS = {"application/pdf": "pdf", "application/epub+zip": "epub"}
FILE_LINK_MODES = frozenset({"imported_file", "imported_url", "linked_file"})
FREQUENCIES = ("manual", "launch", "interval")
INTERVAL_SECONDS = 30 * 60
# Attachments the queue could not take are pushed again by the scheduler once
# it has room, at most this often, even when the user syncs by hand.
BACKFILL_SECONDS = 5 * 60
IMPORT_BATCH = 50
QUEUE_FULL_TEXT = "排队已满，稍后自动重试"
_ITEM_FIELDS = (
    "itemType", "title", "creators", "date", "publisher", "place",
    "publicationTitle", "volume", "issue", "pages", "DOI", "ISBN", "ISSN",
    "university", "bookTitle", "language", "libraryCatalog",
)
# 茉莉花（Jasminum）插件把知网/万方等抓到的题录直接写进 Zotero 标准字段，并在
# extra 里记 CNKICite。检测到这些痕迹时题录来源标为「Zotero 元数据（茉莉花）」。
_JASMINUM_CATALOGS = ("cnki", "知网", "wanfang", "万方", "chinadoi", "yiigle")
ZOTERO_METADATA_SOURCES = frozenset({"zotero", "zotero_jasminum"})
_JOB_DONE = frozenset({"completed"})
_JOB_FAILED = frozenset({"failed", "cancelled", "canceled", "interrupted"})
_CONNECTION_TEXT = {
    "connected": "已连接",
    "api_disabled": "接口未开启",
    "not_running": "未检测到 Zotero",
    "unsupported": "Zotero 版本过旧",
    "error": "连接失败",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data(entry: Mapping[str, object]) -> Mapping[str, object]:
    data = entry.get("data")
    return data if isinstance(data, Mapping) else {}


# ── Zotero data shaping ─────────────────────────────────────────────────────


def effective_collections(
    collections: Sequence[Mapping[str, object]], selected: Iterable[str]
) -> Tuple[Set[str], List[str]]:
    """Expand selected keys with all their descendants.

    Returns ``(effective_keys, missing_selected_keys)``. A parent collection
    stands for every sub-collection, including ones created later.
    """

    children: Dict[str, List[str]] = {}
    known: Set[str] = set()
    for entry in collections:
        key = str(entry.get("key") or _data(entry).get("key") or "")
        if not key:
            continue
        known.add(key)
        parent = _data(entry).get("parentCollection")
        if parent:
            children.setdefault(str(parent), []).append(key)
    effective: Set[str] = set()
    missing: List[str] = []
    stack: List[str] = []
    for key in (str(value) for value in selected):
        (stack if key in known else missing).append(key)
    while stack:
        key = stack.pop()
        if key not in effective:
            effective.add(key)
            stack.extend(children.get(key, ()))
    return effective, missing


def collection_tree(collections: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    """Flatten Zotero collections into ``{key, name, parent, item_count}`` rows."""

    rows = []
    for entry in collections:
        meta = entry.get("meta") if isinstance(entry.get("meta"), Mapping) else {}
        rows.append(
            {
                "key": str(entry.get("key") or _data(entry).get("key") or ""),
                "name": str(_data(entry).get("name") or ""),
                "parent": str(_data(entry).get("parentCollection") or "") or None,
                "item_count": int(meta.get("numItems") or 0),
            }
        )
    return sorted(rows, key=lambda row: row["name"].casefold())


def trim_item(entry: Mapping[str, object]) -> Dict[str, object]:
    data = _data(entry)
    trimmed = {name: data.get(name) for name in _ITEM_FIELDS if data.get(name) not in (None, "", [])}
    # Only the marker is kept, not the volatile citation count in ``extra``.
    if _is_jasminum(data):
        trimmed["jasminum"] = True
    trimmed["collections"] = sorted(str(key) for key in data.get("collections") or [])
    return trimmed


def _is_jasminum(data: Mapping[str, object]) -> bool:
    catalog = str(data.get("libraryCatalog") or "").casefold()
    return bool(
        data.get("jasminum")
        or "CNKICite:" in str(data.get("extra") or "")
        or any(name in catalog for name in _JASMINUM_CATALOGS)
    )


def item_fingerprint(data: Mapping[str, object]) -> str:
    """Fingerprint of the bibliographic fields only (not collection membership)."""

    relevant = {key: value for key, value in data.items() if key != "collections"}
    encoded = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def attachment_record(entry: Mapping[str, object]) -> Dict[str, object]:
    """Normalize a Zotero attachment; non PDF/EPUB files come back ``ignored``."""

    data = _data(entry)
    key = str(entry.get("key") or data.get("key") or "")
    version = int(entry.get("version") or data.get("version") or 0)
    ignored = {"attachment_key": key, "attachment_version": version, "ignored": True}
    if data.get("itemType") != "attachment" or data.get("deleted"):
        return ignored
    link_mode = str(data.get("linkMode") or "")
    if link_mode not in FILE_LINK_MODES:
        return ignored
    content_type = str(data.get("contentType") or "").lower()
    file_name = str(data.get("filename") or Path(str(data.get("path") or "")).name or "")
    source_format = ATTACHMENT_FORMATS.get(content_type) or {
        ".pdf": "pdf", ".epub": "epub"
    }.get(Path(file_name).suffix.lower())
    if source_format is None:
        return ignored
    signature = "|".join(str(data.get(name) or "") for name in ("md5", "mtime", "path", "filename"))
    return {
        "attachment_key": key,
        "parent_item_key": str(data.get("parentItem") or "") or key,
        "attachment_version": version,
        "link_mode": link_mode,
        "content_type": content_type or None,
        "file_name": file_name or None,
        "file_signature": signature,
        "format": source_format,
    }


def _person(creator: Mapping[str, object]) -> str:
    if creator.get("name"):
        return str(creator["name"]).strip()
    last = str(creator.get("lastName") or "").strip()
    first = str(creator.get("firstName") or "").strip()
    if re.search(r"[\u3400-\u9fff]", last + first):
        return f"{last}{first}"
    return " ".join(part for part in (first, last) if part)


def zotero_metadata(data: Mapping[str, object]) -> Dict[str, object]:
    """Map Zotero item fields onto MEFinder bibliographic metadata."""

    creators = [item for item in data.get("creators") or [] if isinstance(item, Mapping)]

    def people(*roles: str) -> Optional[str]:
        names = [_person(item) for item in creators if item.get("creatorType") in roles]
        return "、".join(name for name in names if name) or None

    item_type = str(data.get("itemType") or "")
    translator = people("translator")
    if item_type in {"journalArticle", "magazineArticle", "newspaperArticle"}:
        document_type = "journal_article"
    elif item_type == "thesis":
        document_type = "thesis"
    elif translator:
        document_type = "translated_book"
    else:
        document_type = "book"
    year = re.search(r"(1[5-9]\d\d|20\d\d)", str(data.get("date") or ""))
    isbn = re.split(r"[\s,;]+", str(data.get("ISBN") or "").strip())[0]
    raw = {
        "title": data.get("title"),
        "author": people("author") or people("editor", "bookAuthor"),
        "translator": translator,
        "publisher": data.get("university") if document_type == "thesis" else data.get("publisher"),
        "publish_place": data.get("place"),
        "publish_year": year.group(1) if year else None,
        "isbn": isbn,
        "journal_name": data.get("publicationTitle"),
        "volume": data.get("volume"),
        "issue": data.get("issue"),
        "page_range": data.get("pages"),
        "doi": data.get("DOI"),
        "issn": data.get("ISSN"),
    }
    metadata: Dict[str, object] = {
        key: (str(value).strip() or None) if value is not None else None
        for key, value in raw.items()
    }
    metadata["document_type"] = document_type
    missing = metadata_missing_fields(metadata)
    metadata.update(
        metadata_status="complete" if not missing else "partial",
        metadata_source="zotero_jasminum" if _is_jasminum(data) else "zotero",
        metadata_confidence=1.0,
        metadata_missing_fields=missing,
    )
    return metadata


def merge_metadata(
    existing: Optional[Mapping[str, object]], zotero: Mapping[str, object]
) -> Dict[str, object]:
    """Zotero (and 茉莉花 through it) wins for every field it has a value for.

    A field Zotero leaves empty keeps the value MEFinder already had instead of
    being blanked, so e.g. a publication place read from the CIP page survives.
    """

    # Evidence and conflicts described the automatic recognition being replaced.
    stale = {"metadata_evidence", "metadata_conflicts", "metadata_status", "metadata_missing_fields"}
    merged: Dict[str, object] = {key: value for key, value in (existing or {}).items() if key not in stale}
    for key, value in zotero.items():
        if value not in (None, "") or merged.get(key) in (None, ""):
            merged[key] = value
    missing = metadata_missing_fields(merged)
    merged["metadata_missing_fields"] = missing
    merged["metadata_status"] = "complete" if not missing else "partial"
    return merged


def _existing_metadata(source: Optional[Mapping[str, object]]) -> Dict[str, object]:
    nested = (source or {}).get("bibliographic_metadata")
    return dict(nested) if isinstance(nested, Mapping) else {}


# ── snapshot ────────────────────────────────────────────────────────────────


@dataclass
class ZoteroSnapshot:
    """One complete read of the selected part of "My Library"."""

    collections: List[Dict[str, object]]
    effective: Set[str]
    items: Dict[str, Dict[str, object]]
    attachments: Dict[str, Dict[str, object]]
    attachment_index: Dict[str, Dict[str, object]]
    server_id: Optional[str]


class SyncPaused(RuntimeError):
    """The read was not trustworthy; nothing may be removed."""


def _count_mismatch(collections, effective, per_collection: Mapping[str, int]) -> bool:
    """True when a listing is empty although Zotero reports items in it."""

    for entry in collections:
        key = str(entry.get("key") or "")
        meta = entry.get("meta") if isinstance(entry.get("meta"), Mapping) else {}
        if key in effective and int(meta.get("numItems") or 0) > 0 and per_collection.get(key, 0) == 0:
            return True
    return False


def read_snapshot(
    client: ZoteroLocalClient,
    probe: ZoteroProbe,
    selected: Sequence[str],
    stored: StoredZoteroState,
) -> ZoteroSnapshot:
    """Read collections, in-scope items and their attachments.

    With Zotero 10+ local versions (``probe.local_versions`` and an unchanged
    server id) only new or changed objects are fetched as full JSON; the
    membership listings stay complete either way. Zotero 7 reports synced
    versions that miss local edits, so there everything is read in full.
    """

    try:
        collections = client.collections()
        if selected and not collections:
            raise SyncPaused("Zotero 没有返回任何分类")
        effective, missing = effective_collections(collections, selected)
        if missing:
            raise SyncPaused(f"有 {len(missing)} 个所选分类在 Zotero 中找不到，请重新选择")
        incremental = bool(probe.local_versions and stored.server_id == probe.server_id)
        items: Dict[str, Dict[str, object]] = {}
        per_collection: Dict[str, int] = {}
        if incremental:
            versions: Dict[str, int] = {}
            for key in sorted(effective):
                listed = client.collection_top_versions(key)
                per_collection[key] = len(listed)
                versions.update(listed)
            changed = [
                key for key, version in versions.items()
                if (stored.items.get(key) or {}).get("item_version") != version
            ]
            fetched = {str(entry["key"]): entry for entry in client.items_by_key(changed)} if changed else {}
            for key, version in versions.items():
                if key in fetched:
                    if not _data(fetched[key]).get("deleted"):
                        items[key] = {"version": version, "data": trim_item(fetched[key])}
                    continue
                previous = stored.items[key]
                data = json.loads(str(previous.get("data_json") or "{}"))
                data["collections"] = json.loads(str(previous.get("collections_json") or "[]"))
                items[key] = {"version": version, "data": data}
        else:
            for key in sorted(effective):
                listed = client.collection_top_items(key)
                per_collection[key] = len(listed)
                for entry in listed:
                    if not _data(entry).get("deleted"):
                        items[str(entry["key"])] = {"version": int(entry.get("version") or 0), "data": trim_item(entry)}
        if _count_mismatch(collections, effective, per_collection):
            raise SyncPaused("Zotero 返回的条目列表与分类计数不一致")

        index: Dict[str, Dict[str, object]] = {}
        if incremental:
            versions = client.attachment_versions()
            stale = [
                key for key, version in versions.items()
                if (stored.attachment_index.get(key) or {}).get("attachment_version") != version
            ]
            fetched_records = {
                record["attachment_key"]: record
                for record in (attachment_record(entry) for entry in (client.items_by_key(stale) if stale else []))
            }
            for key in versions:
                record = fetched_records.get(key) or stored.attachment_index.get(key)
                if record is None:
                    raise SyncPaused("Zotero 附件列表读取不完整")
                index[key] = record
        else:
            for entry in client.attachments():
                record = attachment_record(entry)
                index[str(record["attachment_key"])] = record
    except ZoteroIncomplete as exc:
        raise SyncPaused(str(exc)) from exc

    attachments = {
        key: record for key, record in index.items()
        if not record.get("ignored") and record.get("parent_item_key") in items
    }
    return ZoteroSnapshot(
        collections=collections,
        effective=effective,
        items=items,
        attachments=attachments,
        # Zotero 7 versions cannot drive incremental reads, so keep no index.
        attachment_index=index if probe.local_versions else {},
        server_id=probe.server_id,
    )


# ── plan ────────────────────────────────────────────────────────────────────


@dataclass
class SyncPlan:
    new_attachments: List[str] = field(default_factory=list)
    changed_attachments: List[str] = field(default_factory=list)
    retry_attachments: List[str] = field(default_factory=list)
    metadata_items: List[str] = field(default_factory=list)
    removed_attachments: List[str] = field(default_factory=list)
    dropped_items: List[str] = field(default_factory=list)


def plan_sync(
    snapshot: ZoteroSnapshot,
    stored: StoredZoteroState,
    sources: Mapping[str, Mapping[str, object]],
    *,
    retry_failed: bool = False,
) -> SyncPlan:
    """Pure diff between a complete snapshot and the stored link table."""

    plan = SyncPlan()
    for key, record in snapshot.attachments.items():
        row = stored.attachments.get(key)
        if row is None:
            plan.new_attachments.append(key)
            continue
        status = row.get("status")
        source_id = row.get("source_file_id")
        if status == "unavailable" or (status == "failed" and retry_failed):
            plan.retry_attachments.append(key)
        elif status == "pending" and not row.get("import_job_id"):
            # A previous run sized itself to the import queue and left this one
            # out; it never became a job, so the next sync owns it.
            plan.retry_attachments.append(key)
        elif status == "linked" and source_id and source_id not in sources:
            # Deleted in MEFinder or lost in a rebuild, still in Zotero:
            # Zotero wins, bring it back.
            plan.retry_attachments.append(key)
        elif status == "linked" and row.get("file_signature") != record.get("file_signature"):
            plan.changed_attachments.append(key)
    # A pending import is left alone until it finishes; it is removed later.
    plan.removed_attachments = [
        key for key, row in stored.attachments.items()
        if key not in snapshot.attachments and row.get("status") != "pending"
    ]
    for key, value in snapshot.items.items():
        rows = [
            row for row in stored.attachments.values()
            if row.get("parent_item_key") == key and row.get("status") == "linked"
            and row.get("source_file_id") in sources
        ]
        if not rows:
            continue
        applied = (stored.items.get(key) or {}).get("metadata_fingerprint_applied")
        # A rebuild re-extracts metadata from the file; a manual edit made
        # after the last Zotero change is respected until Zotero changes again.
        lost = any(
            str(sources[str(row["source_file_id"])].get("metadata_source") or "") not in {*ZOTERO_METADATA_SOURCES, "manual"}
            for row in rows
        )
        if applied != item_fingerprint(value["data"]) or lost:
            plan.metadata_items.append(key)
    plan.dropped_items = [key for key in stored.items if key not in snapshot.items]
    return plan


def removal_targets(
    rows: Mapping[str, Mapping[str, object]], removed_keys: Iterable[str]
) -> Tuple[List[str], List[str]]:
    """Split removed attachment rows into (documents to delete, documents only unlinked).

    A document is deleted only when the sync imported it and no surviving
    Zotero attachment still points at it. Documents that were already in
    MEFinder before the sync linked them are never deleted, only unlinked.
    """

    removed = set(removed_keys)
    delete: List[str] = []
    unlink: List[str] = []
    by_source: Dict[str, List[str]] = {}
    for key, row in rows.items():
        source_id = str(row.get("source_file_id") or "")
        if source_id:
            by_source.setdefault(source_id, []).append(key)
    for source_id, keys in by_source.items():
        leaving = [key for key in keys if key in removed]
        if not leaving:
            continue
        surviving = [key for key in keys if key not in removed]
        if not surviving and any(rows[key].get("origin") == "imported" for key in leaving):
            delete.append(source_id)
        else:
            unlink.append(source_id)
    return delete, unlink


# ── service ─────────────────────────────────────────────────────────────────


@dataclass
class ZoteroSyncPorts:
    """Existing MEFinder pipelines the sync drives; nothing is re-implemented."""

    read_preferences: Callable[[], Mapping[str, object]]
    sources: Callable[[], Sequence[Mapping[str, object]]]
    import_files: Callable[[Sequence[Path]], Mapping[str, object]]
    job_status: Callable[[str], Optional[Mapping[str, object]]]
    remove_documents: Callable[[Sequence[str]], Mapping[str, object]]
    apply_metadata: Callable[[str, Mapping[str, object]], object]
    hash_file: Callable[[Path], str]
    parse_mode_label: Callable[[], str] = lambda: ""
    # How many imports the in-process queue would take right now; ``None``
    # means the capacity is unknown and every attachment is submitted at once.
    import_capacity: Callable[[], Optional[int]] = lambda: None
    # Re-enters an existing import job into the queue ("继续导入"); ``None``
    # means queue rejections are re-imported by the next sync instead.
    resume_job: Optional[Callable[[str], object]] = None


ClientFactory = Callable[[Optional[str]], ZoteroLocalClient]


def _default_client(server_id: Optional[str]) -> ZoteroLocalClient:
    return ZoteroLocalClient(server_id=server_id)


class ZoteroSyncService:
    """Run syncs one at a time and remember what the last run did."""

    def __init__(
        self,
        store: ZoteroSyncStore,
        ports: ZoteroSyncPorts,
        *,
        client_factory: ClientFactory = _default_client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._ports = ports
        self._client_factory = client_factory
        self._clock = clock
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._status: Dict[str, object] = {"phase": "idle", "rows": []}
        self._stop = threading.Event()
        self._scheduler: Optional[threading.Thread] = None
        self._last_run_started = 0.0

    def _settings(self) -> Tuple[bool, List[str], str]:
        prefs = self._ports.read_preferences()
        frequency = str(prefs.get("zotero_sync_frequency") or "launch")
        return (
            bool(prefs.get("zotero_sync_enabled")),
            [str(key) for key in prefs.get("zotero_sync_collections") or []],
            frequency if frequency in FREQUENCIES else "launch",
        )

    # ── read-only views ──

    def status(self) -> Dict[str, object]:
        # Polling the page is also the quickest moment to link finished
        # imports (and write their Zotero metadata); skipped while a run holds the lock.
        self.resolve_pending()
        stored = self._store.read()
        with self._state_lock:
            status = json.loads(json.dumps(self._status, ensure_ascii=False))
        for row in status.get("rows", []):
            job_id = row.pop("job_id", None)
            if not job_id or row.get("tone") != "busy":
                continue
            job = self._ports.job_status(str(job_id)) or {}
            if job.get("status") in _JOB_DONE:
                row.update(status_text="已导入", tone="ok")
            elif job.get("status") in _JOB_FAILED:
                if str(job.get("failure_stage") or "") == "queue":
                    row.update(status_text=QUEUE_FULL_TEXT, tone="warn")
                else:
                    row.update(status_text="解析失败，可在导入页重试", tone="warn")
            elif job.get("message"):
                row["status_text"] = str(job["message"]).rstrip("。…")
        counts: Dict[str, int] = {"linked": 0, "pending": 0, "unavailable": 0, "failed": 0}
        for row in stored.attachments.values():
            counts[str(row.get("status"))] = counts.get(str(row.get("status")), 0) + 1
        status.update(
            last_success_at=stored.last_success_at,
            last_attempt_at=stored.last_attempt_at,
            last_result=stored.last_result,
            synced_collections=stored.synced_collections,
            documents=counts,
        )
        return status

    def overview(self) -> Dict[str, object]:
        """Connection state, the collection tree and per-collection link counts."""

        client = self._client_factory(None)
        probe = client.probe()
        result: Dict[str, object] = {
            "connection": {
                "state": probe.state,
                "label": (
                    f"已连接 Zotero {probe.zotero_version}"
                    if probe.connected and probe.zotero_version
                    else _CONNECTION_TEXT.get(probe.state, probe.state)
                ),
                "message": probe.message,
            },
            "collections": [],
        }
        if not probe.connected:
            return result
        try:
            collections = client.collections()
        except (ZoteroUnavailable, ZoteroIncomplete) as exc:
            result["connection"] = {"state": "error", "label": "读取失败", "message": str(exc)}
            return result
        stored = self._store.read()
        documented = {
            str(row.get("parent_item_key"))
            for row in stored.attachments.values()
            if row.get("status") in {"linked", "pending"}
        }
        # An attachment that failed, or that throttling left without a job, still
        # needs another sync push; counting it as known would hide it behind
        # "已同步".
        awaiting = {
            str(row.get("parent_item_key")) for row in stored.attachments.values()
            if row.get("status") == "failed"
            or (row.get("status") == "pending" and not row.get("import_job_id"))
        }
        linked: Dict[str, int] = {}
        known: Dict[str, int] = {}
        for key, item in stored.items.items():
            if key in awaiting:
                continue
            for collection in json.loads(str(item.get("collections_json") or "[]")):
                known[collection] = known.get(collection, 0) + 1
                if key in documented:
                    linked[collection] = linked.get(collection, 0) + 1
        rows = collection_tree(collections)
        for row in rows:
            row["linked_count"] = linked.get(row["key"], 0)
            row["unsynced_count"] = max(0, row["item_count"] - known.get(row["key"], 0))
        result["collections"] = rows
        return result

    def preview(self, selected: Sequence[object]) -> Dict[str, object]:
        """How many documents a sync with ``selected`` would remove or unlink."""

        keys = [str(key) for key in selected if str(key or "").strip()]
        stored = self._store.read()
        try:
            collections = self._client_factory(None).collections() if keys else []
        except (ZoteroUnavailable, ZoteroIncomplete):
            return {"known": False, "remove_count": None, "unlink_count": None}
        effective, _missing = effective_collections(collections, keys)
        leaving = []
        for key, row in stored.attachments.items():
            item = stored.items.get(str(row.get("parent_item_key"))) or {}
            if not set(json.loads(str(item.get("collections_json") or "[]"))) & effective:
                leaving.append(key)
        delete, unlink = removal_targets(stored.attachments, leaving)
        return {"known": True, "remove_count": len(delete), "unlink_count": len(unlink)}

    # ── running ──

    def start_sync(self, trigger: str = "manual") -> Dict[str, object]:
        if self._run_lock.locked():
            return {"ok": True, "started": False, "already_running": True}
        threading.Thread(target=self._run_guarded, args=(trigger,), name="zotero-sync", daemon=True).start()
        return {"ok": True, "started": True, "already_running": False}

    def _run_guarded(self, trigger: str) -> None:
        try:
            self.run_sync(trigger)
        except Exception:  # noqa: BLE001 - keep the scheduler alive, surface in status
            logging.exception("zotero sync failed")
            self._finish("error", "同步出错，已停止，没有移除更多文献")

    def _set(self, **updates: object) -> None:
        with self._state_lock:
            self._status.update(updates)

    def _row(self, **row: object) -> None:
        with self._state_lock:
            self._status.setdefault("rows", []).append(row)

    def _finish(self, phase: str, message: str, result: Optional[Mapping[str, object]] = None) -> None:
        self._set(phase=phase, message=message, finished_at=_now())
        payload = {"phase": phase, "message": message, **(result or {})}
        try:
            self._store.write(state={"last_attempt_at": _now(), "last_result_json": json.dumps(payload, ensure_ascii=False)})
        except Exception:  # noqa: BLE001 - bookkeeping must not mask the run
            logging.exception("could not record zotero sync result")

    def run_sync(self, trigger: str = "manual") -> Dict[str, object]:
        """Run one sync synchronously and return the resulting status."""

        if not self._run_lock.acquire(blocking=False):
            return {"phase": "running", "already_running": True}
        try:
            self._last_run_started = self._clock()
            with self._state_lock:
                self._status = {"phase": "running", "trigger": trigger, "started_at": _now(), "rows": [], "message": "正在读取 Zotero"}
            enabled, selected, _frequency = self._settings()
            if not enabled:
                self._finish("idle", "Zotero 同步未开启")
                return self.status()
            self._resolve_pending(self._store.read())
            self._resume_queue_rejected(self._store.read())
            stored = self._store.read()
            # The probe pins the client to the current Zotero-Server-ID, so a
            # database switch in the middle of this run fails with 412.
            client = self._client_factory(None)
            probe = client.probe()
            if not probe.connected:
                self._finish("paused", f"暂停同步：{probe.message}")
                return self.status()
            if probe.server_id != stored.server_id:
                # Another Zotero database, or Zotero 7 ↔ 10: stored versions mean nothing.
                stored.attachment_index = {}
            try:
                snapshot = (
                    read_snapshot(client, probe, selected, stored)
                    if selected
                    else ZoteroSnapshot([], set(), {}, {}, {}, probe.server_id)
                )
            except (SyncPaused, ZoteroUnavailable) as exc:
                self._finish("paused", f"暂停同步：{exc}，没有移除任何文献")
                return self.status()
            result = self._apply(client, snapshot, stored, retry_failed=trigger == "manual")
            self._store.write(
                state={
                    "server_id": probe.server_id,
                    "synced_collections_json": json.dumps(sorted(snapshot.effective)),
                    "attachment_index_json": json.dumps(snapshot.attachment_index, ensure_ascii=False),
                    "last_success_at": _now(),
                }
            )
            labels = (("added", "新增"), ("linked", "关联"), ("metadata", "题录更新"), ("reparsed", "重新解析"), ("removed", "移除"), ("unavailable", "附件不可用"), ("deferred", "排队等待"))
            summary = "，".join(f"{label} {result[key]}" for key, label in labels if result.get(key)) or "没有变化"
            self._finish("done", summary, result)
            return self.status()
        finally:
            self._run_lock.release()

    # ── execution ──

    def _sources(self) -> Dict[str, Mapping[str, object]]:
        return {
            str(item.get("source_file_id")): item
            for item in self._ports.sources()
            if item.get("source_file_id")
        }

    @staticmethod
    def _by_hash(sources: Mapping[str, Mapping[str, object]]) -> Dict[str, str]:
        by_hash: Dict[str, str] = {}
        for source_id, source in sources.items():
            digest = str(source.get("sha256") or "").lower()
            if digest:
                by_hash.setdefault(digest, source_id)
        return by_hash

    def _apply(
        self,
        client: ZoteroLocalClient,
        snapshot: ZoteroSnapshot,
        stored: StoredZoteroState,
        *,
        retry_failed: bool,
    ) -> Dict[str, int]:
        sources = self._sources()
        plan = plan_sync(snapshot, stored, sources, retry_failed=retry_failed)
        result = dict.fromkeys(("added", "linked", "metadata", "reparsed", "removed", "unavailable", "deferred"), 0)
        names = {row["key"]: row["name"] for row in collection_tree(snapshot.collections)}
        rows: Dict[str, Dict[str, object]] = {key: dict(row) for key, row in stored.attachments.items()}
        metadata_items: Set[str] = set(plan.metadata_items)

        def title_of(item_key: str) -> str:
            data = (snapshot.items.get(item_key) or {}).get("data")
            if not data:
                data = json.loads(str((stored.items.get(item_key) or {}).get("data_json") or "{}"))
            return str(data.get("title") or item_key)

        def meta_of(record: Mapping[str, object], extra: str) -> str:
            item = snapshot.items.get(str(record.get("parent_item_key"))) or {}
            collection = next(
                (names[key] for key in (item.get("data") or {}).get("collections", []) if key in snapshot.effective and key in names),
                "",
            )
            fmt = str(record.get("format") or Path(str(record.get("file_name") or "")).suffix.lstrip(".")).upper()
            return " · ".join(part for part in (fmt, collection, extra) if part)

        self._store.write(items=[self._item_row(snapshot, stored, key) for key in snapshot.items])

        # 1. new, retried and changed attachments.
        by_hash = self._by_hash(sources)
        zotero_imported = {
            str(row.get("source_file_id")) for row in rows.values()
            if row.get("origin") == "imported" and row.get("source_file_id")
        }
        imports: List[Tuple[str, Path]] = []
        queued: Set[str] = set()
        retire: List[str] = []
        for key in [*plan.new_attachments, *plan.retry_attachments, *plan.changed_attachments]:
            record = snapshot.attachments[key]
            previous = rows.get(key) or {}
            job_id = str(previous.get("import_job_id") or "")
            if previous.get("status") == "failed" and job_id:
                job = self._ports.job_status(job_id)
                # A job that is still in the queue has finished pages and MinerU
                # quota worth resuming from the import page. A job rejected at
                # the queue parsed nothing, so there is nothing to preserve and
                # skipping it would strand the row on "没有变化".
                if job is not None and (
                    str(job.get("failure_stage") or "") != "queue"
                    or self._ports.resume_job is not None
                ):
                    continue
            row: Dict[str, object] = {**previous}
            for name in ("attachment_key", "parent_item_key", "attachment_version", "link_mode", "content_type", "file_name", "file_signature"):
                row[name] = record.get(name)
            row.setdefault("origin", "imported")
            rows[key] = row
            title = title_of(str(record["parent_item_key"]))
            old_source = str(previous.get("source_file_id") or "")
            was_linked = previous.get("status") == "linked" and old_source in sources
            try:
                path = client.attachment_file_path(key)
            except ZoteroUnavailable as exc:
                row.update(status=previous.get("status") or "unavailable", status_message=str(exc))
                if was_linked:
                    row["file_signature"] = previous.get("file_signature")
                continue
            if path is None or not path.is_file():
                # A missing file is never a deletion; a linked document stays.
                row["status_message"] = "附件不可用"
                if was_linked:
                    row.update(status="linked", file_signature=previous.get("file_signature"))
                else:
                    row["status"] = "unavailable"
                result["unavailable"] += 1
                self._row(action="不可用", title=title, meta=meta_of(record, "Zotero 里找不到文件"), status_text="附件不可用", tone="warn")
                continue
            digest = self._ports.hash_file(path).lower()
            if was_linked and digest == str(previous.get("file_sha256") or "").lower():
                row.update(status="linked", status_message=None)
                continue
            existing = by_hash.get(digest)
            if existing:
                row.update(
                    status="linked",
                    source_file_id=existing,
                    file_sha256=digest,
                    origin="imported" if existing in zotero_imported else "linked_existing",
                    import_job_id=None,
                    status_message=None,
                )
                if was_linked and old_source != existing:
                    retire.append(old_source)
                metadata_items.add(str(record["parent_item_key"]))
                result["linked"] += 1
                self._row(action="关联", title=title, meta=meta_of(record, "文库中已有相同文件"), status_text="已关联，未重新解析", tone="ok")
                continue
            row.update(
                status="pending",
                file_sha256=digest,
                origin="imported",
                status_message=None,
                replaces_source_file_id=old_source if was_linked else None,
                reparse=was_linked,
            )
            if digest not in queued:  # the same file attached twice imports once
                queued.add(digest)
                imports.append((key, path))

        deferred: List[Tuple[str, Path]] = []
        cursor = 0
        while cursor < len(imports):
            room = self._ports.import_capacity()
            if room is not None and room < 1:
                deferred = imports[cursor:]
                break
            limit = IMPORT_BATCH if room is None else min(IMPORT_BATCH, int(room))
            batch = imports[cursor:cursor + limit]
            cursor += len(batch)
            try:
                response = self._ports.import_files([path for _key, path in batch])
            except Exception as exc:  # noqa: BLE001 - one failed batch must not stop the rest
                logging.exception("zotero import batch failed")
                response = {"jobs": [], "errors": [{"path": str(path), "error": str(exc)} for _key, path in batch]}
            jobs = {str(job.get("path")): job for job in response.get("jobs") or []}
            errors = {str(error.get("path")): error for error in response.get("errors") or []}
            for key, path in batch:
                record = snapshot.attachments[key]
                reparse = bool(rows[key].get("reparse"))
                action = "重新解析" if reparse else "新增"
                job = jobs.get(str(path))
                if job and job.get("job_id"):
                    rows[key]["import_job_id"] = str(job["job_id"])
                    result["reparsed" if reparse else "added"] += 1
                    route = self._ports.parse_mode_label() if record.get("format") == "pdf" else "文本通道"
                    self._row(action=action, title=title_of(str(record["parent_item_key"])), meta=meta_of(record, route), status_text="排队解析", tone="busy", job_id=job["job_id"])
                else:
                    message = str((errors.get(str(path)) or {}).get("error") or "导入失败")
                    rows[key].update(status="failed", status_message=message)
                    self._row(action=action, title=title_of(str(record["parent_item_key"])), meta=meta_of(record, ""), status_text=message, tone="warn")

        # The import queue is bounded, so a batch larger than its free slots
        # would come back as a queue rejection. Those attachments stay pending
        # with no job and the next sync picks them up.
        for key, _path in deferred:
            record = snapshot.attachments[key]
            rows[key]["import_job_id"] = None
            rows[key]["status_message"] = QUEUE_FULL_TEXT
            result["deferred"] += 1
            route = self._ports.parse_mode_label() if record.get("format") == "pdf" else "文本通道"
            self._row(
                action="重新解析" if rows[key].get("reparse") else "新增",
                title=title_of(str(record["parent_item_key"])),
                meta=meta_of(record, route),
                status_text=QUEUE_FULL_TEXT,
                tone="warn",
            )
        for row in rows.values():
            row.pop("reparse", None)

        # 2. removals: attachments gone from Zotero or out of the selection,
        #    plus documents replaced by an already-present copy.
        removed_rows = set(plan.removed_attachments)
        delete, unlink = removal_targets(rows, removed_rows)
        still_linked = {str(row.get("source_file_id")) for key, row in rows.items() if key not in removed_rows}
        delete += [source_id for source_id in dict.fromkeys(retire) if source_id not in still_linked and source_id in zotero_imported]
        delete = [source_id for source_id in dict.fromkeys(delete) if source_id in sources]
        failed_delete: Set[str] = set()
        if delete:
            try:
                outcome = self._ports.remove_documents(delete)
                removed_ids = set(outcome.get("removed_source_ids") or delete)
            except Exception as exc:  # noqa: BLE001 - keep the rows, retry next sync
                logging.exception("zotero sync removal failed")
                removed_ids = set()
                self._row(action="移除", title=f"{len(delete)} 篇文献", meta="", status_text=f"移除失败：{exc}", tone="warn")
            failed_delete = set(delete) - removed_ids
            result["removed"] = len(removed_ids)
        removed_rows = {key for key in removed_rows if str(rows[key].get("source_file_id") or "") not in failed_delete}
        for key in sorted(removed_rows):
            row = rows[key]
            source_id = str(row.get("source_file_id") or "")
            label = str(Path(str(row.get("file_name") or "")).suffix.lstrip(".")).upper()
            if source_id in delete:
                self._row(action="移除", title=title_of(str(row.get("parent_item_key"))), meta=" · ".join(part for part in (label, "Zotero 中已删除或移出所选分类") if part), status_text="已移除，含对齐记录", tone="warn")
            elif source_id in unlink:
                self._row(action="解除关联", title=title_of(str(row.get("parent_item_key"))), meta="同步前已在文库中，保留文献", status_text="已解除关联", tone="muted")

        # 3. metadata for items whose linked documents are ready.
        applied: Dict[str, str] = {}
        for item_key in sorted(metadata_items):
            value = snapshot.items.get(item_key)
            targets = [
                str(row["source_file_id"]) for key, row in rows.items()
                if row.get("parent_item_key") == item_key and row.get("status") == "linked"
                and key not in removed_rows and row.get("source_file_id") in sources
            ]
            if value is None or not targets:
                continue
            metadata = zotero_metadata(value["data"])
            try:
                for source_id in dict.fromkeys(targets):
                    self._ports.apply_metadata(source_id, merge_metadata(_existing_metadata(sources.get(source_id)), metadata))
            except Exception as exc:  # noqa: BLE001 - retried at the next sync
                logging.exception("zotero metadata update failed")
                self._row(action="题录", title=title_of(item_key), meta="", status_text=f"题录更新失败：{exc}", tone="warn")
                continue
            applied[item_key] = item_fingerprint(value["data"])
            result["metadata"] += 1
            if (stored.items.get(item_key) or {}).get("metadata_fingerprint_applied"):
                self._row(action="题录", title=title_of(item_key), meta="Zotero 题录有改动", status_text="已更新，未重新解析", tone="ok")

        kept_parents = {str(row.get("parent_item_key")) for key, row in rows.items() if key not in removed_rows}
        self._store.write(
            items=[{**self._item_row(snapshot, stored, key), "metadata_fingerprint_applied": fingerprint} for key, fingerprint in applied.items()],
            deleted_items=[key for key in plan.dropped_items if key not in kept_parents],
            attachments=[row for key, row in rows.items() if key not in removed_rows],
            deleted_attachments=sorted(removed_rows),
        )
        return result

    @staticmethod
    def _item_row(snapshot: ZoteroSnapshot, stored: StoredZoteroState, key: str) -> Dict[str, object]:
        value = snapshot.items[key]
        return {
            "item_key": key,
            "item_version": value["version"],
            "fingerprint": item_fingerprint(value["data"]),
            "data_json": json.dumps({k: v for k, v in value["data"].items() if k != "collections"}, ensure_ascii=False),
            "collections_json": json.dumps(value["data"].get("collections", [])),
            "metadata_fingerprint_applied": (stored.items.get(key) or {}).get("metadata_fingerprint_applied"),
        }

    def resolve_pending(self) -> int:
        """Link finished imports to their documents; safe to call any time."""

        if not self._run_lock.acquire(blocking=False):
            return 0
        try:
            return self._resolve_pending(self._store.read())
        finally:
            self._run_lock.release()

    def resume_queue_rejected(self) -> int:
        """Put jobs the import queue turned away back in line; safe to call any time."""

        if not self._run_lock.acquire(blocking=False):
            return 0
        try:
            return self._resume_queue_rejected(self._store.read())
        finally:
            self._run_lock.release()

    def _resume_queue_rejected(self, stored: StoredZoteroState) -> int:
        # Resuming keeps the job, its copied file and any finished pages, so
        # it needs neither Zotero nor a full sync, and never duplicates a job.
        if self._ports.resume_job is None:
            return 0
        updated: List[Dict[str, object]] = []
        for row in stored.attachments.values():
            job_id = str(row.get("import_job_id") or "")
            if row.get("status") not in {"pending", "failed"} or not job_id:
                continue
            job = self._ports.job_status(job_id) or {}
            if job.get("status") not in _JOB_FAILED or str(job.get("failure_stage") or "") != "queue":
                continue
            room = self._ports.import_capacity()
            if room is not None and room < 1:
                break
            try:
                self._ports.resume_job(job_id)
            except Exception:  # noqa: BLE001 - still rejected; the next tick tries again
                logging.info("zotero import %s still waiting for the queue", job_id)
                break
            updated.append({**row, "status": "pending", "status_message": None})
        if updated:
            self._store.write(attachments=updated)
        return len(updated)

    def _backfill_due(self) -> bool:
        """Whether a sync left attachments outside a queue that now has room."""

        if self._clock() - self._last_run_started < BACKFILL_SECONDS:
            return False
        if not any(
            row.get("status") == "pending" and not row.get("import_job_id")
            for row in self._store.read().attachments.values()
        ):
            return False
        room = self._ports.import_capacity()
        return room is None or room >= 1

    def _resolve_pending(self, stored: StoredZoteroState) -> int:
        waiting = {key: row for key, row in stored.attachments.items() if row.get("status") in {"pending", "failed"}}
        if not waiting:
            return 0
        sources = self._sources()
        by_hash = self._by_hash(sources)
        updated: List[Dict[str, object]] = []
        replaced: List[str] = []
        for row in waiting.values():
            job_id = str(row.get("import_job_id") or "")
            job = self._ports.job_status(job_id) if job_id else None
            job_state = str((job or {}).get("status") or "")
            if job and job_state not in _JOB_DONE | _JOB_FAILED:
                continue  # still parsing; the document may not be final yet
            source_id = by_hash.get(str(row.get("file_sha256") or "").lower())
            if source_id:
                old = str(row.get("replaces_source_file_id") or "")
                updated.append({**row, "status": "linked", "source_file_id": source_id, "import_job_id": None, "replaces_source_file_id": None, "status_message": None})
                if old and old != source_id:
                    replaced.append(old)
            elif job_state in _JOB_FAILED and row.get("status") != "failed":
                updated.append({**row, "status": "failed", "status_message": str((job or {}).get("message") or "解析失败")})
            elif job_state in _JOB_DONE:
                updated.append({**row, "status": "failed", "status_message": "导入已完成但未找到对应文献"})
        if not updated:
            return 0
        rows = {**stored.attachments, **{str(row["attachment_key"]): row for row in updated}}
        referenced = {str(row.get("source_file_id")) for row in rows.values() if row.get("source_file_id")}
        stale = [source_id for source_id in dict.fromkeys(replaced) if source_id not in referenced and source_id in sources]
        if stale:
            try:
                self._ports.remove_documents(stale)
            except Exception:  # noqa: BLE001 - the old version stays searchable
                logging.exception("could not remove replaced Zotero document")
        applied_items = []
        for item_key in {str(row.get("parent_item_key")) for row in updated if row.get("status") == "linked"}:
            item = stored.items.get(item_key)
            if not item:
                continue
            metadata = zotero_metadata(json.loads(str(item.get("data_json") or "{}")))
            try:
                for row in updated:
                    if row.get("parent_item_key") == item_key and row.get("status") == "linked":
                        source_id = str(row["source_file_id"])
                        self._ports.apply_metadata(source_id, merge_metadata(_existing_metadata(sources.get(source_id)), metadata))
            except Exception:  # noqa: BLE001 - retried at the next sync
                logging.exception("zotero metadata update failed")
                continue
            applied_items.append({**item, "metadata_fingerprint_applied": item.get("fingerprint")})
        self._store.write(attachments=updated, items=applied_items)
        return len(updated)

    # ── scheduler ──

    def start_scheduler(self, *, startup_delay: float = 5.0, tick: float = 60.0) -> None:
        """Launch-time sync and the 30-minute cadence; idle when sync is off."""

        if self._scheduler is not None:
            return

        def loop() -> None:
            if self._stop.wait(startup_delay):
                return
            try:
                enabled, _selected, frequency = self._settings()
                if enabled and frequency in {"launch", "interval"}:
                    self._run_guarded("launch")
            except Exception:  # noqa: BLE001 - the loop must survive
                logging.exception("zotero launch sync failed")
            while not self._stop.wait(tick):
                try:
                    enabled, _selected, frequency = self._settings()
                    if not enabled:
                        continue
                    if frequency == "interval" and self._clock() - self._last_run_started >= INTERVAL_SECONDS:
                        self._run_guarded("interval")
                    elif self._backfill_due():
                        # Finishes a run the queue cut short; failures stay put.
                        self._run_guarded("backfill")
                    else:
                        self.resume_queue_rejected()
                        self.resolve_pending()
                except Exception:  # noqa: BLE001 - the loop must survive
                    logging.exception("zotero scheduler tick failed")

        self._scheduler = threading.Thread(target=loop, name="zotero-sync-scheduler", daemon=True)
        self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()

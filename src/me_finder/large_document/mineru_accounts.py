"""Private configuration service for independent MinerU cloud accounts."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..mineru_api import MinerUError, normalize_mineru_token
from .credential_pool import CredentialPool
from .job_ledger import JobLedger


MINERU_CLOUD_PROVIDER_ID = "mineru-cloud"
MINERU_ACCOUNT_CONFIG_VERSION = 1
MINERU_ACCOUNT_SECRET_PREFIX = "mineru-account:"
DEFAULT_MINERU_ACCOUNT_CONFIG_PATH = Path("config/mineru_accounts.local.json")
_ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class MinerUAccountConfigError(MinerUError):
    """The private multi-account config is missing, damaged, or invalid."""


@dataclass(frozen=True)
class MinerUAccountSummary:
    account_id: str
    display_name: str
    enabled: bool
    configured: bool
    current_in_flight: int
    max_concurrency_override: Optional[int]
    health_status: str
    cooldown_until: Optional[str]
    last_401_at: Optional[str]
    last_429_at: Optional[str]
    expires_at: Optional[str]

    def to_dict(self) -> Dict[str, object]:
        """Return a UI/API-safe object that never contains a token or reference."""

        return asdict(self)


@dataclass(frozen=True)
class MinerUBookUsage:
    document_job_id: str
    document_id: str
    source_file_id: str
    source_file_name: str
    parsed_page_count: int
    page_ranges: Tuple[Tuple[int, int], ...]
    completed_at: str


@dataclass(frozen=True)
class MinerUCredentialUsageStatistics:
    account_id: str
    display_name: str
    parsed_book_count: int
    parsed_page_count: int
    books: Tuple[MinerUBookUsage, ...]


@dataclass(frozen=True)
class MinerUUsageStatistics:
    provider_id: str
    parsed_book_count: int
    parsed_page_count: int
    credentials: Tuple[MinerUCredentialUsageStatistics, ...]

    def to_dict(self) -> Dict[str, object]:
        """Return a separate, secret-free settings statistics payload."""

        return asdict(self)


def resolve_mineru_accounts_path(root: Optional[Path] = None) -> Path:
    override = os.environ.get("ME_FINDER_MINERU_ACCOUNTS_CONFIG", "").strip()
    if override:
        return Path(override)
    if root is not None:
        return Path(root) / DEFAULT_MINERU_ACCOUNT_CONFIG_PATH
    return DEFAULT_MINERU_ACCOUNT_CONFIG_PATH


class MinerUAccountService:
    """Save N independent accounts and expose separate local attribution stats."""

    def __init__(self, *, ledger: JobLedger, config_path: Path) -> None:
        self.ledger = ledger
        self.config_path = Path(config_path)
        self._lock = threading.RLock()

    def save_account(
        self,
        *,
        account_id: Optional[str] = None,
        display_name: str,
        token: Optional[str] = None,
        enabled: bool = True,
        max_concurrency_override: Optional[int] = None,
        expires_at: Optional[str] = None,
    ) -> MinerUAccountSummary:
        """Create or update one independent MinerU account.

        An empty token on an existing account preserves the stored token, which
        lets a settings screen edit the label without redisplaying a
        secret.  New accounts always require a token.
        """

        with self._lock:
            normalized_id = _normalize_account_id(account_id)
            normalized_name = str(display_name or "").strip()
            if not normalized_name or len(normalized_name) > 120:
                raise MinerUAccountConfigError("账号名称必须为 1–120 个字符。")
            if (
                max_concurrency_override is not None
                and int(max_concurrency_override) < 1
            ):
                raise MinerUAccountConfigError("账号并发数必须大于 0。")
            original = self._load_private_config()
            accounts = dict(original["accounts"])
            existing = accounts.get(normalized_id)
            if existing is not None and not isinstance(existing, dict):
                raise MinerUAccountConfigError("本地 MinerU 账号配置已损坏。")
            normalized_expiry = (
                str(existing.get("expires_at") or "") or None
                if expires_at is None and isinstance(existing, dict)
                else _normalize_expiry(expires_at)
            )
            raw_token = str(token or "").strip()
            if raw_token:
                stored_token = normalize_mineru_token(raw_token)
            elif isinstance(existing, dict) and existing.get("token"):
                stored_token = normalize_mineru_token(existing["token"])
            else:
                raise MinerUAccountConfigError("新增 MinerU 账号必须填写 Token。")

            accounts[normalized_id] = {
                "token": stored_token,
                "expires_at": normalized_expiry,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            updated = {
                "schema_version": MINERU_ACCOUNT_CONFIG_VERSION,
                "accounts": accounts,
            }
            self._write_private_config(updated)
            try:
                self.ledger.upsert_credential(
                    credential_id=normalized_id,
                    provider_id=MINERU_CLOUD_PROVIDER_ID,
                    display_name=normalized_name,
                    secret_ref=f"{MINERU_ACCOUNT_SECRET_PREFIX}{normalized_id}",
                    enabled=bool(enabled),
                    max_concurrency_override=(
                        int(max_concurrency_override)
                        if max_concurrency_override is not None
                        else None
                    ),
                )
            except Exception:
                # Do not leave a secret that the credential ledger never accepted.
                self._write_private_config(original)
                raise
            return self.get_account(normalized_id)

    def get_account(self, account_id: str) -> MinerUAccountSummary:
        normalized_id = _normalize_account_id(account_id)
        for item in self.list_accounts():
            if item.account_id == normalized_id:
                return item
        raise KeyError(normalized_id)

    def delete_account(self, account_id: str) -> None:
        """Remove one idle account and its locally stored Token."""

        normalized_id = _normalize_account_id(account_id)
        with self._lock:
            original = self._load_private_config()
            accounts = dict(original["accounts"])
            self.ledger.get_credential(normalized_id)
            accounts.pop(normalized_id, None)
            updated = {
                "schema_version": MINERU_ACCOUNT_CONFIG_VERSION,
                "accounts": accounts,
            }
            self._write_private_config(updated)
            try:
                deleted = self.ledger.delete_credential(
                    normalized_id,
                    MINERU_CLOUD_PROVIDER_ID,
                )
                if not deleted:
                    raise MinerUAccountConfigError(
                        "该账号仍有未完成的解析任务，完成或取消任务后才能删除。"
                    )
            except Exception:
                self._write_private_config(original)
                raise

    def private_config_exists(self) -> bool:
        return self.config_path.is_file()

    def list_accounts(self) -> list[MinerUAccountSummary]:
        with self._lock:
            private = self._load_private_config()
            secret_accounts = private["accounts"]
            summaries = []
            for record in self.ledger.list_credentials(MINERU_CLOUD_PROVIDER_ID):
                secret = secret_accounts.get(record.id)
                configured = bool(isinstance(secret, dict) and secret.get("token"))
                expires_at = (
                    str(secret.get("expires_at") or "") or None
                    if isinstance(secret, dict)
                    else None
                )
                summaries.append(
                    MinerUAccountSummary(
                        account_id=record.id,
                        display_name=record.display_name,
                        enabled=record.is_enabled,
                        configured=configured,
                        current_in_flight=record.current_in_flight,
                        max_concurrency_override=record.max_concurrency_override,
                        health_status=record.health_status,
                        cooldown_until=record.cooldown_until,
                        last_401_at=record.last_401_at,
                        last_429_at=record.last_429_at,
                        expires_at=expires_at,
                    )
                )
            return summaries

    def usage_statistics(self) -> MinerUUsageStatistics:
        """Aggregate successful local book/page attribution by credential.

        This is deliberately separate from account configuration.  It neither
        reads MinerU's website nor participates in credential eligibility.
        """

        records = self.ledger.list_credentials(MINERU_CLOUD_PROVIDER_ID)
        grouped: Dict[str, Dict[str, object]] = {
            record.id: {
                "display_name": record.display_name,
                "books": {},
            }
            for record in records
        }
        for item in self.ledger.list_credential_page_attributions(
            MINERU_CLOUD_PROVIDER_ID
        ):
            credential = grouped.setdefault(
                item.credential_id,
                {"display_name": item.display_name, "books": {}},
            )
            books = credential["books"]
            if not isinstance(books, dict):
                raise RuntimeError("invalid local credential statistics state")
            book = books.setdefault(
                item.document_job_id,
                {
                    "document_id": item.document_id,
                    "source_file_id": item.source_file_id,
                    "source_file_name": Path(item.source_path).name,
                    "ranges": [],
                    "completed_at": item.completed_at,
                },
            )
            ranges = book["ranges"]
            if not isinstance(ranges, list):
                raise RuntimeError("invalid local book statistics state")
            ranges.append((item.page_start, item.page_end))
            book["completed_at"] = max(
                str(book["completed_at"]), item.completed_at
            )

        credentials: List[MinerUCredentialUsageStatistics] = []
        all_document_jobs = set()
        for account_id in sorted(grouped):
            group = grouped[account_id]
            raw_books = group["books"]
            if not isinstance(raw_books, dict):
                raise RuntimeError("invalid local credential statistics state")
            books: List[MinerUBookUsage] = []
            for document_job_id, raw_book in raw_books.items():
                ranges = _merge_page_ranges(raw_book["ranges"])
                parsed_pages = sum(end - start + 1 for start, end in ranges)
                books.append(
                    MinerUBookUsage(
                        document_job_id=str(document_job_id),
                        document_id=str(raw_book["document_id"]),
                        source_file_id=str(raw_book["source_file_id"]),
                        source_file_name=str(raw_book["source_file_name"]),
                        parsed_page_count=parsed_pages,
                        page_ranges=ranges,
                        completed_at=str(raw_book["completed_at"]),
                    )
                )
                all_document_jobs.add(str(document_job_id))
            ordered_books = tuple(
                sorted(
                    books,
                    key=lambda item: (item.completed_at, item.document_job_id),
                    reverse=True,
                )
            )
            credentials.append(
                MinerUCredentialUsageStatistics(
                    account_id=account_id,
                    display_name=str(group["display_name"]),
                    parsed_book_count=len(ordered_books),
                    parsed_page_count=sum(
                        item.parsed_page_count for item in ordered_books
                    ),
                    books=ordered_books,
                )
            )
        return MinerUUsageStatistics(
            provider_id=MINERU_CLOUD_PROVIDER_ID,
            parsed_book_count=len(all_document_jobs),
            parsed_page_count=sum(item.parsed_page_count for item in credentials),
            credentials=tuple(credentials),
        )

    def resolve_secret(self, secret_ref: str) -> str:
        reference = str(secret_ref or "")
        if not reference.startswith(MINERU_ACCOUNT_SECRET_PREFIX):
            raise MinerUAccountConfigError("MinerU credential reference is invalid.")
        account_id = _normalize_account_id(
            reference[len(MINERU_ACCOUNT_SECRET_PREFIX) :]
        )
        with self._lock:
            private = self._load_private_config()
            account = private["accounts"].get(account_id)
            if not isinstance(account, dict) or not account.get("token"):
                raise MinerUAccountConfigError(
                    f"MinerU account {account_id} has no configured token."
                )
            return normalize_mineru_token(account["token"])

    def create_pool(
        self,
        *,
        provider_max_concurrency: int,
        rate_limit_cooldown_seconds: int = 60,
    ) -> CredentialPool:
        return CredentialPool(
            ledger=self.ledger,
            provider_id=MINERU_CLOUD_PROVIDER_ID,
            secret_resolver=self.resolve_secret,
            provider_max_concurrency=provider_max_concurrency,
            rate_limit_cooldown_seconds=rate_limit_cooldown_seconds,
        )

    def _load_private_config(self) -> Dict[str, object]:
        if not self.config_path.exists():
            return {
                "schema_version": MINERU_ACCOUNT_CONFIG_VERSION,
                "accounts": {},
            }
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MinerUAccountConfigError(
                "本地 MinerU 多账号配置无法读取。"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != MINERU_ACCOUNT_CONFIG_VERSION
            or not isinstance(payload.get("accounts"), dict)
        ):
            raise MinerUAccountConfigError("本地 MinerU 多账号配置格式无效。")
        return {
            "schema_version": MINERU_ACCOUNT_CONFIG_VERSION,
            "accounts": dict(payload["accounts"]),
        }

    def _write_private_config(self, payload: Dict[str, object]) -> None:
        target = self.config_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        descriptor = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                descriptor = None
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


def _normalize_account_id(value: Optional[str]) -> str:
    account_id = str(value or f"mineru-{uuid.uuid4().hex[:12]}").strip()
    if not _ACCOUNT_ID_PATTERN.fullmatch(account_id):
        raise MinerUAccountConfigError(
            "MinerU account id must use 1-64 letters, digits, dots, dashes, or underscores."
        )
    return account_id


def _normalize_expiry(value: Optional[str]) -> Optional[str]:
    expires_at = str(value or "").strip()
    if not expires_at:
        return None
    try:
        datetime.strptime(expires_at, "%Y-%m-%d")
    except ValueError as exc:
        raise MinerUAccountConfigError("到期日期请使用 YYYY-MM-DD 格式。") from exc
    return expires_at


def _merge_page_ranges(
    ranges: List[Tuple[int, int]],
) -> Tuple[Tuple[int, int], ...]:
    merged: List[List[int]] = []
    for start, end in sorted((int(start), int(end)) for start, end in ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)

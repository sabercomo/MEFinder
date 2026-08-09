"""Private configuration service for independent MinerU cloud accounts."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from ..mineru_api import MinerUError, normalize_mineru_token
from .credential_pool import CredentialPool
from .job_ledger import JobLedger


MINERU_CLOUD_PROVIDER_ID = "mineru-cloud"
MINERU_ACCOUNT_CONFIG_VERSION = 1
MINERU_ACCOUNT_SECRET_PREFIX = "mineru-account:"
DEFAULT_MINERU_ACCOUNT_CONFIG_PATH = Path("config/mineru_accounts.local.json")
DEFAULT_MINERU_DAILY_PAGE_BUDGET = 1000
_ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class MinerUAccountConfigError(MinerUError):
    """The private multi-account config is missing, damaged, or invalid."""


@dataclass(frozen=True)
class MinerUAccountSummary:
    account_id: str
    display_name: str
    enabled: bool
    configured: bool
    daily_page_budget: Optional[int]
    local_pages_used_today: int
    local_pages_remaining_today: Optional[int]
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


def resolve_mineru_accounts_path(root: Optional[Path] = None) -> Path:
    override = os.environ.get("ME_FINDER_MINERU_ACCOUNTS_CONFIG", "").strip()
    if override:
        return Path(override)
    if root is not None:
        return Path(root) / DEFAULT_MINERU_ACCOUNT_CONFIG_PATH
    return DEFAULT_MINERU_ACCOUNT_CONFIG_PATH


class MinerUAccountService:
    """Save N independent accounts and expose their local scheduler usage."""

    def __init__(self, *, ledger: JobLedger, config_path: Path) -> None:
        self.ledger = ledger
        self.config_path = Path(config_path)

    def save_account(
        self,
        *,
        account_id: Optional[str] = None,
        display_name: str,
        token: Optional[str] = None,
        enabled: bool = True,
        daily_page_budget: Optional[int] = DEFAULT_MINERU_DAILY_PAGE_BUDGET,
        max_concurrency_override: Optional[int] = None,
        expires_at: Optional[str] = None,
    ) -> MinerUAccountSummary:
        """Create or update one independent MinerU account.

        An empty token on an existing account preserves the stored token, which
        lets a settings screen edit the label or budget without redisplaying a
        secret.  New accounts always require a token.
        """

        normalized_id = _normalize_account_id(account_id)
        normalized_name = str(display_name or "").strip()
        if not normalized_name or len(normalized_name) > 120:
            raise MinerUAccountConfigError("账号名称必须为 1–120 个字符。")
        if daily_page_budget is not None and int(daily_page_budget) < 0:
            raise MinerUAccountConfigError("每日页数预算不能为负数。")
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
                daily_page_budget=(
                    int(daily_page_budget)
                    if daily_page_budget is not None
                    else None
                ),
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

    def list_accounts(self) -> list[MinerUAccountSummary]:
        private = self._load_private_config()
        secret_accounts = private["accounts"]
        today = datetime.now(timezone.utc).date().isoformat()
        summaries = []
        for record in self.ledger.list_credentials(MINERU_CLOUD_PROVIDER_ID):
            secret = secret_accounts.get(record.id)
            configured = bool(isinstance(secret, dict) and secret.get("token"))
            expires_at = (
                str(secret.get("expires_at") or "") or None
                if isinstance(secret, dict)
                else None
            )
            used = record.pages_used_today if record.usage_date == today else 0
            remaining = (
                max(0, record.daily_page_budget - used)
                if record.daily_page_budget is not None
                else None
            )
            summaries.append(
                MinerUAccountSummary(
                    account_id=record.id,
                    display_name=record.display_name,
                    enabled=record.is_enabled,
                    configured=configured,
                    daily_page_budget=record.daily_page_budget,
                    local_pages_used_today=used,
                    local_pages_remaining_today=remaining,
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

    def resolve_secret(self, secret_ref: str) -> str:
        reference = str(secret_ref or "")
        if not reference.startswith(MINERU_ACCOUNT_SECRET_PREFIX):
            raise MinerUAccountConfigError("MinerU credential reference is invalid.")
        account_id = _normalize_account_id(
            reference[len(MINERU_ACCOUNT_SECRET_PREFIX) :]
        )
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

"""Authorized N-credential scheduling with durable remote-task affinity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence

from ..parser_provider import ParserCredential, ParserProviderError
from .job_ledger import CredentialRecord, JobLedger


SecretResolver = Callable[[str], str]


class CredentialPoolUnavailable(RuntimeError):
    """No configured credential is currently eligible; work remains queued."""


@dataclass(frozen=True)
class CredentialLease:
    credential: ParserCredential
    page_count: int


@dataclass(frozen=True)
class CredentialPlan:
    assignments: Sequence[Optional[str]]
    pages_by_credential: Dict[str, int]
    unassigned_pages: int


class CredentialPool:
    def __init__(
        self,
        *,
        ledger: JobLedger,
        provider_id: str,
        secret_resolver: SecretResolver,
        provider_max_concurrency: int,
        rate_limit_cooldown_seconds: int = 60,
    ) -> None:
        self.ledger = ledger
        self.provider_id = provider_id
        self.secret_resolver = secret_resolver
        self.provider_max_concurrency = max(1, int(provider_max_concurrency))
        self.rate_limit_cooldown_seconds = max(1, int(rate_limit_cooldown_seconds))

    def acquire(self, page_count: int) -> CredentialLease:
        """Reserve one credential for a not-yet-submitted slice."""

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        today = now.date().isoformat()
        records = sorted(
            self.ledger.list_credentials(self.provider_id),
            key=lambda item: (
                item.pages_used_today if item.usage_date == today else 0,
                item.current_in_flight,
                item.id,
            ),
        )
        for record in records:
            reserved = self.ledger.try_reserve_credential(
                record.id,
                page_count=page_count,
                provider_max_concurrency=self.provider_max_concurrency,
                today=today,
                now_iso=now_iso,
            )
            if reserved is None:
                continue
            try:
                secret = self.secret_resolver(reserved.secret_ref)
                if not secret:
                    raise ValueError("credential secret reference resolved empty")
            except Exception:
                self.ledger.release_credential(
                    reserved.id,
                    reserved_pages=page_count,
                    refund_budget=True,
                    today=today,
                )
                self.ledger.update_credential_health(
                    reserved.id,
                    health_status="secret_unavailable",
                )
                continue
            return CredentialLease(
                credential=ParserCredential(
                    credential_id=reserved.id,
                    secret=secret,
                    metadata={"display_name": reserved.display_name},
                ),
                page_count=page_count,
            )
        raise CredentialPoolUnavailable(
            "all configured parser credentials are disabled, cooling down, busy, "
            "or over their configured budget"
        )

    def credential_for_affinity(self, credential_id: Optional[str]) -> ParserCredential:
        """Resolve the exact credential that owns an existing remote task."""

        if not credential_id:
            raise CredentialPoolUnavailable("remote task has no credential affinity")
        record = self.ledger.get_credential(credential_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        if record.cooldown_until and record.cooldown_until > now_iso:
            raise CredentialPoolUnavailable(
                f"credential {credential_id} is cooling down"
            )
        secret = self.secret_resolver(record.secret_ref)
        if not secret:
            raise CredentialPoolUnavailable(
                f"credential {credential_id} secret is unavailable"
            )
        return ParserCredential(
            credential_id=record.id,
            secret=secret,
            metadata={"display_name": record.display_name},
        )

    def release_unsubmitted(self, lease: CredentialLease) -> None:
        self.ledger.release_credential(
            lease.credential.credential_id,
            reserved_pages=lease.page_count,
            refund_budget=True,
            today=datetime.now(timezone.utc).date().isoformat(),
        )

    def finish_remote(self, credential_id: Optional[str]) -> None:
        if credential_id:
            self.ledger.release_credential(credential_id)

    def record_error(
        self, credential_id: Optional[str], error: ParserProviderError
    ) -> None:
        if not credential_id:
            return
        now = datetime.now(timezone.utc)
        if error.authentication_failed:
            self.ledger.update_credential_health(
                credential_id,
                enabled=False,
                health_status="unauthorized",
                last_401_at=now.isoformat(),
            )
        elif error.rate_limited:
            self.ledger.update_credential_health(
                credential_id,
                health_status="cooldown",
                cooldown_until=(
                    now + timedelta(seconds=self.rate_limit_cooldown_seconds)
                ).isoformat(),
                last_429_at=now.isoformat(),
            )

    def recover(self, credential_id: str) -> CredentialRecord:
        return self.ledger.recover_credential(credential_id)

    def reconcile_in_flight(self) -> None:
        """After restart, derive counters from persisted remote-task affinities."""

        active = self.ledger.active_remote_counts(self.provider_id)
        for record in self.ledger.list_credentials(self.provider_id):
            self.ledger.set_credential_in_flight(record.id, active.get(record.id, 0))

    def plan_distribution(self, page_counts: Sequence[int]) -> CredentialPlan:
        """Pure dry-run assignment; it never resolves secrets or mutates usage."""

        today = datetime.now(timezone.utc).date().isoformat()
        records = [
            item
            for item in self.ledger.list_credentials(self.provider_id)
            if item.is_enabled and item.health_status not in {"unauthorized", "disabled"}
        ]
        used = {
            item.id: item.pages_used_today if item.usage_date == today else 0
            for item in records
        }
        planned = {item.id: 0 for item in records}
        by_id = {item.id: item for item in records}
        assignments: List[Optional[str]] = []
        unassigned = 0
        for raw_pages in page_counts:
            pages = max(0, int(raw_pages))
            candidates = [
                item
                for item in records
                if item.daily_page_budget is None
                or used[item.id] + planned[item.id] + pages
                <= item.daily_page_budget
            ]
            if not candidates:
                assignments.append(None)
                unassigned += pages
                continue
            selected = min(
                candidates,
                key=lambda item: (
                    used[item.id] + planned[item.id],
                    planned[item.id],
                    item.id,
                ),
            )
            assignments.append(selected.id)
            planned[selected.id] += pages
        return CredentialPlan(
            assignments=assignments,
            pages_by_credential={key: planned[key] for key in sorted(by_id)},
            unassigned_pages=unassigned,
        )


def redact_secrets(text: str, secrets: Sequence[str]) -> str:
    redacted = str(text)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted

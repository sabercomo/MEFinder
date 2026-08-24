"""Transport-neutral progress event normalization."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Mapping, Optional


@dataclass(frozen=True)
class TaskEvent:
    task_id: str
    status: str
    phase: str
    message: str
    completed: int
    total: Optional[int]
    unit: str
    progress: Optional[float]
    speed_per_second: Optional[float]
    eta_seconds: Optional[float]

    @classmethod
    def from_mapping(
        cls,
        task_id: str,
        payload: Mapping[str, object],
        *,
        unit: str,
    ) -> "TaskEvent":
        progress_payload = payload.get("progress")
        details = progress_payload if isinstance(progress_payload, Mapping) else {}
        completed = _completed_value(payload, details)
        total = _optional_int(
            payload.get("total")
            or payload.get("total_bytes")
            or payload.get("total_pages")
            or details.get("total_pages")
        )
        progress = (
            _optional_float(progress_payload)
            if not isinstance(progress_payload, Mapping)
            else None
        )
        if progress is None and total:
            progress = min(1.0, completed / total)
        return cls(
            task_id=str(task_id),
            status=str(payload.get("status") or payload.get("state") or "unknown"),
            phase=str(payload.get("phase") or payload.get("operation") or ""),
            message=str(payload.get("message") or payload.get("error") or ""),
            completed=completed,
            total=total,
            unit=unit,
            progress=progress,
            speed_per_second=_optional_float(
                payload.get("speed_per_second")
                or payload.get("download_speed_bps")
            ),
            eta_seconds=_optional_float(payload.get("eta_seconds")),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _completed_value(
    payload: Mapping[str, object], details: Mapping[str, object]
) -> int:
    raw = (
        payload.get("completed")
        or payload.get("downloaded_bytes")
        or details.get("completed_pages")
        or 0
    )
    if isinstance(raw, (list, tuple, set)):
        return len(raw)
    return int(raw)


def _optional_int(value: object) -> Optional[int]:
    return None if value in (None, "") else int(value)


def _optional_float(value: object) -> Optional[float]:
    return None if value in (None, "") else float(value)

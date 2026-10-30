"""Shared lifecycle contract for locally managed runtime components."""

from __future__ import annotations

from typing import Dict, Mapping, Protocol, runtime_checkable


@runtime_checkable
class ManagedComponent(Protocol):
    component_id: str

    def summary(self) -> Dict[str, object]: ...

    def perform(self, payload: Mapping[str, object]) -> Dict[str, object]: ...

    def diagnostics(self) -> Dict[str, object]: ...

"""Explicit application paths shared by desktop, CLI, and HTTP adapters.

Historically the application changed the process working directory and let
backend modules discover mutable files through relative paths.  ``AppContext``
is the compatibility seam for removing that process-global dependency: the
composition root resolves paths once and passes the immutable context inward.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def _absolute(path: Path, *, relative_to: Path | None = None) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (relative_to or Path.cwd()) / candidate
    return candidate.resolve()


@dataclass(frozen=True)
class AppPaths:
    """All process-level filesystem roots required by the backend."""

    runtime_root: Path
    index_path: Path
    app_data_root: Path | None = None
    default_app_data_root: Path | None = None

    @classmethod
    def create(
        cls,
        runtime_root: Path,
        *,
        index_path: Path | None = None,
        app_data_root: Path | None = None,
        default_app_data_root: Path | None = None,
    ) -> "AppPaths":
        root = _absolute(runtime_root)
        resolved_index = _absolute(index_path or Path("data/index.sqlite3"), relative_to=root)
        return cls(
            runtime_root=root,
            index_path=resolved_index,
            app_data_root=(
                _absolute(app_data_root) if app_data_root is not None else None
            ),
            default_app_data_root=(
                _absolute(default_app_data_root)
                if default_app_data_root is not None
                else None
            ),
        )

    @property
    def config_root(self) -> Path:
        return self.runtime_root / "config"

    @property
    def corpus_root(self) -> Path:
        return self.runtime_root / "corpus"

    @property
    def data_root(self) -> Path:
        return self.runtime_root / "data"


@dataclass(frozen=True)
class AppContext:
    """Dependencies created once at an application composition root."""

    paths: AppPaths

    @classmethod
    def create(
        cls,
        runtime_root: Path,
        *,
        index_path: Path | None = None,
        app_data_root: Path | None = None,
        default_app_data_root: Path | None = None,
    ) -> "AppContext":
        return cls(
            paths=AppPaths.create(
                runtime_root,
                index_path=index_path,
                app_data_root=app_data_root,
                default_app_data_root=default_app_data_root,
            )
        )

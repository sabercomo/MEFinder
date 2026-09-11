"""Unified native capability entry for the desktop shell.

One object adapts the pywebview window/module into the native capabilities the
HTTP backend consumes (PDF opener, native theme, file choosers). Platform
specifics stay behind the injected ``pdf_viewer``/``native_theme_setter`` —
the business pages only ever see ordinary callables and never learn which
platform they run on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Union

PathLike = Union[str, Path]
Selection = Optional[Sequence[PathLike]]


@dataclass(frozen=True)
class NativeCapabilities:
    """The capability hooks ``web.make_handler`` consumes.

    ``None`` marks a capability the current shell does not provide; the
    transport-neutral controller answers with its own "unsupported" responses.
    """

    pdf_opener: Optional[Callable[..., Any]] = None
    theme_setter: Optional[Callable[[str], None]] = None
    directory_chooser: Optional[Callable[[], Optional[str]]] = None
    export_directory_chooser: Optional[Callable[[], Optional[str]]] = None
    scan_directory_chooser: Optional[Callable[[], List[str]]] = None
    backup_file_chooser: Optional[Callable[[], Optional[str]]] = None


class PywebviewDesktopHost:
    """Adapt one pywebview window into the shell's native capability entry."""

    def __init__(
        self,
        window: Any,
        webview_module: Any,
        *,
        pdf_viewer: Any = None,
        native_theme_setter: Optional[Callable[[str], None]] = None,
        app_data_root: Optional[Path] = None,
        scan_start_directory: Path = Path.home() / "Documents",
        export_start_directory: Path = Path.home() / "Downloads",
    ) -> None:
        self._window = window
        self._webview = webview_module
        self._pdf_viewer = pdf_viewer
        self._native_theme_setter = native_theme_setter
        self._app_data_root = Path(app_data_root) if app_data_root else None
        # Never start a scan at the home folder: picking it is one click away
        # there, and scanning it would walk the user's whole personal library.
        self._scan_start_directory = scan_start_directory
        self._export_start_directory = export_start_directory

    # ------------------------------------------------------------------
    # Native capabilities
    # ------------------------------------------------------------------

    def open_pdf(self, source_path: PathLike, page: object = None) -> Any:
        if self._pdf_viewer is None:
            raise RuntimeError("当前运行方式不支持打开 PDF 原文。")
        return self._pdf_viewer.open(source_path, page)

    def set_native_theme(self, theme: str) -> None:
        if self._native_theme_setter is None:
            return
        self._native_theme_setter(theme)

    def choose_folders(
        self,
        initial_directory: Optional[Path] = None,
        *,
        allow_multiple: bool = False,
    ) -> List[str]:
        """Open the platform folder picker. Works on macOS and Windows alike."""

        start = initial_directory
        if start is None or not start.is_dir():
            start = Path.home()
        selection = self._window.create_file_dialog(
            self._webview.FileDialog.FOLDER,
            directory=str(start),
            allow_multiple=allow_multiple,
        )
        if not selection:
            return []
        if isinstance(selection, (str, Path)):
            selection = [selection]
        return [str(folder) for folder in selection]

    def choose_data_directory(self) -> Optional[str]:
        if self._app_data_root is None:
            return None
        selection = self.choose_folders(self._app_data_root.parent)
        return selection[0] if selection else None

    def choose_backup_file(self) -> Optional[str]:
        try:
            selection = self._window.create_file_dialog(
                self._webview.FileDialog.OPEN,
                directory=str(Path.home()),
                allow_multiple=False,
                file_types=("MEFinder 备份 (*.zip)",),
            )
        except self._webview.errors.WebViewException as exc:
            raise RuntimeError(str(exc)) from exc
        if not selection:
            return None
        if isinstance(selection, (str, Path)):
            return str(selection)
        return str(selection[0])

    def choose_export_directory(self) -> Optional[str]:
        try:
            selection = self.choose_folders(self._export_start_directory)
        except self._webview.errors.WebViewException as exc:
            raise RuntimeError(str(exc)) from exc
        return selection[0] if selection else None

    def choose_scan_directories(self) -> List[str]:
        return self.choose_folders(
            self._scan_start_directory, allow_multiple=True
        )

    # ------------------------------------------------------------------
    # Capability wiring for the HTTP backend
    # ------------------------------------------------------------------

    def capabilities(self) -> NativeCapabilities:
        return NativeCapabilities(
            pdf_opener=(
                self.open_pdf if self._pdf_viewer is not None else None
            ),
            theme_setter=(
                self.set_native_theme
                if self._native_theme_setter is not None
                else None
            ),
            directory_chooser=(
                self.choose_data_directory
                if self._app_data_root is not None
                else None
            ),
            export_directory_chooser=self.choose_export_directory,
            scan_directory_chooser=self.choose_scan_directories,
            backup_file_chooser=self.choose_backup_file,
        )

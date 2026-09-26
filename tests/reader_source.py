"""Read the exact reader script assembled into both application windows."""

from src.me_finder.web_assets import _load_reader_js


def reader_js_source() -> str:
    """Return the reader JavaScript assembled by the application."""

    return _load_reader_js()

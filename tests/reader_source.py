"""Read the exact reader script assembled into both application windows."""

from src.me_finder.web_assets import _load_asset, _load_reader_js


def reader_js_source() -> str:
    """Return the reader JavaScript assembled by the application."""

    return _load_reader_js()


def reader_runtime_source() -> str:
    """Return the reader with the request module both windows load before it."""

    return _load_asset("static/js/07-api.js") + _load_reader_js()

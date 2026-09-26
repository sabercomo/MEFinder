"""Read the exact reader script assembled into both application windows."""

from src.me_finder.web_assets import _load_asset, _load_reader_js


def reader_js_source() -> str:
    """Return the reader JavaScript assembled by the application."""

    return _load_reader_js()


def alignment_jobs_source() -> str:
    """Return the alignment job service both windows load before the reader."""

    return _load_asset("static/js/15-alignment-jobs.js")


def reader_runtime_source() -> str:
    """Return the reader with the modules both windows load before it."""

    return _load_asset("static/js/07-api.js") + alignment_jobs_source() + _load_reader_js()

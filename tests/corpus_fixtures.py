"""Shared guards for tests that need the private, git-ignored corpus.

The raw source books (``corpus/raw_docx`` / ``corpus/raw_pdf``) are copyrighted
and never committed, so on a clean checkout they are absent — ``corpus/raw_pdf``
may even exist as an empty directory.  These helpers let corpus-dependent tests
*skip* visibly instead of erroring, so the release gate can run the whole suite
via ``unittest discover`` without silently dropping modules from a hand-list.
"""

from __future__ import annotations

from pathlib import Path

CORPUS_ROOT = Path("corpus")
RAW_DOCX = CORPUS_ROOT / "raw_docx"
RAW_PDF = CORPUS_ROOT / "raw_pdf"


def _has_files(directory: Path, suffix: str) -> bool:
    return directory.is_dir() and any(directory.glob(f"*{suffix}"))


def has_docx_corpus() -> bool:
    return _has_files(RAW_DOCX, ".docx")


def has_pdf_corpus() -> bool:
    return _has_files(RAW_PDF, ".pdf")


def has_corpus() -> bool:
    """True when either raw corpus is present with at least one source file."""

    return has_docx_corpus() or has_pdf_corpus()


DOCX_CORPUS_REASON = "需要私有语料 corpus/raw_docx（受版权限制，未纳入版本库）"
PDF_CORPUS_REASON = "需要私有语料 corpus/raw_pdf（受版权限制，未纳入版本库）"
CORPUS_REASON = "需要私有语料 corpus/（受版权限制，未纳入版本库）"

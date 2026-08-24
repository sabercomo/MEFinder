"""Canonical SQLite index schema and version constants."""

from pathlib import Path


DEFAULT_DATABASE_PATH = Path("data/index.sqlite3")
DATABASE_SCHEMA_VERSION = 3
ANCHOR_SPEC_VERSION = 1
PARAGRAPH_FTS_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA user_version = 3;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE source_files (
    source_file_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    file_name TEXT,
    relative_path TEXT,
    volume_number INTEGER,
    payload_json TEXT NOT NULL
);

CREATE TABLE volumes (
    volume_id TEXT PRIMARY KEY,
    source_file_id TEXT,
    source_type TEXT NOT NULL,
    volume_number INTEGER,
    display_title TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE works (
    work_id TEXT PRIMARY KEY,
    volume_id TEXT,
    source_type TEXT NOT NULL,
    work_order INTEGER,
    title TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE document_groups (
    document_group_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    base_source_file_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE document_group_members (
    document_group_id TEXT NOT NULL REFERENCES document_groups(document_group_id) ON DELETE CASCADE,
    source_file_id TEXT NOT NULL UNIQUE,
    version_label TEXT,
    member_order INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL
);
CREATE INDEX idx_document_group_members_group ON document_group_members(document_group_id);

CREATE TABLE toc_entries (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id TEXT,
    work_id TEXT,
    title TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE paragraphs (
    paragraph_id TEXT PRIMARY KEY,
    volume_id TEXT,
    work_id TEXT,
    source_file_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    paragraph_index INTEGER NOT NULL,
    eligible_for_search INTEGER NOT NULL,
    text_raw TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    compact_text TEXT NOT NULL,
    plain_text TEXT NOT NULL,
    page_display TEXT,
    page_source_type TEXT,
    page_confidence REAL,
    citation_page_start TEXT,
    citation_page_end TEXT,
    pdf_page_start_index INTEGER,
    pdf_page_end_index INTEGER,
    pdf_page_start_label TEXT,
    pdf_page_end_label TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE page_anchors (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    paragraph_id TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE pdf_pages (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id TEXT,
    pdf_page_index INTEGER,
    payload_json TEXT NOT NULL
);

CREATE TABLE pdf_page_mappings (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id TEXT,
    pdf_page_index INTEGER,
    payload_json TEXT NOT NULL
);

CREATE TABLE pdf_import_runs (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id TEXT,
    status TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE audit_issues (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id TEXT,
    issue_type TEXT,
    payload_json TEXT NOT NULL
);

CREATE INDEX idx_paragraphs_searchable ON paragraphs(eligible_for_search, source_type);
CREATE INDEX idx_paragraphs_volume_position ON paragraphs(volume_id, paragraph_index);
CREATE INDEX idx_paragraphs_source_position ON paragraphs(source_file_id, paragraph_index);
CREATE INDEX idx_pdf_pages_source_page ON pdf_pages(source_file_id, pdf_page_index);
"""

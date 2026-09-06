"""Canonical SQLite index schema and version constants."""

from pathlib import Path


DEFAULT_DATABASE_PATH = Path("data/index.sqlite3")
DATABASE_SCHEMA_VERSION = 6
ANCHOR_SPEC_VERSION = 1
PARAGRAPH_FTS_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA user_version = 6;

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

CREATE TABLE segment_sets (
    segment_set_id TEXT PRIMARY KEY,
    source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id) ON DELETE CASCADE,
    source_text_hash TEXT NOT NULL,
    segmenter TEXT NOT NULL,
    segmenter_version TEXT NOT NULL,
    language_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_file_id, source_text_hash, segmenter, segmenter_version)
);

CREATE TABLE text_segments (
    segment_id TEXT PRIMARY KEY,
    segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    text_raw TEXT NOT NULL,
    UNIQUE(segment_set_id, order_index)
);

CREATE TABLE text_segment_spans (
    segment_id TEXT NOT NULL REFERENCES text_segments(segment_id) ON DELETE CASCADE,
    source_file_id TEXT NOT NULL,
    pdf_page_index INTEGER NOT NULL,
    page_char_start INTEGER NOT NULL,
    page_char_end INTEGER NOT NULL,
    span_order INTEGER NOT NULL,
    PRIMARY KEY(segment_id, span_order)
);

CREATE TABLE text_segment_paragraph_spans (
    segment_id TEXT NOT NULL REFERENCES text_segments(segment_id) ON DELETE CASCADE,
    source_file_id TEXT NOT NULL,
    paragraph_id TEXT NOT NULL,
    paragraph_index INTEGER NOT NULL,
    paragraph_char_start INTEGER NOT NULL,
    paragraph_char_end INTEGER NOT NULL,
    span_order INTEGER NOT NULL,
    PRIMARY KEY(segment_id, span_order)
);

CREATE TABLE alignment_runs (
    alignment_run_id TEXT PRIMARY KEY,
    document_group_id TEXT NOT NULL REFERENCES document_groups(document_group_id) ON DELETE CASCADE,
    pivot_source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id) ON DELETE CASCADE,
    target_source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id) ON DELETE CASCADE,
    pivot_segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
    target_segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) ON DELETE CASCADE,
    algorithm TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE alignment_links (
    alignment_link_id TEXT PRIMARY KEY,
    alignment_run_id TEXT NOT NULL REFERENCES alignment_runs(alignment_run_id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    cost REAL NOT NULL,
    confidence REAL,
    anchor_key TEXT,
    review_status TEXT NOT NULL,
    UNIQUE(alignment_run_id, order_index)
);

CREATE TABLE alignment_link_members (
    alignment_link_id TEXT NOT NULL REFERENCES alignment_links(alignment_link_id) ON DELETE CASCADE,
    side TEXT NOT NULL CHECK(side IN ('pivot', 'target')),
    segment_id TEXT NOT NULL REFERENCES text_segments(segment_id) ON DELETE CASCADE,
    member_order INTEGER NOT NULL,
    PRIMARY KEY(alignment_link_id, side, member_order),
    UNIQUE(alignment_link_id, segment_id)
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
CREATE INDEX idx_segment_sets_source ON segment_sets(source_file_id);
CREATE INDEX idx_segment_spans_source_page ON text_segment_spans(source_file_id, pdf_page_index, page_char_start, page_char_end);
CREATE INDEX idx_segment_paragraph_spans_source_position ON text_segment_paragraph_spans(source_file_id, paragraph_index, paragraph_char_start, paragraph_char_end);
CREATE INDEX idx_alignment_runs_pair ON alignment_runs(document_group_id, pivot_source_file_id, target_source_file_id, status);
CREATE INDEX idx_alignment_members_segment ON alignment_link_members(segment_id, side);
"""

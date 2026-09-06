from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.pdf_extractors import pdf_page_text_hash


PDF_SOURCE_ID = "fixture-pdf"
WORD_SOURCE_ID = "fixture-word"
PARALLEL_SOURCE_ID = "fixture-english-epub"

CALIBRATED_QUOTE = "技术判断必须由可复核证据支撑"
UNCALIBRATED_QUOTE = "物理页位置不能自动视为正式引用页"
WORD_QUOTE = "文献核对首先要区分检索命中与结论成立"
MISSING_QUOTE = "这是一条夹具中不存在的句子"
NFKC_TEXT = "MEFinder 1.0 提供 NFKC 归一化核对。"
NFKC_QUERY = "ＭＥＦｉｎｄｅｒ　１．０ 提供 NFKC 归一化核对"
FUZZY_QUERY = "技术判段必须由可复核证据支撑"
DUPLICATE_QUOTE = "重复原句必须作为多个候选返回"
CROSS_PAGE_QUERY = "跨页证据\n仍可追溯"
SPREAD_LEFT_QUERY = "左页保存独立证据"
SPREAD_RIGHT_QUERY = "右页保存另一证据"
SPREAD_BOTH_QUERY = "独立证据\n右页"


def _searchable_paragraph(
    *,
    paragraph_id: str,
    source_file_id: str,
    source_type: str,
    volume_id: str,
    work_id: str,
    paragraph_index: int,
    text: str,
    document_title: str,
    author: str,
) -> dict[str, object]:
    return {
        "paragraph_id": paragraph_id,
        "source_file_id": source_file_id,
        "source_type": source_type,
        "volume_id": volume_id,
        "volume_number": None,
        "volume_display": document_title,
        "work_id": work_id,
        "work_title": document_title,
        "document_title": document_title,
        "author_label": author,
        "paragraph_index": paragraph_index,
        "eligible_for_search": True,
        "text_raw": text,
        "normalized_text": normalize_text(text),
        "compact_text": compact_text(text),
        "plain_text": punctuationless_text(text),
    }


def build_mcp_v1_fixture(
    database_path: Path,
    *,
    include_quality_cases: bool = False,
) -> None:
    """Build the public synthetic index shared by MCP milestone tests."""

    calibrated_text = f"{CALIBRATED_QUOTE}。"
    uncalibrated_text = f"{UNCALIBRATED_QUOTE}。"
    word_text = f"{WORD_QUOTE}。"
    word_context = "多候选结果必须保留歧义，不应替使用者猜测。"

    calibrated_page_id = f"{PDF_SOURCE_ID}-PAGE-000000"
    uncalibrated_page_id = f"{PDF_SOURCE_ID}-PAGE-000001"

    calibrated_paragraph = _searchable_paragraph(
        paragraph_id=f"{PDF_SOURCE_ID}-P000000",
        source_file_id=PDF_SOURCE_ID,
        source_type="pdf",
        volume_id="fixture-pdf-volume",
        work_id="fixture-pdf-work",
        paragraph_index=0,
        text=calibrated_text,
        document_title="MCP 合成 PDF 样例",
        author="测试作者甲",
    )
    calibrated_paragraph.update(
        {
            "page_source_type": "manual_segment",
            "page_confidence": 1.0,
            "citation_page_start": "38",
            "citation_page_end": "38",
            "citation_page_verified": True,
            "pdf_page_start_index": 0,
            "pdf_page_end_index": 0,
            "pdf_page_start_label": "1",
            "pdf_page_end_label": "1",
            "page_mapping_method": "manual_segment",
            "page_mapping_confidence": 1.0,
            "mapping_confidence_level": "high",
            "segment_id": "fixture-segment-1",
            "original_file_name": "mcp-fixture.pdf",
            "text_source_spans": [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(calibrated_text),
                    "pdf_page_id": calibrated_page_id,
                    "pdf_page_index": 0,
                    "page_char_start": 0,
                    "page_char_end": len(calibrated_text),
                    "page_text_hash": pdf_page_text_hash(calibrated_text),
                    "offset_unit": "unicode_codepoint",
                }
            ],
        }
    )

    uncalibrated_paragraph = _searchable_paragraph(
        paragraph_id=f"{PDF_SOURCE_ID}-P000001",
        source_file_id=PDF_SOURCE_ID,
        source_type="pdf",
        volume_id="fixture-pdf-volume",
        work_id="fixture-pdf-work",
        paragraph_index=1,
        text=uncalibrated_text,
        document_title="MCP 合成 PDF 样例",
        author="测试作者甲",
    )
    uncalibrated_paragraph.update(
        {
            "page_source_type": "uncalibrated",
            "page_confidence": 0.0,
            "citation_page_verified": False,
            "pdf_page_start_index": 1,
            "pdf_page_end_index": 1,
            "page_mapping_method": "uncalibrated",
            "page_mapping_confidence": 0.0,
            "mapping_confidence_level": "low",
            "original_file_name": "mcp-fixture.pdf",
            "text_source_spans": [
                {
                    "paragraph_char_start": 0,
                    "paragraph_char_end": len(uncalibrated_text),
                    "pdf_page_id": uncalibrated_page_id,
                    "pdf_page_index": 1,
                    "page_char_start": 0,
                    "page_char_end": len(uncalibrated_text),
                    "page_text_hash": pdf_page_text_hash(uncalibrated_text),
                    "offset_unit": "unicode_codepoint",
                }
            ],
        }
    )

    word_paragraph = _searchable_paragraph(
        paragraph_id=f"{WORD_SOURCE_ID}-P000000",
        source_file_id=WORD_SOURCE_ID,
        source_type="word",
        volume_id="fixture-word-volume",
        work_id="fixture-word-work",
        paragraph_index=0,
        text=word_text,
        document_title="MCP 合成 Word 样例",
        author="测试作者乙",
    )
    word_paragraph.update(
        {
            "page_source_type": "section_break_verified",
            "page_display": "第 7 页",
            "original_page_start": "7",
            "original_page_end": "7",
            "original_file_name": "mcp-fixture.docx",
        }
    )

    word_context_paragraph = _searchable_paragraph(
        paragraph_id=f"{WORD_SOURCE_ID}-P000001",
        source_file_id=WORD_SOURCE_ID,
        source_type="word",
        volume_id="fixture-word-volume",
        work_id="fixture-word-work",
        paragraph_index=1,
        text=word_context,
        document_title="MCP 合成 Word 样例",
        author="测试作者乙",
    )
    word_context_paragraph.update(
        {
            "page_source_type": "section_break_verified",
            "page_display": "第 8 页",
            "original_page_start": "8",
            "original_page_end": "8",
            "original_file_name": "mcp-fixture.docx",
        }
    )

    paragraphs = [
        calibrated_paragraph,
        uncalibrated_paragraph,
        word_paragraph,
        word_context_paragraph,
    ]
    pdf_pages = [
        {
            "pdf_page_id": calibrated_page_id,
            "source_file_id": PDF_SOURCE_ID,
            "pdf_page_index": 0,
            "pdf_page_number_1based": 1,
            "pdf_page_label": "1",
            "text_raw": calibrated_text,
            "page_text_hash": pdf_page_text_hash(calibrated_text),
            "citation_page": "38",
            "citation_page_verified": True,
            "page_mapping_method": "manual_segment",
            "page_mapping_confidence": 1.0,
            "mapping_confidence_level": "high",
            "segment_id": "fixture-segment-1",
        },
        {
            "pdf_page_id": uncalibrated_page_id,
            "source_file_id": PDF_SOURCE_ID,
            "pdf_page_index": 1,
            "pdf_page_number_1based": 2,
            "text_raw": uncalibrated_text,
            "page_text_hash": pdf_page_text_hash(uncalibrated_text),
            "citation_page_verified": False,
            "page_mapping_method": "uncalibrated",
            "page_mapping_confidence": 0.0,
            "mapping_confidence_level": "low",
        },
    ]

    if include_quality_cases:
        nfkc_paragraph = _searchable_paragraph(
            paragraph_id=f"{WORD_SOURCE_ID}-P000002",
            source_file_id=WORD_SOURCE_ID,
            source_type="word",
            volume_id="fixture-word-volume",
            work_id="fixture-word-work",
            paragraph_index=2,
            text=NFKC_TEXT,
            document_title="MCP 合成 Word 样例",
            author="测试作者乙",
        )
        nfkc_paragraph.update(
            {
                "page_source_type": "section_break_verified",
                "page_display": "第 9 页",
                "original_page_start": "9",
                "original_page_end": "9",
                "original_file_name": "mcp-fixture.docx",
            }
        )

        duplicate_word_text = f"{DUPLICATE_QUOTE}。"
        duplicate_word_paragraph = _searchable_paragraph(
            paragraph_id=f"{WORD_SOURCE_ID}-P000003",
            source_file_id=WORD_SOURCE_ID,
            source_type="word",
            volume_id="fixture-word-volume",
            work_id="fixture-word-work",
            paragraph_index=3,
            text=duplicate_word_text,
            document_title="MCP 合成 Word 样例",
            author="测试作者乙",
        )
        duplicate_word_paragraph.update(
            {
                "page_source_type": "section_break_verified",
                "page_display": "第 10 页",
                "original_page_start": "10",
                "original_page_end": "10",
                "original_file_name": "mcp-fixture.docx",
            }
        )

        cross_left = "跨页证据"
        cross_right = "仍可追溯"
        cross_text = f"{cross_left}\n{cross_right}"
        cross_page_ids = [
            f"{PDF_SOURCE_ID}-PAGE-000002",
            f"{PDF_SOURCE_ID}-PAGE-000003",
        ]
        cross_paragraph = _searchable_paragraph(
            paragraph_id=f"{PDF_SOURCE_ID}-P000002",
            source_file_id=PDF_SOURCE_ID,
            source_type="pdf",
            volume_id="fixture-pdf-volume",
            work_id="fixture-pdf-work",
            paragraph_index=2,
            text=cross_text,
            document_title="MCP 合成 PDF 样例",
            author="测试作者甲",
        )
        cross_paragraph.update(
            {
                "page_source_type": "manual_segment",
                "page_confidence": 1.0,
                "citation_page_start": "39",
                "citation_page_end": "40",
                "citation_page_verified": True,
                "pdf_page_start_index": 2,
                "pdf_page_end_index": 3,
                "pdf_page_start_label": "3",
                "pdf_page_end_label": "4",
                "page_mapping_method": "manual_segment",
                "page_mapping_confidence": 1.0,
                "mapping_confidence_level": "high",
                "segment_id": "fixture-segment-2",
                "is_cross_page": True,
                "original_file_name": "mcp-fixture.pdf",
                "text_source_spans": [
                    {
                        "paragraph_char_start": 0,
                        "paragraph_char_end": len(cross_left),
                        "pdf_page_id": cross_page_ids[0],
                        "pdf_page_index": 2,
                        "page_char_start": 0,
                        "page_char_end": len(cross_left),
                        "page_text_hash": pdf_page_text_hash(cross_left),
                        "offset_unit": "unicode_codepoint",
                    },
                    {
                        "paragraph_char_start": len(cross_left) + 1,
                        "paragraph_char_end": len(cross_text),
                        "pdf_page_id": cross_page_ids[1],
                        "pdf_page_index": 3,
                        "page_char_start": 0,
                        "page_char_end": len(cross_right),
                        "page_text_hash": pdf_page_text_hash(cross_right),
                        "offset_unit": "unicode_codepoint",
                    },
                ],
            }
        )

        spread_text = f"{SPREAD_LEFT_QUERY}\n{SPREAD_RIGHT_QUERY}"
        spread_right_start = spread_text.index(SPREAD_RIGHT_QUERY)
        spread_page_id = f"{PDF_SOURCE_ID}-PAGE-000004"
        spread_paragraph = _searchable_paragraph(
            paragraph_id=f"{PDF_SOURCE_ID}-P000003",
            source_file_id=PDF_SOURCE_ID,
            source_type="pdf",
            volume_id="fixture-pdf-volume",
            work_id="fixture-pdf-work",
            paragraph_index=3,
            text=spread_text,
            document_title="MCP 合成 PDF 样例",
            author="测试作者甲",
        )
        spread_paragraph.update(
            {
                "page_source_type": "manual_segment",
                "page_confidence": 1.0,
                "citation_page_start": "41",
                "citation_page_end": "42",
                "citation_page_verified": True,
                "pdf_page_start_index": 4,
                "pdf_page_end_index": 4,
                "pdf_page_start_label": "5",
                "pdf_page_end_label": "5",
                "page_mapping_method": "manual_segment",
                "page_mapping_confidence": 1.0,
                "mapping_confidence_level": "high",
                "segment_id": "fixture-segment-3",
                "layout_mode": "spread",
                "reading_direction": "ltr",
                "gutter_x": 0.5,
                "original_file_name": "mcp-fixture.pdf",
                "text_source_spans": [
                    {
                        "paragraph_char_start": 0,
                        "paragraph_char_end": len(spread_text),
                        "pdf_page_id": spread_page_id,
                        "pdf_page_index": 4,
                        "page_char_start": 0,
                        "page_char_end": len(spread_text),
                        "page_text_hash": pdf_page_text_hash(spread_text),
                        "offset_unit": "unicode_codepoint",
                    }
                ],
            }
        )

        duplicate_pdf_text = f"{DUPLICATE_QUOTE}。"
        duplicate_page_id = f"{PDF_SOURCE_ID}-PAGE-000005"
        duplicate_pdf_paragraph = _searchable_paragraph(
            paragraph_id=f"{PDF_SOURCE_ID}-P000004",
            source_file_id=PDF_SOURCE_ID,
            source_type="pdf",
            volume_id="fixture-pdf-volume",
            work_id="fixture-pdf-work",
            paragraph_index=4,
            text=duplicate_pdf_text,
            document_title="MCP 合成 PDF 样例",
            author="测试作者甲",
        )
        duplicate_pdf_paragraph.update(
            {
                "page_source_type": "manual_segment",
                "page_confidence": 1.0,
                "citation_page_start": "43",
                "citation_page_end": "43",
                "citation_page_verified": True,
                "pdf_page_start_index": 5,
                "pdf_page_end_index": 5,
                "pdf_page_start_label": "6",
                "pdf_page_end_label": "6",
                "page_mapping_method": "manual_segment",
                "page_mapping_confidence": 1.0,
                "mapping_confidence_level": "high",
                "segment_id": "fixture-segment-4",
                "original_file_name": "mcp-fixture.pdf",
                "text_source_spans": [
                    {
                        "paragraph_char_start": 0,
                        "paragraph_char_end": len(duplicate_pdf_text),
                        "pdf_page_id": duplicate_page_id,
                        "pdf_page_index": 5,
                        "page_char_start": 0,
                        "page_char_end": len(duplicate_pdf_text),
                        "page_text_hash": pdf_page_text_hash(duplicate_pdf_text),
                        "offset_unit": "unicode_codepoint",
                    }
                ],
            }
        )

        paragraphs.extend(
            [
                nfkc_paragraph,
                duplicate_word_paragraph,
                cross_paragraph,
                spread_paragraph,
                duplicate_pdf_paragraph,
            ]
        )
        pdf_pages.extend(
            [
                {
                    "pdf_page_id": cross_page_ids[0],
                    "source_file_id": PDF_SOURCE_ID,
                    "pdf_page_index": 2,
                    "pdf_page_number_1based": 3,
                    "pdf_page_label": "3",
                    "text_raw": cross_left,
                    "page_text_hash": pdf_page_text_hash(cross_left),
                    "citation_page": "39",
                    "citation_page_verified": True,
                    "page_mapping_method": "manual_segment",
                    "page_mapping_confidence": 1.0,
                    "mapping_confidence_level": "high",
                    "segment_id": "fixture-segment-2",
                },
                {
                    "pdf_page_id": cross_page_ids[1],
                    "source_file_id": PDF_SOURCE_ID,
                    "pdf_page_index": 3,
                    "pdf_page_number_1based": 4,
                    "pdf_page_label": "4",
                    "text_raw": cross_right,
                    "page_text_hash": pdf_page_text_hash(cross_right),
                    "citation_page": "40",
                    "citation_page_verified": True,
                    "page_mapping_method": "manual_segment",
                    "page_mapping_confidence": 1.0,
                    "mapping_confidence_level": "high",
                    "segment_id": "fixture-segment-2",
                },
                {
                    "pdf_page_id": spread_page_id,
                    "source_file_id": PDF_SOURCE_ID,
                    "pdf_page_index": 4,
                    "pdf_page_number_1based": 5,
                    "pdf_page_label": "5",
                    "text_raw": spread_text,
                    "page_text_hash": pdf_page_text_hash(spread_text),
                    "citation_page": "41",
                    "citation_page_start": "41",
                    "citation_page_end": "42",
                    "citation_page_verified": True,
                    "page_mapping_method": "manual_segment",
                    "page_mapping_confidence": 1.0,
                    "mapping_confidence_level": "high",
                    "segment_id": "fixture-segment-3",
                    "layout_mode": "spread",
                    "reading_direction": "ltr",
                    "gutter_x": 0.5,
                    "page_width": 1000,
                    "page_height": 800,
                    "blocks": [
                        {
                            "text": SPREAD_LEFT_QUERY,
                            "page_char_start": 0,
                            "page_char_end": len(SPREAD_LEFT_QUERY),
                            "bbox_normalized": [0.08, 0.08, 0.46, 0.88],
                        },
                        {
                            "text": SPREAD_RIGHT_QUERY,
                            "page_char_start": spread_right_start,
                            "page_char_end": len(spread_text),
                            "bbox_normalized": [0.54, 0.08, 0.94, 0.88],
                        },
                    ],
                },
                {
                    "pdf_page_id": duplicate_page_id,
                    "source_file_id": PDF_SOURCE_ID,
                    "pdf_page_index": 5,
                    "pdf_page_number_1based": 6,
                    "pdf_page_label": "6",
                    "text_raw": duplicate_pdf_text,
                    "page_text_hash": pdf_page_text_hash(duplicate_pdf_text),
                    "citation_page": "43",
                    "citation_page_verified": True,
                    "page_mapping_method": "manual_segment",
                    "page_mapping_confidence": 1.0,
                    "mapping_confidence_level": "high",
                    "segment_id": "fixture-segment-4",
                },
            ]
        )

    build_database(
        {
            "metadata": {"anchor_spec_version": 1, "fixture": "mcp-v1"},
            "source_files": [
                {
                    "source_file_id": PDF_SOURCE_ID,
                    "source_type": "pdf",
                    "file_name": "mcp-fixture.pdf",
                    "original_file_name": "mcp-fixture.pdf",
                    "display_title": "MCP 合成 PDF 样例",
                    "document_title": "MCP 合成 PDF 样例",
                    "file_format": "pdf",
                    "bibliographic_metadata": {
                        "document_type": "book",
                        "title": "MCP 合成 PDF 样例",
                        "author": "测试作者甲",
                        "publish_place": "测试地",
                        "publisher": "测试出版社",
                        "publish_year": "2026",
                    },
                },
                {
                    "source_file_id": WORD_SOURCE_ID,
                    "source_type": "word",
                    "file_name": "mcp-fixture.docx",
                    "original_file_name": "mcp-fixture.docx",
                    "display_title": "MCP 合成 Word 样例",
                    "document_title": "MCP 合成 Word 样例",
                    "file_format": "docx",
                    "bibliographic_metadata": {
                        "document_type": "book",
                        "title": "MCP 合成 Word 样例",
                        "author": "测试作者乙",
                        "publish_place": "测试地",
                        "publisher": "测试出版社",
                        "publish_year": "2026",
                    },
                },
            ],
            "volumes": [
                {
                    "volume_id": "fixture-pdf-volume",
                    "source_file_id": PDF_SOURCE_ID,
                    "source_type": "pdf",
                    "display_title": "MCP 合成 PDF 样例",
                },
                {
                    "volume_id": "fixture-word-volume",
                    "source_file_id": WORD_SOURCE_ID,
                    "source_type": "word",
                    "display_title": "MCP 合成 Word 样例",
                },
            ],
            "works": [
                {
                    "work_id": "fixture-pdf-work",
                    "volume_id": "fixture-pdf-volume",
                    "source_file_id": PDF_SOURCE_ID,
                    "source_type": "pdf",
                    "title": "MCP 合成 PDF 样例",
                },
                {
                    "work_id": "fixture-word-work",
                    "volume_id": "fixture-word-volume",
                    "source_file_id": WORD_SOURCE_ID,
                    "source_type": "word",
                    "title": "MCP 合成 Word 样例",
                },
            ],
            "paragraphs": paragraphs,
            "pdf_pages": pdf_pages,
        },
        database_path,
    )


def add_mcp_parallel_fixture(database_path: Path) -> None:
    """Add one completed PDF→EPUB alignment to the public MCP fixture."""

    target_before = "A nearby passage discusses how technical decisions are recorded."
    target_text = "Technical judgments must be supported by verifiable evidence."
    target_after = "The following passage explains how that evidence can be reviewed."
    source_segment_id = "fixture-parallel-source-segment"
    target_before_segment_id = "fixture-parallel-target-before"
    target_segment_id = "fixture-parallel-target-segment"
    target_after_segment_id = "fixture-parallel-target-after"
    connection = sqlite3.connect(str(database_path))
    connection.execute(
        "INSERT INTO source_files(source_file_id, source_type, file_name, "
        "relative_path, volume_number, payload_json) VALUES (?, 'word', ?, NULL, NULL, ?)",
        (
            PARALLEL_SOURCE_ID,
            "mcp-fixture-en.epub",
            json.dumps(
                {
                    "source_file_id": PARALLEL_SOURCE_ID,
                    "source_type": "word",
                    "file_name": "mcp-fixture-en.epub",
                    "title": "MCP Synthetic English Edition",
                    "file_format": "epub",
                    "language_code": "en-US",
                }
            ),
        ),
    )
    connection.executemany(
        "INSERT INTO paragraphs(paragraph_id, volume_id, work_id, source_file_id, "
        "source_type, paragraph_index, eligible_for_search, text_raw, normalized_text, "
        "compact_text, plain_text, payload_json) "
        "VALUES (?, NULL, NULL, ?, 'word', ?, 1, ?, ?, ?, ?, ?)",
        tuple(
            (
                f"fixture-english-epub-p{paragraph_index}",
                PARALLEL_SOURCE_ID,
                paragraph_index,
                text,
                normalize_text(text),
                compact_text(text),
                punctuationless_text(text),
                json.dumps(
                    {
                        "paragraph_id": f"fixture-english-epub-p{paragraph_index}",
                        "paragraph_index": paragraph_index,
                        "source_format": "epub",
                        "volume_number": None,
                        "document_title": "MCP Synthetic English Edition",
                        "work_title": "MCP Synthetic English Edition",
                    }
                ),
            )
            for paragraph_index, text in enumerate(
                (target_before, target_text, target_after)
            )
        ),
    )
    connection.execute(
        "INSERT INTO document_groups(document_group_id, title, base_source_file_id, "
        "created_at, updated_at) VALUES ('fixture-parallel-work', 'MCP parallel work', ?, 't', 't')",
        (PDF_SOURCE_ID,),
    )
    connection.executemany(
        "INSERT INTO document_group_members(document_group_id, source_file_id, "
        "version_label, member_order, added_at) "
        "VALUES ('fixture-parallel-work', ?, ?, ?, 't')",
        (
            (PDF_SOURCE_ID, "中文译本", 0),
            (PARALLEL_SOURCE_ID, "English original", 1),
        ),
    )
    connection.executemany(
        "INSERT INTO segment_sets(segment_set_id, source_file_id, source_text_hash, "
        "segmenter, segmenter_version, language_code, created_at) "
        "VALUES (?, ?, ?, 'me-finder-multilingual-sentence', '12', ?, 't')",
        (
            ("fixture-parallel-source-set", PDF_SOURCE_ID, "source-hash", "zh-Hans"),
            ("fixture-parallel-target-set", PARALLEL_SOURCE_ID, "target-hash", "en-US"),
        ),
    )
    connection.executemany(
        "INSERT INTO text_segments(segment_id, segment_set_id, order_index, text_raw) "
        "VALUES (?, ?, ?, ?)",
        (
            (
                source_segment_id,
                "fixture-parallel-source-set",
                0,
                f"{CALIBRATED_QUOTE}。",
            ),
            (
                target_before_segment_id,
                "fixture-parallel-target-set",
                0,
                target_before,
            ),
            (target_segment_id, "fixture-parallel-target-set", 1, target_text),
            (
                target_after_segment_id,
                "fixture-parallel-target-set",
                2,
                target_after,
            ),
        ),
    )
    connection.execute(
        "INSERT INTO text_segment_spans(segment_id, source_file_id, pdf_page_index, "
        "page_char_start, page_char_end, span_order) VALUES (?, ?, 0, 0, ?, 0)",
        (source_segment_id, PDF_SOURCE_ID, len(CALIBRATED_QUOTE) + 1),
    )
    connection.executemany(
        "INSERT INTO text_segment_paragraph_spans(segment_id, source_file_id, "
        "paragraph_id, paragraph_index, paragraph_char_start, paragraph_char_end, "
        "span_order) VALUES (?, ?, ?, ?, 0, ?, 0)",
        (
            (
                target_before_segment_id,
                PARALLEL_SOURCE_ID,
                "fixture-english-epub-p0",
                0,
                len(target_before),
            ),
            (
                target_segment_id,
                PARALLEL_SOURCE_ID,
                "fixture-english-epub-p1",
                1,
                len(target_text),
            ),
            (
                target_after_segment_id,
                PARALLEL_SOURCE_ID,
                "fixture-english-epub-p2",
                2,
                len(target_after),
            ),
        ),
    )
    connection.execute(
        "INSERT INTO alignment_runs(alignment_run_id, document_group_id, "
        "pivot_source_file_id, target_source_file_id, pivot_segment_set_id, "
        "target_segment_set_id, algorithm, algorithm_version, parameters_json, "
        "status, created_at, completed_at) VALUES "
        "('fixture-parallel-run', 'fixture-parallel-work', ?, ?, "
        "'fixture-parallel-source-set', 'fixture-parallel-target-set', "
        "'chapter-anchored-semantic-dp', '19', '{\"heading_anchors\": []}', "
        "'completed', 't', 't')",
        (PDF_SOURCE_ID, PARALLEL_SOURCE_ID),
    )
    connection.execute(
        "INSERT INTO alignment_links(alignment_link_id, alignment_run_id, "
        "order_index, cost, review_status) VALUES "
        "('fixture-parallel-link', 'fixture-parallel-run', 0, 0, 'automatic')"
    )
    connection.executemany(
        "INSERT INTO alignment_link_members(alignment_link_id, side, segment_id, "
        "member_order) VALUES ('fixture-parallel-link', ?, ?, 0)",
        (("pivot", source_segment_id), ("target", target_segment_id)),
    )
    connection.commit()
    connection.close()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder.citations import build_citation_formats, format_citation
from src.me_finder.bibliographic_metadata import (
    canonical_metadata,
    detect_pdf_bibliographic_metadata,
    invalid_metadata_fields,
    is_valid_bibliographic_value,
    manual_metadata,
    marx_engels_collection_metadata,
    marx_engels_first_edition_metadata,
    metadata_missing_fields,
    update_metadata_in_database,
)
from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.search import SearchEngine


def _build_search_citation_fixture(database_path: Path) -> None:
    """Create the two search records used by citation integration tests.

    These tests used to depend on a developer's local ``data/index.json``.
    Keeping the fixture self-contained makes the release gate deterministic
    on a clean checkout and prevents personal corpus data from affecting the
    expected citation.
    """

    pdf_text = "许多例子都可以说明经验主义在整个世界中的作用"
    word_text = "宗教是人民的鸦片。"
    index = {
        "metadata": {"eligible_paragraph_count": 2},
        "source_files": [
            {
                "source_file_id": "pdf-citation-fixture",
                "source_type": "pdf",
                "file_name": "批判理论.pdf",
                "relative_path": "批判理论.pdf",
                "bibliographic_metadata": {
                    "document_type": "translated_book",
                    "title": "批判理论",
                    "author": "马克斯·霍克海默",
                    "country": "德",
                    "translator": "李小兵等",
                    "publish_place": "重庆",
                    "publisher": "重庆出版社",
                    "publish_year": "1990",
                },
            },
            {
                "source_file_id": "source-01",
                "source_type": "word",
                "file_name": "马克思恩格斯文集第1卷.docx",
                "relative_path": "马克思恩格斯文集第1卷.docx",
                "volume_number": 1,
            },
        ],
        "volumes": [
            {
                "volume_id": "PDF-CITATION",
                "source_file_id": "pdf-citation-fixture",
                "source_type": "pdf",
                "display_title": "批判理论",
            },
            {
                "volume_id": "MEWJ-01",
                "source_file_id": "source-01",
                "source_type": "word",
                "display_title": "《马克思恩格斯文集》第1卷",
                "volume_number": 1,
            },
        ],
        "works": [
            {
                "work_id": "PDF-CITATION-W0001",
                "volume_id": "PDF-CITATION",
                "source_file_id": "pdf-citation-fixture",
                "source_type": "pdf",
                "work_order": 1,
                "title": "批判理论",
            },
            {
                "work_id": "MEWJ-01-W0001",
                "volume_id": "MEWJ-01",
                "source_file_id": "source-01",
                "source_type": "word",
                "work_order": 1,
                "title": "《黑格尔法哲学批判》导言",
                "author_label": "卡·马克思",
            },
        ],
        "paragraphs": [
            {
                "paragraph_id": "pdf-citation-fixture-P000000",
                "volume_id": "PDF-CITATION",
                "work_id": "PDF-CITATION-W0001",
                "source_file_id": "pdf-citation-fixture",
                "source_type": "pdf",
                "volume_number": None,
                "paragraph_index": 0,
                "eligible_for_search": True,
                "text_raw": pdf_text,
                "normalized_text": normalize_text(pdf_text),
                "compact_text": compact_text(pdf_text),
                "plain_text": punctuationless_text(pdf_text),
                "document_title": "批判理论",
                "work_title": "批判理论",
                "volume_display": "批判理论",
                "page_display": "147",
                "page_source_type": "manual_segment",
                "citation_page_start": "147",
                "citation_page_end": "147",
                "pdf_page_start_index": 146,
                "pdf_page_end_index": 146,
            },
            {
                # The source and paragraph prefixes intentionally differ:
                # this is the persisted shape of the bundled Word corpus.
                "paragraph_id": "MEWJ-01-P000001",
                "volume_id": "MEWJ-01",
                "work_id": "MEWJ-01-W0001",
                "source_file_id": "source-01",
                "source_type": "word",
                "volume_number": 1,
                "paragraph_index": 1,
                "eligible_for_search": True,
                "text_raw": word_text,
                "normalized_text": normalize_text(word_text),
                "compact_text": compact_text(word_text),
                "plain_text": punctuationless_text(word_text),
                "document_title": "马克思恩格斯文集",
                "work_title": "《黑格尔法哲学批判》导言",
                "author_label": "卡·马克思",
                "page_display": "4",
                "page_source_type": "section_break_inferred",
                "original_page_start": "4",
                "original_page_end": "4",
            },
        ],
    }
    build_database(index, database_path)


class CitationFormatTests(unittest.TestCase):
    def test_bibliographic_missing_fields_exclude_isbn_and_survive_canonicalization(self) -> None:
        metadata = {
            "document_type": "translated_book",
            "title": "测试译著",
            "author": "测试作者",
            "publisher": None,
            "publish_place": None,
            "publish_year": "2026",
            "isbn": None,
            "metadata_missing_fields": ["translator", "publisher", "publish_place"],
        }
        self.assertEqual(
            metadata_missing_fields(metadata),
            ["translator", "publisher", "publish_place"],
        )
        self.assertNotIn("isbn", metadata_missing_fields(metadata))
        self.assertEqual(
            canonical_metadata(metadata)["metadata_missing_fields"],
            ["translator", "publisher", "publish_place"],
        )

    def test_journal_citation_chinese_hit_page_gb_article_range(self) -> None:
        # 中文脚注引命中页；GB/T 条目引文章起止页（用户示例：
        # 郑作彧. …[J]. 社会科学, 2021(3): 49-60.）。
        metadata = {
            "document_type": "journal_article",
            "author": "郑作彧",
            "article_title": "化用的生活形式，还是共鸣的世界关系？——批判理论第四代的共识与分歧",
            "journal_name": "社会科学",
            "publication_year": "2021",
            "issue": "3",
            "page_range": "49-60",
        }
        hit_page = {"start": "53"}
        self.assertEqual(
            format_citation(metadata, hit_page, "chinese"),
            "郑作彧：《化用的生活形式，还是共鸣的世界关系？——批判理论第四代的共识与分歧》，《社会科学》2021年第3期，第53页。",
        )
        self.assertEqual(
            format_citation(metadata, hit_page, "gb"),
            "郑作彧. 化用的生活形式，还是共鸣的世界关系？——批判理论第四代的共识与分歧[J]. 社会科学, 2021(3): 49-60.",
        )
        self.assertNotIn("49-60", format_citation(metadata, hit_page, "chinese"))

    def test_journal_citation_with_volume_matches_user_example(self) -> None:
        # 孙向晨《学术月刊》2017 年第 4 期（第 49 卷），文章 15-27 页，命中第 18 页。
        metadata = {
            "document_type": "journal_article",
            "author": "孙向晨",
            "title": "现代社会中的“家庭”及其所代表的伦理性原则——黑格尔《法哲学原理》中“家庭”问题的解读",
            "journal_name": "学术月刊",
            "publish_year": "2017",
            "volume": "49",
            "issue": "4",
            "page_range": "15-27",
        }
        hit_page = {"start": "18"}
        self.assertEqual(
            format_citation(metadata, hit_page, "gb"),
            "孙向晨. 现代社会中的“家庭”及其所代表的伦理性原则——黑格尔《法哲学原理》中“家庭”问题的解读[J]. 学术月刊, 2017, 49(4): 15-27.",
        )
        self.assertEqual(
            format_citation(metadata, hit_page, "chinese"),
            "孙向晨：《现代社会中的“家庭”及其所代表的伦理性原则——黑格尔〈法哲学原理〉中“家庭”问题的解读》，《学术月刊》2017年第4期，第18页。",
        )

    def test_chicago_note_forms_by_document_type(self) -> None:
        hit = {"start": "55"}
        journal = {
            "document_type": "journal_article", "author": "孙向晨", "title": "现代社会中的家庭",
            "journal_name": "学术月刊", "publish_year": "2017", "volume": "49", "issue": "4",
            "page_range": "15-27",
        }
        # 芝加哥脚注引命中页（55），而非文章起止页；有卷有期用 vol, no. issue。
        self.assertEqual(
            format_citation(journal, hit, "chicago"),
            '孙向晨, "现代社会中的家庭," 学术月刊 49, no. 4 (2017): 55.',
        )
        issue_only = dict(journal, volume="")
        self.assertEqual(
            format_citation(issue_only, hit, "chicago"),
            '孙向晨, "现代社会中的家庭," 学术月刊, no. 4 (2017): 55.',
        )
        book = {
            "document_type": "book", "author": "张三", "title": "现代性批判",
            "publisher": "商务印书馆", "publish_place": "北京", "publish_year": "2020",
        }
        self.assertEqual(
            format_citation(book, hit, "chicago"),
            "张三, 现代性批判 (北京: 商务印书馆, 2020), 55.",
        )
        thesis = {
            "document_type": "thesis", "author": "金芳冰", "title": "耶吉生活形式批判理论研究",
            "publisher": "大连理工大学", "publish_year": "2025",
        }
        self.assertEqual(
            format_citation(thesis, hit, "chicago"),
            '金芳冰, "耶吉生活形式批判理论研究" (学位论文, 大连理工大学, 2025), 55.',
        )
        translated = {
            "document_type": "translated_book", "author": "耶吉", "country": "德",
            "title": "生活形式批判", "translator": "李四", "publisher": "人民出版社",
            "publish_place": "北京", "publish_year": "2021",
        }
        self.assertEqual(
            format_citation(translated, hit, "chicago"),
            "耶吉, 生活形式批判, trans. 李四 (北京: 人民出版社, 2021), 55.",
        )

    def test_build_citation_formats_includes_chicago(self) -> None:
        metadata = {
            "document_type": "journal_article", "author": "周爱民", "title": "根本挑战",
            "journal_name": "社会科学", "publish_year": "2024", "issue": "5", "page_range": "53-61",
        }
        formats = build_citation_formats(metadata, {"start": "56"})
        self.assertIn("chicago", formats)
        self.assertEqual(formats["chicago_status"], "complete")
        self.assertEqual(formats["chicago_missing_fields"], [])
        self.assertTrue(formats["chicago"].endswith("(2024): 56."))
        # 缺刊名时芝加哥应判为不完整。
        incomplete = build_citation_formats(dict(metadata, journal_name=""), {"start": "56"})
        self.assertEqual(incomplete["chicago_status"], "metadata_incomplete")
        self.assertIn("journal_name", incomplete["chicago_missing_fields"])

    def test_build_citation_formats_includes_apa_and_mla(self) -> None:
        metadata = {
            "document_type": "journal_article", "author": "周爱民", "title": "根本挑战",
            "journal_name": "社会科学", "publish_year": "2024", "issue": "5",
            "page_range": "53-61", "doi": "10.13644/j.cnki.cn31-1112.2024.05.009",
        }
        formats = build_citation_formats(metadata, {"start": "56"})
        self.assertEqual(
            formats["apa"],
            "周爱民. (2024). 根本挑战. 社会科学(5), 53-61. "
            "https://doi.org/10.13644/j.cnki.cn31-1112.2024.05.009.",
        )
        self.assertEqual(
            formats["mla"],
            "周爱民. “根本挑战”. 社会科学, no. 5, 2024, pp. 53-61, "
            "https://doi.org/10.13644/j.cnki.cn31-1112.2024.05.009.",
        )
        self.assertEqual(formats["apa_status"], "complete")
        self.assertEqual(formats["mla_status"], "complete")
        self.assertEqual(formats["apa_missing_fields"], [])
        self.assertEqual(formats["mla_missing_fields"], [])

    def test_apa_and_mla_book_references_do_not_require_a_hit_page(self) -> None:
        metadata = {
            "document_type": "book", "author": "Amy Allen", "title": "The End of Progress",
            "publisher": "Columbia University Press", "publish_year": "2016",
        }
        formats = build_citation_formats(metadata, None)
        self.assertEqual(
            formats["apa"],
            "Amy Allen. (2016). The End of Progress. Columbia University Press.",
        )
        self.assertEqual(
            formats["mla"],
            "Amy Allen. The End of Progress. Columbia University Press, 2016.",
        )
        self.assertEqual(formats["apa_status"], "complete")
        self.assertEqual(formats["mla_status"], "complete")

    def test_zotero_filename_pattern_beats_cnki_embedded_author(self) -> None:
        detected = detect_pdf_bibliographic_metadata(
            Path("孙向晨 - 2017 - 现代社会中的“家庭”及其所代表的伦理性原则——黑格尔《法哲学原理》中“家庭”问题的解读.pdf"),
            [],
            {
                "title": "孙向晨 - 2017 - 现代社会中的“家庭”及其所代表的伦理性原则——黑格尔《法哲学原理》中“家庭”问题的解读",
                "author": "CNKI",
                "metadata_source": "automatic_recognition",
            },
        )
        self.assertEqual(detected["author"], "孙向晨")
        self.assertEqual(detected["publish_year"], "2017")
        self.assertEqual(
            detected["title"],
            "现代社会中的“家庭”及其所代表的伦理性原则——黑格尔《法哲学原理》中“家庭”问题的解读",
        )

    def test_pdf_file_label_and_artifact_author_do_not_replace_catalog_title(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "马恩全集第50卷.pdf"
            path.write_bytes(b"pdf")
            with patch(
                "src.me_finder.bibliographic_metadata._embedded_pdf_metadata",
                return_value={"title": "K93.pdf", "author": "kdc"},
            ):
                detected = detect_pdf_bibliographic_metadata(
                    path,
                    [],
                    {
                        "title": "K93.pdf",
                        "author": "kdc",
                        "metadata_source": "automatic_recognition",
                    },
                )

        self.assertEqual(detected["title"], "马恩全集第50卷")
        self.assertEqual(detected["author"], "马克思、恩格斯")
        self.assertEqual(detected["publisher"], "人民出版社")
        self.assertEqual(detected["publish_place"], "北京")
        self.assertEqual(detected["publish_year"], "1985")
        self.assertEqual(detected["volume"], "50")
        self.assertEqual(detected["metadata_evidence"]["title"]["source"], "collection_rule")

    def test_marx_engels_chinese_first_edition_volume_year_table(self) -> None:
        expected = {
            1: "1956", 2: "1957", 3: "1960", 4: "1958", 5: "1958",
            6: "1961", 7: "1959", 8: "1961", 9: "1961", 10: "1962",
            11: "1962", 12: "1962", 13: "1962", 14: "1964", 15: "1963",
            16: "1964", 17: "1963", 18: "1964", 19: "1963", 20: "1971",
            21: "1965", 22: "1965", 23: "1972", 24: "1972", 25: "1974",
            27: "1972", 28: "1973", 29: "1972", 30: "1975", 31: "1972",
            32: "1975", 33: "1973", 34: "1972", 35: "1971", 36: "1974",
            37: "1971", 38: "1972", 39: "1974", 40: "1982", 41: "1982",
            42: "1979", 43: "1982", 44: "1982", 45: "1985", 47: "1979",
            48: "1985", 49: "1982", 50: "1985",
        }
        for volume, year in expected.items():
            with self.subTest(volume=volume):
                metadata = marx_engels_first_edition_metadata(f"马恩全集第{volume:02d}卷.pdf")
                self.assertEqual(metadata["publish_year"], year)
                self.assertEqual(metadata["publisher"], "人民出版社")
                self.assertEqual(metadata["publish_place"], "北京")
                self.assertEqual(metadata["author"], "马克思、恩格斯")

        part_cases = {
            "马恩全集第26卷（一）.pdf": ("1972", "26卷第一册"),
            "马恩全集第26卷（二）.pdf": ("1973", "26卷第二册"),
            "马恩全集第26卷（三）.pdf": ("1974", "26卷第三册"),
            "马恩全集第26卷（中）.pdf": ("1973", "26卷第二册"),
            "马恩全集第46卷（上）.pdf": ("1979", "46卷上册"),
            "马恩全集第46卷（下）.pdf": ("1980", "46卷下册"),
        }
        for file_name, (year, volume_label) in part_cases.items():
            with self.subTest(file_name=file_name):
                metadata = marx_engels_first_edition_metadata(file_name)
                self.assertEqual(metadata["publish_year"], year)
                self.assertEqual(metadata["volume"], volume_label)
        self.assertEqual(
            marx_engels_first_edition_metadata("马恩全集第14卷（上）.pdf")["volume"],
            "14",
        )
        self.assertEqual(
            marx_engels_first_edition_metadata("马恩全集第01卷（下）.pdf")["publish_year"],
            "1956",
        )
        self.assertEqual(marx_engels_first_edition_metadata("马恩全集第50卷（三）.pdf"), {})

    def test_marx_engels_chinese_second_edition_from_me2_file_names(self) -> None:
        # 用户提供的第二版逐卷出版年份。第二版仍在出版，表中没有的卷不给年份。
        expected = {
            1: "1995", 2: "2005", 3: "2002", 10: "1998", 11: "1995",
            12: "1998", 13: "1998", 14: "2013", 16: "2007", 19: "2006",
            21: "2003", 25: "2001", 26: "2014", 28: "2018", 29: "2021",
            30: "1995", 31: "1998", 32: "1998", 33: "2004", 34: "2008",
            35: "2013", 36: "2015", 37: "2019", 38: "2019", 42: "2016",
            43: "2016", 44: "2001", 45: "2003", 46: "2003", 47: "2004",
            48: "2007", 49: "2016", 50: "2022",
        }
        for volume, year in expected.items():
            for file_name in (f"me2-{volume}.pdf", f"me2-{volume:02d}.pdf", f"ME2_{volume}.pdf"):
                with self.subTest(file_name=file_name):
                    metadata, rule = marx_engels_collection_metadata(file_name)
                    self.assertEqual(rule, "marx_engels_chinese_second_edition")
                    self.assertEqual(metadata["publish_year"], year)
                    self.assertEqual(metadata["publisher"], "人民出版社")
                    self.assertEqual(metadata["publish_place"], "北京")
                    self.assertEqual(metadata["author"], "马克思、恩格斯")
                    self.assertEqual(metadata["volume"], str(volume))
        # 书名用中文数字，与卷端页一致。
        self.assertEqual(
            marx_engels_collection_metadata("me2-31.pdf")[0]["title"],
            "马克思恩格斯全集第三十一卷",
        )
        self.assertEqual(
            marx_engels_collection_metadata("me2-1.pdf")[0]["title"],
            "马克思恩格斯全集第一卷",
        )

    def test_second_edition_never_invents_a_year_for_unpublished_volumes(self) -> None:
        # 第二版尚未出版的卷（如第 4、20、27 卷）：给书目框架，但绝不编造年份。
        for volume in (4, 5, 20, 27, 40, 41):
            with self.subTest(volume=volume):
                metadata, rule = marx_engels_collection_metadata(f"me2-{volume}.pdf")
                self.assertEqual(rule, "marx_engels_chinese_second_edition")
                self.assertNotIn("publish_year", metadata)
                self.assertEqual(metadata["author"], "马克思、恩格斯")
        # 越界卷号与不相关文件名都不匹配第二版规则。
        self.assertEqual(marx_engels_collection_metadata("me2-51.pdf"), ({}, ""))
        self.assertEqual(marx_engels_collection_metadata("me2-0.pdf"), ({}, ""))
        self.assertEqual(marx_engels_collection_metadata("随便一本书.pdf"), ({}, ""))
        # 第一版文件名仍优先命中第一版规则。
        self.assertEqual(
            marx_engels_collection_metadata("马恩全集第07卷.pdf")[1],
            "marx_engels_chinese_first_edition",
        )

    def test_slash_inside_title_is_not_a_responsibility_separator(self) -> None:
        # 《24/7：晚期资本主义与睡眠的终结》：CIP 题名里数字间的斜杠
        # 不是题名/责任者分隔符，书名不得被截断成"24"。
        pages = [
            {
                "pdf_page_index": 2,
                "text_raw": (
                    "图书在版编目(CIP)数据\n"
                    "24/7：晚期资本主义与睡眠的终结/（美）乔纳森·克拉里著；许多译. — 北京：中信出版社，2015.9\n"
                    "ISBN 978-7-5086-5122-6"
                ),
            },
        ]
        detected = detect_pdf_bibliographic_metadata(Path("missing.pdf"), pages, {})
        self.assertEqual(detected["title"], "24/7：晚期资本主义与睡眠的终结")
        self.assertEqual(detected["publisher"], "中信出版社")
        self.assertEqual(detected["publish_year"], "2015")

    def test_article_footnote_publishers_are_not_book_metadata(self) -> None:
        # 论文正文脚注引用"北京：人民出版社，1972年版"与版权页声明同形，
        # 不得被当作本篇的出版社/出版地/出版年份（孙向晨一文的真实污染源）。
        pages = [
            {
                "pdf_page_index": 3,
                "text_raw": (
                    "在《法哲学原理》中，黑格尔把家庭作为伦理生活的第一环节。\n"
                    "① 黑格尔：《法哲学原理》，范扬、张企泰译，北京：人民出版社，1972年版，第175页。\n"
                    "② 黑格尔：《精神现象学》，贺麟、王玖兴译，北京：商务印书馆，1979年版，第122页。"
                ),
            },
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("孙向晨 - 2017 - 现代社会中的“家庭”及其所代表的伦理性原则.pdf"),
            pages,
            {},
        )
        self.assertNotEqual(detected.get("publisher"), "人民出版社")
        self.assertNotEqual(detected.get("publish_year"), "1972")
        self.assertNotEqual(detected.get("publish_place"), "北京")
        self.assertEqual(detected.get("publish_year"), "2017")
        self.assertEqual(detected.get("author"), "孙向晨")

    def test_journal_name_from_masthead_cn_en(self) -> None:
        # 首页报头「中文刊名 + 英文刊名」相邻是强佐证，应认出刊名。
        pages = [
            {
                "pdf_page_index": 0,
                "text_raw": (
                    "复旦学报（社会科学版）\n"
                    "FUDAN JOURNAL (Social Sciences)\n"
                    "2018 年第 1 期\n"
                    "第二自然与自由\n"
                    "张双利\n"
                    "摘要 本文讨论卢卡奇对黑格尔第二自然概念的转化。"
                ),
            },
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("张双利 - 2018 - 第二自然与自由.pdf"), pages, {}
        )
        self.assertEqual(detected["document_type"], "journal_article")
        self.assertEqual(detected["journal_name"], "复旦学报(社会科学版)")

    def test_journal_name_from_masthead_name_and_year(self) -> None:
        # 同一行「刊名 YYYY 年第 N 期」。
        pages = [
            {
                "pdf_page_index": 0,
                "text_raw": (
                    "社会科学 2024 年第 5 期\n"
                    "论法兰克福学派批判理论面临的“根本挑战”\n"
                    "周爱民\n"
                    "摘要 本文从社会批判的方法论视角展开分析。"
                ),
            },
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("周爱民 - 2024 - 论法兰克福学派批判理论.pdf"), pages, {}
        )
        self.assertEqual(detected["journal_name"], "社会科学")

    def test_offprint_without_masthead_leaves_journal_name_missing(self) -> None:
        # 抽印本首页只有篇名/作者/摘要；篇名以“理论”结尾也不得被当作刊名，
        # 认不出刊名时保持缺失而非猜测。
        pages = [
            {
                "pdf_page_index": 0,
                "text_raw": (
                    "重思马克思的市民社会理论\n"
                    "张双利\n"
                    "摘要 “黑格尔−马克思问题”是理解马克思市民社会理论的关键线索。\n"
                    "关键词 市民社会 伦理性"
                ),
            },
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("重思马克思的市民社会理论_张双利.pdf"), pages, {}
        )
        self.assertEqual(detected["document_type"], "journal_article")
        self.assertIsNone(detected.get("journal_name"))

    def test_thesis_cover_extracts_title_author_school_and_defense_year(self) -> None:
        pages = [
            {
                "pdf_page_index": 0,
                "text_raw": (
                    "硕士学位论文\n"
                    "拉埃尔·耶吉生活形式批判理论研究\n"
                    "Research on Rahel Jaeggi’s Critical Theory of Forms of Life\n"
                    "作者姓名：\n"
                    "金芳冰\n"
                    "学\n"
                    "号：\n"
                    "22219008\n"
                    "指导教师：\n"
                    "李雪梅副教授\n"
                    "学科、专业：\n"
                    "马克思主义理论\n"
                    "答辩日期：\n"
                    "2025 年5 月29 日\n"
                    "大连理工大学\n"
                    "Dalian University of Technology"
                ),
            }
        ]

        detected = detect_pdf_bibliographic_metadata(
            Path("金芳冰.pdf"),
            pages,
            {
                "document_type": "journal_article",
                "journal_name": "旧误识别刊名",
                "issue": "1",
                "page_range": "1-20",
                "publish_place": "大连",
            },
        )

        self.assertEqual(detected["document_type"], "thesis")
        self.assertNotIn("subtype", detected)
        self.assertEqual(detected["title"], "拉埃尔·耶吉生活形式批判理论研究")
        self.assertEqual(detected["author"], "金芳冰")
        self.assertEqual(detected["publisher"], "大连理工大学")
        self.assertEqual(detected["publish_year"], "2025")
        self.assertEqual(detected["metadata_missing_fields"], [])
        self.assertEqual(detected["metadata_status"], "complete")
        for field in ("journal_name", "volume", "issue", "page_range", "publish_place"):
            self.assertIsNone(detected[field])

    def test_thesis_title_drops_author_prefix_from_filename(self) -> None:
        pages = [
            {
                "pdf_page_index": 0,
                "text_raw": (
                    "硕士学位论文\n"
                    "拉埃尔·耶吉生活形式批判理论研究\n"
                    "作者姓名：\n"
                    "金芳冰\n"
                    "答辩日期：\n"
                    "2025 年5 月29 日\n"
                    "大连理工大学"
                ),
            }
        ]

        detected = detect_pdf_bibliographic_metadata(
            Path("金芳冰 - 拉埃尔·耶吉生活形式批判理论研究.pdf"),
            pages,
            {"title": "金芳冰 - 拉埃尔·耶吉生活形式批判理论研究"},
        )

        self.assertEqual(detected["document_type"], "thesis")
        self.assertEqual(detected["author"], "金芳冰")
        self.assertEqual(detected["title"], "拉埃尔·耶吉生活形式批判理论研究")

    def test_thesis_title_keeps_author_like_leading_characters(self) -> None:
        # 作者名恰好是篇名开头字符、但后面不是分隔符时，不能误删。
        pages = [
            {
                "pdf_page_index": 0,
                "text_raw": (
                    "硕士学位论文\n"
                    "金芳冰研究综述\n"
                    "作者姓名：\n"
                    "金芳冰\n"
                    "答辩日期：\n"
                    "2025 年5 月29 日\n"
                    "大连理工大学"
                ),
            }
        ]

        detected = detect_pdf_bibliographic_metadata(
            Path("金芳冰研究综述.pdf"),
            pages,
            {"title": "金芳冰研究综述"},
        )

        self.assertEqual(detected["document_type"], "thesis")
        self.assertEqual(detected["title"], "金芳冰研究综述")

    def test_foreign_article_isbn_in_references_not_classified_as_book(self) -> None:
        # 外文论文正文无版权页，仅参考文献引用了带 ISBN 的图书；不应误判为著作。
        pages = [
            {"pdf_page_index": 0, "text_raw": (
                "Against Manichaeism: The Politics of Forms of Life and the Possibilities of Critique\n"
                "Rahel Jaeggi\n"
            )},
            {"pdf_page_index": 1, "text_raw": "Body text discussing forms of life and critique."},
            {"pdf_page_index": 8, "text_raw": (
                "References\n"
                "Honneth, A. (2014). Freedom's Right. Cambridge: Polity. ISBN 978-0-231-15645-0.\n"
            )},
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("Against Manichaeism.pdf"), pages, {}
        )
        self.assertEqual(detected["document_type"], "journal_article")

    def test_front_page_isbn_still_classifies_as_book(self) -> None:
        pages = [
            {"pdf_page_index": 0, "text_raw": "现代性批判\n张三 著"},
            {"pdf_page_index": 1, "text_raw": "版权页\nISBN 978-7-100-15860-2\n商务印书馆"},
        ]
        detected = detect_pdf_bibliographic_metadata(Path("现代性批判.pdf"), pages, {})
        self.assertIn(detected["document_type"], {"book", "translated_book"})

    def test_thesis_required_fields_do_not_use_journal_or_book_requirements(self) -> None:
        metadata = {
            "document_type": "thesis",
            "title": "拉埃尔·耶吉生活形式批判理论研究",
            "author": "金芳冰",
            "publisher": None,
            "publish_year": "2025",
        }

        self.assertEqual(metadata_missing_fields(metadata), ["publisher"])
        metadata["publisher"] = "大连理工大学"
        self.assertEqual(metadata_missing_fields(metadata), [])

        saved = manual_metadata(metadata)
        self.assertEqual(saved["document_type"], "thesis")
        self.assertEqual(saved["metadata_status"], "complete")

    def test_thesis_gb_citation_uses_degree_document_marker(self) -> None:
        metadata = {
            "document_type": "thesis",
            "title": "拉埃尔·耶吉生活形式批判理论研究",
            "author": "金芳冰",
            "publisher": "大连理工大学",
            "publish_year": "2025",
            "publish_place": "不应进入学位论文引文的旧值",
            "journal_name": "不应进入学位论文引文的旧值",
            "issue": "9",
            "page_range": "1-99",
        }

        self.assertEqual(
            format_citation(metadata, None, "gb"),
            "金芳冰. 拉埃尔·耶吉生活形式批判理论研究[D]. 大连理工大学, 2025.",
        )
        formats = build_citation_formats(metadata, None)
        self.assertEqual(formats["gb_status"], "complete")
        self.assertEqual(formats["gb_missing_fields"], [])

    def test_manual_metadata_accepts_journal_article_type(self) -> None:
        from src.me_finder.bibliographic_metadata import manual_metadata

        saved = manual_metadata({
            "document_type": "journal_article",
            "author": "孙向晨",
            "title": "现代社会中的“家庭”及其所代表的伦理性原则",
            "journal_name": "学术月刊",
            "publish_year": "2017",
            "volume": "49",
            "issue": "4",
            "page_range": "15-27",
        })
        self.assertEqual(saved["document_type"], "journal_article")
        self.assertEqual(saved["metadata_status"], "complete")
        self.assertEqual(saved["journal_name"], "学术月刊")
        self.assertEqual(saved["metadata_missing_fields"], [])
        partial = manual_metadata({
            "document_type": "journal_article",
            "author": "孙向晨",
            "title": "某篇论文",
        })
        self.assertIn("journal_name", partial["metadata_missing_fields"])
        self.assertIn("issue", partial["metadata_missing_fields"])
        self.assertNotIn("publisher", partial["metadata_missing_fields"])

    def test_manual_journal_metadata_normalizes_identifiers_and_preserves_matching_cnki_evidence(self) -> None:
        record_url = "https://oversea.cnki.net/kcms2/article/abstract?v=opaque"
        saved = manual_metadata(
            {
                "document_type": "journal_article",
                "author": "张双利",
                "title": "重思马克思的市民社会理论",
                "journal_name": "学术月刊",
                "publish_year": "2020",
                "issue": "09",
                "doi": "https://doi.org/10.19862/J.CNKI.XSYK.000034",
                "issn": "0439 8041",
                "metadata_evidence": {
                    "journal_name": {
                        "source": "cnki_lookup",
                        "value": "学术月刊",
                        "evidence_text": "学术月刊",
                        "record_url": record_url,
                    },
                    "title": {
                        "source": "cnki_lookup",
                        "value": "另一篇文章",
                        "evidence_text": "不应保留",
                        "record_url": record_url,
                    },
                },
            }
        )

        self.assertEqual(saved["doi"], "10.19862/j.cnki.xsyk.000034")
        self.assertEqual(saved["issn"], "0439-8041")
        self.assertEqual(saved["metadata_evidence"]["journal_name"]["record_url"], record_url)
        self.assertNotIn("title", saved["metadata_evidence"])

    def test_manual_edit_removes_stale_automatic_evidence(self) -> None:
        previous = {
            "document_type": "journal_article",
            "author": "张双利",
            "title": "旧篇名",
            "journal_name": "学术月刊",
            "publish_year": "2020",
            "issue": "9",
            "metadata_evidence": {
                "title": {"source": "journal_title_line", "evidence_text": "旧篇名"},
                "journal_name": {"source": "masthead", "evidence_text": "学术月刊"},
            },
        }
        saved = manual_metadata({**previous, "title": "人工修订篇名"}, previous)

        self.assertNotIn("title", saved["metadata_evidence"])
        self.assertIn("journal_name", saved["metadata_evidence"])

    def test_journal_citation_completeness_requires_catalog_fields(self) -> None:
        metadata = {
            "document_type": "journal_article",
            "author": "张双利",
            "title": "重思马克思的市民社会理论",
            "publish_year": "2020",
        }
        formats = build_citation_formats(metadata, {"start": "18"})

        self.assertEqual(formats["gb_status"], "metadata_incomplete")
        self.assertIn("journal_name", formats["gb_missing_fields"])
        self.assertIn("issue", formats["gb_missing_fields"])

    def test_book_and_translated_book_formats(self) -> None:
        book = {
            "document_type": "book",
            "author": "张一兵",
            "title": "回到马克思：经济学语境中的哲学话语",
            "publication_place": "南京",
            "publisher": "江苏人民出版社",
            "publication_year": "2009",
        }
        self.assertEqual(
            format_citation(book, "125", "chinese"),
            "张一兵：《回到马克思：经济学语境中的哲学话语》，江苏人民出版社，2009年，第125页。",
        )
        self.assertEqual(
            format_citation(book, "125", "gb"),
            "张一兵. 回到马克思：经济学语境中的哲学话语[M]. 南京: 江苏人民出版社, 2009: 125.",
        )

        translated = {
            "document_type": "translated_book",
            "country": "德",
            "author": "哈特穆特·罗萨",
            "title": "新异化的诞生：社会加速批判理论大纲",
            "translator": "郑作彧",
            "publication_place": "上海",
            "publisher": "上海人民出版社",
            "publication_year": "2018",
        }
        self.assertEqual(
            format_citation(translated, "110", "chinese"),
            "[德]哈特穆特·罗萨：《新异化的诞生：社会加速批判理论大纲》，郑作彧译，上海人民出版社，2018年，第110页。",
        )
        self.assertEqual(
            format_citation(translated, "110", "gb"),
            "[德]哈特穆特·罗萨. 新异化的诞生：社会加速批判理论大纲[M]. 郑作彧, 译. 上海: 上海人民出版社, 2018: 110.",
        )

    def test_strict_translated_book_and_cross_page_formats(self) -> None:
        metadata = {
            "document_type": "translated_book",
            "author": "南希·弗雷泽",
            "title": "食人资本主义",
            "translator": "蓝江",
            "publish_place": "上海",
            "publisher": "上海人民出版社",
            "publish_year": "2023",
        }
        self.assertEqual(
            format_citation(metadata, {"start": "197"}, "chinese"),
            "南希·弗雷泽：《食人资本主义》，蓝江译，上海人民出版社，2023年，第197页。",
        )
        self.assertEqual(
            format_citation(metadata, {"start": "197"}, "gb"),
            "南希·弗雷泽. 食人资本主义[M]. 蓝江, 译. 上海: 上海人民出版社, 2023: 197.",
        )
        self.assertEqual(
            format_citation(metadata, {"start": "197", "end": "198"}, "chinese"),
            "南希·弗雷泽：《食人资本主义》，蓝江译，上海人民出版社，2023年，第197—198页。",
        )
        self.assertEqual(
            format_citation(metadata, {"start": "197", "end": "198"}, "gb"),
            "南希·弗雷泽. 食人资本主义[M]. 蓝江, 译. 上海: 上海人民出版社, 2023: 197-198.",
        )

    def test_plain_monograph_omits_translator(self) -> None:
        metadata = {
            "document_type": "book",
            "author": "夏莹",
            "title": "生活形式与社会批判",
            "publish_place": "北京",
            "publisher": "中国社会科学出版社",
            "publish_year": "2020",
        }
        self.assertEqual(
            format_citation(metadata, "35", "chinese"),
            "夏莹：《生活形式与社会批判》，中国社会科学出版社，2020年，第35页。",
        )
        self.assertEqual(
            format_citation(metadata, "35", "gb"),
            "夏莹. 生活形式与社会批判[M]. 北京: 中国社会科学出版社, 2020: 35.",
        )

    def test_gb_fails_safely_for_missing_metadata(self) -> None:
        base = {"document_type": "book", "author": "南希·弗雷泽", "title": "食人资本主义"}
        for field, metadata in (
            ("出版社", {**base, "publish_place": "上海", "publish_year": "2023"}),
            ("出版地", {**base, "publisher": "上海人民出版社", "publish_year": "2023"}),
            ("出版年份", {**base, "publisher": "上海人民出版社", "publish_place": "上海"}),
        ):
            with self.subTest(field=field):
                output = format_citation(metadata, "111", "gb")
                self.assertIn(field, output)
                self.assertNotIn("[M]:111", output)
        formats = build_citation_formats(base, "111")
        self.assertEqual(formats["gb_status"], "metadata_incomplete")

    def test_uncalibrated_page_never_uses_pdf_physical_page(self) -> None:
        metadata = {
            "document_type": "book",
            "author": "夏莹",
            "title": "生活形式与社会批判",
            "publish_place": "北京",
            "publisher": "中国社会科学出版社",
            "publish_year": "2020",
        }
        hit = {"display": "PDF 第 228 页，引用页码尚未校准", "uncalibrated": True, "pdf_page_index": 227}
        self.assertEqual(format_citation(metadata, hit, "chinese"), "该文献页码尚未校准，不能生成可靠脚注。")
        self.assertEqual(format_citation(metadata, hit, "gb"), "该文献页码尚未校准，不能生成 GB/T 引文。")

    def test_front_matter_metadata_detection_and_manual_priority(self) -> None:
        pages = [
            {
                "pdf_page_index": 3,
                "text_raw": "[美]南希·弗雷泽 著，蓝江 译\n上海人民出版社\n2023年9月第1版",
            }
        ]
        detected = detect_pdf_bibliographic_metadata(
            __import__("pathlib").Path("missing.pdf"),
            pages,
            {"title": "食人资本主义", "author": "南希·弗雷泽"},
        )
        self.assertEqual(detected["translator"], "蓝江")
        self.assertEqual(detected["publisher"], "上海人民出版社")
        self.assertEqual(detected["publish_year"], "2023")
        self.assertEqual(detected["publish_place"], "上海")
        self.assertEqual(detected["metadata_evidence"]["translator"]["source_page"], 4)
        manual = manual_metadata({**detected, "translator": "人工译者"}, detected)
        preserved = detect_pdf_bibliographic_metadata(
            __import__("pathlib").Path("missing.pdf"), pages, manual
        )
        self.assertEqual(preserved["translator"], "人工译者")
        self.assertEqual(preserved["metadata_source"], "manual")

    def test_question_mark_corruption_is_rejected(self) -> None:
        self.assertFalse(is_valid_bibliographic_value("??????"))
        self.assertFalse(is_valid_bibliographic_value("??????2023"))
        self.assertTrue(is_valid_bibliographic_value("生活形式可以被批判吗？"))
        self.assertEqual(invalid_metadata_fields({"title": "??????"}), ["title"])
        self.assertEqual(invalid_metadata_fields({"author": "乔纳森?克拉里"}), ["author"])
        with self.assertRaisesRegex(ValueError, "title"):
            manual_metadata({"title": "??????", "author": "南希·弗雷泽"})
        with self.assertRaisesRegex(ValueError, "author"):
            manual_metadata({"title": "晚期资本主义与睡眠的终结", "author": "乔纳森?克拉里"})
        detected = detect_pdf_bibliographic_metadata(
            Path("missing.pdf"),
            [],
            {"title": "??????", "author": "??????", "publisher": "???????"},
        )
        self.assertEqual(detected["metadata_status"], "recognition_failed")
        self.assertNotEqual(detected["metadata_status"], "complete")

    def test_chinese_cover_cip_and_copyright_page_detection(self) -> None:
        pages = [
            {
                "pdf_page_index": 0,
                "text_raw": (
                    "晚期资本主义与睡眠的终结\n"
                    "[美] 乔纳森·克拉里 / 著\n"
                    "Jonathan Crary\n"
                    "许多 沈清 / 译\n"
                    "中信出版集团·CHINACITICPRESS"
                ),
            },
            {
                "pdf_page_index": 2,
                "text_raw": (
                    "图书在版编目（CIP）数据\n"
                    "24/7：晚期资本主义与睡眠的终结/（美）克拉里著；许多，沈清译．"
                    "一 北京：中信出版社，2015.9\n"
                    "译者：许多 沈清\n"
                    "出版发行：中信出版集团股份有限公司\n"
                    "版次：2015年9月第1版\n"
                    "书号：ISBN 978-7-5086-5282-5/D·316"
                ),
            },
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("missing.pdf"),
            pages,
            {
                "title": "晚期资本主义与睡眠的终结",
                "author": "（美）乔纳森?克拉里著",
            },
        )
        self.assertEqual(detected["author"], "乔纳森·克拉里")
        self.assertEqual(detected["translator"], "许多、沈清")
        self.assertEqual(detected["publisher"], "中信出版社")
        self.assertEqual(detected["publish_place"], "北京")
        self.assertEqual(detected["publish_year"], "2015")
        self.assertEqual(detected["isbn"], "978-7-5086-5282-5")
        self.assertEqual(detected["document_type"], "translated_book")
        self.assertEqual(
            detected["metadata_evidence"]["author"]["rejected_evidence"]["reason"],
            "suspicious_person_punctuation",
        )

    def test_chinese_publishing_center_cip_replaces_noisy_file_name(self) -> None:
        path = Path(
            "[象形文字·经典译丛]利维坦 ([英]托马斯·霍布斯；段保良译) "
            "(z-library.sk, 1lib.sk, z-lib.sk).pdf"
        )
        detected = detect_pdf_bibliographic_metadata(
            path,
            [
                {
                    "pdf_page_index": 1,
                    "text_raw": (
                        "图书在版编目（CIP）数据\n"
                        "利维坦 / （英）托马斯·霍布斯著；段保良译.\n"
                        "上海：东方出版中心，2024.10. -- "
                        "ISBN 978-7-5473-2525-4"
                    ),
                },
                {
                    "pdf_page_index": 2,
                    "text_raw": (
                        "利维坦\n"
                        "著 者 [英]托马斯·霍布斯\n"
                        "译 者 段保良\n"
                        "出版发行 东方出版中心\n"
                        "地 址 上海市仙霞路345号\n"
                        "版 次 2025年1月第1版"
                    ),
                },
            ],
            {
                "title": path.stem,
                "metadata_source": "automatic_recognition",
            },
        )

        self.assertEqual(detected["title"], "利维坦")
        self.assertEqual(detected["publisher"], "东方出版中心")
        self.assertEqual(detected["publish_place"], "上海")
        self.assertEqual(detected["publish_year"], "2025")
        self.assertEqual(
            detected["metadata_evidence"]["title"]["rule"],
            "chinese_cip_statement",
        )

    def test_traditional_classical_book_colophon_and_duplicate_suffix(self) -> None:
        detected = detect_pdf_bibliographic_metadata(
            Path("唐律疏议(1).pdf"),
            [
                {"pdf_page_index": 1, "text_raw": "廣\n律\n疏\n議唐長孫無忌等撰"},
                {
                    "pdf_page_index": 3,
                    "text_raw": (
                        "唐律疏議\n"
                        "[唐] 長孫無忌等撰\n"
                        "劉俊文 點校\n"
                        "中華書局出版\n"
                        "(北京王府井大街 36 號)\n"
                        "1983年11月第1版 1983年11月北京第1次印刷"
                    ),
                },
                {"pdf_page_index": 100, "text_raw": ""},
            ],
            {
                "title": "唐律疏议(1)",
                "metadata_source": "automatic_recognition",
            },
        )

        self.assertEqual(detected["title"], "唐律疏议")
        self.assertEqual(detected["author"], "長孫無忌等")
        self.assertEqual(detected["country"], "唐")
        self.assertEqual(detected["publisher"], "中华书局")
        self.assertEqual(detected["publish_place"], "北京")
        self.assertEqual(detected["publish_year"], "1983")
        self.assertEqual(detected["metadata_status"], "complete")
        self.assertEqual(
            detected["metadata_evidence"]["title"]["rule"],
            "windows_duplicate_suffix",
        )

    def test_classical_authorship_prose_resolves_colophon_ocr_error(self) -> None:
        detected = detect_pdf_bibliographic_metadata(
            Path("唐鉴(1)(1).pdf"),
            [
                {
                    "pdf_page_index": 2,
                    "text_raw": "(宋) 范祖禹撰\n金鑑\n上海古籍出版社",
                },
                {
                    "pdf_page_index": 3,
                    "text_raw": (
                        "唐鑑\n"
                        "[宋]范礼禹撰\n"
                        "上海古籍出版社出版\n"
                        "(上海瑞金二路272號)\n"
                        "1984年10月第1版 1984年10月第1次印刷"
                    ),
                },
                {
                    "pdf_page_index": 5,
                    "text_raw": "《唐鑑》十二卷，宋范祖禹（一〇四一——一〇九八）撰。",
                },
                {"pdf_page_index": 100, "text_raw": ""},
            ],
            {
                "title": "唐鉴(1)(1)",
                "metadata_source": "automatic_recognition",
            },
        )

        self.assertEqual(detected["title"], "唐鉴")
        self.assertEqual(detected["author"], "范祖禹")
        self.assertEqual(detected["country"], "宋")
        self.assertEqual(detected["publisher"], "上海古籍出版社")
        self.assertEqual(detected["publish_place"], "上海")
        self.assertEqual(detected["publish_year"], "1984")
        self.assertEqual(detected["metadata_status"], "complete")
        self.assertEqual(detected["metadata_conflicts"], [])

    def test_foucault_cip_statement_supplies_complete_current_edition_metadata(self) -> None:
        detected = detect_pdf_bibliographic_metadata(
            Path("性经验史 第二卷 快感的享用.pdf"),
            [
                {
                    "pdf_page_index": 2,
                    "text_raw": (
                        "图书在版编目(CIP)数据\n"
                        "性经验史.第2卷,快感的享用/(法)米歇尔·福柯著;余碧平译."
                        "—上海:上海人民出版社,2016\nISBN 978-7-208-13810-0"
                    ),
                }
            ],
            {"title": "性经验史 第二卷 快感的享用"},
            force=True,
        )
        self.assertEqual(detected["title"], "性经验史 第2卷：快感的享用")
        self.assertEqual(detected["author"], "米歇尔·福柯")
        self.assertEqual(detected["country"], "法")
        self.assertEqual(detected["translator"], "余碧平")
        self.assertEqual(detected["publisher"], "上海人民出版社")
        self.assertEqual(detected["publish_place"], "上海")
        self.assertEqual(detected["publish_year"], "2016")
        self.assertEqual(detected["metadata_status"], "complete")
        self.assertEqual(
            format_citation(detected, "3", "chinese"),
            "[法]米歇尔·福柯：《性经验史 第2卷：快感的享用》，余碧平译，上海人民出版社，2016年，第3页。",
        )

    def test_noisy_foucault_front_matter_uses_explicit_filename_roles(self) -> None:
        path = Path(
            "[学术前沿]规训与惩罚：监狱的诞生（修订译本）第5版第30次印刷 "
            "(（法）米歇尔·福柯著 刘北成, 杨远婴译) (Z-Library).pdf"
        )
        detected = detect_pdf_bibliographic_metadata(
            path,
            [
                {"pdf_page_index": 4, "text_raw": "[法] 米歇示·福柯著\n対北成栃远嬰译"},
                {
                    "pdf_page_index": 5,
                    "text_raw": (
                        "Simplified Chinese Copyright© 2019 by SDX Joint Publishing Company\n"
                        "ISBN 978-7-108-06584-I\n"
                        "2007 年 4 月北京第 3 版\n2019 年 9 月北京第 5 版\n"
                        "2022 年 12 月北京第 30 次印刷"
                    ),
                },
            ],
            {"title": path.stem},
            force=True,
        )
        self.assertEqual(detected["title"], "规训与惩罚：监狱的诞生（修订译本）")
        self.assertEqual(detected["author"], "米歇尔·福柯")
        self.assertEqual(detected["country"], "法")
        self.assertEqual(detected["translator"], "刘北成、杨远婴")
        self.assertEqual(detected["publisher"], "生活·读书·新知三联书店")
        self.assertEqual(detected["publish_place"], "北京")
        self.assertEqual(detected["publish_year"], "2019")
        self.assertEqual(detected["isbn"], "978-7-108-06584-1")
        self.assertEqual(detected["metadata_status"], "complete")
        self.assertEqual(
            format_citation(detected, "125", "chinese"),
            "[法]米歇尔·福柯：《规训与惩罚：监狱的诞生（修订译本）》，刘北成、杨远婴译，生活·读书·新知三联书店，2019年，第125页。",
        )

    def test_english_title_and_catalog_page_detection(self) -> None:
        pages = [
            {
                "pdf_page_index": 3,
                "text_raw": (
                    "The Belknap Press of\n"
                    "Harvard University Press\n"
                    "Cambridge, Massachusetts\n"
                    "London, England\n"
                    "2018\n"
                    "CRITIQUE OF FORMS OF LIFE\n"
                    "Rahel Jaeggi\n"
                    "translated by\n"
                    "Ciaran Cronin"
                ),
            },
            {
                "pdf_page_index": 4,
                "text_raw": (
                    "Title: Critique of forms of life / Rahel Jaeggi ; translated by Ciaran Cronin.\n"
                    "Description: Cambridge, Massachusetts : The Belknap Press of Harvard University Press, 2018.\n"
                    "Identifiers: ISBN 9780674737754 (cloth)"
                ),
            },
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("missing.pdf"),
            pages,
            {"title": "Critique of Forms of Life", "author": "Rahel Jaeggi"},
        )
        self.assertEqual(detected["translator"], "Ciaran Cronin")
        self.assertEqual(detected["publisher"], "The Belknap Press of Harvard University Press")
        self.assertEqual(detected["publish_place"], "Cambridge, Massachusetts")
        self.assertEqual(detected["publish_year"], "2018")
        self.assertEqual(detected["isbn"], "9780674737754")
        self.assertEqual(detected["document_type"], "translated_book")

    def test_loc_cip_block_overrides_stale_config_title_and_author(self) -> None:
        # 真实场景（Wilhelm, Axel Honneth: Reconceiving Social Philosophy）：
        # MVP 时代手工写进配置的书名/作者是错的（把研究对象当成了作者），
        # 且没有 metadata_source 标记；美国国会图书馆 CIP 数据应以高置信度
        # 覆盖这类无出处旧值。年份取版权行 © 2019，而非 CIP 登记年 [2018]。
        pages = [
            {
                "pdf_page_index": 4,
                "text_raw": (
                    "Axel Honneth\n"
                    "Reconceiving Social Philosophy\n"
                    "Dagmar Wilhelm\n"
                    "London • New York"
                ),
            },
            {
                "pdf_page_index": 5,
                "text_raw": (
                    "Published by Rowman & Littlefield International, Ltd.\n"
                    "6 Tinworth Street, London SE11 5AL, United Kingdom\n"
                    "Copyright © 2019 by Dagmar Wilhelm\n"
                    "Library of Congress Cataloging-in-Publication Data\n"
                    "Names: Wilhelm, Dagmar, 1975– author.\n"
                    "Title: Axel Honneth : reconceiving social philosophy / Dagmar Wilhelm.\n"
                    "Description: London ; New York : Rowman & Littlefield International Ltd, [2018] | Series: Refram-\n"
                    "ing the boundaries: thinking the political | Includes bibliographical references and index.\n"
                    "Identifiers: LCCN 2018041794"
                ),
            },
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("missing.pdf"),
            pages,
            {"title": "Reconceiving Social Philosophy", "author": "Axel Honneth"},
        )
        self.assertEqual(detected["title"], "Axel Honneth: Reconceiving Social Philosophy")
        self.assertEqual(detected["author"], "Dagmar Wilhelm")
        self.assertEqual(detected["publisher"], "Rowman & Littlefield International")
        self.assertEqual(detected["publish_place"], "London")
        self.assertEqual(detected["publish_year"], "2019")
        self.assertEqual(detected["metadata_status"], "complete")

    def test_existing_value_with_same_words_keeps_its_casing(self) -> None:
        # CIP 行的小写书名与配置里既有的正确大小写只是大小写差异时，
        # 保留既有写法，不做无谓替换。
        pages = [
            {
                "pdf_page_index": 4,
                "text_raw": "Title: Critique of forms of life / Rahel Jaeggi.\nIdentifiers: LCCN 2018",
            },
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("missing.pdf"),
            pages,
            {"title": "Critique of Forms of Life", "author": "Rahel Jaeggi"},
        )
        self.assertEqual(detected["title"], "Critique of Forms of Life")

    def test_back_matter_colophon_supplies_metadata(self) -> None:
        # 真实场景（劳动的主权者）：中文版权页在全书最后一页，CIP 的 ISBN
        # 标签与号码分在两行，版权页号码被 OCR 打散成"978 - 7 - 2 0 8 …"。
        pages = [
            {"pdf_page_index": 0, "text_raw": "劳动的主权者\n劳动的规范理论"},
            {"pdf_page_index": 150, "text_raw": "正文中间的一页，不含书目信息。"},
            {
                "pdf_page_index": 286,
                "text_raw": (
                    "图书在版编目(O P)数据\n"
                    "劳动的主权者：劳动的规范理论/ ( 德）阿克塞尔\n"
                    "• 霍耐特（ Axel Honneth)著 ；周爱民译. 一 上 海 ：\n"
                    "上海人民出版社，2025.— (霍耐特选集)• 一  ISBN \n"
                    "978-7-208-19833-3\n"
                    "发\n行 上海人民出版社发行中心\n"
                    "版\n次 2025年 12月第1 版\n"
                    "ISBN 978 - 7 - 2 0 8 - 1 9 8 3 3 -3 /0  759\n"
                    "定\n价\n88.00 元"
                ),
            },
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("missing.pdf"),
            pages,
            {"title": "劳动的主权者：劳动的规范理论", "author": "阿克塞尔·霍耐特"},
        )
        self.assertEqual(detected["translator"], "周爱民")
        self.assertEqual(detected["publisher"], "上海人民出版社")
        self.assertEqual(detected["publish_place"], "上海")
        self.assertEqual(detected["publish_year"], "2025")
        self.assertEqual(detected["isbn"], "978-7-208-19833-3")
        self.assertEqual(detected["metadata_status"], "complete")

    def test_journal_offprint_decodes_its_article_number(self) -> None:
        # 真实场景（学术月刊 2020 年第 9 期，知网导出件）：刊名与卷号根本没有
        # 印在版面上，但文章编号按 GB/T 编码了年、期、起始页与篇幅。
        pages = [
            {
                "pdf_page_index": 0,
                "text_raw": (
                    "重思马克思的市民社会理论\n"
                    "张双利\n"
                    "摘    要    “黑格尔−马克思问题”是我们理解马克思的市民社会理论的关键线索。\n"
                    "关键词    市民社会 现代国家\n"
                    "作者张双利，复旦大学哲学学院教授（上海 200433）。\n"
                    "中图分类号 A1\n"
                    "文献标识码 A\n"
                    "文章编号 0439-8041(2020)09-0015-13\n"
                    "DOI: 10.19862/j.cnki.xsyk.000034\n"
                    "引 言\n"
                    "近十多年来，中国学界对马克思市民社会理论的研究兴趣日益浓厚。\n"
                    "15"
                ),
            },
            {"pdf_page_index": 1, "text_raw": "正文继续。\n16"},
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("重思马克思的市民社会理论_张双利.pdf"), pages
        )

        self.assertEqual(detected["document_type"], "journal_article")
        self.assertEqual(detected["title"], "重思马克思的市民社会理论")
        self.assertEqual(detected["author"], "张双利")
        self.assertEqual(detected["publish_year"], "2020")
        self.assertEqual(detected["issue"], "9")
        # 起始页 15 加篇幅 13 页 → 止页 27。
        self.assertEqual(detected["page_range"], "15-27")
        self.assertEqual(detected["issn"], "0439-8041")
        self.assertEqual(detected["doi"], "10.19862/j.cnki.xsyk.000034")
        self.assertEqual(detected["metadata_evidence"]["issn"]["source_page"], 1)
        self.assertEqual(detected["metadata_evidence"]["doi"]["rule"], "explicit_doi")
        # 版面上没有刊名和卷号，必须留空待补，不得臆造。
        self.assertIsNone(detected["journal_name"])
        self.assertIsNone(detected["volume"])
        self.assertEqual(detected["metadata_missing_fields"], ["journal_name"])
        # 期刊不应被追问出版社/出版地。
        self.assertIsNone(detected["publisher"])
        self.assertIsNone(detected["publish_place"])

    def test_journal_offprint_renders_the_expected_gb_citation(self) -> None:
        metadata = {
            "document_type": "journal_article",
            "author": "张双利",
            "title": "重思马克思的市民社会理论",
            "journal_name": "学术月刊",
            "volume": "52",
            "issue": "9",
            "page_range": "15-27",
            "publish_year": "2020",
        }
        self.assertEqual(
            format_citation(metadata, 20, "gb"),
            "张双利. 重思马克思的市民社会理论[J]. 学术月刊, 2020, 52(9): 15-27.",
        )

    def test_book_front_matter_is_not_mistaken_for_a_journal(self) -> None:
        # 专著的内容提要同样含“摘要”“关键词”字样，不能因此走期刊链路。
        pages = [
            {
                "pdf_page_index": 0,
                "text_raw": (
                    "消费社会\n"
                    "内容摘要：本书讨论消费社会的结构。\n"
                    "关键词：消费 符号\n"
                    "图书在版编目(CIP)数据\n"
                    "消费社会/（法）让·鲍德里亚著；刘成富译. 一 南京：\n"
                    "南京大学出版社，2014. ISBN 978-7-305-04227-9\n"
                ),
            }
        ]
        detected = detect_pdf_bibliographic_metadata(Path("消费社会.pdf"), pages)

        self.assertEqual(detected["document_type"], "translated_book")
        self.assertEqual(detected["publisher"], "南京大学出版社")

    def test_series_list_does_not_supply_this_books_translator(self) -> None:
        pages = [
            {
                "pdf_page_index": 3,
                "text_raw": (
                    "Titles in the Series\n"
                    "The Risk of Freedom, Francesco Tava, translated by Jane Ledlie\n"
                    "Axel Honneth: Reconceiving Social Philosophy, Dagmar Wilhelm"
                ),
            },
            {
                "pdf_page_index": 5,
                "text_raw": (
                    "Published by Rowman & Littlefield International, Ltd.\n"
                    "Copyright © 2019 by Dagmar Wilhelm\n"
                    "Library of Congress Cataloging-in-Publication Data\n"
                    "Names: Wilhelm, Dagmar, 1975– author.\n"
                    "Title: Axel Honneth : reconceiving social philosophy / Dagmar Wilhelm.\n"
                    "Description: London ; New York : Rowman & Littlefield International Ltd, [2018]\n"
                    "Identifiers: LCCN 2018034388 (print) | ISBN 9781783486410 (electronic) | "
                    "ISBN 9781783486397 (cloth : alk. paper)"
                ),
            },
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("missing.pdf"),
            pages,
            {"title": "Reconceiving Social Philosophy", "author": "Axel Honneth"},
            force=True,
        )
        self.assertEqual(detected["author"], "Dagmar Wilhelm")
        self.assertIsNone(detected["translator"])
        self.assertEqual(detected["publisher"], "Rowman & Littlefield International")
        self.assertEqual(detected["publish_place"], "London")
        self.assertEqual(detected["publish_year"], "2019")
        self.assertEqual(detected["isbn"], "9781783486397")

    def test_preface_references_are_not_used_as_book_metadata(self) -> None:
        pages = [
            {
                "pdf_page_index": 14,
                "text_raw": (
                    "在我的这本著作中，我试图分三步表明。\n"
                    "这部著作最终于1992年在苏尔坎普出版社作为专著出版。"
                ),
            }
        ]
        detected = detect_pdf_bibliographic_metadata(
            Path("missing.pdf"),
            pages,
            {"title": "劳动的主权者：劳动的规范理论"},
        )
        self.assertIsNone(detected["author"])
        self.assertIsNone(detected["publisher"])
        self.assertIsNone(detected["publish_year"])

    def test_source_edition_year_is_not_used_as_current_publication_year(self) -> None:
        detected = detect_pdf_bibliographic_metadata(
            Path("missing.pdf"),
            [
                {
                    "pdf_page_index": 5,
                    "text_raw": (
                        "Leipzig 1921 / Verlag von Felix Meiner\n"
                        "据莱比锡费利克斯·迈纳出版社1921年版译出"
                    ),
                }
            ],
            {"title": "法哲学原理", "author": "黑格尔"},
        )
        self.assertIsNone(detected["publish_year"])

    def test_filename_separator_recovers_fused_translator_boundaries(self) -> None:
        detected = detect_pdf_bibliographic_metadata(
            Path("法哲学原理（德）黑格尔著 范杨, 张企泰译.pdf"),
            [
                {
                    "pdf_page_index": 4,
                    "text_raw": "法哲学原理\n〔德〕黑格尔著\n范扬张企泰译",
                }
            ],
            {"title": "法哲学原理", "author": "黑格尔"},
        )
        self.assertEqual(detected["translator"], "范扬，张企泰")
        evidence = detected["metadata_evidence"]["translator"]
        self.assertEqual(evidence["rule"], "front_matter_with_filename_name_boundaries")
        self.assertEqual(evidence["filename_boundary_evidence"], "范杨，张企泰")

    def test_automatic_detection_is_preview_and_does_not_claim_manual_source(self) -> None:
        existing = {"title": "食人资本主义", "author": "南希·弗雷泽"}
        detected = detect_pdf_bibliographic_metadata(
            Path("missing.pdf"),
            [{"pdf_page_index": 2, "text_raw": "蓝江 译\n上海人民出版社\n2023年9月第1版"}],
            existing,
        )
        self.assertEqual(detected["title"], "食人资本主义")
        self.assertEqual(detected["metadata_source"], "automatic_recognition")
        self.assertNotEqual(detected["metadata_source"], "manual")
        self.assertEqual(existing, {"title": "食人资本主义", "author": "南希·弗雷泽"})

    def test_chinese_metadata_round_trip_database_search_and_citation(self) -> None:
        metadata = {
            "document_type": "translated_book",
            "title": "食人资本主义",
            "author": "南希·弗雷泽",
            "translator": "蓝江",
            "publisher": "上海人民出版社",
            "publish_place": "上海",
            "publish_year": "2023",
            "metadata_status": "complete",
            "metadata_source": "manual",
        }
        serialized = json.dumps(metadata, ensure_ascii=False)
        self.assertEqual(json.loads(serialized), metadata)
        api_bytes = json.dumps({"ok": True, "metadata": metadata}, ensure_ascii=False).encode("utf-8")
        ui_state = json.loads(api_bytes.decode("utf-8"))["metadata"]
        self.assertEqual(ui_state, metadata)
        raw = "资本主义对照护活动的吞噬不是偶然现象。"
        source_id = "pdf-unicode-roundtrip"
        index = {
            "metadata": {"eligible_paragraph_count": 1},
            "source_files": [
                {
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "file_name": "食人资本主义.pdf",
                    "relative_path": "corpus/raw_pdf/食人资本主义.pdf",
                    "title": "旧标题",
                    "bibliographic_metadata": {"title": "旧标题"},
                }
            ],
            "volumes": [
                {
                    "volume_id": "PDF-UNICODE",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "display_title": "旧标题",
                }
            ],
            "works": [
                {
                    "work_id": "PDF-UNICODE-W0001",
                    "volume_id": "PDF-UNICODE",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "work_order": 1,
                    "title": "旧标题",
                }
            ],
            "paragraphs": [
                {
                    "paragraph_id": "PDF-UNICODE-P0001",
                    "volume_id": "PDF-UNICODE",
                    "work_id": "PDF-UNICODE-W0001",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "volume_number": None,
                    "paragraph_index": 1,
                    "eligible_for_search": True,
                    "text_raw": raw,
                    "normalized_text": normalize_text(raw),
                    "compact_text": compact_text(raw),
                    "plain_text": punctuationless_text(raw),
                    "document_title": "旧标题",
                    "work_title": "旧标题",
                    "volume_display": "旧标题",
                    "author_label": None,
                    "page_display": "第197页",
                    "page_source_type": "manual_segment",
                    "citation_page_start": "197",
                    "citation_page_end": "197",
                    "original_file_name": "食人资本主义.pdf",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "index.sqlite3"
            build_database(index, database_path)
            counts = update_metadata_in_database(database_path, source_id, metadata)
            self.assertEqual(counts["sources"], 1)
            engine = SearchEngine(database_path)
            try:
                result = engine.search("资本主义对照护活动的吞噬", source_type="pdf")
            finally:
                engine.close()
            restarted_engine = SearchEngine(database_path)
            try:
                restarted_result = restarted_engine.search("资本主义对照护活动的吞噬", source_type="pdf")
            finally:
                restarted_engine.close()
        self.assertEqual(result["total"], 1)
        self.assertEqual(restarted_result["total"], 1)
        item = result["results"][0]
        self.assertEqual(item["document_title"], "食人资本主义")
        self.assertEqual(item["author_label"], "南希·弗雷泽")
        self.assertEqual(
            item["citation_formats"]["chinese"],
            "南希·弗雷泽：《食人资本主义》，蓝江译，上海人民出版社，2023年，第197页。",
        )
        self.assertEqual(
            item["citation_formats"]["gb"],
            "南希·弗雷泽. 食人资本主义[M]. 蓝江, 译. 上海: 上海人民出版社, 2023: 197.",
        )

    def test_personal_collections_keep_the_personal_author(self) -> None:
        cases = (
            ("鲁迅全集", "鲁迅", "1"),
            ("毛泽东选集", "毛泽东", "1"),
            ("列宁全集", "列宁", "2"),
            ("费尔巴哈文集", "费尔巴哈", "3"),
        )
        for title, author, volume in cases:
            metadata = {
                "document_type": "book",
                "title": title,
                "volume": volume,
                "publisher": "人民出版社",
                "publish_place": "北京",
                "publish_year": "2025",
            }
            with self.subTest(title=title):
                self.assertEqual(
                    format_citation(metadata, "36", "chinese"),
                    f"{author}：《{title}》第{volume}卷，人民出版社，2025年，第36页。",
                )
                self.assertEqual(
                    format_citation(metadata, "36", "gb"),
                    f"{author}. {title}:第{volume}卷[M]. 北京: 人民出版社, 2025: 36.",
                )

    def test_edited_collection_uses_editor_responsibility(self) -> None:
        metadata = {
            "document_type": "book",
            "title": "中国哲学文集",
            "volume": "1",
            "editor": "张三",
            "publisher": "示例出版社",
            "publish_place": "北京",
            "publish_year": "2025",
        }
        self.assertEqual(
            format_citation(metadata, "36", "gb"),
            "张三，主编. 中国哲学文集:第1卷[M]. 北京: 示例出版社, 2025: 36.",
        )

    def test_confirmed_authorless_work_starts_with_title(self) -> None:
        metadata = {
            "document_type": "book",
            "title": "康熙字典：巳集上 水部",
            "responsibility_status": "none",
            "publisher": "中华书局",
            "publish_place": "北京",
            "publish_year": "1962",
        }
        self.assertEqual(
            format_citation(metadata, "50", "gb"),
            "康熙字典：巳集上 水部[M]. 北京: 中华书局, 1962: 50.",
        )

    def test_marx_engels_collection_volume_is_a_special_case(self) -> None:
        metadata = {
            "document_type": "marx_engels_collection",
            "collection_title": "马克思恩格斯文集",
            "volume_number": 1,
            "publication_place": "北京",
            "publisher": "人民出版社",
            "publication_year": "2009",
            "work_title": "关于费尔巴哈的提纲",
            "author": "卡·马克思",
        }
        self.assertEqual(
            format_citation(metadata, "499", "chinese"),
            "《马克思恩格斯文集》第1卷，北京：人民出版社，2009年，第499页。",
        )
        self.assertEqual(
            format_citation(metadata, "117", "gb"),
            "马克思恩格斯文集:第1卷[M].北京:人民出版社,2009,117.",
        )
        uncalibrated = {
            "display": "PDF 第 117 页，引用页码尚未校准",
            "uncalibrated": True,
        }
        self.assertEqual(
            format_citation(metadata, uncalibrated, "gb"),
            "该文献页码尚未校准，不能生成 GB/T 引文。",
        )

        first_edition = marx_engels_first_edition_metadata("马恩全集第26卷（一）.pdf")
        self.assertEqual(
            format_citation(first_edition, "1", "chinese"),
            "《马克思恩格斯全集》第26卷第一册，北京：人民出版社，1972年，第1页。",
        )
        self.assertEqual(
            format_citation(first_edition, "1", "gb"),
            "马克思恩格斯全集:第26卷第一册[M].北京:人民出版社,1972,1.",
        )

    def test_word_hit_page_only_accepts_verified_page_source_types(self) -> None:
        unverified_cases = (
            "section_break_inferred",
            "toc_range_bound",
            "unknown",
        )
        for source_type in unverified_cases:
            with self.subTest(page_source_type=source_type):
                hit = SearchEngine._hit_page(
                    {
                        "source_type": "word",
                        "page_source_type": source_type,
                        "original_page_start": "38",
                        "page_display": "38",
                    },
                    "word",
                    "第 38 页（未验证）",
                )
                self.assertTrue(hit["uncalibrated"])
                self.assertNotIn("start", hit)

        verified = SearchEngine._hit_page(
            {
                "source_type": "word",
                "page_source_type": "section_break_verified",
                "original_page_start": "38",
                "original_page_end": "39",
            },
            "word",
            "第 38–39 页",
        )
        self.assertEqual(verified["start"], "38")
        self.assertEqual(verified["end"], "39")

    def test_search_results_include_copyable_citation_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "index.sqlite3"
            _build_search_citation_fixture(database_path)
            engine = SearchEngine(database_path)
            try:
                result = engine.search(
                    "许多例子都可以说明经验主义在整个世界中的作用",
                    source_type="pdf",
                    limit=3,
                )
            finally:
                engine.close()
        self.assertGreater(result["total"], 0)
        item = result["results"][0]
        self.assertIn("citation_formats", item)
        self.assertIn("chinese", item["citation_formats"])
        self.assertIn("gb", item["citation_formats"])
        self.assertIn("第147页", item["citation_formats"]["chinese"])
        self.assertEqual(item["citation_formats"]["gb_status"], "complete")
        self.assertEqual(
            item["citation_formats"]["gb"],
            "[德]马克斯·霍克海默. 批判理论[M]. 李小兵等, 译. 重庆: 重庆出版社, 1990: 147.",
        )

    def test_word_search_uses_collection_volume_citation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "index.sqlite3"
            _build_search_citation_fixture(database_path)
            engine = SearchEngine(database_path)
            try:
                result = engine.search(
                    "宗教是人民的鸦片。",
                    source_type="word",
                    limit=1,
                )
            finally:
                engine.close()
        self.assertGreater(result["total"], 0)
        item = result["results"][0]
        self.assertEqual(
            item["citation_formats"]["chinese"],
            "该文献页码尚未校准，不能生成可靠脚注。",
        )
        self.assertEqual(
            item["citation_formats"]["gb"],
            "该文献页码尚未校准，不能生成 GB/T 引文。",
        )
        self.assertIn(
            "citation_page",
            item["citation_formats"]["chinese_missing_fields"],
        )


if __name__ == "__main__":
    unittest.main()

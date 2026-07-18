from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.me_finder.citations import build_citation_formats, format_citation
from src.me_finder.bibliographic_metadata import (
    canonical_metadata,
    detect_pdf_bibliographic_metadata,
    invalid_metadata_fields,
    is_valid_bibliographic_value,
    manual_metadata,
    metadata_missing_fields,
    update_metadata_in_database,
)
from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.search import SearchEngine


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

    def test_journal_citation_uses_hit_page_not_article_range(self) -> None:
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
            "郑作彧. 化用的生活形式，还是共鸣的世界关系？——批判理论第四代的共识与分歧[J]. 社会科学, 2021(3):53.",
        )
        self.assertNotIn("49-60", format_citation(metadata, hit_page, "chinese"))

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

    def test_search_results_include_copyable_citation_formats(self) -> None:
        engine = SearchEngine()
        try:
            result = engine.search("许多例子都可以说明经验主义在整个世界中的作用", source_type="pdf", limit=3)
            self.assertGreater(result["total"], 0)
            item = result["results"][0]
            self.assertIn("citation_formats", item)
            self.assertIn("chinese", item["citation_formats"])
            self.assertIn("gb", item["citation_formats"])
            self.assertIn("第147页", item["citation_formats"]["chinese"])
            self.assertEqual(item["citation_formats"]["gb_status"], "metadata_incomplete")
            self.assertIn("无法生成完整 GB/T 引文", item["citation_formats"]["gb"])
        finally:
            engine.close()

    def test_word_search_uses_collection_volume_citation(self) -> None:
        engine = SearchEngine()
        try:
            result = engine.search("宗教是人民的鸦片。", source_type="word", limit=1)
            self.assertGreater(result["total"], 0)
            item = result["results"][0]
            self.assertEqual(
                item["citation_formats"]["chinese"],
                "《马克思恩格斯文集》第1卷，北京：人民出版社，2009年，第4页。",
            )
            self.assertEqual(
                item["citation_formats"]["gb"],
                "马克思恩格斯文集:第1卷[M].北京:人民出版社,2009,4.",
            )
            self.assertNotIn("黑格尔法哲学批判", item["citation_formats"]["chinese"])
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()

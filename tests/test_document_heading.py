from __future__ import annotations

import unittest

from src.me_finder.document_heading import (
    HEADING_SOURCE_DOCUMENT_TOC,
    HEADING_SOURCE_MINERU_V2,
    HEADING_SOURCE_PDF_OUTLINE,
    apply_heading_assignments,
    assign_toc_candidate_levels,
    classify_outline_entries,
    derive_toc_headings,
    extract_toc_candidates,
    locate_toc_page,
    map_semantic_outline_to_blocks,
    map_v2_titles_to_blocks,
)
from src.me_finder.markdown_export import document_to_markdown


def _block(text, **extra):
    block = {"text": text, "type": "text"}
    block.update(extra)
    return block


def _page(index, blocks, text_raw=None):
    return {
        "pdf_page_index": index,
        "text_raw": text_raw if text_raw is not None else "\n".join(
            str(b.get("text") or "") for b in blocks
        ),
        "blocks": blocks,
    }


class OutlineClassificationTests(unittest.TestCase):
    def test_semantic_outline_like_horkheimer(self) -> None:
        entries = [
            {"level": 1, "title": "目录", "pdf_page": 47},
            {"level": 1, "title": "传统理论与批判理论", "pdf_page": 230},
            {"level": 2, "title": "批判态度不相信现存社会准则", "pdf_page": 247},
            {"level": 2, "title": "批判思想及其理论", "pdf_page": 250},
        ]
        self.assertEqual(classify_outline_entries(entries), "semantic")

    def test_page_navigation_outline_like_bronner(self) -> None:
        entries = [{"level": 1, "title": name, "pdf_page": i + 1} for i, name in
                   enumerate(["封面", "书名", "版权", "前言", "目录"])]
        entries += [{"level": 1, "title": str(n), "pdf_page": 16 + n} for n in range(1, 122)]
        self.assertEqual(classify_outline_entries(entries), "page_navigation")

    def test_freedom_rights_is_page_navigation(self) -> None:
        entries = [{"level": 1, "title": name, "pdf_page": i + 1} for i, name in
                   enumerate(["封面", "书名", "版权", "前言", "目录"])]
        entries += [{"level": 1, "title": str(n), "pdf_page": 9 + n} for n in range(1, 556)]
        self.assertEqual(classify_outline_entries(entries), "page_navigation")

    def test_empty_and_thin_outlines(self) -> None:
        self.assertEqual(classify_outline_entries([]), "none")
        # Two semantic titles is too thin to trust -> mixed (conservative).
        self.assertEqual(
            classify_outline_entries(
                [{"level": 1, "title": "序", "pdf_page": 1},
                 {"level": 1, "title": "正文", "pdf_page": 2}]
            ),
            "mixed",
        )


class SemanticOutlineMappingTests(unittest.TestCase):
    def test_l1_l2_map_to_blocks_and_export(self) -> None:
        pages = [
            _page(0, [_block("传统理论与批判理论"), _block("正文一")]),
            _page(1, [_block("批判态度不相信现存社会准则"), _block("正文二")]),
        ]
        outline = {
            "classification": "semantic",
            "entries": [
                {"level": 1, "title": "传统理论与批判理论", "pdf_page": 1},
                {"level": 2, "title": "批判态度不相信现存社会准则", "pdf_page": 2},
            ],
        }
        assignments, diagnostics = map_semantic_outline_to_blocks(outline, pages)
        self.assertEqual(diagnostics, [])
        written = apply_heading_assignments(pages, assignments, HEADING_SOURCE_PDF_OUTLINE)
        self.assertEqual(written, 2)
        self.assertEqual(pages[0]["blocks"][0]["document_heading_level"], 1)
        self.assertEqual(
            pages[0]["blocks"][0]["document_heading_source"], HEADING_SOURCE_PDF_OUTLINE
        )
        self.assertEqual(pages[1]["blocks"][0]["document_heading_level"], 2)
        markdown = document_to_markdown(pages)
        self.assertIn("# 传统理论与批判理论", markdown)
        self.assertIn("## 批判态度不相信现存社会准则", markdown)

    def test_nonunique_entry_is_not_guessed(self) -> None:
        pages = [_page(0, [_block("重复"), _block("重复")])]
        outline = {
            "classification": "semantic",
            "entries": [{"level": 1, "title": "重复", "pdf_page": 1}],
        }
        assignments, diagnostics = map_semantic_outline_to_blocks(outline, pages)
        self.assertEqual(assignments, [])
        self.assertEqual(len(diagnostics), 1)

    def test_nonsemantic_outline_maps_nothing(self) -> None:
        pages = [_page(16, [_block("正文")])]
        outline = {
            "classification": "page_navigation",
            "entries": [{"level": 1, "title": "1", "pdf_page": 17}],
        }
        assignments, _ = map_semantic_outline_to_blocks(outline, pages)
        self.assertEqual(assignments, [])


class PageNavigationGuardTests(unittest.TestCase):
    def test_numeric_bookmarks_never_enter_heading_tree(self) -> None:
        # Even if (hypothetically) fed as semantic, numeric titles are skipped.
        pages = [_page(16, [_block("1"), _block("正文")])]
        outline = {
            "classification": "semantic",
            "entries": [{"level": 1, "title": "1", "pdf_page": 17}],
        }
        assignments, _ = map_semantic_outline_to_blocks(outline, pages)
        self.assertEqual(assignments, [])
        self.assertNotIn("document_heading_level", pages[0]["blocks"][0])


class V2FallbackMappingTests(unittest.TestCase):
    def _bronner_like_v2(self):
        # Mirrors content_list_v2.json title-node shape: type/title, content.level,
        # content.title_content[].content, bbox, one title per page list.
        def title(text, level, bbox):
            return {
                "type": "title",
                "content": {"title_content": [{"type": "text", "content": text}], "level": level},
                "bbox": bbox,
            }
        return [
            [title("批判理论 Critical Theory", 1, [63, 73, 350, 141])],
            [{"type": "paragraph", "content": {}, "bbox": [0, 0, 1, 1]},
             title("核心集体", 2, [130, 287, 286, 315])],
        ]

    def test_v2_titles_map_to_blocks(self) -> None:
        pages = [
            _page(0, [_block("批判理论 Critical Theory", bbox=[63, 73, 350, 141])]),
            _page(1, [_block("正文", bbox=[125, 104, 933, 265]),
                      _block("核心集体", bbox=[130, 287, 286, 315])]),
        ]
        assignments, diagnostics = map_v2_titles_to_blocks(self._bronner_like_v2(), pages)
        self.assertEqual(diagnostics, [])
        apply_heading_assignments(pages, assignments, HEADING_SOURCE_MINERU_V2)
        self.assertEqual(pages[0]["blocks"][0]["document_heading_level"], 1)
        self.assertEqual(
            pages[0]["blocks"][0]["document_heading_source"], HEADING_SOURCE_MINERU_V2
        )
        self.assertEqual(pages[1]["blocks"][1]["document_heading_level"], 2)
        markdown = document_to_markdown(pages)
        self.assertIn("# 批判理论 Critical Theory", markdown)
        self.assertIn("## 核心集体", markdown)

    def test_bbox_disambiguates_duplicate_text(self) -> None:
        pages = [_page(0, [
            _block("核心集体", bbox=[10, 10, 20, 20]),   # running header (aside)
            _block("核心集体", bbox=[130, 287, 286, 315]),  # real title
        ])]
        v2 = [[{
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "核心集体"}], "level": 2},
            "bbox": [130, 287, 286, 315],
        }]]
        assignments, _ = map_v2_titles_to_blocks(v2, pages)
        self.assertEqual(assignments, [{"pdf_page_index": 0, "block_index": 1, "level": 2}])


class SourcePriorityTests(unittest.TestCase):
    def test_pdf_outline_wins_over_v2_but_text_level_preserved(self) -> None:
        pages = [_page(0, [_block("第一章", text_level=2, bbox=[1, 1, 2, 2])])]
        outline = {
            "classification": "semantic",
            "entries": [
                {"level": 1, "title": "第一章", "pdf_page": 1},
                {"level": 1, "title": "占位甲", "pdf_page": 1},
                {"level": 1, "title": "占位乙", "pdf_page": 1},
            ],
        }
        # Only "第一章" exists as a block; the two placeholders keep the outline
        # classified as semantic and are simply unmapped.
        pdf_assign, _ = map_semantic_outline_to_blocks(outline, pages)
        apply_heading_assignments(pages, pdf_assign, HEADING_SOURCE_PDF_OUTLINE)
        v2 = [[{
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": "第一章"}], "level": 2},
            "bbox": [1, 1, 2, 2],
        }]]
        v2_assign, _ = map_v2_titles_to_blocks(v2, pages)
        apply_heading_assignments(pages, v2_assign, HEADING_SOURCE_MINERU_V2)
        block = pages[0]["blocks"][0]
        self.assertEqual(block["document_heading_level"], 1)  # pdf_outline wins
        self.assertEqual(block["document_heading_source"], HEADING_SOURCE_PDF_OUTLINE)
        self.assertEqual(block["text_level"], 2)  # raw text_level untouched


def _v2_title(text, level, bbox):
    return {
        "type": "title",
        "content": {"title_content": [{"type": "text", "content": text}], "level": level},
        "bbox": bbox,
    }


class TocLocateAndClusterTests(unittest.TestCase):
    def test_locate_toc_page_from_frontmatter_bookmark(self) -> None:
        outline = {
            "classification": "page_navigation",
            "entries": [
                {"level": 1, "title": "封面", "pdf_page": 1},
                {"level": 1, "title": "目录", "pdf_page": 16},
                {"level": 1, "title": "1", "pdf_page": 17},
            ],
        }
        self.assertEqual(locate_toc_page(outline), 15)  # 0-based

    def test_flat_toc_single_block_is_all_level_1(self) -> None:
        pages = [{
            "pdf_page_index": 15,
            "blocks": [
                {"text": "目录", "bbox": [258, 75, 363, 108], "mineru_type": "text"},
                {"text": "第一章 法兰克福学派 1  \n第二章 方法问题 11  \n索引 105",
                 "bbox": [150, 273, 530, 756], "mineru_type": "text"},
            ],
        }]
        cands = extract_toc_candidates(pages, 15)
        self.assertEqual([c["text"] for c in cands],
                         ["第一章 法兰克福学派", "第二章 方法问题", "索引"])
        assign_toc_candidate_levels(cands, page_width=360.85)
        self.assertEqual([c["level"] for c in cands], [1, 1, 1])

    def test_indented_toc_yields_two_levels(self) -> None:
        pages = [{
            "pdf_page_index": 5,
            "blocks": [
                {"text": "第一章 甲", "bbox": [100, 200, 300, 220], "mineru_type": "text"},
                {"text": "小节一", "bbox": [140, 230, 300, 250], "mineru_type": "text"},
                {"text": "第二章 乙", "bbox": [100, 260, 300, 280], "mineru_type": "text"},
            ],
        }]
        cands = extract_toc_candidates(pages, 5)
        assign_toc_candidate_levels(cands, page_width=600.0)
        self.assertEqual([c["level"] for c in cands], [1, 2, 1])


class BronnerLikeTocHierarchyTests(unittest.TestCase):
    """A. page_navigation outline + 目录 bookmark -> correct nested outline."""

    def _doc(self):
        pages = [
            _page(0, [_block("批判理论 Critical Theory", bbox=[0, 0, 9, 9])]),  # book title
            _page(15, [
                {"text": "目录", "bbox": [258, 75, 363, 108], "mineru_type": "text"},
                {"text": "第一章 法兰克福学派 1  \n第二章 方法问题 11  \n索引 105",
                 "bbox": [150, 273, 530, 756], "mineru_type": "text"},
            ]),
            _page(16, [_block("第一章法兰克福学派", bbox=[63, 73, 350, 141]),
                       _block("社会研究所创立于1923年……正文")]),
            _page(17, [_block("核心集体", bbox=[130, 287, 286, 315]),
                       _block("霍克海默出生……正文")]),
            _page(26, [_block("方法问题", bbox=[63, 73, 350, 141]),
                       _block("正文……")]),
            _page(120, [_block("索引", bbox=[63, 73, 200, 141]),
                        _block("A", bbox=[63, 200, 90, 220])]),
        ]
        v2 = [
            [_v2_title("批判理论 Critical Theory", 1, [0, 0, 9, 9])],  # page 0
            [_v2_title("目录", 2, [258, 75, 363, 108])],               # page 15
        ]
        v2 += [[] for _ in range(16 - len(v2))]
        v2.append([_v2_title("第一章法兰克福学派", 2, [63, 73, 350, 141])])  # 16
        v2.append([_v2_title("核心集体", 2, [130, 287, 286, 315])])          # 17
        v2 += [[] for _ in range(26 - len(v2))]
        v2.append([_v2_title("方法问题", 2, [63, 73, 350, 141])])            # 26
        v2 += [[] for _ in range(120 - len(v2))]
        v2.append([_v2_title("索引", 2, [63, 73, 200, 141]),
                   _v2_title("A", 2, [63, 200, 90, 220])])                    # 120
        return pages, v2

    def test_nested_outline_and_exclusions(self) -> None:
        pages, v2 = self._doc()
        cands = extract_toc_candidates(pages, 15)
        assign_toc_candidate_levels(cands, page_width=360.85)
        from src.me_finder.document_heading import _collect_v2_titles
        v2_titles = _collect_v2_titles([(v2, 0)])
        toc_assign, section_assign, diags = derive_toc_headings(
            pages, v2_titles, cands, toc_page_index=15, page_count=121
        )
        apply_heading_assignments(pages, toc_assign, HEADING_SOURCE_DOCUMENT_TOC)
        apply_heading_assignments(pages, section_assign, HEADING_SOURCE_MINERU_V2)

        markdown = document_to_markdown(pages)
        heads = [l for l in markdown.splitlines() if l.startswith("#") and " " in l]
        self.assertEqual(
            heads,
            [
                "# 第一章法兰克福学派",  # chapter (document_toc, direct)
                "## 核心集体",          # section (mineru_v2)
                "# 方法问题",            # chapter (document_toc via 第二章-strip)
                "# 索引",                # end structure (document_toc)
            ],
        )
        # A章 book title / 目录 / index letter never enter the tree...
        self.assertNotIn("# 批判理论 Critical Theory", markdown)
        self.assertNotIn("目录", heads)
        self.assertNotIn("## A", markdown)
        # ...but 50-flat-## regression is gone (chapters are level 1).
        self.assertEqual(markdown.count("\n# "), 3)  # 3 level-1 chapters/index

    def test_source_tags_and_raw_levels_preserved(self) -> None:
        pages, v2 = self._doc()
        cands = extract_toc_candidates(pages, 15)
        assign_toc_candidate_levels(cands, page_width=360.85)
        from src.me_finder.document_heading import _collect_v2_titles
        toc_assign, section_assign, _ = derive_toc_headings(
            pages, _collect_v2_titles([(v2, 0)]), cands, toc_page_index=15, page_count=121
        )
        apply_heading_assignments(pages, toc_assign, HEADING_SOURCE_DOCUMENT_TOC)
        apply_heading_assignments(pages, section_assign, HEADING_SOURCE_MINERU_V2)
        chapter = pages[2]["blocks"][0]
        self.assertEqual(chapter["document_heading_level"], 1)
        self.assertEqual(chapter["document_heading_source"], HEADING_SOURCE_DOCUMENT_TOC)
        section = pages[3]["blocks"][0]
        self.assertEqual(section["document_heading_source"], HEADING_SOURCE_MINERU_V2)
        # Index letter block retains its original text but gets no heading level.
        index_letter = pages[5]["blocks"][1]
        self.assertEqual(index_letter["text"], "A")
        self.assertNotIn("document_heading_level", index_letter)


class PreChapterSectionTests(unittest.TestCase):
    """Regression: legitimate pre-chapter sections must not be range-excluded."""

    def test_pre_chapter_v2_title_becomes_level_1(self) -> None:
        pages = [
            _page(0, [_block("书 名 Book Title", bbox=[0, 0, 9, 9])]),   # cover/book title
            _page(3, [_block("序言", bbox=[52, 78, 150, 110]),          # pre-chapter section
                      _block("正文……preface")]),
            _page(15, [{"text": "目录", "bbox": [258, 75, 363, 108], "mineru_type": "text"},
                       {"text": "第一章 甲 1", "bbox": [150, 273, 530, 400], "mineru_type": "text"}]),
            _page(16, [_block("第一章甲", bbox=[63, 73, 350, 141]), _block("正文")]),
            _page(20, [_block("索引", bbox=[63, 73, 200, 141]),
                       _block("A", bbox=[63, 200, 90, 220])]),
        ]
        # 序言 IS a v2 title here (unlike Bronner), so the fix must admit it.
        v2 = [
            [_v2_title("书 名 Book Title", 1, [0, 0, 9, 9])],  # p0 cover -> excluded
            [], [], [_v2_title("序言", 2, [52, 78, 150, 110])],  # p3 pre-chapter section
        ]
        v2 += [[] for _ in range(15 - len(v2))]
        v2.append([_v2_title("目录", 2, [258, 75, 363, 108])])            # 15
        v2.append([_v2_title("第一章甲", 2, [63, 73, 350, 141])])          # 16
        v2 += [[] for _ in range(20 - len(v2))]
        v2.append([_v2_title("索引", 2, [63, 73, 200, 141]),
                   _v2_title("A", 2, [63, 200, 90, 220])])                # 20

        cands = extract_toc_candidates(pages, 15)
        assign_toc_candidate_levels(cands, page_width=360.85)
        from src.me_finder.document_heading import _collect_v2_titles
        toc_assign, section_assign, _ = derive_toc_headings(
            pages, _collect_v2_titles([(v2, 0)]), cands, toc_page_index=15, page_count=21
        )
        apply_heading_assignments(pages, toc_assign, HEADING_SOURCE_DOCUMENT_TOC)
        apply_heading_assignments(pages, section_assign, HEADING_SOURCE_MINERU_V2)

        preface = pages[1]["blocks"][0]
        self.assertEqual(preface["text"], "序言")
        self.assertEqual(preface["document_heading_level"], 1)  # pre-chapter -> level 1
        markdown = document_to_markdown(pages)
        self.assertIn("# 序言", markdown)
        # cover book title / 目录 / index letter still excluded
        self.assertNotIn("# 书 名 Book Title", markdown)
        self.assertNotIn("## A", markdown)
        self.assertNotIn("目录", [l for l in markdown.splitlines() if l.startswith("#")])


class FidelityTests(unittest.TestCase):
    """D. Original content is never mutated by heading enrichment."""

    def test_text_and_page_number_blocks_untouched(self) -> None:
        page_number_block = {"text": "1", "mineru_type": "page_number", "bbox": [461, 935, 480, 950]}
        pages = [_page(16, [_block("第一章甲", bbox=[1, 1, 2, 2], text_level=2), page_number_block],
                       text_raw="第一章甲\n正文\n1")]
        v2 = [[] for _ in range(16)] + [[_v2_title("第一章甲", 2, [1, 1, 2, 2])]]
        assignments, _ = map_v2_titles_to_blocks(v2, pages)
        apply_heading_assignments(pages, assignments, HEADING_SOURCE_MINERU_V2)
        self.assertEqual(pages[0]["text_raw"], "第一章甲\n正文\n1")
        self.assertEqual(pages[0]["blocks"][0]["text_level"], 2)  # raw level untouched
        self.assertEqual(page_number_block, {"text": "1", "mineru_type": "page_number",
                                             "bbox": [461, 935, 480, 950]})  # untouched


if __name__ == "__main__":
    unittest.main()

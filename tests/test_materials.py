from __future__ import annotations

import base64
import json
import os
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import fitz

from src.me_finder.app_context import AppContext
from src.me_finder.backup_service import create_backup, restore_backup
from src.me_finder.database import build_database, delete_source_from_database, replace_source_in_database
from src.me_finder.errors import UserFacingError
from src.me_finder.materials.images import collect_image, edit_image, image_path, preview_pdf, read_image
from src.me_finder.materials.service import list_materials, mutate_materials
from src.me_finder.persistence.connection import open_write
from src.me_finder.persistence.materials_store import TABLES, replace_materials_snapshot
from src.me_finder.persistence.migrations import migrate_index_database
from src.me_finder.web import ManagedThreadingHTTPServer, make_handler


TITLE = "薄膜实验记录"
SOURCE_ID = "materials-test"
TEXTS = ["实验开始。🎞薄膜的红外光谱保留峰位。", "薄膜的制备过程列出温度。"]


def document(texts: list[str], *, source: str = SOURCE_ID, prefix: str = "38",
             source_format: str = "epub", printed: bool = True) -> dict:
    """Build a small searchable text source with printed page anchors."""
    return {
        "metadata": {}, "volumes": [], "works": [],
        "source_files": [{
            "source_file_id": source, "source_type": "word", "source_format": source_format,
            "file_name": f"demo.{source_format}", "display_title": TITLE,
            "bibliographic_metadata": {"author": "实验作者", "title": TITLE},
        }],
        "paragraphs": [
            {"paragraph_id": f"{source}-P{index:06d}", "paragraph_index": index,
             "work_id": f"{source}-work", "source_type": "word", "source_file_id": source, "volume_number": None,
             "text_raw": text, "normalized_text": text, "compact_text": text, "plain_text": text,
             "eligible_for_search": True, "document_title": TITLE,
             "page_source_type": "epub_page_list" if printed else "uncalibrated",
             "original_page_start": str(int(prefix) + index) if printed else None,
             "page_display": str(int(prefix) + index) if printed else None}
            for index, text in enumerate(texts)
        ],
    }


def selection(text: str, *, index: int = 0, offset: int = 0, source: str = SOURCE_ID) -> dict:
    """Represent one exact paragraph slice using Unicode codepoints."""
    return {"source_id": source, "kind": "paragraph", "text": text,
            "start_anchor": f"{source}-P{index:06d}", "end_anchor": f"{source}-P{index:06d}",
            "start_offset": offset, "end_offset": offset + len(text)}


class MaterialsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "index.sqlite3"
        build_database(document(TEXTS), self.db)

    def collect(self, *selections: dict, tags: list[str] | None = None) -> dict:
        return mutate_materials(self.db, {
            "action": "collect", "tags": ["#实验／表征／FTIR"] if tags is None else tags,
            "selections": list(selections),
        })

    def locate(self, excerpt_id: int) -> dict:
        return mutate_materials(self.db, {"action": "locate", "excerpt_id": excerpt_id})

    def test_v11_migration_preserves_existing_index(self) -> None:
        with open_write(self.db) as connection:
            for table in reversed(TABLES):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("PRAGMA user_version=11")
            connection.commit()
        self.assertEqual(list_materials(self.db)["material_excerpts"], [])
        captured = self.collect(selection(TEXTS[0]))
        self.assertEqual(self.locate(captured["excerpt_ids"][0])["excerpt"]["citation_start"], "38")
        with open_write(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 13)
        self.assertFalse(migrate_index_database(self.db))

    def test_batch_is_atomic_and_repeated_collection_only_adds_tags(self) -> None:
        first = selection("🎞薄膜", offset=5)
        second = selection(TEXTS[1], index=1)
        with self.assertRaisesRegex(UserFacingError, "原文位置已变化"):
            self.collect(first, {**second, "text": "并非原文"})
        self.assertEqual(list_materials(self.db), {table: [] for table in TABLES})
        with self.assertRaisesRegex(UserFacingError, "最多三级"):
            self.collect(first, tags=["实验/表征/FTIR/峰高"])
        result = self.collect(first, second)
        self.assertEqual(result["created"], 2)
        repeated = self.collect(first, tags=["实验/表征/XPS"])
        self.assertEqual(repeated, {"created": 0, "excerpt_ids": result["excerpt_ids"][:1]})
        snapshot = list_materials(self.db)
        self.assertEqual([tag["path"] for tag in snapshot["material_tags"]],
                         ["实验", "实验/表征", "实验/表征/FTIR", "实验/表征/XPS"])
        saved = self.locate(result["excerpt_ids"][0])
        self.assertEqual(saved["excerpt"]["tags"], ["实验/表征/FTIR", "实验/表征/XPS"])
        self.assertEqual(saved["reader"]["pageMatchSpans"][0]["page_char_start"], 5)
        mutate_materials(self.db, {"action": "edit", "excerpt_id": result["excerpt_ids"][0],
                                   "tags": ["实验/表征"], "note": "比较峰位"})
        saved = self.locate(result["excerpt_ids"][0])["excerpt"]
        self.assertEqual((saved["tags"], saved["note"]), (["实验/表征"], "比较峰位"))

    def test_concurrent_collection_preserves_both_tag_assignments(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.collect, selection(TEXTS[0]), tags=[path])
                       for path in ("实验/FTIR", "实验/XPS")]
        excerpts = [result.result() for result in futures]
        self.assertEqual(sum(result["created"] for result in excerpts), 1)
        self.assertEqual(self.locate(excerpts[0]["excerpt_ids"][0])["excerpt"]["tags"],
                         ["实验/FTIR", "实验/XPS"])

    def test_reimport_reanchors_exact_text_and_persists_new_printed_page(self) -> None:
        excerpt = self.collect(selection(TEXTS[0]))["excerpt_ids"][0]
        replace_source_in_database(document(["插入的段落", TEXTS[1], "新版：" + TEXTS[0]], prefix="54"),
                                   self.db, backup_existing=False)
        located = self.locate(excerpt)
        self.assertEqual(located["status"], "located")
        self.assertEqual((located["excerpt"]["start_anchor"], located["excerpt"]["start_offset"]),
                         (f"{SOURCE_ID}-P000002", 3))
        self.assertEqual(located["excerpt"]["citation_start"], "56")
        persisted = list_materials(self.db)["material_excerpts"][0]
        self.assertEqual((persisted["start_anchor"], persisted["citation_start"]),
                         (f"{SOURCE_ID}-P000002", "56"))
        replace_source_in_database(document(["不同内容", TEXTS[0], "修改后不含原文", TEXTS[0]]),
                                   self.db, backup_existing=False)
        result = self.locate(excerpt)
        self.assertEqual(result["status"], "text_changed")
        self.assertEqual((result["excerpt"]["text"], result["excerpt"]["citation_start"]), (TEXTS[0], "56"))
        self.assertNotIn("reader", result)

    def test_collect_after_reimport_reuses_existing_excerpt_before_opening_it(self) -> None:
        saved = self.collect(selection(TEXTS[0]))["excerpt_ids"][0]
        replace_source_in_database(document([TEXTS[1], TEXTS[0]], prefix="53"),
                                   self.db, backup_existing=False)
        result = self.collect(selection(TEXTS[0], index=1), tags=["实验/XPS"])
        self.assertEqual(result, {"created": 0, "excerpt_ids": [saved]})
        excerpt = self.locate(saved)["excerpt"]
        self.assertEqual(excerpt["tags"], ["实验/XPS", "实验/表征/FTIR"])
        self.assertEqual(excerpt["citation_start"], "54")
        second_instance = replace_source_in_database(
            document([TEXTS[0], TEXTS[0]], prefix="53"), self.db, backup_existing=False)
        self.assertEqual(second_instance["paragraph_count"], 2)
        self.assertEqual(self.collect(selection(TEXTS[0]))["created"], 1)

    def test_rebuild_and_delete_preserve_material_with_original_citation(self) -> None:
        excerpt = self.collect(selection(TEXTS[0]))["excerpt_ids"][0]
        second = document(["另一本文献留下"], source="second-test")
        rebuilt = document(TEXTS)
        for field in ("source_files", "paragraphs"):
            rebuilt[field] += second[field]
        build_database(rebuilt, self.db)
        self.assertEqual(self.locate(excerpt)["status"], "located")
        another = self.collect(selection("另一本文献留下", source="second-test"))["excerpt_ids"][0]
        delete_source_from_database(SOURCE_ID, self.db, backup_existing=False)
        result = self.locate(excerpt)
        self.assertEqual(result["status"], "source_deleted")
        self.assertEqual((result["excerpt"]["text"], result["excerpt"]["citation_start"],
                          result["excerpt"]["tags"]), (TEXTS[0], "38", ["实验/表征/FTIR"]))
        self.assertEqual(self.locate(another)["status"], "located")

    def test_backup_round_trips_material_even_without_original_files(self) -> None:
        excerpt = self.collect(selection(TEXTS[0]))["excerpt_ids"][0]
        mutate_materials(self.db, {"action": "edit", "excerpt_id": excerpt, "note": "备份中的备注"})
        archive = create_backup(self.root, index_path=self.db)
        with zipfile.ZipFile(BytesIO(archive)) as backup:
            self.assertNotIn("index.sqlite3", backup.namelist())
            self.assertIn("config/materials.json", backup.namelist())
        restore = restore_backup(self.root, archive)
        restored_db = self.root / "restored.sqlite3"
        build_database(document([], source="other-source"), restored_db)
        with open_write(restored_db) as connection:
            for table in reversed(TABLES):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("PRAGMA user_version=11")
            connection.commit()
        replace_materials_snapshot(restore["materials_snapshot"], restored_db)
        self.assertEqual(list_materials(restored_db), list_materials(self.db))
        self.assertEqual(mutate_materials(restored_db, {"action": "locate", "excerpt_id": excerpt})["status"],
                         "source_deleted")
        self.assertIsNone(restore_backup(self.root, create_backup(self.root))["materials_snapshot"])

    def test_import_image_deduplicates_and_survives_rebuild_and_backup(self) -> None:
        with fitz.open() as pdf:
            pdf.new_page(width=20, height=20)
            png = pdf[0].get_pixmap().tobytes("png")
        payload = {"action": "collect_image", "title": "diagram.png", "tags": ["实验/表征"],
                   "note": "外部参考图", "data": base64.b64encode(png).decode("ascii")}
        saved = collect_image(self.db, self.root, payload, lambda _: self.fail("external image opened a PDF"))
        self.assertTrue(saved["created"])
        duplicate = collect_image(self.db, self.root, {**payload, "title": "copy.png"}, lambda _: self.fail())
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["digest"], saved["digest"])
        self.assertEqual(list(image_path(self.root, saved["digest"]).parent.iterdir()),
                         [image_path(self.root, saved["digest"])])
        edit_image(self.db, {"digest": saved["digest"], "tags": ["实验/光谱"], "note": "修订说明"})
        snapshot = list_materials(self.db)
        self.assertEqual(snapshot["material_images"][0]["note"], "修订说明")
        self.assertEqual(snapshot["material_image_tags"], [{"digest": saved["digest"], "path": "实验/光谱"}])
        legacy = {name: [] for name in ("material_tags", "material_excerpts", "material_excerpt_tags")}
        replace_materials_snapshot(legacy, self.db)
        preserved = list_materials(self.db)
        self.assertEqual(preserved["material_images"], snapshot["material_images"])
        self.assertEqual(preserved["material_image_tags"], snapshot["material_image_tags"])
        snapshot = preserved
        build_database(document(TEXTS), self.db)
        self.assertEqual(list_materials(self.db), snapshot)
        archive = create_backup(self.root, index_path=self.db)
        with TemporaryDirectory() as directory:
            restored_root = Path(directory)
            restored_db = restored_root / "index.sqlite3"
            build_database(document([]), restored_db)
            restored = restore_backup(restored_root, archive)
            replace_materials_snapshot(restored["materials_snapshot"], restored_db)
            self.assertEqual(list_materials(restored_db), snapshot)
            self.assertEqual(read_image(restored_db, restored_root, saved["digest"]), (png, "image/png"))

    def test_crop_rotated_pdf_uses_display_coordinates_and_printed_page(self) -> None:
        original = self.root / "rotated.pdf"
        with fitz.open() as pdf:
            page = pdf.new_page(width=100, height=160)
            page.draw_rect(fitz.Rect(0, 0, 100, 80), fill=(1, 0, 0), color=None)
            page.set_rotation(90)
            pdf.save(original)
        index = document([])
        index["source_files"][0].update(source_type="pdf", source_format="pdf", file_name=original.name)
        index["pdf_pages"] = [{"source_file_id": SOURCE_ID, "pdf_page_index": 0,
                               "page_source_type": "manual_page", "citation_page": "38"}]
        build_database(index, self.db)
        request = {"source_id": SOURCE_ID, "page": 1}
        preview = preview_pdf(self.db, {**request, "action": "preview_pdf"}, lambda _: original)
        self.assertEqual(preview["page_count"], 1)
        saved = collect_image(self.db, self.root, {
            **request, "action": "collect_image", "revision": preview["revision"],
            "box": [0.60, 0.10, 0.90, 0.60], "tags": ["实验"],
        }, lambda _: original)
        data, _ = read_image(self.db, self.root, saved["digest"])
        crop = fitz.Pixmap(data)
        self.assertEqual(crop.pixel(crop.width // 2, crop.height // 2), (255, 0, 0))
        origin = json.loads(list_materials(self.db)["material_images"][0]["origin"])
        self.assertEqual(origin["citation_page"], "38")
        self.assertEqual(origin["page"], 1)
        delete_source_from_database(SOURCE_ID, self.db, backup_existing=False)
        self.assertEqual(read_image(self.db, self.root, saved["digest"])[0], data)

    def test_rejects_backup_with_missing_material_parent(self) -> None:
        archive = BytesIO()
        payload = {table: [] for table in TABLES}
        payload["material_tags"] = [{"path": "实验/XPS"}]
        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.writestr("backup.json", json.dumps({"marker": "me_finder_backup", "version": 1}))
            zip_file.writestr("config/materials.json", json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "素材库快照格式无效"):
            restore_backup(self.root, archive.getvalue())

    def test_desktop_markdown_export_writes_only_selected_excerpt_with_pages(self) -> None:
        ids = self.collect(selection(TEXTS[0]), selection(TEXTS[1], index=1))["excerpt_ids"]
        result = mutate_materials(self.db, {
            "action": "export", "heading": "实验 / 表征", "excerpt_ids": ids[:1],
            "output_dir": str(self.root),
        })
        path = Path(result["path"])
        self.assertEqual(path.parent, self.root)
        markdown = path.read_text(encoding="utf-8")
        self.assertIn(TEXTS[0], markdown)
        self.assertIn("第 38 页", markdown)
        self.assertIn("实验/表征/FTIR", markdown)
        self.assertNotIn(TEXTS[1], markdown)

    def test_epub_without_publisher_pagination_stays_unverified(self) -> None:
        build_database(document(TEXTS, printed=False), self.db)
        result = self.locate(self.collect(selection(TEXTS[0]))["excerpt_ids"][0])
        self.assertEqual(result["status"], "located")
        self.assertIsNone(result["excerpt"]["citation_start"])
        self.assertIsNone(result["excerpt"]["citation_end"])

    def test_reader_range_spans_two_pdf_pages(self) -> None:
        pdf = "pdf-materials"
        anchors = [f"{pdf}-PAGE-00000{i}" for i in range(2)]
        build_database({
            "metadata": {}, "volumes": [], "works": [], "paragraphs": [],
            "source_files": [{"source_file_id": pdf, "source_type": "pdf", "file_name": "demo.pdf"}],
            "pdf_pages": [{"source_file_id": pdf, "pdf_page_index": i, "pdf_page_id": anchor,
                           "text_raw": text, "citation_page": page, "page_mapping_method": "manual_segment"}
                          for i, (anchor, text, page) in enumerate(zip(anchors, ["书中第一页末", "第二页开头"], ["66", "68"]))],
        }, self.db)
        request = {"source_id": pdf, "kind": "pdf_page", "start_anchor": anchors[0], "end_anchor": anchors[1],
                   "start_offset": 2, "end_offset": 3, "text": "第一页末\n第二页"}
        result = self.locate(self.collect(request)["excerpt_ids"][0])
        self.assertEqual(result["status"], "located")
        self.assertEqual((result["excerpt"]["citation_start"], result["excerpt"]["citation_end"]), ("66", "68"))
        self.assertEqual([span["page_char_end"] for span in result["reader"]["pageMatchSpans"]], [6, 3])


@unittest.skipUnless(
    os.environ.get("MEFINDER_BROWSER_TESTS") == "1"
    or os.environ.get("MEFINDER_BROWSER_TESTS_REQUIRED") == "1",
    "requires Chrome and Playwright",
)
class MaterialsBrowserTests(unittest.TestCase):
    def test_search_collect_browse_export_and_reader_selection(self) -> None:
        from playwright.sync_api import expect, sync_playwright

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db = root / "index.sqlite3"
        texts = [TEXTS[0].replace("红外光谱", "红外\n光谱"), r"先验证 $x_{2}$ 的值。" + TEXTS[1],
                 "历史整段收录仍然可以查看。"]
        build_database(document(texts), db)
        mutate_materials(db, {
            "action": "collect", "tags": ["历史归档"], "selections": [selection(texts[2], index=2)],
        })
        mutate_materials(db, {"action": "create_tag", "path": "实验/表征/FTIR记录"})
        mutate_materials(db, {"action": "create_tag", "path": "测量/流程/FTIR"})
        environment = patch.dict(os.environ, {"ME_FINDER_PREFERENCES": str(root / "preferences.json")})
        environment.start()
        self.addCleanup(environment.stop)
        handler = make_handler(db, app_context=AppContext.create(root, index_path=db))
        handler.log_message = lambda *args: None
        server = ManagedThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            server.shutdown()
            handler.begin_shutdown()
            handler.close_runtime()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome")
            page = browser.new_page(viewport={"width": 1420, "height": 900},
                                    permissions=["clipboard-read", "clipboard-write"])
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
            page.locator("#query").fill("历史整段")
            page.locator("#query").press("Enter")
            expect(page.locator("#results-list .materials-chip")).to_have_text("历史归档")
            page.locator("#query").fill("薄膜")
            page.locator("#query").press("Enter")
            rows = page.locator("#results-list .result-row")
            expect(rows).to_have_count(2)
            expect(rows.get_by_role("checkbox")).to_have_count(0)
            expect(page.locator("#material-search-selection")).to_have_count(0)
            toolbar = page.get_by_role("toolbar", name="选中文字操作")
            picker = page.get_by_role("dialog", name="给选中文字加标签")
            hit = page.locator(".detail-hit")
            selected = ["薄膜的红外\n光谱保留峰位。", "薄膜的制备过程"]
            tag_button = toolbar.get_by_role("button", name="加标签")
            expect(toolbar).to_have_count(0)

            for index, excerpt in enumerate(selected):
                if index == 1:
                    page.set_viewport_size({"width": 640, "height": 600})
                    page.get_by_role("button", name="返回结果列表").click()
                rows.filter(has_text=excerpt.splitlines()[0]).click()
                hit.scroll_into_view_if_needed()
                # Drag across real DOM text, including the separate search-highlight node.
                coords = hit.evaluate("""(element, excerpt) => {
                    const start = element.textContent.indexOf(excerpt);
                    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
                    const rects = [];
                    for (let node = walker.nextNode(), pos = 0; node; node = walker.nextNode()) {
                        for (const index of [start, start + excerpt.length - 1]) {
                            if (index < pos || index >= pos + node.length) continue;
                            const range = new Range();
                            range.setStart(node, index - pos);
                            range.setEnd(node, index - pos + 1);
                            const rect = range.getBoundingClientRect();
                            rects.push([index === start ? rect.left + .5 : rect.right - .5,
                                        rect.top + rect.height / 2]);
                        }
                        pos += node.length;
                    }
                    return rects;
                }""", excerpt)
                page.mouse.move(*coords[0])
                page.mouse.down()
                page.mouse.move(*coords[1], steps=8)
                page.mouse.up()
                expect(toolbar).to_be_visible()
                self.assertEqual(page.evaluate("getSelection().toString()"), excerpt)
                if index == 0:
                    self.assertEqual(hit.evaluate("""element => {
                        const tint = getComputedStyle(element, '::selection').backgroundColor;
                        const segments = [...CSS.highlights.get('material-sentence')].map(range => range.toString());
                        return {tint, segments};
                    }"""), {"tint": "rgba(0, 0, 0, 0)", "segments": ["薄膜", "的红外", "光谱保留峰位。"]})
                    toolbar.get_by_role("button", name="复制", exact=True).click()
                    expect(toolbar.get_by_role("button", name="已复制")).to_be_visible()
                    self.assertEqual(page.evaluate("navigator.clipboard.readText()"), excerpt)
                    self.assertEqual(page.evaluate("getSelection().toString()"), excerpt)
                tag_button.click()
                expect(picker).to_be_visible()
                expect(toolbar).to_be_hidden()
                expect(page.locator(".materials-dialog")).to_have_count(0)
                search = picker.get_by_role("combobox", name="搜索或新建标签")
                expect(picker.get_by_role("option", name="历史归档", exact=True)).to_be_visible()
                if index == 0:
                    search.press("Escape")
                    expect(picker).to_have_count(0)
                    expect(tag_button).to_be_focused()
                    self.assertEqual(page.evaluate("getSelection().toString()"), excerpt)
                    tag_button.click()
                    search.fill("#实验/表征/FTIR/峰位")
                    expect(picker.get_by_role("status")).to_have_text("标签最多三级")
                    expect(picker.get_by_role("option")).to_have_count(0)
                    search.fill("FTIR")
                    expect(picker.get_by_role("option", name="创建并收录「FTIR」")).to_be_visible()
                    expect(picker.get_by_role("option", name="测量 / 流程 / FTIR", exact=True)).to_be_visible()
                    search.fill("#实验/表征/FTIR")
                    expect(picker.get_by_role("option", name="实验 / 表征 / FTIR记录")).to_be_visible()
                    picker.get_by_role("option", name="创建并收录「实验/表征/FTIR」").click()
                else:
                    search.fill("ftir")
                    expect(picker.get_by_role("option", name="实验 / 表征 / FTIR", exact=True)).to_be_visible()
                    expect(picker.get_by_role("option", name="测量 / 流程 / FTIR", exact=True)).to_be_visible()
                    bounds = picker.bounding_box()
                    self.assertGreaterEqual(bounds["x"], 8)
                    self.assertGreaterEqual(bounds["y"], 8)
                    self.assertLessEqual(bounds["x"] + bounds["width"], 632)
                    self.assertLessEqual(bounds["y"] + bounds["height"], 592)
                    search.fill("实验/表征/FTIR")
                    search.press("Meta+Enter")
                    expect(picker).to_be_visible()
                    expect(search).to_have_value("")
                    expect(page.locator("#detail-saved-sentences .materials-chip")).to_have_text("实验 / 表征 / FTIR")
                    search.fill("历史归档")
                    search.press("Enter")
                expect(picker).to_have_count(0)
                expect(page.locator(".materials-saved-sentence p")).to_have_text(excerpt)

            page.set_viewport_size({"width": 1420, "height": 900})
            records = {row["text"]: row for row in list_materials(db)["material_excerpts"]}
            self.assertEqual(set(records), {*selected, texts[2]})
            for original, excerpt in zip(texts[:2], selected, strict=True):
                start = original.index(excerpt)
                self.assertEqual((records[excerpt]["start_offset"], records[excerpt]["end_offset"]),
                                 (start, start + len(excerpt)))
            expect(page.locator("#detail-saved-sentences .materials-chip")).to_have_count(2)
            page.locator("#detail-saved-sentences").get_by_role("button", name="实验 / 表征 / FTIR").click()
            expect(page.locator("#materials-active-tag")).to_have_text("实验 / 表征 / FTIR")
            expect(page.locator("#materials-count")).to_have_text("2 条")
            page.locator(".materials-tree-tag").filter(has_text="表征").click()
            expect(page.locator("#materials-count")).to_have_text("2 条 · 含子标签")
            plus = page.get_by_role("button", name="在 实验 / 表征 下新建子标签")
            plus.click()
            dialog = page.locator(".materials-dialog")
            dialog.locator("#material-tag-name").fill("XPS/能级")
            dialog.get_by_role("button", name="创建", exact=True).click()
            expect(dialog.locator(".materials-error")).to_have_text("标签最多三级")
            dialog.locator("#material-tag-name").fill("XPS")
            dialog.get_by_role("button", name="创建", exact=True).click()
            expect(page.locator("#materials-active-tag")).to_have_text("实验 / 表征 / XPS")
            page.locator('.materials-tree-tag[data-tag-path="实验/表征/FTIR"]').click()
            page.locator("#materials-filter").fill("薄膜")
            expect(page.locator(".materials-excerpt-row")).to_have_count(2)
            with page.expect_download() as download:
                page.locator("#materials-export").click()
            markdown = Path(download.value.path()).read_text()
            self.assertIn("第 38 页", markdown)
            self.assertIn("第 39 页", markdown)
            quoted_text = markdown.replace("\n> ", "\n")
            for text in selected:
                self.assertIn(text, quoted_text)
            for text in texts:
                self.assertNotIn(text, quoted_text)
            page.locator(".materials-excerpt-title").first.click()
            expect(page.locator("#materials-detail")).to_be_visible()
            page.get_by_role("button", name="编辑备注", exact=True).click()
            page.locator("#material-note").fill("保留作横向对照")
            page.get_by_role("button", name="保存备注", exact=True).click()
            expect(page.locator(".materials-note-preview")).to_have_text("保留作横向对照")
            page.locator("#materials-detail").get_by_role("button", name="打开原文").click()

            paragraph = page.locator(".mef-reader-item-text").first
            expect(paragraph).to_be_visible()
            paragraph.evaluate("""element => {
                const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
                const text = walker.nextNode();
                const range = document.createRange();
                range.setStart(text, 0);
                range.setEnd(text, Math.min(text.length, 3));
                const selection = window.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
                element.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            }""")
            tag = page.get_by_role("button", name="加标签", exact=True).filter(visible=True)
            tag.click()
            dialog.get_by_role("checkbox", name="实验 / 表征 / XPS").check()
            dialog.get_by_role("button", name="收集", exact=True).click()
            expect(dialog).to_have_count(0)
            after = list_materials(db)
            self.assertEqual(len(after["material_excerpts"]), 4)
            self.assertEqual(errors, [])
            browser.close()

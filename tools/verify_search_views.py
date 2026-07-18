from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8773"
    output_dir = Path("test-output")
    output_dir.mkdir(exist_ok=True)
    captured_search: dict[str, object] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)

        def capture_request(request) -> None:
            if request.url.endswith("/api/search") and request.method == "POST":
                captured_search.clear()
                captured_search.update(request.post_data_json or {})

        page.on("request", capture_request)
        page.goto(base_url, wait_until="networkidle")

        mode_box = page.locator("#mode-control").bounding_box()
        source_box = page.locator("#source-type-control").bounding_box()
        assert mode_box and source_box and abs(mode_box["y"] - source_box["y"]) < 4

        page.locator("#limit-select-trigger").click()
        page.locator('#limit-options [data-value="200"]').click()
        assert page.locator("#limit-select-label").inner_text() == "200 条"
        page.locator("#limit-select-trigger").click()
        page.locator('#limit-options [data-value="all"]').click()
        assert page.locator("#limit-select-label").inner_text() == "全部"
        assert page.locator("select").count() == 0

        page.locator("#document-select-trigger").click()
        page.locator("#document-options .app-select-option").nth(1).wait_for()
        selected_source = page.locator("#document-options .app-select-option").nth(1).get_attribute("data-value")
        assert selected_source
        page.locator("#document-options .app-select-option").nth(1).click()
        assert page.locator("#document-select-label").inner_text() != "全部文献"

        page.locator("#query").fill("抵抗")
        page.locator("#search-btn").click()
        page.locator("#results-status").wait_for()
        page.wait_for_timeout(500)
        assert captured_search.get("limit") == "all"
        assert captured_search.get("source_file_id") == selected_source
        page.screenshot(path=output_dir / "search-controls-and-document-scope.png", full_page=True)

        page.locator('.sidebar-item[data-page="library"]').click()
        page.locator("#library-list .library-entry").first.wait_for()
        # The user's persisted preference may open the library in card mode.
        # Normalize the verifier before making list-specific assertions.
        page.locator("#library-view-list").click()
        page.locator("#library-list .library-row").first.wait_for()
        all_document_count = page.locator("#library-list .library-entry").count()
        pdf_total = int(page.locator('#library-stats [data-status="pdf_all"] .status-stat__count').inner_text())
        page.locator('#library-stats [data-status="pdf_all"]').click()
        assert page.locator("#library-list .library-entry").count() == pdf_total
        assert page.locator("#library-list .library-entry:not(:has(.status-chip))").count() == 0
        page.locator('#library-stats [data-status="pdf_all"]').click()
        assert page.locator("#library-list .library-entry").count() == all_document_count
        page.locator("#library-sort-field-select .app-select-trigger").click()
        page.locator('#library-sort-field-select [data-value="title"]').click()
        assert page.locator("#library-sort-field-label").inner_text() == "书名"
        page.locator("#library-sort-direction-select .app-select-trigger").click()
        page.locator('#library-sort-direction-select [data-value="asc"]').click()
        assert page.locator("#library-sort-direction-label").inner_text() == "升序"
        first_title_ascending = page.locator("#library-list .library-row-title").first.inner_text()
        page.locator("#library-sort-direction-select .app-select-trigger").click()
        page.locator('#library-sort-direction-select [data-value="desc"]').click()
        first_title_descending = page.locator("#library-list .library-row-title").first.inner_text()
        assert first_title_descending != first_title_ascending
        page.locator("#library-sort-direction-select .app-select-trigger").click()
        page.locator('#library-sort-direction-select [data-value="asc"]').click()
        page.locator("#library-view-grid").click()
        page.locator("#library-list .library-card").first.wait_for()
        assert page.locator("#library-list .library-card").count() > 0
        page.screenshot(path=output_dir / "library-card-view.png", full_page=True)
        page.locator("#library-view-list").click()
        assert page.locator("#library-list .library-row").count() > 0

        assert page.locator('.sidebar-item[data-page="calibration"]').count() == 0
        assert page.locator("#library-stats .status-stat").count() == 5
        page.locator("#library-sort-field-select .app-select-trigger").click()
        page.locator('#library-sort-field-select [data-value="status"]').click()
        assert page.locator("#library-sort-field-label").inner_text() == "校准状态"

        pdf_entry = page.locator("#library-list .library-entry:has(.status-chip)").first
        pdf_entry.click()
        page.locator("#library-drawer-calibration").wait_for(state="visible")
        assert page.locator("#cal-section-body").is_hidden()
        page.locator("#cal-collapse-toggle").click()
        page.locator("#cal-editor").wait_for(state="visible")
        page.locator("#cal-detail-actions .action-btn").first.wait_for()
        assert page.locator("#cal-segments-body").count() == 1
        page.screenshot(path=output_dir / "library-drawer-calibration.png", full_page=True)
        page.locator("#cal-collapse-toggle").click()
        assert page.locator("#cal-editor").is_hidden()

        theme_colors = {}
        for theme, expected in (
            ("frost-blue", "#1677FF"),
            ("sage-ivory", "#637A50"),
            ("warm-sand", "#B85C2B"),
            ("rose-mist", "#C9446A"),
            ("lavender-purple", "#7B5EC7"),
            ("midnight", "#2485FF"),
        ):
            page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
            page.wait_for_timeout(250)
            color = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
            assert color == expected, (theme, color)
            calibration_bg = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--calibration-card-bg').trim()")
            surface_bg = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--surface-secondary').trim()")
            rendered_bg = page.locator("#cal-collapse-toggle").evaluate("element => getComputedStyle(element).backgroundColor")
            assert calibration_bg and calibration_bg.lower() != surface_bg.lower(), (theme, calibration_bg, surface_bg)
            assert rendered_bg != "rgba(0, 0, 0, 0)", (theme, rendered_bg)
            theme_colors[theme] = color
            page.screenshot(path=output_dir / f"{theme}-current-source.png", full_page=True)

        browser.close()

    print(json.dumps({"ok": True, "search": captured_search, "theme_colors": theme_colors}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

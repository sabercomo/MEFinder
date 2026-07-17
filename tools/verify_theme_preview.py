from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


THEMES = (
    "frost-blue",
    "sage-ivory",
    "warm-sand",
    "rose-mist",
    "lavender-purple",
    "midnight",
)


def wait_for_theme(page: Page, theme: str) -> None:
    page.wait_for_function(
        "expected => document.documentElement.dataset.theme === expected",
        arg=theme,
    )
    page.wait_for_timeout(350)


def grid_columns(page: Page) -> dict[str, object]:
    return page.evaluate(
        """() => {
            const options = [...document.querySelectorAll('.theme-option')];
            const rects = options.map(option => option.getBoundingClientRect());
            const columns = new Set(rects.map(rect => Math.round(rect.x))).size;
            const heights = rects.map(rect => rect.height);
            const container = document.querySelector('.theme-options');
            return {
                columns,
                cardCount: options.length,
                equalHeights: Math.max(...heights) - Math.min(...heights) < 1,
                noHorizontalOverflow: container.scrollWidth <= container.clientWidth + 1,
            };
        }"""
    )


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/"
    output_dir = Path("test-output")
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page.goto(base_url, wait_until="networkidle")
        initial_theme = page.locator("html").get_attribute("data-theme") or "frost-blue"
        page.locator('.sidebar-item[data-page="settings"]').click()
        page.locator(".theme-options").wait_for(state="visible")

        results: dict[str, object] = {"initial_theme": initial_theme, "themes": {}}
        for theme in THEMES:
            option = page.locator(f'.theme-option[data-theme-choice="{theme}"]')
            option.click()
            wait_for_theme(page, theme)
            preview = option.locator(".theme-preview")
            state = page.evaluate(
                """theme => {
                    const option = document.querySelector(`[data-theme-choice="${theme}"]`);
                    const preview = option.querySelector('.theme-preview');
                    const style = getComputedStyle(preview);
                    const success = preview.querySelector('.theme-mini-state.is-success');
                    const danger = preview.querySelector('.theme-mini-state.is-danger');
                    const match = preview.querySelector('.theme-mini-match');
                    return {
                        selected: option.classList.contains('selected'),
                        ariaChecked: option.getAttribute('aria-checked'),
                        tone: option.querySelector('.theme-option-tone').textContent,
                        cards: preview.querySelectorAll('.theme-mini-doc-card').length,
                        navItems: preview.querySelectorAll('.theme-mini-nav-item').length,
                        sidebarBg: getComputedStyle(preview.querySelector('.theme-mini-sidebar')).backgroundColor,
                        mainBg: getComputedStyle(preview.querySelector('.theme-mini-main')).backgroundColor,
                        cardBg: getComputedStyle(preview.querySelector('.theme-mini-doc-card')).backgroundColor,
                        success: getComputedStyle(success).color,
                        danger: getComputedStyle(danger).color,
                        matchBg: getComputedStyle(match).backgroundColor,
                        matchBorder: getComputedStyle(match).boxShadow,
                        accentToken: style.getPropertyValue('--accent').trim(),
                        matchToken: style.getPropertyValue('--match-block-accent').trim(),
                        dangerToken: style.getPropertyValue('--danger').trim(),
                    };
                }""",
                theme,
            )
            state["previewBox"] = preview.bounding_box()
            results["themes"][theme] = state
            page.screenshot(path=str(output_dir / f"theme-preview-{theme}.png"), full_page=False)

        sage = page.locator('.theme-option[data-theme-choice="sage-ivory"]')
        sage.focus()
        page.keyboard.press("Space")
        wait_for_theme(page, "sage-ivory")
        lavender = page.locator('.theme-option[data-theme-choice="lavender-purple"]')
        lavender.focus()
        page.keyboard.press("Enter")
        wait_for_theme(page, "lavender-purple")
        results["keyboard_selects"] = lavender.get_attribute("aria-checked") == "true"

        responsive: dict[str, object] = {}
        for width, expected in ((1440, 3), (1100, 2), (800, 1)):
            page.set_viewport_size({"width": width, "height": 1000})
            page.wait_for_timeout(220)
            responsive[str(width)] = {"expected": expected, **grid_columns(page)}
        results["responsive"] = responsive

        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator('.sidebar-item[data-page="calibration"]').click()
        page.locator("#calibration-stats .status-stat").first.wait_for(state="visible")
        page.evaluate("applyCalStatusFilter('failed')")
        semantic: dict[str, object] = {}
        for theme in THEMES:
            page.evaluate("theme => applyTheme(theme)", theme)
            wait_for_theme(page, theme)
            semantic[theme] = page.evaluate(
                """() => {
                    const expected = ['info','success','neutral','warning','danger'];
                    return {
                        activeFilter: document.querySelector('#calibration-stats .status-stat.active')?.dataset.status,
                        stats: expected.map(variant => {
                            const item = document.querySelector(`.status-stat--${variant}`);
                            const icon = item.querySelector('.status-stat__icon');
                            const svg = icon.querySelector('svg');
                            const style = getComputedStyle(item);
                            const iconStyle = getComputedStyle(icon);
                            return {
                                variant,
                                color: style.color,
                                background: style.backgroundColor,
                                border: style.borderColor,
                                iconColor: iconStyle.color,
                                iconOpacity: iconStyle.opacity,
                                iconWidth: svg.getBoundingClientRect().width,
                                iconHeight: svg.getBoundingClientRect().height,
                                count: item.querySelector('.status-stat__count').textContent,
                            };
                        }),
                    };
                }"""
            )
        results["semantic_stats"] = semantic

        page.locator('.sidebar-item[data-page="settings"]').click()
        restore = page.locator(f'.theme-option[data-theme-choice="{initial_theme}"]')
        restore.click()
        wait_for_theme(page, initial_theme)
        browser.close()

    theme_results = list(results["themes"].values())
    responsive_results = list(results["responsive"].values())
    semantic_results = list(results["semantic_stats"].values())
    checks = {
        "six_themes": len(theme_results) == 6,
        "shared_preview_complete": all(
            result["cards"] == 3 and result["navItems"] == 3 for result in theme_results
        ),
        "match_differs_from_accent_and_danger": all(
            result["matchToken"].lower() not in {
                result["accentToken"].lower(), result["dangerToken"].lower()
            }
            for result in theme_results
        ),
        "semantic_preview_states_visible": all(
            result["success"] != result["danger"] and result["matchBg"] != "rgba(0, 0, 0, 0)"
            for result in theme_results
        ),
        "keyboard_selects": bool(results["keyboard_selects"]),
        "responsive_3_2_1": all(
            result["columns"] == result["expected"]
            and result["cardCount"] == 6
            and result["equalHeights"]
            and result["noHorizontalOverflow"]
            for result in responsive_results
        ),
        "semantic_stats_complete": all(
            entry["activeFilter"] == "failed"
            and len(entry["stats"]) == 5
            and all(
                stat["background"] != "rgba(0, 0, 0, 0)"
                and stat["border"] != "rgba(0, 0, 0, 0)"
                and stat["iconOpacity"] == "1"
                and stat["iconWidth"] == 16
                and stat["iconHeight"] == 16
                for stat in entry["stats"]
            )
            for entry in semantic_results
        ),
    }
    results["checks"] = checks
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

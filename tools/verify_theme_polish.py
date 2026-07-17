from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


THEMES = ("rose-mist", "lavender-purple")


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8892/"
    output_dir = Path("test-output")
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(base_url + "?page=calibration", wait_until="networkidle")
        page.locator(".cal-status-tabs").wait_for(state="visible")
        initial_theme = page.locator("html").get_attribute("data-theme") or "frost-blue"

        results: dict[str, object] = {"initial_theme": initial_theme, "themes": {}}
        for theme in THEMES:
            page.evaluate("theme => applyTheme(theme)", theme)
            page.wait_for_function(
                "theme => document.documentElement.dataset.theme === theme", arg=theme
            )
            page.wait_for_timeout(220)
            state = page.evaluate(
                """() => {
                    const root = getComputedStyle(document.documentElement);
                    const tabs = [...document.querySelectorAll('.cal-status-tab')].map(tab => {
                        const icon = tab.querySelector('.status-stat__icon').getBoundingClientRect();
                        const label = tab.querySelector('.cal-status-tab__label').getBoundingClientRect();
                        const button = tab.getBoundingClientRect();
                        return {
                            label: tab.textContent.trim(),
                            gap: label.left - icon.right,
                            centerDelta: Math.abs(
                                (icon.top + icon.height / 2) - (label.top + label.height / 2)
                            ),
                            iconInside: icon.left >= button.left && icon.right <= button.right,
                            labelInside: label.left >= button.left && label.right <= button.right,
                        };
                    });
                    return {
                        tokens: {
                            appBg: root.getPropertyValue('--app-bg').trim(),
                            sidebarBg: root.getPropertyValue('--sidebar-bg').trim(),
                            surface: root.getPropertyValue('--surface-primary').trim(),
                            accent: root.getPropertyValue('--accent').trim(),
                            accentSoft: root.getPropertyValue('--accent-soft').trim(),
                            border: root.getPropertyValue('--border-default').trim(),
                        },
                        tabs,
                        tabCount: tabs.length,
                        horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
                    };
                }"""
            )
            results["themes"][theme] = state
            page.screenshot(
                path=str(output_dir / f"calibration-{theme}-polished.png"),
                full_page=False,
            )

        page.evaluate("theme => applyTheme(theme)", initial_theme)
        browser.close()

    theme_states = list(results["themes"].values())
    checks = {
        "two_themes_rendered": len(theme_states) == 2,
        "five_tabs": all(state["tabCount"] == 5 for state in theme_states),
        "icon_label_spacing": all(
            tab["gap"] >= 6 and tab["centerDelta"] <= 1
            for state in theme_states
            for tab in state["tabs"]
        ),
        "content_inside_buttons": all(
            tab["iconInside"] and tab["labelInside"]
            for state in theme_states
            for tab in state["tabs"]
        ),
        "no_horizontal_overflow": all(
            not state["horizontalOverflow"] for state in theme_states
        ),
        "palettes_are_distinct": (
            theme_states[0]["tokens"]["accent"]
            != theme_states[1]["tokens"]["accent"]
        ),
    }
    results["checks"] = checks
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

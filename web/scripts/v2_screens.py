"""Screenshots of the v2 views (Tracker, Layouts, Benchmark, presenter mode) against the mock.

    npm run dev:mock                                   # terminal 1: UI on :5181, mock on :5182
    .venv/bin/python web/scripts/v2_screens.py         # terminal 2

Saves web/screenshots/v2-*.png. SHOTS=tracker,present limits which ones; BASE=<url> points elsewhere.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = os.environ.get("BASE", "http://localhost:5181")
OUT = Path(__file__).resolve().parent.parent / "screenshots"
ONLY = set(filter(None, os.environ.get("SHOTS", "").split(",")))


def want(name: str) -> bool:
    return not ONLY or any(name.startswith(o) for o in ONLY)


def shot(page: Page, name: str, full: bool = True) -> None:
    page.wait_for_timeout(500)
    page.screenshot(path=str(OUT / f"v2-{name}.png"), full_page=full)
    print("saved", f"v2-{name}.png")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        errors: list[str] = []
        for theme in ("light", "dark"):
            ctx = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1, color_scheme=theme)
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(f"PAGE ERROR: {e}"))
            page.on("console", lambda m: m.type == "error" and errors.append(f"CONSOLE: {m.text}"))
            page.goto(f"{BASE}/#tracker")
            page.wait_for_selector("text=Pages sent with the hotkey")
            page.wait_for_timeout(800)
            if want("header") and theme == "light":
                page.screenshot(path=str(OUT / "v2-header-1280.png"), clip={"x": 0, "y": 0, "width": 1280, "height": 70})
                print("saved v2-header-1280.png")
            if want("tracker"):
                shot(page, f"tracker-{theme}")
            if want("evidence") and theme == "light":
                page.get_by_text("learned now ·").first.click()
                page.wait_for_selector("text=every value checked by code")
                page.wait_for_timeout(500)
                shot(page, "tracker-evidence-light")
                page.get_by_text("held: unknown layout").first.click()
                shot(page, "tracker-held-light")
            if want("bench"):
                page.goto(f"{BASE}/#bench")
                page.wait_for_selector("text=AI calls per posting")
                shot(page, f"bench-{theme}")
            if want("present"):
                page.goto(f"{BASE}/?present=1")
                page.wait_for_timeout(1200)
                for i in range(6):
                    shot(page, f"present-{i}-{theme}", full=False)
                    page.keyboard.press("ArrowRight")
                    page.wait_for_timeout(300)
            ctx.close()
        browser.close()
        for e in errors:
            print(e)
        return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

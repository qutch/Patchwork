"""Screenshots of the v2 views (Tracker, Layouts, Benchmark, presenter mode) against the mock.

    npm run dev:mock                                   # terminal 1: UI on :5181, mock on :5182
    .venv/bin/python web/scripts/v2_screens.py         # terminal 2

Saves web/screenshots/v2-*.png. SHOTS=tracker,present limits which ones; BASE=<url> points elsewhere.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
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


MOCK = os.environ.get("MOCK", "http://127.0.0.1:5182")


def press(kind: str, n: int = 0) -> None:
    """What the hotkey does (mock/press.mjs), so the flow needs no extension."""
    subprocess.run(["node", str(Path(__file__).resolve().parent.parent / "mock" / "press.mjs"), kind, "--n", str(n), "--base", MOCK],
                   check=True, capture_output=True)


def flow(browser, errors: list[str]) -> None:
    """The 2-minute demo in presenter mode against the mock: reset, presses, learn, break, repair."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1, color_scheme="light")
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"PAGE ERROR: {e}"))
    press("reset")
    page.goto(f"{BASE}/?present=1&beat=3")
    page.wait_for_selector("text=Regardless of layout")
    t0 = time.monotonic()
    press("lever")
    page.wait_for_selector("text=code · 0 AI calls", timeout=5000)
    print(f"lever row visible after {time.monotonic() - t0:.1f} s")
    press("greenhouse")
    press("workable")
    page.wait_for_selector("text=learning this layout", timeout=8000)
    shot(page, "flow-1-learning", full=False)
    page.wait_for_selector("text=learned now ·", timeout=15000)
    press("workable", 1)
    page.wait_for_timeout(2500)
    shot(page, "flow-2-learned", full=False)
    page.keyboard.press("ArrowRight")
    press("rename")
    press("ashby")
    page.wait_for_selector("section[role=alert]", timeout=8000)
    shot(page, "flow-3-blocked", full=False)
    page.get_by_role("button", name="Repair with AI →").click()
    page.get_by_role("button", name=re.compile("Approve")).first.wait_for(timeout=15000)
    page.wait_for_timeout(300)
    shot(page, "flow-4-approve", full=False)
    page.get_by_role("button", name=re.compile("Approve")).first.click()
    page.wait_for_selector("section[role=alert]", state="detached", timeout=15000)
    page.wait_for_timeout(2500)
    shot(page, "flow-5-repaired", full=False)
    ctx.close()
    press("reset")


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
        if want("flow"):
            flow(browser, errors)
        browser.close()
        for e in errors:
            print(e)
        return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

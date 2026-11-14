"""Click through the 4-minute demo against the UI (mock or real control plane) and screenshot
every tab in light and dark themes.

    npm run dev:mock                    # terminal 1
    python web/scripts/demo_screens.py  # terminal 2 (Python Playwright + Chromium)

Against the real control plane nobody drives the capture browser, so set REAL=1: the script
starts and stops a live recording (to check capture), then uses the fixture recording.
SCREENS_OUT=<dir> writes the screenshots elsewhere; INBOX=host:port sets the start URL.

Only clicks and typing, no API shortcuts apart from the initial demo reset.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5180"
OUT = Path(os.environ.get("SCREENS_OUT") or Path(__file__).resolve().parent.parent / "screenshots")
OUT.mkdir(parents=True, exist_ok=True)
REAL = os.environ.get("REAL") == "1"
INBOX = os.environ.get("INBOX", "localhost:8001")
T = 60_000


def shot(page: Page, name: str) -> None:
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    print("saved", name)


def tab(page: Page, name: str) -> None:
    page.get_by_role("navigation").get_by_role("link", name=name).click()
    page.wait_for_timeout(300)


def ai(page: Page, on: bool) -> None:
    sw = page.get_by_role("banner").get_by_role("switch")
    if (sw.get_attribute("aria-checked") == "true") != on:
        sw.click()
    expect(sw).to_have_attribute("aria-checked", "true" if on else "false")


def run_batch(page: Page, limit: str) -> None:
    page.locator("#limit").fill(limit)
    page.get_by_role("button", name="Run batch").click()


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page = ctx.new_page()
        page.on("pageerror", lambda e: print("PAGE ERROR:", e))
        page.on("console", lambda m: m.type == "error" and print("CONSOLE:", m.text))
        page.goto(f"{BASE}/?theme=light#record")
        page.evaluate("fetch('/api/demo/reset', {method: 'POST'}).then(() => localStorage.clear())")
        page.wait_for_timeout(300)
        page.goto(f"{BASE}/?theme=light#record")
        ai(page, True)

        # 2. Show it once
        if REAL:
            page.locator("#allow").fill(f"{INBOX}, localhost:38082")
            page.locator("#url").fill(f"http://{INBOX}/")
        page.get_by_role("button", name="Start recording").click()
        page.wait_for_timeout(4500)
        shot(page, "1-record-live-light")
        if REAL:
            page.get_by_role("button", name="Stop").click()
            expect(page.get_by_role("button", name="New recording")).to_be_visible(timeout=T)
            page.get_by_role("button", name="New recording").click()
            page.get_by_role("button", name="Use fixture recording").click()
        else:
            expect(page.get_by_text("12 events captured")).to_be_visible(timeout=T)
            page.get_by_role("button", name="Stop").click()
        page.get_by_role("button", name=re.compile("Find mappings|Re-run matcher")).click()
        expect(page.get_by_text("Mappings found in the recording")).to_be_visible()
        shot(page, "1-record-light")

        # 3. Build
        page.get_by_role("button", name="Build integration").click()
        expect(page.get_by_text("Human gate · the agent is asking")).to_be_visible(timeout=T)
        shot(page, "3-agent-ask-light")
        tab(page, "Review")
        expect(page.get_by_text("Destination field ← source")).to_be_visible()
        shot(page, "2-review-light")
        page.get_by_role("button", name="Quarantine them for review").click()
        tab(page, "Agent Activity")
        expect(page.get_by_text("Human gate · approve deploy")).to_be_visible(timeout=T)
        shot(page, "3-agent-deploy-light")
        page.get_by_role("button", name="Approve & deploy").click()
        expect(page.get_by_text("Deployed po_to_stockroom v1")).to_be_visible(timeout=T)
        tab(page, "Integration")
        expect(page.get_by_text("Generated code")).to_be_visible()
        shot(page, "4-integration-light")
        page.get_by_role("tab", name="Tests", exact=True).click()
        shot(page, "4-integration-tests-light")

        # 4. AI off, run 100
        tab(page, "Runs")
        ai(page, False)
        run_batch(page, "100")
        expect(page.get_by_text("Run completed")).to_be_visible(timeout=T)
        shot(page, "5-runs-light")

        # 5. Break 1
        page.get_by_role("button", name="Switch to v2 (Break 1)").click()
        run_batch(page, "")
        expect(page.get_by_text("Run blocked before any write")).to_be_visible(timeout=T)
        page.get_by_role("button", name="Show 30 contract errors").click()
        shot(page, "5-runs-incident-light")

        # 6. Repair: AI back on
        page.get_by_role("alert").get_by_role("switch").click()
        page.get_by_role("button", name="Repair with AI").click()
        expect(page.get_by_text("Human gate · the agent is asking")).to_be_visible(timeout=T)
        page.get_by_role("button", name="By supplier").click()
        expect(page.get_by_text("Human gate · approve deploy")).to_be_visible(timeout=T)
        shot(page, "3-agent-repair-deploy-light")
        page.get_by_role("button", name="Approve & deploy").click()
        expect(page.get_by_text("Deployed po_to_stockroom v2")).to_be_visible(timeout=T)
        page.wait_for_timeout(1500)
        shot(page, "3-agent-repair-done-light")

        # 7. Run again, AI off
        tab(page, "Runs")
        ai(page, False)
        run_batch(page, "")
        expect(page.get_by_text("Run completed")).to_be_visible(timeout=T)
        page.wait_for_timeout(800)
        shot(page, "5-runs-final-light")
        tab(page, "Integration")
        page.get_by_role("tab", name="Diff", exact=True).click()
        expect(page.get_by_text("Spec and code diff")).to_be_visible()
        page.wait_for_timeout(500)
        shot(page, "4-integration-diff-light")

        # dark theme pass over the final state
        for t, name in (("record", "1-record"), ("review", "2-review"), ("agent", "3-agent"), ("integration", "4-integration"), ("runs", "5-runs")):
            page.goto(f"{BASE}/?theme=dark#{t}")
            page.wait_for_timeout(1500)
            shot(page, f"{name}-dark")
        page.goto(f"{BASE}/?theme=dark#integration")
        page.get_by_role("tab", name="Diff", exact=True).click()
        page.wait_for_timeout(800)
        shot(page, "4-integration-diff-dark")
        browser.close()


if __name__ == "__main__":
    main()

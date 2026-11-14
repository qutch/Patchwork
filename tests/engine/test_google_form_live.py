"""W1-6: opt-in live check against the demo Google Form (contracts V0). Skipped unless
PATCHWORK_DEMO_FORM_URL is set. Loads the form and fills it; NEVER submits: besides dry_run=True,
every request that could record a response (formResponse, POST) is aborted at the network layer."""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import sync_playwright

from runner.forms import fill_and_submit, form_contract, open_form, resolve_control, snapshot_form

URL = os.environ.get("PATCHWORK_DEMO_FORM_URL", "")
LABELS = ["Email", "Job Title", "Company", "Link", "Date Applied"]
VALUES = {"Email": "demo@example.com", "Job Title": "PATCHWORK dry run (not submitted)", "Company": "Patchwork",
          "Link": "https://example.com/patchwork-dry-run", "Date Applied": "2026-11-14"}

pytestmark = [pytest.mark.live, pytest.mark.skipif(not URL, reason="set PATCHWORK_DEMO_FORM_URL to run")]


@pytest.fixture(scope="module")
def live_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        blocked: list[str] = []

        def guard(route, request):
            if request.method != "GET" or "formResponse" in request.url:
                blocked.append(f"{request.method} {request.url}")
                return route.abort()
            return route.continue_()

        ctx.route("**/*", guard)
        page = ctx.new_page()
        open_form(page, URL)
        yield page, blocked
        browser.close()


def test_live_snapshot(live_page):
    page, _ = live_page
    snap = snapshot_form(page)
    assert [q["label"] for q in snap] == LABELS
    assert [q["label"] for q in snap if q["required"]] == ["Email"]
    doc = form_contract(snap, title=page.title(), url=URL)
    assert doc["info"]["x-patchwork-kind"] == "form"
    for label in LABELS:
        resolve_control(page, label)
    assert page.get_by_role("button", name="Submit", exact=True).count() == 1


def test_live_dry_run_fill(live_page):
    page, blocked = live_page
    fill_and_submit(page, VALUES, submit="Submit", confirmation_text="Your response has been recorded", dry_run=True)
    assert resolve_control(page, "Job Title")[1].input_value() == VALUES["Job Title"]
    assert "formResponse" not in page.url
    assert not [b for b in blocked if "formResponse" in b], blocked  # nothing even tried to submit

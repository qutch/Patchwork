"""V11 recorder additions: element paths and page snapshots on copy, form questions on the submit click,
the confirmation page, bare-host allowlist entries. Headless, offline (pages are routed, not fetched)."""

from __future__ import annotations

import json

import pytest

from capture.recorder import host_allowed
from runner.html import css_all
from tests.capture.form_app import create_app
from tests.capture.helpers import record_with, run
from tests.capture.jobs_demo import EMAIL, FORM_ALIAS, LEVER_URL, record_jobs_demo
from tests.conftest import _Server


@pytest.fixture(scope="module")
def form():
    with _Server(create_app()) as s:
        yield s


@pytest.fixture(scope="module")
def recorded(form, tmp_path_factory):
    out = tmp_path_factory.mktemp("rec") / "rec-jobs"
    recording, sent = run(record_jobs_demo(out, form.url))
    return out, recording, sent


def _events(recording, type_):
    return [e for e in recording["events"] if e["type"] == type_]


def test_copy_records_path_and_snapshot(recorded):
    out, recording, _ = recorded
    copies = _events(recording, "copy")
    assert [c["text"] for c in copies] == ["American Tech Fellowship", "Palantir Technologies"]
    first = copies[0]
    assert first["url"] == LEVER_URL and first["snapshot"] == "snapshots/1.html"
    snap = (out / first["snapshot"]).read_text()
    assert (out / "snapshots" / "1.html").exists()
    assert css_all(snap, first["el"]["path"]) == ["American Tech Fellowship"]
    assert first["el"]["tag"] == "h2"
    # The same unchanged DOM is stored once.
    assert copies[1]["snapshot"] == "snapshots/1.html"
    assert css_all(snap, copies[1]["el"]["path"]) == ["Palantir Technologies Home Page"]
    assert "application/ld+json" in snap  # JSON-LD kept for readers


def test_form_fields_carry_their_question(recorded):
    _, recording, sent = recorded
    by_question = {e["el"].get("question"): e["value"] for e in recording["events"]
                   if e["type"] in ("input", "paste")}
    assert by_question == sent
    email = next(e for e in recording["events"] if e["type"] == "input" and e["value"] == EMAIL)
    assert email["el"]["label"] == "Your email" and email["el"]["question"] == "Email"


def test_submit_click_snapshots_the_form_and_confirmation_is_seen(recorded, form):
    _, recording, _ = recorded
    click = next(e for e in _events(recording, "click") if e["el"]["accessible_name"] == "Submit")
    assert click["url"] == FORM_ALIAS + "/"
    assert [q["label"] for q in click["form"]["questions"]] == ["Email", "Job Title", "Company", "Link", "Date Applied"]
    assert [q["required"] for q in click["form"]["questions"]] == [True, False, False, False, False]
    # Email is type=email, as on the real demo Google Form (fixtures/google_form/viewform.html)
    assert [q["input"] for q in click["form"]["questions"]] == ["email", "text", "text", "text", "text"]
    conf = _events(recording, "confirmation")
    assert len(conf) == 1 and "Your response has been recorded." in conf[0]["lines"]
    assert conf[0]["t"] >= click["t"]
    import httpx
    assert len(httpx.get(form.url + "/admin/rows").json()) >= 1


def test_v1_event_shapes_unchanged_on_plain_pages(tmp_path):
    """No question/form fields appear where the page has no Google-like question list."""
    async def script(page):
        await page.set_content('<form><label>Qty <input name="q"></label><button>Save</button></form>')
        await page.fill("input", "5")
        await page.click("button")
    async def go():
        async def wrapped(page):
            await page.route("http://plain.test/", lambda r: r.fulfill(body="<html><body></body></html>",
                                                                       content_type="text/html"))
            await page.goto("http://plain.test/")
            await script(page)
        return await record_with(tmp_path / "rec", ["plain.test"], wrapped)
    recording = run(go())
    for e in recording["events"]:
        assert "form" not in e
        if e.get("el"):
            assert "question" not in e["el"] and "path" not in e["el"]


def test_snapshot_never_holds_secret_values(tmp_path):
    html = ('<html><body><p id="t">Hello world</p><input type="password" value="hunter2">'
            '<input autocomplete="cc-number" value="4111111111111111"></body></html>')

    async def script(page):
        await page.route("http://secret.test/", lambda r: r.fulfill(body=html, content_type="text/html"))
        await page.goto("http://secret.test/")
        await page.evaluate("""() => { const r = document.createRange(); r.selectNodeContents(document.getElementById('t'));
            getSelection().removeAllRanges(); getSelection().addRange(r); document.execCommand('copy'); }""")
    out = tmp_path / "rec"
    recording = run(record_with(out, ["secret.test"], script))
    copy = _events(recording, "copy")[0]
    assert copy["el"]["path"] == "#t"
    snap = (out / copy["snapshot"]).read_text()
    assert "Hello world" in snap and "hunter2" not in snap and "4111111111111111" not in snap


def test_bare_host_allowlist(form, tmp_path):
    assert host_allowed("http://127.0.0.1:5555/x", ["127.0.0.1"])
    assert host_allowed("https://jobs.lever.co/a", ["jobs.lever.co"])
    assert not host_allowed("http://127.0.0.2:5555/x", ["127.0.0.1"])
    assert not host_allowed("http://127.0.0.1:5555/x", ["127.0.0.1:5556"])

    async def script(page):
        await page.goto(form.url + "/")
        await page.get_by_role("textbox", name="Company", exact=True).fill("Acme")
        await page.get_by_role("textbox", name="Company", exact=True).blur()
    recording = run(record_with(tmp_path / "rec", ["127.0.0.1"], script))
    assert any(e["type"] == "framenavigated" and e["url"] == form.url + "/" for e in recording["events"])
    assert [e["value"] for e in recording["events"] if e["type"] == "input"] == ["Acme"]
    assert json.loads((tmp_path / "rec" / "recording.json").read_text())["allowlist"] == ["127.0.0.1"]


def test_real_google_form_markup_offline(tmp_path):
    """fixtures/google_form/viewform.html (the demo Form's real markup) served at a fake host with every other
    request aborted: questions, the Email heading and required flags are read the way V5 resolves them.
    Only "Clear form" is clicked; nothing can leave the browser."""
    from tests.capture.jobs_demo import REPO
    html = (REPO / "fixtures" / "google_form" / "viewform.html").read_bytes()

    async def script(page):
        async def handle(route):
            if route.request.url == "https://forms.test/viewform":
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            else:
                await route.abort()
        await page.context.route("**/*", handle)
        await page.goto("https://forms.test/viewform")
        await page.get_by_role("textbox", name="Your email").fill("demo@example.com")
        await page.get_by_role("textbox", name="Your email").blur()
        await page.get_by_role("button", name="Clear form").click()
    recording = run(record_with(tmp_path / "rec", ["forms.test"], script))
    email = next(e for e in recording["events"] if e["type"] == "input")
    assert email["el"]["question"] == "Email" and email["value"] == "demo@example.com"
    click = next(e for e in recording["events"] if e["type"] == "click" and e.get("form"))
    qs = click["form"]["questions"]
    assert [q["label"] for q in qs] == ["Email", "Job Title", "Company", "Link", "Date Applied"]
    assert [q["required"] for q in qs] == [True, False, False, False, False]

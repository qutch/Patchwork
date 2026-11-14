"""W1-4: runner/forms.py on the fixture tracker form (free port, headless Chromium) and, offline,
on the rendered demo Google Form (contracts V5, V6). Nothing here talks to Google."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

from demo_apps.tracker_form.app import create_app
from runner.contract import openapi_hash, validate_body
from runner.forms import fetch_form_contract, fill_and_submit, form_contract, open_form, resolve_control, snapshot_form
from runner.lib import Quarantine
from tests.conftest import _Server

REPO = Path(__file__).resolve().parents[2]
LABELS = ["Email", "Job Title", "Company", "Link", "Date Applied"]
VALUES = {"Email": "demo@example.com", "Job Title": "Software Engineer, Android", "Company": "Ramp",
          "Link": "https://jobs.ashbyhq.com/ramp/f564dcf9", "Date Applied": "2026-11-14"}
CONFIRM = "Your response has been recorded"
GOLDEN = dict(VALUES)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def form():
    with _Server(create_app()) as srv:
        yield srv.url


def _rows(url: str) -> list[dict]:
    return httpx.get(url + "/admin/rows").json()


def test_snapshot_fixture_form(page, form):
    open_form(page, form + "/")
    snap = snapshot_form(page)
    assert [q["label"] for q in snap] == LABELS
    assert [q["label"] for q in snap if q["required"]] == ["Email"]
    assert snap[0]["input"] == "email"  # the Google email-collection field is type=email
    assert all(q["input"] == "text" for q in snap[1:])
    assert all(q["options"] == [] for q in snap)


def test_contract_shape_and_hash_stable(form):
    a = fetch_form_contract(form + "/")
    b = fetch_form_contract(form + "/")
    assert openapi_hash(a) == openapi_hash(b)
    assert a["info"] == {"title": "Job tracker", "x-patchwork-kind": "form", "x-form-url": form + "/"}
    schema = a["paths"]["/form"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False and schema["required"] == ["Email"]
    assert list(schema["properties"]) == LABELS
    assert schema["properties"]["Email"] == {"type": "string", "x-input": "email"}
    assert validate_body(a, "POST", "/form", GOLDEN) == []


def test_form_contract_radio_enum():
    snap = [{"label": "Seniority", "input": "radio", "required": False, "options": ["Junior", "Senior"]},
            {"label": "Email", "input": "email", "required": True, "options": []}]
    doc = form_contract(snap, title="T", url="https://x/")
    schema = doc["paths"]["/form"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["properties"]["Seniority"] == {"type": "string", "x-input": "radio", "enum": ["Junior", "Senior"]}
    assert validate_body(doc, "POST", "/form", {"Email": "a", "Seniority": "Mid"}) == [
        "body.Seniority: 'Mid' not in enum ['Junior', 'Senior']"]


def test_rename_breaks_contract_with_exact_errors(form):
    before = fetch_form_contract(form + "/")
    httpx.post(form + "/admin/rename", json={"from": "Company", "to": "Employer"})
    after = fetch_form_contract(form + "/")
    assert openapi_hash(before) != openapi_hash(after)
    assert validate_body(after, "POST", "/form", GOLDEN) == [
        "body.Company: not accepted by destination (no such property)"]
    questions = [{"label": "Email", "required": True}, {"label": "Job Title"}, {"label": "Employer", "required": True},
                 {"label": "Link"}, {"label": "Date Applied"}]
    with _Server(create_app(questions=questions)) as srv:
        required = fetch_form_contract(srv.url + "/")
    assert sorted(validate_body(required, "POST", "/form", GOLDEN)) == sorted([
        "body.Employer: required by destination but missing from body",
        "body.Company: not accepted by destination (no such property)"])


def test_fill_and_submit_writes_one_row(page, form):
    open_form(page, form + "/")
    fill_and_submit(page, VALUES, submit="Submit", confirmation_text=CONFIRM, dry_run=False)
    rows = _rows(form)
    assert len(rows) == 1
    assert {k: v for k, v in rows[0].items() if k != "_ts"} == VALUES  # "Clear form" was never clicked


def test_dry_run_never_submits(page, form):
    open_form(page, form + "/")
    fill_and_submit(page, VALUES, submit="Submit", confirmation_text=CONFIRM, dry_run=True)
    assert page.get_by_label("Your email").input_value() == VALUES["Email"]  # filled
    assert _rows(form) == []
    assert "/formResponse" not in page.url


@pytest.mark.parametrize("case", ["missing", "two_company", "case_insensitive", "email_heading", "submit_exact",
                                  "submit_partial", "whitespace_and_star"])
def test_label_resolution_fails_closed(page, case):
    questions = None
    if case == "two_company":
        questions = [{"label": "Email", "required": True}, {"label": "Company"}, {"label": "Company"}]
    with _Server(create_app(questions=questions)) as srv:
        open_form(page, srv.url + "/")
        if case == "missing":
            with pytest.raises(Quarantine) as e:
                fill_and_submit(page, {**VALUES, "Salary": "1"}, submit="Submit", confirmation_text=CONFIRM,
                                dry_run=False)
            assert e.value.code == "SINK_CONTRACT" and "Salary" in e.value.detail
        elif case == "two_company":
            with pytest.raises(Quarantine) as e:
                fill_and_submit(page, {"Email": "a@b.c", "Company": "Ramp"}, submit="Submit",
                                confirmation_text=CONFIRM, dry_run=False)
            assert e.value.code == "SINK_CONTRACT" and "Company" in e.value.detail
            assert page.locator("input[name='entry.1']").input_value() == ""  # nothing filled at all
            assert page.locator("input[name='entry.2']").input_value() == ""
        elif case == "case_insensitive":
            kind, loc = resolve_control(page, "Job title")
            assert loc.get_attribute("name") == "entry.1" and kind == "text"
        elif case == "email_heading":
            kind, loc = resolve_control(page, "Email")
            assert loc.get_attribute("aria-label") == "Your email" and kind == "email"
        elif case == "whitespace_and_star":
            kind, loc = resolve_control(page, "  Email *")
            assert loc.get_attribute("aria-label") == "Your email"
        elif case in ("submit_exact", "submit_partial"):
            name = "Send" if case == "submit_exact" else "Clear"
            with pytest.raises(Quarantine) as e:
                fill_and_submit(page, VALUES, submit=name, confirmation_text=CONFIRM, dry_run=False)
            assert e.value.code == "SINK_CONTRACT" and name in e.value.detail
            # the button is checked before anything is typed: nothing filled, nothing clicked
            assert page.get_by_label("Your email").input_value() == ""
            assert "/formResponse" not in page.url
        assert _rows(srv.url) == []


_CHOICES = """<html><body><div role="list">
<div role="listitem"><div role="heading" aria-level="3">Company</div><input type="text" aria-label="Employer"></div>
<div role="listitem"><div role="heading" aria-level="3">Notes</div><input type="text" aria-label="Company"></div>
<div role="listitem"><div role="heading" aria-level="3">Seniority</div><div role="radiogroup">
  <div role="radio" aria-label="Junior" tabindex="0" onclick="this.setAttribute('aria-checked','true')">o</div>
  <div role="radio" aria-label="Senior" tabindex="0" onclick="this.setAttribute('aria-checked','true')">o</div></div></div>
<div role="listitem"><div role="heading" aria-level="3">Office</div>
  <select aria-label="Office"><option>Choose</option><option>NYC</option><option>SF</option></select></div>
</div><div role="button" aria-label="Submit">Submit</div></body></html>"""


def test_name_and_heading_disagree_fails_closed(page):
    page.set_content(_CHOICES)
    with pytest.raises(Quarantine) as e:
        resolve_control(page, "Company")  # one control is *named* Company, another question is *titled* Company
    assert e.value.code == "SINK_CONTRACT" and "ambiguous" in e.value.detail


def test_radio_and_select_options(page):
    page.set_content(_CHOICES)
    snap = {q["label"]: q for q in snapshot_form(page)}
    assert (snap["Seniority"]["input"], snap["Seniority"]["options"]) == ("radio", ["Junior", "Senior"])
    assert (snap["Office"]["input"], snap["Office"]["options"]) == ("select", ["NYC", "SF"])
    fill_and_submit(page, {"Seniority": "Senior", "Office": "SF"}, submit="Submit", dry_run=True)
    assert page.get_by_role("radio", name="Senior").get_attribute("aria-checked") == "true"
    assert page.get_by_label("Office").input_value() == "SF"
    for values in ({"Seniority": "Staff"}, {"Office": "LA"}):
        with pytest.raises(Quarantine) as e:
            fill_and_submit(page, values, submit="Submit", dry_run=True)
        assert e.value.code == "SINK_CONTRACT" and "no option" in e.value.detail


def test_no_confirmation_is_verify_mismatch(page, form):
    open_form(page, form + "/")
    with pytest.raises(Quarantine) as e:
        fill_and_submit(page, VALUES, submit="Submit", confirmation_text="Thanks, we got it",
                        dry_run=False, confirm_timeout_ms=1500)
    assert e.value.code == "VERIFY_MISMATCH"


def test_missing_required_answer_is_verify_mismatch_with_alert(page, form):
    open_form(page, form + "/")
    with pytest.raises(Quarantine) as e:
        fill_and_submit(page, {"Job Title": "x"}, submit="Submit", confirmation_text=CONFIRM,
                        dry_run=False, confirm_timeout_ms=1500)
    assert e.value.code == "VERIFY_MISMATCH" and "This is a required question" in e.value.detail
    assert _rows(form) == []


def test_unreachable_form_is_unknown(page):
    from tests.conftest import _free_port
    with pytest.raises(Quarantine) as e:
        open_form(page, f"http://127.0.0.1:{_free_port()}/", timeout_ms=3000)
    assert e.value.code == "UNKNOWN"


def test_google_form_snapshot_offline(browser):
    ctx = browser.new_context(java_script_enabled=False)
    ctx.route("**/*", lambda route: route.abort())  # strictly offline: never reach Google
    pg = ctx.new_page()
    pg.set_content((REPO / "fixtures" / "google_form" / "viewform.html").read_text())
    snap = snapshot_form(pg)
    assert [q["label"] for q in snap] == LABELS
    assert [q["label"] for q in snap if q["required"]] == ["Email"]
    assert [q["input"] for q in snap] == ["email", "text", "text", "text", "text"]
    for label in LABELS:
        resolve_control(pg, label)  # each resolves to exactly one control
    kind, loc = resolve_control(pg, "Email")
    assert loc.get_attribute("aria-label") == "Your email"
    assert pg.get_by_role("button", name="Submit", exact=True).count() == 1
    fixture_doc = form_contract(snap, title="t", url="u")
    assert validate_body(fixture_doc, "POST", "/form", GOLDEN) == []
    ctx.close()

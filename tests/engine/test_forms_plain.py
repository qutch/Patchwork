"""runner/forms.py on plain HTML forms (no Google markup): label[for], wrapping labels, aria-label,
fieldset/legend groups, HTML5 validation alerts, and an end-to-end run against demo_apps/plain_form."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import sync_playwright

from demo_apps.plain_form.app import CONFIRMATION, LABELS, SUBMIT_LABEL, create_app
from runner.contract import openapi_hash
from runner.forms import _alerts, fill_and_submit, form_contract, open_form, resolve_control, snapshot_form
from runner.lib import Quarantine
from tests.conftest import _Server

CONFIRM = "Thanks, application received"
VALUES = {"Full name": "Ada Lovelace", "Email": "ada@example.com", "Phone": "555-0100", "Stage": "Onsite",
          "Notes": "Strong referral", "Source": "Referral", "Skills": "Python, SQL"}


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
def app_url():
    with _Server(create_app()) as srv:
        yield srv.url


def _rows(url: str) -> list[dict]:
    return httpx.get(url + "/admin/rows").json()


_HTML = """
<form>
  <label for="a">Full name *</label><input id="a" type="text" required>
  <label>Email <input type="email"></label>
  <input type="text" aria-label="Phone">
  <label for="s">Stage</label><select id="s"><option value="">Pick</option><option>One</option><option>Two</option></select>
  <label for="t">Notes</label><textarea id="t"></textarea>
  <fieldset><legend>Source</legend>
    <label><input type="radio" name="src" value="a" required> LinkedIn</label>
    <label><input type="radio" name="src" value="b"> Referral</label></fieldset>
  <fieldset><legend>Skills</legend>
    <label><input type="checkbox" name="sk"> Python</label>
    <label><input type="checkbox" name="sk"> SQL</label></fieldset>
  <input type="submit" value="Go">
</form>"""


def test_snapshot_finds_every_label_strategy(page):
    page.set_content(_HTML)
    snap = {q["label"]: q for q in snapshot_form(page)}
    assert list(snap) == ["Full name", "Email", "Phone", "Stage", "Notes", "Source", "Skills"]
    assert snap["Full name"] == {"label": "Full name", "input": "text", "required": True, "options": []}
    assert snap["Email"]["input"] == "email" and not snap["Email"]["required"]
    assert snap["Stage"]["options"] == ["One", "Two"] and snap["Stage"]["input"] == "select"
    assert snap["Notes"]["input"] == "textarea"
    assert snap["Source"] == {"label": "Source", "input": "radio", "required": True, "options": ["LinkedIn", "Referral"]}
    assert snap["Skills"]["input"] == "checkbox" and snap["Skills"]["options"] == ["Python", "SQL"]


@pytest.mark.parametrize("label,kind", [("Full name", "text"), ("Email", "email"), ("Phone", "text"),
                                        ("Stage", "select"), ("Notes", "textarea"),
                                        ("Source", "radio"), ("Skills", "checkbox")])
def test_resolve_each_strategy(page, label, kind):
    page.set_content(_HTML)
    assert resolve_control(page, label)[0] == kind


@pytest.mark.parametrize("label,needle", [("Nope", "no question"), ("", "empty")])
def test_resolve_fails_closed(page, label, needle):
    page.set_content(_HTML)
    with pytest.raises(Quarantine) as e:
        resolve_control(page, label)
    assert e.value.code == "SINK_CONTRACT" and needle in e.value.detail


def test_duplicate_legend_is_ambiguous(page):
    page.set_content("<form>" + 2 * '<fieldset><legend>Pick</legend><label><input type="radio" name="x"> A</label>'
                                    '</fieldset>' + "</form>")
    with pytest.raises(Quarantine) as e:
        resolve_control(page, "Pick")
    assert e.value.code == "SINK_CONTRACT" and "2 fieldsets" in e.value.detail


def test_fill_every_control_kind_offline(page):
    page.set_content(_HTML)
    fill_and_submit(page, {"Full name": "Ada", "Email": "a@b.co", "Phone": "1", "Stage": "Two", "Notes": "hi",
                           "Source": "Referral", "Skills": "Python, SQL"}, submit="Go", dry_run=True)
    assert page.get_by_label("Full name").input_value() == "Ada"
    assert page.locator("#s").input_value() == "Two"
    assert page.get_by_role("radio", name="Referral").is_checked()
    assert page.get_by_role("checkbox", name="SQL").is_checked()


def test_unknown_label_fails_before_typing(page):
    page.set_content(_HTML)
    with pytest.raises(Quarantine):
        fill_and_submit(page, {"Full name": "Ada", "Missing": "x"}, submit="Go", dry_run=True)
    assert page.get_by_label("Full name").input_value() == ""


def test_unknown_option_is_quarantined(page):
    page.set_content(_HTML)
    with pytest.raises(Quarantine) as e:
        fill_and_submit(page, {"Source": "Nobody"}, submit="Go", dry_run=True)
    assert e.value.code == "SINK_CONTRACT" and "Nobody" in e.value.detail


def test_alerts_report_html5_and_aria_errors(page):
    page.set_content(_HTML + '<p role="alert">Server says no</p>'
                     '<input aria-label="Zip" aria-invalid="true" aria-describedby="z"><span id="z">Bad zip</span>')
    got = _alerts(page)
    assert "Server says no" in got
    assert any(a.startswith("Full name:") for a in got)       # required, empty
    assert any(a.startswith("Source:") for a in got)          # radio group is named by its legend
    assert "Zip: Bad zip" in got


def test_alerts_empty_when_valid(page):
    page.set_content("<form><input aria-label='x'></form>")
    assert _alerts(page) == []


def test_contract_from_plain_snapshot_is_stable(page):
    page.set_content(_HTML)
    c1 = form_contract(snapshot_form(page), title="t", url="u")
    c2 = form_contract(snapshot_form(page), title="t", url="u")
    props = c1["paths"]["/form"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert props["required"] == ["Full name", "Source"]
    assert props["properties"]["Source"]["enum"] == ["LinkedIn", "Referral"]
    assert props["properties"]["Skills"]["x-options"] == ["Python", "SQL"]
    assert openapi_hash(c1) == openapi_hash(c2)


# ---- end to end on demo_apps/plain_form ------------------------------------------------------------

def test_demo_snapshot_matches_labels(page, app_url):
    open_form(page, app_url)
    assert [q["label"] for q in snapshot_form(page)] == LABELS


def test_demo_fill_and_submit_writes_one_row(page, app_url):
    open_form(page, app_url)
    fill_and_submit(page, VALUES, submit=SUBMIT_LABEL, confirmation_text=CONFIRM, dry_run=False)
    rows = _rows(app_url)
    assert len(rows) == 1
    assert {k: v for k, v in rows[0].items() if k != "_ts"} == VALUES
    assert CONFIRMATION in page.content()


def test_demo_dry_run_stores_nothing(page, app_url):
    open_form(page, app_url)
    fill_and_submit(page, VALUES, submit=SUBMIT_LABEL, dry_run=True)
    assert _rows(app_url) == []


def test_demo_button_is_not_called_submit(page, app_url):
    open_form(page, app_url)
    with pytest.raises(Quarantine) as e:
        fill_and_submit(page, VALUES, dry_run=True)  # default submit="Submit"
    assert e.value.code == "SINK_CONTRACT" and "'Submit'" in e.value.detail


@pytest.mark.parametrize("values,needle", [
    ({"Email": "ada@example.com"}, "Full name:"),                       # required empty
    ({"Full name": "Ada", "Email": "not-an-email"}, "Email:"),          # type=email mismatch
])
def test_demo_html5_errors_become_verify_mismatch(page, app_url, values, needle):
    open_form(page, app_url)
    with pytest.raises(Quarantine) as e:
        fill_and_submit(page, values, submit=SUBMIT_LABEL, confirmation_text=CONFIRM, dry_run=False,
                        confirm_timeout_ms=1500)
    assert e.value.code == "VERIFY_MISMATCH" and needle in e.value.detail
    assert _rows(app_url) == []


def test_star_in_label_marks_a_field_required_without_the_attribute(page):
    page.set_content('<form><label for="n">Nickname *</label><input id="n" type="text">'
                     '<label for="o">Other</label><input id="o" type="text"><button>Go</button></form>')
    snap = {q["label"]: q["required"] for q in snapshot_form(page)}
    assert snap == {"Nickname": True, "Other": False}

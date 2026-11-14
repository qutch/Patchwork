"""W1-3: the fixture tracker form (contracts V8) mimics the demo Google Form's markup facts."""

from __future__ import annotations

from fastapi.testclient import TestClient
from selectolax.lexbor import LexborHTMLParser

from demo_apps.tracker_form.app import DEFAULT_QUESTIONS, create_app

LABELS = ["Email", "Job Title", "Company", "Link", "Date Applied"]


def _questions(html: str) -> list[dict]:
    tree = LexborHTMLParser(html)
    out = []
    for li in tree.css("div[role=listitem]"):
        heading = li.css_first("div[role=heading][aria-level='3']")
        req = heading.css_first('span[aria-label="Required question"]')
        label = heading.text(deep=True).replace(req.text(deep=True), "") if req else heading.text(deep=True)
        inputs = li.css("input")
        assert len(inputs) == 1
        out.append({"label": " ".join(label.split()), "required": req is not None, "heading_id": heading.attributes.get("id"),
                    "input": dict(inputs[0].attributes)})
    return out


def _form_data(client: TestClient, values: dict[str, str]) -> dict[str, str]:
    """Map labels to the form's input names, as a browser would submit them."""
    qs = _questions(client.get("/").text)
    return {q["input"]["name"]: values.get(q["label"], "") for q in qs}


def test_form_matches_demo_form():
    client = TestClient(create_app())
    html = client.get("/").text
    qs = _questions(html)
    assert [q["label"] for q in qs] == LABELS
    assert [q["required"] for q in qs] == [True, False, False, False, False]
    email = qs[0]["input"]
    assert email["aria-label"] == "Your email" and "aria-labelledby" not in email and email["type"] == "email"
    for q in qs[1:]:
        assert q["input"]["type"] == "text"
        assert q["input"]["aria-labelledby"].split()[0] == q["heading_id"]
    tree = LexborHTMLParser(html)
    assert tree.css_first('div[role=button][aria-label="Submit"]') is not None
    assert tree.css_first('div[role=button][aria-label="Clear form"]') is not None
    assert [x["label"] for x in DEFAULT_QUESTIONS] == LABELS


def test_submit_stores_row_and_confirms():
    client = TestClient(create_app())
    values = {"Email": "demo@example.com", "Job Title": "Engineer", "Company": "Ramp",
              "Link": "https://jobs.ashbyhq.com/ramp/1", "Date Applied": "2026-11-14"}
    resp = client.post("/submit", data=_form_data(client, values))
    assert resp.status_code == 200
    assert str(resp.url).endswith("/formResponse")
    assert "Your response has been recorded." in resp.text
    rows = client.get("/admin/rows").json()
    assert len(rows) == 1
    assert {k: v for k, v in rows[0].items() if k != "_ts"} == values
    assert rows[0]["_ts"]


def test_rename_changes_label():
    client = TestClient(create_app())
    assert client.post("/admin/rename", json={"from": "Company", "to": "Employer"}).status_code == 200
    labels = [q["label"] for q in _questions(client.get("/").text)]
    assert labels == ["Email", "Job Title", "Employer", "Link", "Date Applied"]
    client.post("/submit", data=_form_data(client, {"Email": "a@b.c", "Employer": "Ramp"}))
    assert client.get("/admin/rows").json()[0]["Employer"] == "Ramp"
    assert client.post("/admin/rename", json={"from": "Nope", "to": "X"}).status_code == 404


def test_reset():
    client = TestClient(create_app())
    client.post("/admin/rename", json={"from": "Company", "to": "Employer"})
    client.post("/submit", data=_form_data(client, {"Email": "a@b.c"}))
    assert len(client.get("/admin/rows").json()) == 1
    client.post("/admin/reset")
    assert client.get("/admin/rows").json() == []
    assert [q["label"] for q in _questions(client.get("/").text)] == LABELS


def test_missing_required_rejected():
    client = TestClient(create_app())
    resp = client.post("/submit", data=_form_data(client, {"Job Title": "Engineer"}))
    assert "Your response has been recorded." not in resp.text
    tree = LexborHTMLParser(resp.text)
    alerts = [a.text(deep=True).strip() for a in tree.css("[role=alert]") if a.text(deep=True).strip()]
    assert alerts == ["This is a required question"]
    assert [q["label"] for q in _questions(resp.text)] == LABELS  # form re-shown
    assert client.get("/admin/rows").json() == []


def test_custom_questions():
    client = TestClient(create_app(questions=[{"label": "Company", "required": False},
                                              {"label": "Employer", "required": True}]))
    qs = _questions(client.get("/").text)
    assert [(q["label"], q["required"]) for q in qs] == [("Company", False), ("Employer", True)]

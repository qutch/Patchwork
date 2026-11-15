"""Fixture tracker form (contracts V8): an offline stand-in for the demo Google Form.

The markup copies what spike a recorded in fixtures/google_form/viewform.html: each question is a
div[role=listitem] with a div[role=heading][aria-level=3] title (required ones end with
<span aria-label="Required question"> *</span>); the Email input is labelled "Your email" by its
aria-label, not by its heading; the other inputs point at their heading with aria-labelledby. Submit
and "Clear form" are div[role=button]s. Submitting leads to /formResponse ("Your response has been
recorded."). An empty required answer re-shows the form with a role=alert and stores nothing.

    GET  /                  the form
    POST /submit            form-encoded answers (entry.<n>) -> 303 /formResponse, or the form with alerts
    GET  /formResponse      confirmation page
    GET  /admin/rows        [{<label>: value, "_ts": iso}]
    POST /admin/rename      {"from": "Company", "to": "Employer"}  (Break: the owner renames a question)
    POST /admin/reset       rows cleared, labels back to the defaults
"""

from __future__ import annotations

import copy
import html
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

TITLE = "Job tracker"
BLURB = "Track every job I apply to."
CONFIRMATION = "Your response has been recorded."
REQUIRED_ALERT = "This is a required question"
DEFAULT_QUESTIONS: list[dict[str, Any]] = [
    {"label": "Email", "required": True, "input": "email"},
    {"label": "Job Title", "required": False},
    {"label": "Company", "required": False},
    {"label": "Link", "required": False},
    {"label": "Date Applied", "required": False},
]

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body {{ font-family: Roboto, Arial, sans-serif; background: #f0ebf8; margin: 0; padding: 24px; }}
 .card {{ background: #fff; border-radius: 8px; max-width: 640px; margin: 0 auto 12px; padding: 24px; }}
 .q-title {{ font-size: 16px; margin-bottom: 12px; }}
 .req {{ color: #d93025; }}
 input {{ width: 100%; font-size: 14px; border: 0; border-bottom: 1px solid #ccc; padding: 6px 0; }}
 .alert {{ color: #d93025; font-size: 12px; min-height: 0; margin-top: 8px; }}
 .buttons {{ max-width: 640px; margin: 0 auto; display: flex; justify-content: space-between; }}
 [role=button] {{ cursor: pointer; padding: 8px 24px; border-radius: 4px; user-select: none; }}
 .submit {{ background: #673ab7; color: #fff; }}
 .clear {{ color: #673ab7; }}
</style></head>
<body>{body}</body></html>"""


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    required: bool = False
    input: str = "text"


class Rename(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str


def _question_html(i: int, q: dict[str, Any], value: str, error: bool) -> str:
    hid, aid = f"i{i * 5 + 5}", f"i{i * 5 + 7}"
    label = html.escape(q["label"])
    req = ' <span class="req" aria-label="Required question"> *</span>' if q["required"] else ""
    kind = q.get("input") or "text"
    required_attr = " required" if q["required"] else ""
    if kind == "email":  # as on Google: labelled by aria-label, not by its heading
        labelling = 'aria-label="Your email"'
    else:
        labelling = f'aria-labelledby="{hid}" aria-describedby="{aid}"'
    alert = REQUIRED_ALERT if error else ""
    return (f'<div class="card" role="listitem"><div class="q-title" id="{hid}" role="heading" aria-level="3">'
            f'{label}{req}</div>'
            f'<input type="{kind}" name="entry.{i}" value="{html.escape(value)}" autocomplete="off" '
            f'{labelling}{required_attr}>'
            f'<div class="alert" id="{aid}" role="alert">{alert}</div></div>')


def render_form(questions: list[dict[str, Any]], values: dict[int, str] | None = None,
                errors: set[int] | None = None, *, title: str = TITLE, blurb: str = BLURB) -> str:
    values, errors = values or {}, errors or set()
    items = "".join(_question_html(i, q, values.get(i, ""), i in errors) for i, q in enumerate(questions))
    body = (f'<form id="f" method="post" action="/submit" novalidate>'
            f'<div class="card"><div role="heading" aria-level="1" style="font-size:28px">{html.escape(title)}</div>'
            f'<div>{html.escape(blurb)}</div></div>'
            f'<div role="list">{items}</div>'
            '<div class="buttons">'
            '<div role="button" tabindex="0" class="submit" aria-label="Submit" '
            'onclick="document.getElementById(\'f\').submit()" '
            'onkeydown="if(event.key===\'Enter\')document.getElementById(\'f\').submit()"><span>Submit</span></div>'
            '<div role="button" tabindex="0" class="clear" aria-label="Clear form" '
            'onclick="document.getElementById(\'f\').reset()"><span>Clear form</span></div>'
            '</div></form>')
    return _PAGE.format(title=html.escape(title), body=body)


def render_confirmation(*, title: str = TITLE) -> str:
    body = (f'<div class="card"><div role="heading" aria-level="1" style="font-size:28px">{html.escape(title)}</div>'
            f'<div class="confirm">{CONFIRMATION}</div>'
            '<a href="/">Submit another response</a></div>')
    return _PAGE.format(title=html.escape(title), body=body)


def create_app(questions: list[dict[str, Any]] | None = None, *, title: str = TITLE, blurb: str = BLURB) -> FastAPI:
    """The form with `questions` (default: the job tracker's), headed `title` and `blurb`."""
    defaults = []
    for q in questions or DEFAULT_QUESTIONS:
        item = Question(**q).model_dump()
        if "input" not in q:  # like Google: an "Email" question is the email-collection field
            item["input"] = "email" if item["label"] == "Email" else "text"
        defaults.append(item)
    state: dict[str, Any] = {"questions": copy.deepcopy(defaults), "rows": []}
    lock = threading.Lock()
    app = FastAPI(title="Tracker form (fixture)")

    @app.get("/", response_class=HTMLResponse)
    def form() -> str:
        with lock:
            return render_form(state["questions"], title=title, blurb=blurb)

    @app.post("/submit")
    async def submit(request: Request):
        pairs = parse_qsl((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
        answers: dict[int, str] = {}
        for k, v in pairs:
            if k.startswith("entry.") and k[6:].isdigit():
                answers[int(k[6:])] = v
        with lock:
            qs = state["questions"]
            errors = {i for i, q in enumerate(qs) if q["required"] and not answers.get(i, "").strip()}
            if errors:
                return HTMLResponse(render_form(qs, answers, errors, title=title, blurb=blurb))
            row = {q["label"]: answers.get(i, "") for i, q in enumerate(qs)}
            row["_ts"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            state["rows"].append(row)
        return RedirectResponse("/formResponse", status_code=303)

    @app.get("/formResponse", response_class=HTMLResponse)
    def confirmation() -> str:
        return render_confirmation(title=title)

    @app.get("/admin/rows")
    def rows() -> list[dict[str, Any]]:
        with lock:
            return copy.deepcopy(state["rows"])

    @app.post("/admin/rename")
    def rename(body: Rename) -> dict[str, Any]:
        with lock:
            hits = [q for q in state["questions"] if q["label"] == body.from_]
            if not hits:
                raise HTTPException(404, f"no question labelled {body.from_!r}")
            for q in hits:
                q["label"] = body.to
            return {"labels": [q["label"] for q in state["questions"]]}

    @app.post("/admin/reset")
    def reset() -> dict[str, Any]:
        with lock:
            state["questions"] = copy.deepcopy(defaults)
            state["rows"] = []
            return {"labels": [q["label"] for q in state["questions"]]}

    return app

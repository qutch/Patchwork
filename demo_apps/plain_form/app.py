"""Fixture plain form: an ordinary HTML5 job-application form, nothing Google-shaped about it.

Each field is found a different way: <label for>, a wrapping <label>, an aria-label, and fieldset/legend
groups for the radio and checkbox questions. The form has no novalidate, so the browser blocks an
empty required field or a bad email before anything is sent. The submit button is "Send application",
not "Submit".

    GET  /             the form
    POST /submit       form-encoded answers -> 303 /thanks (stores a row), or 400 with an error list
    GET  /thanks       confirmation page ("Thanks, application received.")
    GET  /admin/rows   [{<label>: value, "_ts": iso}]  (checkbox answers are comma-joined)
    POST /admin/reset  rows cleared
"""

from __future__ import annotations

import copy
import html
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

TITLE = "Job application"
CONFIRMATION = "Thanks, application received."
SUBMIT_LABEL = "Send application"
STAGES = ["Applied", "Phone screen", "Onsite", "Offer"]
SOURCES = ["LinkedIn", "Referral", "Company site"]
SKILLS = ["Python", "SQL", "Go"]

LABELS = ["Full name", "Email", "Phone", "Stage", "Notes", "Source", "Skills"]

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body {{ font-family: system-ui, sans-serif; margin: 0; padding: 24px; background: #f6f6f6; }}
 form, .card {{ background: #fff; max-width: 560px; margin: 0 auto; padding: 24px; border-radius: 8px; }}
 label, legend {{ display: block; font-weight: 600; margin: 14px 0 4px; }}
 label.opt {{ font-weight: 400; display: inline-block; margin-right: 12px; }}
 input[type=text], input[type=email], input[type=tel], select, textarea {{ width: 100%; padding: 6px; }}
 fieldset {{ border: 0; padding: 0; margin: 0; }}
 .err {{ color: #b00020; }}
 button {{ margin-top: 20px; padding: 8px 20px; }}
</style></head><body>{body}</body></html>"""


def _opts(name: str, kind: str, values: list[str]) -> str:
    return "".join(f'<label class="opt"><input type="{kind}" name="{name}" value="{html.escape(v)}"> '
                   f'{html.escape(v)}</label>' for v in values)


def render_form(errors: list[str] | None = None) -> str:
    err = "".join(f'<p class="err">{html.escape(e)}</p>' for e in errors or [])
    body = (
        f'<form method="post" action="/submit"><h1>{TITLE}</h1>{err}'
        '<label for="full-name">Full name <span aria-hidden="true">*</span></label>'
        '<input id="full-name" type="text" name="full_name" required>'
        '<label>Email <input type="email" name="email" required></label>'
        '<input type="tel" name="phone" aria-label="Phone">'
        '<label for="stage">Stage</label><select id="stage" name="stage">'
        '<option value="">Choose a stage</option>'
        + "".join(f'<option>{html.escape(s)}</option>' for s in STAGES) +
        '</select>'
        '<label for="notes">Notes</label><textarea id="notes" name="notes" rows="3"></textarea>'
        f'<fieldset><legend>Source</legend>{_opts("source", "radio", SOURCES)}</fieldset>'
        f'<fieldset><legend>Skills</legend>{_opts("skills", "checkbox", SKILLS)}</fieldset>'
        f'<button type="submit">{SUBMIT_LABEL}</button></form>')
    return _PAGE.format(title=TITLE, body=body)


def render_thanks() -> str:
    return _PAGE.format(title=TITLE, body=f'<div class="card"><h1>{CONFIRMATION}</h1>'
                                          '<a href="/">Send another</a></div>')


def create_app() -> FastAPI:
    rows: list[dict[str, Any]] = []
    lock = threading.Lock()
    app = FastAPI(title="Plain form (fixture)")

    @app.get("/", response_class=HTMLResponse)
    def form() -> str:
        return render_form()

    @app.post("/submit")
    async def submit(request: Request):
        got: dict[str, list[str]] = {}
        for k, v in parse_qsl((await request.body()).decode("utf-8", "replace"), keep_blank_values=True):
            got.setdefault(k, []).append(v)
        one = lambda k: (got.get(k) or [""])[0].strip()  # noqa: E731
        errors = []
        if not one("full_name"):
            errors.append("Full name is required")
        if "@" not in one("email"):
            errors.append("Email must be a valid address")
        if errors:
            return HTMLResponse(render_form(errors), status_code=400)
        row = {"Full name": one("full_name"), "Email": one("email"), "Phone": one("phone"),
               "Stage": one("stage"), "Notes": one("notes"), "Source": one("source"),
               "Skills": ", ".join(got.get("skills", [])),
               "_ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        with lock:
            rows.append(row)
        return RedirectResponse("/thanks", status_code=303)

    @app.get("/thanks", response_class=HTMLResponse)
    def thanks() -> str:
        return render_thanks()

    @app.get("/admin/rows")
    def admin_rows() -> list[dict[str, Any]]:
        with lock:
            return copy.deepcopy(rows)

    @app.post("/admin/reset")
    def reset() -> dict[str, Any]:
        with lock:
            rows.clear()
        return {"labels": LABELS}

    return app

"""A ~stand-in for W1's fixture tracker form (docs/contracts.md V8) until `demo_apps/tracker_form` lands.

Same API (`create_app(questions=None)`, `GET /`, `POST /submit`, `GET /formResponse`, `GET /admin/rows`,
`POST /admin/rename`, `POST /admin/reset`) and the Google-Forms-like markup V8 describes: `div[role=listitem]`
questions with a `div[role=heading][aria-level=3]` title, inputs tied by `aria-labelledby` (Email by
`aria-label="Your email"` instead), `div[role=button][aria-label=Submit]` / `"Clear form"`, and a native form POST
that lands on a page saying `Your response has been recorded.` Tests import it through `tests/capture/form_app.py`.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

DEFAULT_QUESTIONS = [{"label": "Email", "required": True}, {"label": "Job Title", "required": False},
                     {"label": "Company", "required": False}, {"label": "Link", "required": False},
                     {"label": "Date Applied", "required": False}]


def _page(questions: list[dict], error_for: set[str] = frozenset()) -> str:
    items = []
    for i, q in enumerate(questions):
        hid = f"h{i}"
        req = '<span aria-label="Required question"> *</span>' if q["required"] else ""
        if q["label"] == "Email":
            control = f'<input type="text" name="entry.{i}" aria-label="Your email" autocomplete="email">'
        else:
            control = f'<input type="text" name="entry.{i}" aria-labelledby="{hid}">'
        alert = '<div role="alert">This is a required question</div>' if q["label"] in error_for else ""
        items.append(f'<div role="listitem"><div role="heading" aria-level="3" id="{hid}">'
                     f'<span>{html.escape(q["label"])}</span>{req}</div>{control}{alert}</div>')
    return f"""<!doctype html><html><head><title>Patchwork Test Form</title></head><body>
<form id="f" method="post" action="/submit"><div role="list">{''.join(items)}</div>
<div role="button" tabindex="0" aria-label="Submit" id="submit"><span>Submit</span></div>
<div role="button" tabindex="0" aria-label="Clear form" id="clear"><span>Clear form</span></div></form>
<script>
  const f = document.getElementById('f');
  document.getElementById('submit').addEventListener('click', () => f.submit());
  document.getElementById('clear').addEventListener('click', () => f.reset());
</script></body></html>"""


def create_app(questions: list[dict] | None = None) -> FastAPI:
    app = FastAPI()
    defaults = [dict(q) for q in (questions or DEFAULT_QUESTIONS)]
    state: dict = {"questions": [dict(q) for q in defaults], "rows": []}

    @app.get("/", response_class=HTMLResponse)
    def form() -> str:
        return _page(state["questions"])

    @app.post("/submit")
    async def submit(request: Request):
        data = dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
        qs = state["questions"]
        values = {q["label"]: data.get(f"entry.{i}", "") for i, q in enumerate(qs)}
        missing = {q["label"] for q in qs if q["required"] and not values[q["label"]].strip()}
        if missing:
            return HTMLResponse(_page(qs, missing))
        state["rows"].append({**values, "_ts": datetime.now(timezone.utc).isoformat()})
        return RedirectResponse("/formResponse", status_code=303)

    @app.get("/formResponse", response_class=HTMLResponse)
    def confirmation() -> str:
        return ("<!doctype html><html><head><title>Patchwork Test Form</title></head><body>"
                "<div>Patchwork Test Form</div><div>Your response has been recorded.</div>"
                '<a href="/">Submit another response</a></body></html>')

    @app.get("/admin/rows")
    def rows() -> list[dict]:
        return state["rows"]

    @app.post("/admin/rename")
    async def rename(request: Request) -> dict:
        body = await request.json()
        for q in state["questions"]:
            if q["label"] == body["from"]:
                q["label"] = body["to"]
        return {"ok": True}

    @app.post("/admin/reset")
    def reset() -> dict:
        state["questions"] = [dict(q) for q in defaults]
        state["rows"] = []
        return {"ok": True}

    return app

"""Supplier Inbox (:8001): the source. HTML inbox UI plus GET /api/messages.

The HTML pages render from the JSON API with fetch(), like a real webmail client, so the
recorder sees the GET that carried the raw source record.

Admin (demo control, not part of the source API):
  POST /admin/deliver {"count": N}       N new messages, same 3 formats and mix as the seed
  POST /admin/new_supplier {"count": N}  Break 2: N messages in a 4th format from umbrella.com
  POST /admin/reset                      back to the 150-message seed
  GET  /admin/ground_truth               {message_id: {outcome, reason, body?, ...}} for every message
"""

from __future__ import annotations

import html
import json
import os
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from demo_apps.seed import deliveries, generate, new_supplier_messages


class Message(BaseModel):
    id: str
    sender: str
    sender_domain: str
    subject: str
    body: str
    received_at: str


class Count(BaseModel):
    count: int = Field(1, ge=1, le=1000)


def create_app() -> FastAPI:
    app = FastAPI(title="Supplier Inbox", version="1.0.0")
    lock = threading.Lock()
    limit_env = os.environ.get("INBOX_LIMIT")

    def reset_state() -> None:
        seed = generate()
        messages = seed.messages[: int(limit_env)] if limit_env else seed.messages
        app.state.messages = list(messages)
        app.state.truth = {m["id"]: seed.ground_truth[m["id"]] for m in messages}
        app.state.n_delivered = 0
        app.state.n_new_supplier = 0
        app.state.next_id = len(seed.messages) + 1  # ids never reuse a seed id, even with INBOX_LIMIT

    reset_state()

    def append(batch) -> dict:
        app.state.messages.extend(batch.messages)
        app.state.truth.update(batch.ground_truth)
        app.state.next_id += len(batch.messages)
        return {"delivered": [m["id"] for m in batch.messages], "total": len(app.state.messages)}

    def latest() -> str:
        return max((m["received_at"] for m in app.state.messages), default="2026-11-02T08:00:00Z")

    @app.get("/api/messages", response_model=list[Message])
    def list_messages(since: str = "", limit: int | None = None) -> list[dict]:
        out = [m for m in app.state.messages if m["received_at"] > since]
        return out[:limit] if limit else out

    @app.get("/api/messages/{message_id}", response_model=Message)
    def get_message(message_id: str) -> dict:
        for m in app.state.messages:
            if m["id"] == message_id:
                return m
        raise HTTPException(404, "message not found")

    @app.post("/admin/deliver", include_in_schema=False)
    def deliver(body: Count | None = None) -> dict:
        n = (body or Count()).count
        with lock:
            batch = deliveries(app.state.n_delivered, n, after=latest(), first_id=app.state.next_id)
            app.state.n_delivered += n
            return append(batch)

    @app.post("/admin/new_supplier", include_in_schema=False)
    def new_supplier(body: Count | None = None) -> dict:
        n = (body or Count()).count
        with lock:
            batch = new_supplier_messages(app.state.n_new_supplier, n, after=latest(),
                                          first_id=app.state.next_id)
            app.state.n_new_supplier += n
            return append(batch)

    @app.post("/admin/reset", include_in_schema=False)
    def reset() -> dict:
        with lock:
            reset_state()
        return {"ok": True, "total": len(app.state.messages)}

    @app.get("/admin/ground_truth", include_in_schema=False)
    def ground_truth() -> dict:
        return dict(app.state.truth)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def inbox() -> str:
        body = """
<table id="inbox"><thead><tr><th>Received</th><th>From</th><th>Subject</th></tr></thead><tbody></tbody></table>
<script>
fetch('/api/messages').then(r => r.json()).then(msgs => {
  const tb = document.querySelector('#inbox tbody');
  for (const m of msgs) {
    const tr = document.createElement('tr');
    for (const v of [m.received_at, m.sender]) {
      const td = document.createElement('td'); td.textContent = v; tr.appendChild(td);
    }
    const td = document.createElement('td'), a = document.createElement('a');
    a.href = '/messages/' + encodeURIComponent(m.id); a.textContent = m.subject;
    td.appendChild(a); tr.appendChild(td); tb.appendChild(tr);
  }
});
</script>"""
        return _page("Supplier Inbox", body)

    @app.get("/messages/{message_id}", response_class=HTMLResponse, include_in_schema=False)
    def message_page(message_id: str) -> str:
        m = get_message(message_id)
        body = (
            f'<p><a href="/">&larr; Inbox</a></p><h2 id="subject">{html.escape(m["subject"])}</h2>'
            '<p class="meta" id="meta"></p><pre data-testid="message-body" id="body"></pre>'
            "<script>"
            f"fetch('/api/messages/' + encodeURIComponent({json.dumps(m['id'])})).then(r => r.json()).then(m => {{"
            "  document.getElementById('meta').textContent = 'From ' + m.sender + ' \\u00b7 ' + m.received_at;"
            "  const pre = document.getElementById('body'); pre.textContent = m.body; pre.dataset.loaded = '1';"
            "});"
            "</script>"
        )
        return _page(m["subject"], body)

    return app


def _page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui;margin:2rem;max-width:60rem}td,th{padding:.3rem .6rem;"
        "text-align:left;border-bottom:1px solid #ddd}pre{background:#f6f6f6;padding:1rem;white-space:pre-wrap}"
        ".meta{color:#666}</style></head>"
        f"<body>{body}</body></html>"
    )


app = create_app()

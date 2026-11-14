"""Supplier Inbox (:8001): the source. HTML inbox UI plus GET /api/messages."""

from __future__ import annotations

import html
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from demo_apps.seed import generate


class Message(BaseModel):
    id: str
    sender: str
    sender_domain: str
    subject: str
    body: str
    received_at: str


def create_app() -> FastAPI:
    app = FastAPI(title="Supplier Inbox", version="1.0.0")
    limit_env = os.environ.get("INBOX_LIMIT")
    messages = generate().messages
    if limit_env:
        messages = messages[: int(limit_env)]
    app.state.messages = messages

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

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def inbox() -> str:
        rows = "".join(
            f'<tr><td>{html.escape(m["received_at"])}</td><td>{html.escape(m["sender"])}</td>'
            f'<td><a href="/messages/{m["id"]}">{html.escape(m["subject"])}</a></td></tr>'
            for m in app.state.messages
        )
        return _page("Supplier Inbox", f"<table><tr><th>Received</th><th>From</th><th>Subject</th></tr>{rows}</table>")

    @app.get("/messages/{message_id}", response_class=HTMLResponse, include_in_schema=False)
    def message_page(message_id: str) -> str:
        m = get_message(message_id)
        body = (
            f'<p><a href="/">&larr; Inbox</a></p><h2>{html.escape(m["subject"])}</h2>'
            f'<p class="meta">From {html.escape(m["sender"])} &middot; {html.escape(m["received_at"])}</p>'
            f'<pre data-testid="message-body">{html.escape(m["body"])}</pre>'
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

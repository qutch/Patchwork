"""In-process stand-in for the control plane's page inbox (contracts V1, V9 `GET /api/pages`).

W4a owns the real one; this serves fixture pages so the engine's end-to-end tests don't depend on it."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from fixtures.jobs.pages import CAPTURED_DATE, fixture_records, page_record


class NewPage(BaseModel):
    url: str
    html: str
    title: str | None = None


def create_app(records: list[dict[str, Any]] | None = None) -> FastAPI:
    pages = list(records if records is not None else fixture_records())
    lock = threading.Lock()
    app = FastAPI(title="fake page inbox")

    @app.get("/api/pages")
    def list_pages(since: str = "", limit: int = 100) -> list[dict[str, Any]]:
        after = int(since) if since.strip().isdigit() else 0
        with lock:
            return [p for p in pages if p["seq"] > after][:limit]

    @app.post("/api/pages", status_code=201)
    def add_page(body: NewPage) -> dict[str, Any]:
        with lock:
            seq = (pages[-1]["seq"] if pages else 0) + 1
            rec = page_record(seq, body.url, body.html, title=body.title,
                              captured_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                              captured_date=CAPTURED_DATE)
            pages.append(rec)
        return {k: rec[k] for k in ("id", "seq", "site", "canonical_url")}

    return app

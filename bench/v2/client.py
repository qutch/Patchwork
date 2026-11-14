"""A small V9 client (docs/contracts.md Part 2) shared by bench/v2/run.py and scripts/smoke_v2.py.

Only HTTP, the same calls the extension and the UI make: POST /api/pages with the extension token, poll
GET /api/pages/{id} for the TrackerRow, drive agent threads through /api/agent/{id}[/resume].
"""

from __future__ import annotations

import gzip
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[2]
POSTINGS = REPO / "fixtures" / "postings"
NAME = "jobs_to_tracker"
FINAL = {"success", "duplicate", "failed"}


class Fail(Exception):
    pass


def check(cond: Any, msg: str) -> None:
    if not cond:
        raise Fail(msg)


def load_key(root: Path = POSTINGS) -> list[dict]:
    return json.loads((root / "answer_key.json").read_text())


def load_posting(entry: dict, root: Path = POSTINGS) -> dict[str, str]:
    """The body the extension would send for this stored page: {url, title, html}."""
    html = gzip.decompress((root / entry["file"]).read_bytes()).decode("utf-8")
    return {"url": entry["url"], "title": entry.get("document_title") or entry["title"], "html": html}


def presenter_answer(question: str, options: list[str], entry: dict | None = None) -> tuple[str, str]:
    """Answer an ask_user like a person reading the page: pick the option equal to the answer key's value
    (title or company) when one is offered; else the first option. Returns (answer, how)."""
    if entry:
        for o in options:
            for f in ("title", "company"):
                if " ".join(o.split()).casefold() == " ".join(str(entry.get(f, "")).split()).casefold():
                    return o, f"picked the option equal to the answer key's {f}"
    if "Skip this field" in options:  # V14 TEACH: a form question the Airtable table has no field for
        return "Skip this field", "skipped the form question the table has no field for"
    q = question.lower()
    if "email" in q:
        return "demo@example.com", "demo email"
    return (options[0], "first option") if options else ("Use your best judgement and say what you chose.", "free text")


class Control:
    def __init__(self, base: str, timeout: float = 60):
        self.base = base.rstrip("/")
        self.api = httpx.Client(base_url=self.base, timeout=timeout)
        self._token: str | None = None

    # -- plain HTTP ---------------------------------------------------------------------------
    def get(self, path: str, ok: tuple[int, ...] = (200,), **kw: Any) -> Any:
        r = self.api.get(path, **kw)
        check(r.status_code in ok, f"GET {path} -> {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    def post(self, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201), **kw: Any) -> Any:
        r = self.api.post(path, json=body, **kw)
        check(r.status_code in ok, f"POST {path} -> {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    def put(self, path: str, body: Any) -> Any:
        r = self.api.put(path, json=body)
        check(r.status_code == 200, f"PUT {path} -> {r.status_code} {r.text[:300]}")
        return r.json()

    # -- V9 -----------------------------------------------------------------------------------
    @property
    def token(self) -> str:
        if self._token is None:
            self._token = self.get("/api/extension")["token"]
        return self._token

    def post_page(self, page: dict[str, str]) -> dict:
        return self.post("/api/pages", page, ok=(201,), headers={"X-Patchwork-Token": self.token})

    def page(self, page_id: str) -> dict:
        return self.get(f"/api/pages/{page_id}")

    def tracker(self, limit: int = 200) -> dict:
        return self.get("/api/tracker", params={"integration": NAME, "limit": limit})

    def layouts(self) -> list[dict]:
        return self.get("/api/layouts", params={"integration": NAME})

    def thread(self, tid: str) -> dict:
        return self.get(f"/api/agent/{tid}")

    # -- waiting ------------------------------------------------------------------------------
    def wait_row(self, page_id: str, *, timeout: float, on_pending: Callable[[str, dict], None] | None = None,
                 settle_s: float = 1.5, until: set[str] | None = None) -> dict:
        """Poll the page's TrackerRow until it is final. A `held` row is final only once nothing more will happen
        to it: no learn thread was started for it within `settle_s`, or its learn thread finished and no run is
        pending. `on_pending(tid, pending)` is called when the page's learn thread waits for a person.
        With `until`, wait for one of those statuses instead (e.g. {"success"} after a repair, when a held row
        stays held until the replay has run)."""
        deadline = time.monotonic() + timeout
        held_since: float | None = None
        handled: set[str] = set()
        while time.monotonic() < deadline:
            row = self.page(page_id)["row"]
            st = row.get("status")
            if until is not None:
                if st in until:
                    return row
                time.sleep(0.1)
                continue
            if st in FINAL:
                return row
            tid = row.get("learn_thread_id")
            if tid:
                s = self.thread(tid)
                p = s.get("pending") or {}
                if s.get("state") == "waiting" and p.get("interrupt_id") and p["interrupt_id"] not in handled:
                    handled.add(p["interrupt_id"])
                    if on_pending:
                        on_pending(tid, s)
            if st == "held":
                learning = tid and self.thread(tid).get("state") not in ("done", "failed")
                if learning:
                    held_since = None
                else:
                    held_since = held_since or time.monotonic()
                    if time.monotonic() - held_since >= settle_s:
                        return row
            else:
                held_since = None
            time.sleep(0.1)
        raise Fail(f"page {page_id} not final after {timeout:.0f}s: {row}")

    def wait_thread(self, tid: str, want: set[str], timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.thread(tid)
            if s["state"] in want:
                return s
            check(s["state"] != "failed", f"agent thread {tid} failed: {s.get('error') or s}")
            time.sleep(0.1)
        raise Fail(f"agent thread {tid} not {want} after {timeout:.0f}s")

    def answer_until_deploy(self, tid: str, timeout: float, entry: dict | None = None,
                            log: list | None = None) -> dict:
        """Answer ask_user like a person and approve the deploy (no human think time). Returns the deploy info."""
        for _ in range(12):
            s = self.wait_thread(tid, {"waiting", "done"}, timeout)
            if s["state"] == "done":
                return {"version": (s.get("result") or {}).get("version"), "approved_by": "auto"}
            p = s["pending"]
            if p["type"] == "ask_user":
                ans, how = presenter_answer(p.get("question", ""), p.get("options") or [], entry)
                if log is not None:
                    log.append({"question": p.get("question"), "answer": ans, "how": how})
                self.post(f"/api/agent/{tid}/resume", {"interrupt_id": p["interrupt_id"], "answer": ans}, ok=(200,))
                continue
            tests = p.get("tests") or {}
            check(tests.get("passed", True), f"generated tests failed: {tests}")
            self.post(f"/api/agent/{tid}/resume", {"interrupt_id": p["interrupt_id"], "decision": "approve"}, ok=(200,))
            self.wait_thread(tid, {"done"}, timeout)
            return {"version": p.get("version"), "approved_by": "human"}
        raise Fail(f"thread {tid}: too many questions")

"""Shared inputs for TEACH from example pages (wizard approach A), without the control plane's teach endpoint:
a `draft-<name>` recording dir (recording.json, snapshots, candidates.json in the Task 4 shape) under the test
workspace, the example pages in the workspace's PageInbox, and a fake destination. No langchain at import.

    products_destination(...)    fake Airtable fields for Product/Price/Store/Link/Date Added
    write_teach_draft(...)       -> (recording_id, page_ids)
    TeachCtx(...)                .call(tool, **args) and .spec() over the real tool functions
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import yaml

from demo_apps.fake_airtable.app import BASE_ID, TABLE, TOKEN

PRODUCT_LABELS = ["Product", "Price", "Store", "Link", "Date Added"]
# The owner's real Products table (design doc, section 6), as fake Airtable fields.
PRODUCT_FIELDS: list[dict[str, Any]] = [
    {"id": "fldProduct000001", "name": "Product", "type": "singleLineText"},
    {"id": "fldPrice00000001", "name": "Price", "type": "number", "options": {"precision": 2}},
    {"id": "fldStore00000001", "name": "Store", "type": "singleLineText"},
    {"id": "fldLink000000001", "name": "Link", "type": "url"},
    {"id": "fldDateAdded0001", "name": "Date Added", "type": "date",
     "options": {"dateFormat": {"name": "iso", "format": "YYYY-MM-DD"}}},
]
_TYPES = {"singleLineText": "text", "number": "number", "url": "url", "date": "date"}


def airtable_destination(fields: list[dict] = PRODUCT_FIELDS, required: tuple[str, ...] = (),
                         credential: str = "AIRTABLE_TOKEN") -> dict:
    """The candidates' `sink` for a fake Airtable table: the destination plus its fields as labels/questions."""
    labels = [f["name"] for f in fields]
    return {"kind": "airtable", "base_id": BASE_ID, "table": TABLE, "credential": credential, "labels": labels,
            "questions": [{"label": f["name"], "type": _TYPES.get(f["type"], f["type"]), "required": f["name"] in required}
                          for f in fields]}


def write_teach_draft(name: str, pages: list[tuple[str, str]], sink: dict, intent: str) -> tuple[str, list[str]]:
    """Store `pages` ([(url, html)]) in the PageInbox for `name`, then write recordings/draft-<name>/ the way the
    teach endpoint does (Task 4). Returns (recording_id, page ids)."""
    from control.pages import PageInbox
    from runner.html import css_first
    from runner.store import workspace_dir
    inbox = PageInbox()
    recs = [inbox.add(url, css_first(html, "title") or "", html, integration=name) for url, html in pages]
    rid = f"draft-{name}"
    d = workspace_dir() / "recordings" / rid
    (d / "snapshots").mkdir(parents=True, exist_ok=True)
    examples = []
    for i, ((url, html), rec) in enumerate(zip(pages, recs), start=1):
        (d / "snapshots" / f"{i}.html").write_text(html)
        examples.append({"page_id": rec["id"], "site": rec["site"], "url": url, "snapshot": f"snapshots/{i}.html"})
    (d / "recording.json").write_text(json.dumps({"id": rid, "intent": intent, "events": [], "start_url": pages[0][0]}))
    first = examples[0]
    c = {"recording_id": rid, "intent": intent, "teach_mode": "examples",
         "source": {"kind": "page", "url": first["url"], "site": first["site"], "snapshot": first["snapshot"],
                    "page_id": first["page_id"]},
         "examples": examples, "sink": sink, "mappings": [], "unexplained": []}
    (d / "candidates.json").write_text(json.dumps(c, indent=1))
    return rid, [r["id"] for r in recs]


def product_pages() -> list[tuple[str, str]]:
    from fixtures.products.pages import PAGES, html
    return [(PAGES[f]["url"], html(f)) for f in ("shop-a.html", "shop-b.html")]


class TeachCtx:
    """The real tool functions on one BUILD thread of a ScriptedAgentService (no model)."""

    def __init__(self, name: str, recording_id: str, page_ids: list[str]):
        from agents.deep_tools import ToolContext, tool_functions
        from agents.interface import _Thread
        from agents.scripted import ScriptedAgentService, work_dir
        work_dir(name)
        self.svc = ScriptedAgentService(pace_s=0)
        self.t = _Thread(id="build-teachex01", mode="build", subject=recording_id, integration=name)
        self.svc._threads[self.t.id] = self.t
        self.ctx = ToolContext(self.svc, self.t, name=name, recording_id=recording_id)
        self.tools = tool_functions(self.ctx)
        self.page_ids = page_ids

    def call(self, tool: str, **args: Any) -> Any:
        return self.tools[tool](**args)

    def spec(self) -> dict:
        return yaml.safe_load(self.ctx.spec_path.read_text())

    def events(self) -> list[dict]:
        return self.svc.events(self.t.id)


def call_answering(svc, t, fn, answers: list[dict], timeout: float = 120):
    """Run `fn` in a thread; resume each interrupt with the next payload in `answers`. Returns (result, asked)."""
    out, asked = {}, []
    th = threading.Thread(target=lambda: out.setdefault("r", fn()), daemon=True)
    th.start()
    deadline = time.time() + timeout
    while th.is_alive() and time.time() < deadline:
        p = t.pending
        if p is not None:
            asked.append(p)
            svc.resume(t.id, p["interrupt_id"], answers.pop(0))
        time.sleep(0.01)
    th.join(1)
    assert "r" in out, "tool did not finish"
    return out["r"], asked


__all__ = ["PRODUCT_LABELS", "PRODUCT_FIELDS", "TOKEN", "airtable_destination", "write_teach_draft",
           "product_pages", "TeachCtx", "call_answering"]

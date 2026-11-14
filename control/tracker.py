"""The Tracker view and the Learned layouts panel (docs/contracts.md V9): pure functions over the
store (runs, records, incidents), the page inbox, the agent (learn threads) and the compiled
packages' manifests. No FastAPI, no network: the integrator can call these directly.

    tracker_rows(store, inbox, agent, integration, limit) -> {rows: [TrackerRow] newest first, totals}
    page_row(store, inbox, agent, integration, page_id)   -> TrackerRow | None
    layouts(store, integration)                           -> [Layout]
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from control.autorun import version_manifest, version_spec
from runner.store import Store

LABELS = ("code", "learned_now", "held", "duplicate")
ACTIVE_THREAD_STATES = ("running", "waiting")
PAGE_FIELDS = ("title", "company")


def _dt(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def _sink_values(spec: dict[str, Any] | None) -> dict[str, Any]:
    sink = (spec or {}).get("sink") or {}
    return sink.get("fields") or sink.get("body") or {}


def _spec_parsers(spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list(((spec or {}).get("extract") or {}).get("parsers") or [])


class _View:
    """Everything one tracker request needs, read once."""

    def __init__(self, store: Store, inbox: Any, agent: Any, integration: str):
        self.store, self.inbox, self.agent, self.integration = store, inbox, agent, integration
        self.outcomes = self._latest_outcomes()
        self.learns = inbox.learns(integration) if hasattr(inbox, "learns") else {}
        self.runs = [r for r in store.runs(integration, limit=50) if not r["dry_run"]]
        self._threads: dict[str, dict[str, Any]] = {}
        self._versions: dict[int, tuple[dict, dict]] = {}

    def _latest_outcomes(self) -> dict[str, dict[str, Any]]:
        """Latest live outcome per page id, without the (large) raw page."""
        cols = [r["name"] for r in self.store.db.execute("PRAGMA table_info(records)") if r["name"] != "raw"]
        sel = ", ".join(f"r.{c}" for c in cols)
        rows = self.store.db.execute(
            f"SELECT {sel}, u.version AS run_version, u.kind AS run_kind FROM records r JOIN runs u ON u.id = r.run_id "
            "WHERE u.integration = ? AND u.dry_run = 0 ORDER BY r.run_id", (self.integration,)).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            d = dict(row)
            d["sink_body"] = json.loads(d["sink_body"]) if d.get("sink_body") else None
            out[d["record_id"]] = d
        return out

    def version(self, v: int | None) -> tuple[dict, dict]:
        """(spec, manifest) of a version, cached."""
        if v is None:
            return {}, {}
        if v not in self._versions:
            self._versions[v] = (version_spec(self.integration, v) or {}, version_manifest(self.integration, v))
        return self._versions[v]

    def parser_origin(self, v: int | None, parser_id: str | None) -> dict[str, Any] | None:
        if not parser_id:
            return None
        spec, manifest = self.version(v)
        for p in manifest.get("parsers") or []:
            if p.get("id") == parser_id:
                return p.get("origin")
        for p in _spec_parsers(spec):
            if p.get("id") == parser_id:
                return p.get("origin")
        return None

    def thread(self, thread_id: str | None) -> dict[str, Any]:
        """{state, model_calls, deployed} of a learn thread (unknown thread → state None, 0 calls)."""
        if not thread_id:
            return {"state": None, "model_calls": 0, "deployed": False}
        if thread_id not in self._threads:
            info: dict[str, Any] = {"state": None, "model_calls": 0, "deployed": False}
            if self.agent is not None:
                try:
                    info["state"] = self.agent.status(thread_id).get("state")
                    events = self.agent.events(thread_id)
                except (KeyError, OSError, ValueError):
                    events = []
                for ev in events:
                    if ev.get("type") == "done":
                        result = ev.get("result") or {}
                        if isinstance(result, dict):
                            info["model_calls"] = int(result.get("model_calls") or 0)
                    elif ev.get("type") == "deployed" and ev.get("name") == self.integration:
                        info["deployed"] = True
            self._threads[thread_id] = info
        return self._threads[thread_id]

    def extracted(self, v: int | None, body: dict | None) -> dict[str, Any]:
        """title/company from the mapped sink body, via the spec's `{from: title}` style mappings."""
        if not body:
            return {}
        spec, _ = self.version(v)
        out = {}
        for label, src in _sink_values(spec).items():
            if isinstance(src, dict) and src.get("from") in PAGE_FIELDS and label in body:
                out[src["from"]] = body[label]
        return out

    def latest_run(self) -> dict[str, Any] | None:
        return next((r for r in self.runs if r.get("kind", "run") == "run"), None)

    def row(self, page: dict[str, Any]) -> dict[str, Any]:
        pid = page["id"]
        rec = self.outcomes.get(pid)
        learn = self.learns.get(pid)
        tid = learn["thread_id"] if learn else None
        thread = self.thread(tid)
        row: dict[str, Any] = {
            "page_id": pid, "seq": page["seq"], "captured_at": page["captured_at"], "site": page["site"],
            "url": page["url"], "title": page.get("title") or None, "company": None,
            "status": "pending", "label": None, "reason": None, "detail": None,
            "parser": None, "version": None, "routed_by": None, "parser_origin": None,
            "ai_calls": thread["model_calls"] if tid else 0, "run_id": None, "learn_thread_id": tid,
            "incident_id": None,
        }
        if rec is not None:
            v = rec.get("run_version")
            row.update(parser=rec.get("parser"), version=v, routed_by=rec.get("routed_by"), run_id=rec["run_id"],
                       reason=rec.get("reason"), detail=rec.get("detail"),
                       parser_origin=self.parser_origin(v, rec.get("parser")))
            row.update({k: val for k, val in self.extracted(v, rec.get("sink_body")).items() if val is not None})
            if rec["status"] == "success":
                origin = row["parser_origin"] or {}
                learned_here = tid is not None and origin.get("thread_id") == tid
                row.update(status="success", label="learned_now" if learned_here else "code")
            elif rec["status"] == "duplicate":
                row.update(status="duplicate", label="duplicate")
            elif tid and thread["state"] in ACTIVE_THREAD_STATES:
                row.update(status="learning")
            elif tid and thread["deployed"]:
                row.update(status="running")  # learned and deployed: the replay is queued or running
            else:
                row.update(status="held", label="held")
            return row
        run = self.latest_run()
        if run is not None:
            started, captured = _dt(run.get("started_at")), _dt(page.get("captured_at"))
            after_page = started is None or captured is None or started.replace(microsecond=0) >= captured.replace(
                microsecond=0)
            if run["status"] == "running":
                row.update(status="running", run_id=run["id"])
            elif run["status"] == "blocked" and after_page:
                inc = self.store.incident(run["incident_id"]) if run.get("incident_id") else None
                row.update(status="held", label="held", run_id=run["id"], incident_id=run.get("incident_id"),
                           reason=(inc or {}).get("type") or "CONTRACT_CHANGED",
                           detail=(inc or {}).get("summary") or run.get("message"))
            elif run["status"] in ("failed", "tripped") and after_page:
                row.update(status="failed", run_id=run["id"], detail=run.get("message"))
        return row


def _totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"pages": len(rows), "ai_calls": sum(int(r.get("ai_calls") or 0) for r in rows)}
    for label in LABELS:
        totals[label] = sum(1 for r in rows if r.get("label") == label)
    return totals


def tracker_rows(store: Store, inbox: Any, agent: Any, integration: str = "jobs_to_tracker",
                 limit: int = 50) -> dict[str, Any]:
    """{rows: newest first (at most `limit`), totals over every page in the inbox}."""
    view = _View(store, inbox, agent, integration)
    rows = [view.row(p) for p in inbox.summaries()]
    return {"rows": rows[:max(0, int(limit))], "totals": _totals(rows)}


def page_row(store: Store, inbox: Any, agent: Any, integration: str, page_id: str) -> dict[str, Any] | None:
    page = next((p for p in inbox.summaries() if p["id"] == page_id), None)
    if page is None:
        return None
    return _View(store, inbox, agent, integration).row(page)


# ---- learned layouts ---------------------------------------------------------------------------

def _parser_site(parser: dict[str, Any]) -> str | None:
    match = parser.get("match") or {}
    return match.get("site") or (next(iter(match.values())) if len(match) == 1 else None)


def layouts(store: Store, integration: str = "jobs_to_tracker") -> list[dict[str, Any]]:
    """One Layout per parser id ever compiled into the integration, from the versions' manifests
    (V4 `parsers: [{id, match, fingerprint, origin}]`, falling back to the compiled spec):
    {integration, parser_id, site, fingerprint, fields, origin, introduced_in, active}.
    Active parsers first in the active spec's order, then removed ones, newest first."""
    active_v = store.active_version(integration)
    seen: dict[str, dict[str, Any]] = {}
    active_ids: list[str] = []
    for ver in store.versions(integration):
        v = ver["version"]
        spec, manifest = version_spec(integration, v) or {}, version_manifest(integration, v)
        spec_parsers = {p.get("id"): p for p in _spec_parsers(spec)}
        listed = manifest.get("parsers")
        entries = listed if isinstance(listed, list) else list(spec_parsers.values())
        for entry in entries:
            pid = entry.get("id")
            if not pid:
                continue
            full = {**spec_parsers.get(pid, {}), **{k: val for k, val in entry.items() if val is not None}}
            if pid not in seen:
                seen[pid] = {"integration": integration, "parser_id": pid, "introduced_in": v}
            seen[pid].update(site=_parser_site(full), fingerprint=full.get("fingerprint"),
                             fields=full.get("fields") or spec_parsers.get(pid, {}).get("fields") or {},
                             origin=full.get("origin"), last_version=v)
            if v == active_v:
                active_ids.append(pid)
    out = []
    for pid in active_ids:
        out.append({**seen[pid], "active": True})
    rest = sorted((p for p in seen if p not in active_ids), key=lambda p: -seen[p]["last_version"])
    out += [{**seen[pid], "active": False} for pid in rest]
    for layout in out:
        layout.pop("last_version", None)
    return out

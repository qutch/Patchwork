"""A stand-in for `python -m runner run` on a page integration, until W1's engine lands.

Same CLI, stdout protocol (started / fetched / summary), store writes and cursor handling as the real
runner, but routing is by `match.site` (then `fingerprint`) on the compiled spec.yaml, values are
fake, and nothing is sent anywhere. Pages are fetched over HTTP from $PATCHWORK_CONTROL_URL, so tests
see the control URL reach the runner.

Test switches (files in the workspace):
  fake_runner_gate      while it exists, a run holds after fetching (a "busy" run)
  fake_form_renamed     preflight blocks with a CONTRACT_CHANGED incident (nothing fetched)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

import httpx
import yaml

from runner.html import fingerprint_present
from runner.pipeline import Outcome
from runner.store import Store, workspace_dir


def route(parsers: list[dict], rec: dict) -> tuple[dict | None, str | None]:
    for p in parsers:
        if p.get("match") and all(rec.get(k) == v for k, v in p["match"].items()):
            return p, "match"
    for p in parsers:
        if p.get("fingerprint") and fingerprint_present(rec.get("html") or "", p["fingerprint"]):
            return p, "fingerprint"
    return None, None


def value(src: dict, rec: dict) -> str:
    if "const" in src:
        return str(src["const"])
    ref = src.get("from", "")
    if ref.startswith("record."):
        return str(rec.get(ref.removeprefix("record."), ""))
    if ref == "title":
        return rec.get("title") or ""
    if ref == "company":
        return f"Company at {rec.get('site')}"
    return f"{ref} at {rec.get('site')}" if ref else ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("name")
    ap.add_argument("--version", default="active")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--replay-quarantined", action="store_true")
    a = ap.parse_args()

    ws = workspace_dir()
    store = Store()
    v = store.active_version(a.name) if a.version == "active" else int(a.version)
    spec = yaml.safe_load((ws / "integrations" / a.name / f"v{v}" / "spec.yaml").read_text())
    parsers = spec["extract"]["parsers"]
    kind = "replay" if a.replay_quarantined else "run"
    scope = "staging" if a.dry_run else "live"
    run_id = store.start_run(a.name, v, a.dry_run, kind=kind, pid=os.getpid())
    print(json.dumps({"event": "started", "run_id": run_id}), flush=True)
    t0 = time.monotonic()
    counts: Counter[str] = Counter()

    def finish(status: str, message: str | None = None, incident_id: int | None = None) -> None:
        store.finish_run(run_id, status, dict(counts), time.monotonic() - t0, incident_id, message)
        print(json.dumps({"run_id": run_id, "integration": a.name, "version": v, "dry_run": a.dry_run, "kind": kind,
                          "status": status, "counts": dict(counts), "duration_s": 0.0, "llm_calls": 0,
                          "incident_id": incident_id, "message": message}), flush=True)
        sys.exit(0)

    if (ws / "fake_form_renamed").exists():
        inc = store.open_incident(name=a.name, version=v, run_id=run_id, type="CONTRACT_CHANGED",
                                  reason_codes=["SINK_CONTRACT"], summary="Destination contract changed; 1 contract "
                                  "errors. Blocked before any write.",
                                  detail={"contract_errors": ["body.Company: not accepted by destination (no such property)"]},
                                  old_openapi={}, new_openapi={})
        finish("blocked", "preflight: destination contract changed", inc)

    if a.replay_quarantined:
        records = store.quarantined_raw(a.name)
    else:
        base = os.environ.get("PATCHWORK_CONTROL_URL")
        if not base:
            finish("failed", "PATCHWORK_CONTROL_URL is not set")
        cursor = store.get_cursor(a.name, scope) or ""
        params = {k: str(v).replace("{cursor}", cursor)  # like the compiled adapter: the spec's list params
                  for k, v in ((spec["source"].get("list") or {}).get("params") or {"since": "{cursor}"}).items()}
        resp = httpx.get(base.rstrip("/") + "/api/pages", params=params, timeout=10)
        resp.raise_for_status()
        records = resp.json()
    if a.limit is not None:
        records = records[:a.limit]
    print(json.dumps({"event": "fetched", "run_id": run_id, "total": len(records)}), flush=True)

    while (ws / "fake_runner_gate").exists():
        time.sleep(0.02)

    for rec in records:
        out = Outcome(record_id=rec["id"], status="quarantined", route_value=rec.get("site"))
        parser, _routed_by = route(parsers, rec)
        if parser is None:
            out.reason, out.detail = "PARSE_NO_MATCH", f"no parser for site={rec.get('site')}; no fingerprint matched"
        else:
            out.parser = parser["id"]
            out.key = rec["canonical_url"]
            if store.is_processed(a.name, scope, out.key):
                out.status = "duplicate"
            else:
                out.body = {label: value(src, rec) for label, src in spec["sink"]["fields"].items()}
                store.mark_processed(a.name, scope, out.key, run_id)
                out.status = "success"
        store.add_record(run_id, out, rec)
        counts[out.status] += 1
        if out.reason:
            counts[f"quarantined:{out.reason}"] += 1
        if not a.replay_quarantined:
            store.set_cursor(a.name, scope, str(rec["seq"]))
    finish("completed")


if __name__ == "__main__":
    main()

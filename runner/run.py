"""The run loop: preflight, fetch, per-record pipeline, circuit breaker, incidents, logs.

This process never calls an LLM. Every run summary carries llm_calls: 0.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml

from runner.contract import openapi_hash, validate_body
from runner.credentials import redact
from runner.http import ConfigError, Http
from runner.lib import AI_MODULES
from runner.loader import load_integration
from runner.pipeline import process_record
from runner.store import Store, workspace_dir

CIRCUIT_BREAKER_THRESHOLD = 3
QUARANTINE_THRESHOLD = 3


class _ScopedStore:
    def __init__(self, store: Store, name: str, scope: str, run_id: int):
        self.store, self.name, self.scope, self.run_id = store, name, scope, run_id

    def is_processed(self, key: str) -> bool:
        return self.store.is_processed(self.name, self.scope, key)

    def mark(self, key: str) -> None:
        self.store.mark_processed(self.name, self.scope, key, self.run_id)


def version_dir(name: str, version: int, ws: Path | None = None) -> Path:
    return (ws or workspace_dir()) / "integrations" / name / f"v{version}"


def _live_contract(http: Http, integ: Any) -> dict[str, Any]:
    """Packages compiled by the v2 templates fetch their own contract (API: /openapi.json; form: a
    snapshot in the run's browser). v1-compiled packages have no fetch_contract: GET /openapi.json."""
    fetch = getattr(integ, "fetch_contract", None)
    if fetch is not None:
        return fetch(http)
    resp = http.request("sink", "GET", "/openapi.json")
    resp.raise_for_status()
    return resp.json()


def preflight(http: Http, vdir: Path, manifest: dict, integ: Any = None) -> tuple[bool, dict[str, Any]]:
    """Compare the sink's live contract with the snapshot this version was compiled against.
    Returns (ok, info). Runs the contract check on the golden bodies only if the hash changed."""
    current = _live_contract(http, integ)
    cur_hash = openapi_hash(current)
    info: dict[str, Any] = {"openapi_hash": cur_hash, "expected_hash": manifest.get("openapi_hash"),
                            "changed": cur_hash != manifest.get("openapi_hash")}
    if not info["changed"]:
        return True, info
    golden = json.loads((vdir / "golden.json").read_text())
    method, path = manifest["sink_call"]["method"], manifest["sink_call"]["path"]
    errors = []
    for ex in golden:
        if "body" in ex["expect"]:
            errors += [f"[{ex['name']}] {e}" for e in validate_body(current, method, path, ex["expect"]["body"])]
    info["contract_errors"] = sorted(set(errors))
    info["current_openapi"] = current
    return not errors, info


def _pace_s() -> float:
    """Optional per-record delay (PATCHWORK_RUN_PACE_MS) so live counters are watchable in a demo."""
    try:
        return max(0.0, float(os.environ.get("PATCHWORK_RUN_PACE_MS", "0") or 0) / 1000)
    except ValueError:
        return 0.0


@contextmanager
def run_lock(name: str, scope: str) -> Iterator[None]:
    """Exclusive across threads and processes: one run of `name` in `scope` ("live" | "staging") at a time.
    The idempotency check and the mark of a record are separate steps, so two overlapping runs (say the
    control plane's after-deploy run and a person's run) could both write one record. Runs wait here instead.
    An OS file lock: released when the holder exits, even if it crashes."""
    path = workspace_dir() / "locks" / f"run-{name}-{scope}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def run(name: str, version: int | str = "active", *, dry_run: bool = False, limit: int | None = None,
        replay: bool = False, store: Store | None = None, log=print) -> dict[str, Any]:
    """Run an integration. With `replay=True` the records come from the DB instead of the source:
    raw records whose latest live outcome is quarantined. Idempotency keys keep that safe, and a
    replay never moves the cursor or opens a NEW_SOURCE_FORMAT incident.

    Runs of one integration in one scope never overlap (`run_lock`), so a record is never written twice.

    The first line logged is {"event": "started", "run_id": N}; the last is the run summary.
    Every line logged or written to the run log passes through `redact` (contracts V13)."""
    with run_lock(name, "staging" if dry_run else "live"):
        return _run(name, version, dry_run=dry_run, limit=limit, replay=replay, store=store, log=log)


def _run(name: str, version: int | str = "active", *, dry_run: bool = False, limit: int | None = None,
         replay: bool = False, store: Store | None = None, log=print) -> dict[str, Any]:
    raw_log = log

    def log(line: str) -> None:
        raw_log(redact(line))

    ws = workspace_dir()
    store = store or Store()
    if version == "active":
        v = store.active_version(name)
        if v is None:
            raise SystemExit(f"{name}: no active version; deploy one first")
    else:
        v = int(version)
    vdir = version_dir(name, v, ws)
    manifest = json.loads((vdir / "manifest.json").read_text())
    config = yaml.safe_load((vdir / "config.yaml").read_text())
    integ = load_integration(vdir)
    leaked = sorted(m for m in sys.modules if m.split(".")[0] in AI_MODULES)
    if leaked:
        raise SystemExit(f"refusing to run: AI modules imported in runner process: {leaked}")

    scope = "staging" if dry_run else "live"
    kind = "replay" if replay else "run"
    try:
        http = Http(config, staging=dry_run)
    except ConfigError as e:  # e.g. patchwork://control without PATCHWORK_CONTROL_URL: a failed run, said clearly
        run_id = store.start_run(name, v, dry_run, kind=kind, pid=os.getpid())
        log(json.dumps({"event": "started", "run_id": run_id}))
        store.finish_run(run_id, "failed", {}, 0.0, None, str(e))
        summary = {"run_id": run_id, "integration": name, "version": v, "dry_run": dry_run, "kind": kind,
                   "status": "failed", "counts": {}, "duration_s": 0.0, "llm_calls": 0, "incident_id": None,
                   "message": str(e), "log": None}
        log(json.dumps(summary))
        return summary
    run_id = store.start_run(name, v, dry_run, kind=kind, pid=os.getpid())
    log(json.dumps({"event": "started", "run_id": run_id}))
    pace = _pace_s()
    log_path = ws / "runs" / f"{run_id}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    counts: Counter[str] = Counter()
    incident_id: int | None = None
    status, message = "completed", None

    def finish() -> dict[str, Any]:
        duration = time.monotonic() - t0
        store.finish_run(run_id, status, dict(counts), duration, incident_id, message)
        summary = {"run_id": run_id, "integration": name, "version": v, "dry_run": dry_run, "kind": kind,
                   "status": status, "counts": dict(counts), "duration_s": round(duration, 3),
                   "llm_calls": 0, "incident_id": incident_id, "message": message, "log": str(log_path)}
        log(json.dumps(summary))
        http.close()
        return summary

    with log_path.open("w") as jl:
        try:
            ok, info = preflight(http, vdir, manifest, integ)
        except Exception as e:  # noqa: BLE001
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (401, 403) and http.sink_credential:  # V13: name the credential, never show it
                message = f"preflight: {http.sink_label} rejected the credential {http.sink_credential} (HTTP {code})"
            else:
                message = f"preflight could not reach sink: {e}"
            status = "failed"
            return finish()
        jl.write(redact(json.dumps({"event": "preflight", "ok": ok,
                                    **{k: v_ for k, v_ in info.items() if k != "current_openapi"}})) + "\n")
        jl.flush()
        if not ok:
            snapshot = vdir / "contract" / "openapi.json"
            old = json.loads(snapshot.read_text()) if snapshot.exists() else None
            incident_id = store.open_incident(
                name=name, version=v, run_id=run_id, type="CONTRACT_CHANGED", reason_codes=["SINK_CONTRACT"],
                summary=f"Destination contract changed; {len(info['contract_errors'])} contract errors. "
                        "Blocked before any write.",
                detail={"contract_errors": info["contract_errors"], "sink_call": manifest["sink_call"]},
                old_openapi=old, new_openapi=info["current_openapi"])
            status, message = "blocked", "preflight: destination contract changed"
            return finish()

        try:
            records = (store.quarantined_raw(name) if replay
                       else integ.list_records(http, store.get_cursor(name, scope)))
        except Exception as e:  # noqa: BLE001
            status, message = "failed", f"could not fetch source records: {e}"
            return finish()
        if limit is not None:
            records = records[:limit]
        log(json.dumps({"event": "fetched", "run_id": run_id, "total": len(records)}))
        streak, quarantined = 0, []
        for rec in records:
            out = process_record(integ, rec, http, _ScopedStore(store, name, scope, run_id))
            store.add_record(run_id, out, rec)
            jl.write(redact(json.dumps({"event": "record", **out.to_dict()})) + "\n")
            jl.flush()
            counts[out.status] += 1
            if out.reason:
                counts[f"quarantined:{out.reason}"] += 1
                quarantined.append((out, rec))
            if not replay and integ.CURSOR_FIELD and rec.get(integ.CURSOR_FIELD):
                store.set_cursor(name, scope, str(rec[integ.CURSOR_FIELD]))
            if pace:
                time.sleep(pace)
            streak = streak + 1 if out.reason == "SINK_CONTRACT" else 0
            if streak >= CIRCUIT_BREAKER_THRESHOLD:
                samples = [{"record_id": o.record_id, "detail": o.detail, "body": o.body}
                           for o, _ in quarantined[-CIRCUIT_BREAKER_THRESHOLD:]]
                incident_id = store.open_incident(
                    name=name, version=v, run_id=run_id, type="CIRCUIT_BREAKER", reason_codes=["SINK_CONTRACT"],
                    summary=f"{CIRCUIT_BREAKER_THRESHOLD} consecutive SINK_CONTRACT failures; run stopped.",
                    samples=samples)
                status, message = "tripped", "circuit breaker: consecutive sink contract failures"
                break

        by_route = Counter(o.route_value for o, _ in quarantined if o.reason == "PARSE_NO_MATCH")
        for route_value, n in by_route.items():
            if n >= QUARANTINE_THRESHOLD and incident_id is None and not replay:
                samples = [{"record_id": o.record_id, "detail": o.detail, "record": r}
                           for o, r in quarantined if o.reason == "PARSE_NO_MATCH" and o.route_value == route_value][:5]
                incident_id = store.open_incident(
                    name=name, version=v, run_id=run_id, type="NEW_SOURCE_FORMAT", reason_codes=["PARSE_NO_MATCH"],
                    summary=f"{n} records from {integ.ROUTE_BY}={route_value} matched no parser.", samples=samples)
        return finish()

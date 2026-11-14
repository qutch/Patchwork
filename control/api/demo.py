"""Demo controls (proxies to the demo apps) and the accuracy benchmark.

App URLs: PATCHWORK_INBOX_URL, PATCHWORK_STOCKROOM_URL, PATCHWORK_STAGING_URL
(defaults http://localhost:8001, :8002, :8003), and for v2 the fixture tracker form
PATCHWORK_TRACKER_FORM_URL (default http://127.0.0.1:18010; offline rehearsal only). Upstream status codes are passed through,
so an Inbox without /admin/deliver answers 404 here too. Unreachable apps answer 502.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from control import ops
from runner.store import Store

router = APIRouter(prefix="/api", tags=["demo"])

DEFAULTS = {"inbox": ("PATCHWORK_INBOX_URL", "http://localhost:8001"),
            "stockroom": ("PATCHWORK_STOCKROOM_URL", "http://localhost:8002"),
            "staging": ("PATCHWORK_STAGING_URL", "http://localhost:8003"),
            "tracker_form": ("PATCHWORK_TRACKER_FORM_URL", "http://127.0.0.1:18010")}


def app_url(app: str) -> str:
    env, default = DEFAULTS[app]
    return (os.environ.get(env) or default).rstrip("/")


class Count(BaseModel):
    count: int = Field(1, ge=1, le=10_000)


class SchemaVersion(BaseModel):
    version: int


async def _call(app: str, method: str, path: str, json: Any = None) -> tuple[int, Any]:
    url = app_url(app) + path
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.request(method, url, json=json)
    except httpx.HTTPError as e:
        return 502, {"detail": f"{app} unreachable at {app_url(app)}: {e}"}
    try:
        data = resp.json()
    except ValueError:
        data = {"detail": resp.text[:500]}
    if resp.status_code == 404 and isinstance(data, dict) and data.get("detail") == "Not Found":
        data = {"detail": f"{app} has no {method} {path} (404); the Capture workstream adds it"}
    return resp.status_code, data


async def _proxy(app: str, method: str, path: str, json: Any = None) -> JSONResponse:
    status, data = await _call(app, method, path, json)
    return JSONResponse(data, status_code=status)


@router.post("/demo/stockroom/schema")
async def stockroom_schema(body: SchemaVersion) -> JSONResponse:
    """Break 1 switch: flips the contract on Stockroom live and staging."""
    out, worst = {}, 200
    for app in ("stockroom", "staging"):
        status, data = await _call(app, "POST", "/admin/schema", {"version": body.version})
        out[app] = {"status": status, **(data if isinstance(data, dict) else {"body": data})}
        worst = max(worst, status)
    return JSONResponse(out, status_code=worst if worst >= 400 else 200)


@router.post("/demo/inbox/deliver")
async def inbox_deliver(body: Count) -> JSONResponse:
    return await _proxy("inbox", "POST", "/admin/deliver", {"count": body.count})


@router.post("/demo/inbox/new_supplier")
async def inbox_new_supplier(body: Count) -> JSONResponse:
    """Break 2: messages from a supplier whose format no parser knows."""
    return await _proxy("inbox", "POST", "/admin/new_supplier", {"count": body.count})


@router.post("/demo/reset")
async def reset(request: Request) -> dict[str, Any]:
    """Reset Inbox, Stockroom live + staging, and the workspace (DB, run logs, integrations,
    agent threads). Recordings and settings are kept. Runs in progress are killed."""
    state = request.app.state
    # Stop every writer first: agent threads (cancelled and joined) and runs (killed),
    # and only then wipe the workspace, so nothing writes into the fresh state.
    agents = await run_in_threadpool(reset_agent, state)
    state.runs.stop_all()
    state.runs.clear()
    apps = {}
    for app in ("inbox", "stockroom", "staging"):
        status, data = await _call(app, "POST", "/admin/reset")
        apps[app] = {"status": status, **({"detail": data.get("detail")} if isinstance(data, dict) and status >= 400 else {})}
    removed = ops.reset_workspace()
    root = getattr(getattr(state, "agent", None), "root", None)
    if root is not None:
        Path(root).mkdir(parents=True, exist_ok=True)
    return {"ok": True, "apps": apps, "removed": removed, "agents": agents}


def reset_agent(state) -> dict[str, Any]:
    """Cancel all agent threads, including ones blocked on ask_user/request_deploy."""
    svc = getattr(state, "agent", None)
    if svc is None or not hasattr(svc, "reset"):
        return {"cancelled": [], "still_running": []}
    return svc.reset() or {"cancelled": [], "still_running": []}


async def ground_truth() -> tuple[dict[str, dict], str]:
    """The Inbox's GET /admin/ground_truth if it has one (covers delivered messages), else the seed."""
    status, data = await _call("inbox", "GET", "/admin/ground_truth")
    if status == 200:
        if isinstance(data, dict) and isinstance(data.get("ground_truth"), dict):
            data = data["ground_truth"]
        if isinstance(data, list):
            data = {str(d.get("record_id") or d.get("id")): d for d in data if isinstance(d, dict)}
        if isinstance(data, dict) and data:
            return data, "inbox"
    from demo_apps.seed import generate
    return generate().ground_truth, "seed"


@router.get("/bench/accuracy")
async def accuracy(integration: str | None = None) -> dict[str, Any]:
    """Exact accuracy of the latest live outcome per record against ground truth."""
    from bench.accuracy import score

    store = Store()
    names = [integration] if integration else [i["name"] for i in store.integrations()]
    if integration and store.version(integration, store.latest_version(integration)) is None:
        raise HTTPException(404, f"integration {integration} not found")
    records = [r for n in names for r in store.latest_outcomes(n)]
    truth, source = await ground_truth()
    return {"integration": integration, "truth_source": source, **score(records, truth)}


BENCH_RESULTS = Path(__file__).resolve().parents[2] / "bench" / "results.json"


@router.get("/bench/results")
def bench_results() -> JSONResponse:
    """The committed benchmark (`make bench` writes bench/results.json; `PATCHWORK_BENCH_RESULTS`
    overrides the path). 404 until it exists."""
    path = Path(os.environ.get("PATCHWORK_BENCH_RESULTS") or BENCH_RESULTS)
    try:
        return JSONResponse(json.loads(path.read_text()))
    except FileNotFoundError:
        raise HTTPException(404, "no benchmark results yet: run `make bench`") from None
    except ValueError as e:
        raise HTTPException(500, f"{path.name} is not valid JSON: {e}") from None


# ---- v2 demo controls (docs/contracts.md V9) ------------------------------------------------------

V2_INTEGRATION = "jobs_to_tracker"
BENCH_V2_RESULTS = Path(__file__).resolve().parents[2] / "bench" / "v2" / "results.json"


class FormRename(BaseModel):
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)


class V2Target(BaseModel):
    integration: str = V2_INTEGRATION


@router.post("/demo/form/rename")
async def form_rename(body: FormRename) -> JSONResponse:
    """The break beat on the fixture tracker form (the Google Form is renamed by a person)."""
    return await _proxy("tracker_form", "POST", "/admin/rename", {"from": body.from_, "to": body.to})


def _baseline_dir() -> Path:
    from runner.store import workspace_dir
    return workspace_dir() / "v2_baseline"


def _read_baseline() -> dict[str, Any] | None:
    try:
        return json.loads((_baseline_dir() / "baseline.json").read_text())
    except (OSError, ValueError):
        return None


@router.post("/demo/v2/save-baseline")
def save_baseline(body: V2Target | None = None) -> dict[str, Any]:
    """Remember the integration's versions, active pointer and agent work dir as the rehearsal baseline."""
    import shutil
    from datetime import datetime, timezone

    name = (body or V2Target()).integration
    store = Store()
    active = store.active_version(name)
    if active is None:
        raise HTTPException(409 if store.versions(name) else 404, f"{name} has no active version to save as the baseline")
    bdir = _baseline_dir()
    work = ops.version_dir(name, 0).parent / "work"
    if (bdir / "work").exists():
        shutil.rmtree(bdir / "work")
    bdir.mkdir(parents=True, exist_ok=True)
    if work.is_dir():
        shutil.copytree(work, bdir / "work")
    baseline = {"integration": name, "active_version": active,
                "versions": [v["version"] for v in store.versions(name)], "has_work": work.is_dir(),
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (bdir / "baseline.json").write_text(json.dumps(baseline, indent=2))
    return baseline


def cancel_threads(svc: Any, ids: list[str], timeout: float = 5.0) -> list[str]:
    """Cancel some agent threads (learn/repair of the v2 integration), leaving the rest alone.
    Uses the agent's own `cancel(thread_id)` if it has one, else ThreadedAgentService's internals the
    way its `reset()` does (cancelled flag under its lock, wake, join)."""
    import time

    if svc is None or not ids:
        return []
    if hasattr(svc, "cancel"):
        for tid in ids:
            try:
                svc.cancel(tid)
            except KeyError:
                pass
        return list(ids)
    lock, threads = getattr(svc, "_lock", None), getattr(svc, "_threads", None)
    if lock is None or threads is None:
        return []
    with lock:
        picked = [threads.pop(tid) for tid in ids if tid in threads]
        for t in picked:
            t.cancelled, t.pending, t.answer = True, None, None
    for t in picked:
        t.wake.set()
    deadline = time.monotonic() + timeout
    for t in picked:
        if getattr(t, "worker", None) is not None:
            t.worker.join(max(0.0, deadline - time.monotonic()))
    return [t.id for t in picked]


def _reset_integration_rows(name: str, keep_versions: list[int], active: int) -> dict[str, int]:
    """Delete the integration's runs, records, processed keys, cursors and incidents, and versions
    newer than the baseline. Other integrations' rows are never touched."""
    import shutil

    from runner.store import workspace_dir

    store = Store()
    db = store.db
    run_ids = [r[0] for r in db.execute("SELECT id FROM runs WHERE integration=?", (name,))]
    removed = {"runs": len(run_ids)}
    removed["records"] = db.execute(
        "DELETE FROM records WHERE run_id IN (SELECT id FROM runs WHERE integration=?)", (name,)).rowcount
    db.execute("DELETE FROM runs WHERE integration=?", (name,))
    removed["processed"] = db.execute("DELETE FROM processed WHERE integration=?", (name,)).rowcount
    db.execute("DELETE FROM cursors WHERE integration=?", (name,))
    removed["incidents"] = db.execute("DELETE FROM incidents WHERE integration=?", (name,)).rowcount
    drop = [v["version"] for v in store.versions(name) if v["version"] not in keep_versions]
    for v in drop:
        db.execute("DELETE FROM versions WHERE name=? AND version=?", (name, v))
        shutil.rmtree(ops.version_dir(name, v), ignore_errors=True)
    removed["versions"] = len(drop)
    db.execute("UPDATE integrations SET active_version=? WHERE name=?", (active, name))
    store.close()
    for rid in run_ids:
        (workspace_dir() / "runs" / f"{rid}.jsonl").unlink(missing_ok=True)
    return removed


@router.post("/demo/v2/reset")
async def v2_reset(request: Request) -> dict[str, Any]:
    """Restore the saved baseline of jobs_to_tracker: stop its runs, cancel its learn (and repair)
    threads, clear pages and its runs/records/processed/cursor/incidents, drop newer versions, put the
    active pointer and the agent's work dir back. Never touches po_to_stockroom (v1's reset is unchanged)."""
    import shutil

    baseline = _read_baseline()
    if baseline is None:
        raise HTTPException(409, "no v2 baseline saved yet: POST /api/demo/v2/save-baseline first")
    name = baseline["integration"]
    state = request.app.state
    state.auto.forget(name)
    stopped = await run_in_threadpool(state.runs.stop, name)
    svc = getattr(state, "agent", None)
    thread_ids = [learn["thread_id"] for learn in state.inbox.learns(name).values()]
    if svc is not None:
        incident_ids = {str(i["id"]) for i in Store().incidents(name, status=None)}
        for t in svc.list_threads():
            if t.get("mode") == "repair" and str(t.get("subject")) in incident_ids:
                thread_ids.append(t["thread_id"])
    cancelled = await run_in_threadpool(cancel_threads, svc, thread_ids)
    state.auto.forget(name)  # anything a cancelled thread queued on its way out
    removed = await run_in_threadpool(_reset_integration_rows, name, baseline["versions"], baseline["active_version"])
    removed["pages"] = state.inbox.clear()
    work = ops.version_dir(name, 0).parent / "work"
    if baseline.get("has_work") and (_baseline_dir() / "work").is_dir():
        shutil.rmtree(work, ignore_errors=True)
        shutil.copytree(_baseline_dir() / "work", work)
    out: dict[str, Any] = {"ok": True, "integration": name, "active_version": baseline["active_version"],
                           "removed": removed, "stopped_runs": stopped, "cancelled_threads": cancelled}
    if os.environ.get("PATCHWORK_TRACKER_FORM_URL"):  # the offline fixture form: rows and labels back to default
        status, _ = await _call("tracker_form", "POST", "/admin/reset")
        out["form"] = {"status": status}
    return out


@router.get("/bench/v2")
def bench_v2() -> JSONResponse:
    """bench/v2/results.json (W5, V12; `PATCHWORK_BENCH_V2_RESULTS` overrides the path). 404 until it exists."""
    path = Path(os.environ.get("PATCHWORK_BENCH_V2_RESULTS") or BENCH_V2_RESULTS)
    try:
        return JSONResponse(json.loads(path.read_text()))
    except FileNotFoundError:
        raise HTTPException(404, "no v2 benchmark results yet: run `make bench-v2`") from None
    except ValueError as e:
        raise HTTPException(500, f"{path.name} is not valid JSON: {e}") from None

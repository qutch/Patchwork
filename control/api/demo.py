"""Demo controls (proxies to the demo apps) and the accuracy benchmark.

App URLs: PATCHWORK_INBOX_URL, PATCHWORK_STOCKROOM_URL, PATCHWORK_STAGING_URL
(defaults http://localhost:8001, :8002, :8003). Upstream status codes are passed through,
so an Inbox without /admin/deliver answers 404 here too. Unreachable apps answer 502.
"""

from __future__ import annotations

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
            "staging": ("PATCHWORK_STAGING_URL", "http://localhost:8003")}


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

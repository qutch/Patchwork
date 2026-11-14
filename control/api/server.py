"""Control-plane API (docs/contracts.md section 4). `python -m control.api --port 38080`.

Wraps control.ops, the runner (background subprocesses), the AgentService and the capture
session. JSON everywhere; errors are {"detail": "..."} with a 4xx status.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from control import ops
from control.api import demo, recordings, sse
from control.api.runs import RunManager, RunStartError
from control.api.settings import Settings, load_dotenv
from runner.store import Store, workspace_dir

CORS_ORIGIN_REGEX = r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"


# ---- request bodies ---------------------------------------------------------------------------

class SettingsUpdate(BaseModel):
    ai_enabled: bool


class BuildRequest(BaseModel):
    recording_id: str


class ResumeRequest(BaseModel):
    interrupt_id: str
    answer: str | None = None
    decision: Literal["approve", "reject"] | None = None
    comment: str | None = None


class VersionBody(BaseModel):
    version: int


class RunRequest(BaseModel):
    integration: str
    dry_run: bool = False
    limit: int | None = None
    version: int | None = None  # default: the active version


# ---- helpers ----------------------------------------------------------------------------------

def agent_service(request: Request):
    state = request.app.state
    if state.agent is None:
        from agents import get_agent_service
        state.agent = get_agent_service()
    return state.agent


def agent_kind(svc: Any | None) -> str:
    if svc is None:
        return os.environ.get("PATCHWORK_AGENT", "scripted")
    return "scripted" if "Scripted" in type(svc).__name__ else "deep"


def require_ai(request: Request) -> None:
    if not request.app.state.settings.ai_enabled:
        raise HTTPException(409, "AI is off. Turn it on in settings to build or repair; runs work either way.")


def require_integration(store: Store, name: str) -> None:
    if not any(i["name"] == name for i in store.integrations()):
        raise HTTPException(404, f"integration {name} not found")


def run_view(run: dict[str, Any], runs: RunManager) -> dict[str, Any]:
    out = {**run, "dry_run": bool(run["dry_run"])}
    live = runs.get(run["id"])
    out["total"] = live.total if live else None
    if out["status"] == "running" and not out.get("counts"):
        out["counts"] = {}
    return out


def run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": run["id"], "integration": run["integration"], "version": run["version"],
            "dry_run": bool(run["dry_run"]), "kind": run.get("kind", "run"), "status": run["status"],
            "counts": run["counts"] or {}, "duration_s": run["duration_s"], "llm_calls": run["llm_calls"],
            "incident_id": run["incident_id"], "message": run["message"]}


def processed(counts: dict[str, int]) -> int:
    return sum(n for k, n in counts.items() if ":" not in k)


def start_run(request: Request, name: str, **kw: Any) -> int:
    try:
        return request.app.state.runs.start(name, **kw)
    except RunStartError as e:
        raise HTTPException(500, f"runner failed to start: {e}") from None
    except RuntimeError as e:  # a run of this integration is already in progress
        raise HTTPException(409, str(e)) from None


# ---- routes -----------------------------------------------------------------------------------

api = APIRouter(prefix="/api")


@api.get("/health")
def health(request: Request) -> dict[str, Any]:
    return {"status": "ok", "workspace": str(workspace_dir()), "agent": agent_kind(request.app.state.agent),
            "ai_enabled": request.app.state.settings.ai_enabled}


@api.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    kind = agent_kind(request.app.state.agent)
    model = "scripted" if kind == "scripted" else os.environ.get("PATCHWORK_MODEL", "")
    return {"ai_enabled": request.app.state.settings.ai_enabled, "agent": kind, "model": model}


@api.put("/settings")
def put_settings(body: SettingsUpdate, request: Request) -> dict[str, Any]:
    request.app.state.settings.update(ai_enabled=body.ai_enabled)
    return get_settings(request)


# -- agent ----------------------------------------------------------------------------------------

@api.post("/integrations/build")
def build(body: BuildRequest, request: Request) -> dict[str, str]:
    require_ai(request)
    recordings.load_recording(body.recording_id)  # 404 unknown, 409 still recording
    return {"thread_id": agent_service(request).start_build(body.recording_id)}


@api.post("/incidents/{incident_id}/repair")
def repair(incident_id: int, request: Request) -> dict[str, str]:
    require_ai(request)
    if Store().incident(incident_id) is None:
        raise HTTPException(404, f"incident {incident_id} not found")
    return {"thread_id": agent_service(request).start_repair(incident_id)}


@api.get("/agent/threads")
def agent_threads(request: Request) -> list[dict[str, Any]]:
    return agent_service(request).list_threads()


def _thread_status(svc, thread_id: str) -> dict[str, Any]:
    try:
        return svc.status(thread_id)
    except KeyError:
        raise HTTPException(404, f"agent thread {thread_id} not found") from None


@api.get("/agent/{thread_id}")
def agent_status(thread_id: str, request: Request) -> dict[str, Any]:
    return _thread_status(agent_service(request), thread_id)


@api.get("/agent/{thread_id}/stream")
async def agent_stream(thread_id: str, request: Request, after: int | None = None):
    """SSE over AgentService.events(after=). Ends once the thread is done or failed."""
    svc = agent_service(request)
    _thread_status(svc, thread_id)
    start = sse.resume_from(request, after)

    async def gen():
        seq, idle = start, 0.0
        while not await request.is_disconnected():
            state = svc.status(thread_id)["state"]
            evs = svc.events(thread_id, after=seq)
            for ev in evs:
                seq = ev["seq"]
                yield sse.sse(ev["type"], ev, ev["seq"])
            if not evs and state in ("done", "failed"):
                await asyncio.sleep(0.1)  # the state flips just before its status event is appended
                for ev in svc.events(thread_id, after=seq):
                    yield sse.sse(ev["type"], ev, ev["seq"])
                return
            if evs:
                idle = 0.0
            else:
                idle += sse.POLL_S
                if idle >= sse.KEEPALIVE_S:
                    idle = 0.0
                    yield sse.keepalive()
                await asyncio.sleep(sse.POLL_S)
    return sse.stream(gen())


@api.post("/agent/{thread_id}/resume")
def agent_resume(thread_id: str, body: ResumeRequest, request: Request) -> dict[str, Any]:
    svc = agent_service(request)
    status = _thread_status(svc, thread_id)
    pending = status.get("pending") or {}
    if pending.get("type") == "request_deploy":
        if body.decision is None:
            raise HTTPException(422, "request_deploy needs decision: approve | reject")
        payload: dict[str, Any] = {"decision": body.decision, "comment": body.comment or ""}
    elif body.answer is not None:
        payload = {"answer": body.answer}
    elif body.decision is not None:
        payload = {"decision": body.decision, "comment": body.comment or ""}
    else:
        raise HTTPException(422, "send answer (ask_user) or decision (request_deploy)")
    try:
        svc.resume(thread_id, body.interrupt_id, payload)
    except KeyError as e:
        raise HTTPException(409, str(e).strip("'\"")) from None
    return _thread_status(svc, thread_id)


# -- integrations ---------------------------------------------------------------------------------

@api.get("/integrations")
def integrations() -> list[dict[str, Any]]:
    return ops.list_integrations()


@api.get("/integrations/{name}")
def integration(name: str) -> dict[str, Any]:
    store = Store()
    require_integration(store, name)
    return ops.status(name, store=store)


@api.get("/integrations/{name}/versions/{version}")
def integration_version(name: str, version: int) -> dict[str, Any]:
    out = ops.version_detail(name, version)
    if out is None:
        raise HTTPException(404, f"{name} v{version} not found")
    return out


@api.get("/integrations/{name}/diff")
def integration_diff(name: str, from_: int | None = Query(None, alias="from"), to: int | None = None) -> dict[str, Any]:
    """Defaults: to = latest version, from = to - 1."""
    store = Store()
    require_integration(store, name)
    to = to or store.latest_version(name)
    from_ = from_ if from_ is not None else to - 1
    try:
        return ops.diff_versions(name, from_, to)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from None


def _move_pointer(fn, name: str, version: int) -> dict[str, Any]:
    store = Store()
    if store.version(name, version) is None:
        raise HTTPException(404, f"{name} v{version} not found")
    try:
        return fn(name, version, store=store)
    except ops.DeployRefused as e:
        raise HTTPException(409, str(e)) from None


@api.post("/integrations/{name}/deploy")
def integration_deploy(name: str, body: VersionBody) -> dict[str, Any]:
    return _move_pointer(ops.deploy, name, body.version)


@api.post("/integrations/{name}/rollback")
def integration_rollback(name: str, body: VersionBody) -> dict[str, Any]:
    return _move_pointer(ops.rollback, name, body.version)


@api.post("/integrations/{name}/replay")
def integration_replay(name: str, request: Request) -> dict[str, int]:
    store = Store()
    require_integration(store, name)
    if store.active_version(name) is None:
        raise HTTPException(409, f"{name} has no active version to replay on")
    return {"run_id": start_run(request, name, replay=True)}


# -- runs -----------------------------------------------------------------------------------------

@api.post("/runs")
def create_run(body: RunRequest, request: Request) -> dict[str, int]:
    """Background run. Never gated by the AI switch."""
    store = Store()
    require_integration(store, body.integration)
    if body.version is None and store.active_version(body.integration) is None:
        raise HTTPException(409, f"{body.integration} has no active version; deploy one first")
    if body.version is not None and store.version(body.integration, body.version) is None:
        raise HTTPException(404, f"{body.integration} v{body.version} not found")
    return {"run_id": start_run(request, body.integration, version=body.version or "active",
                                dry_run=body.dry_run, limit=body.limit)}


@api.get("/runs")
def list_runs(request: Request, integration: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    return [run_view(r, request.app.state.runs) for r in Store().runs(integration, limit=limit)]


@api.get("/runs/{run_id}")
def get_run(run_id: int, request: Request) -> dict[str, Any]:
    store = Store()
    run = store.run(run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    if run["status"] == "running":
        run["counts"] = store.record_counts(run_id)
    incident = store.incident(run["incident_id"]) if run["incident_id"] else None
    return {"run": run_view(run, request.app.state.runs), "records": store.records(run_id), "incident": incident}


@api.get("/runs/{run_id}/stream")
async def run_stream(run_id: int, request: Request):
    """SSE: `progress` {run_id, status, counts, processed, total} whenever it changes, then
    `finished` {summary}. Snapshots, so reconnecting just resends the current state;
    ids are the processed count."""
    if Store().run(run_id) is None:
        raise HTTPException(404, f"run {run_id} not found")
    runs: RunManager = request.app.state.runs

    async def gen():
        store = Store()
        last = None
        idle = 0.0
        while not await request.is_disconnected():
            run = store.run(run_id)
            if run is None:  # workspace reset under us
                return
            live = runs.get(run_id)
            counts = store.record_counts(run_id) if run["status"] == "running" else (run["counts"] or {})
            progress = {"run_id": run_id, "status": run["status"], "counts": counts,
                        "processed": processed(counts), "total": live.total if live else None}
            if progress != last:
                last, idle = progress, 0.0
                yield sse.sse("progress", progress, progress["processed"])
            if run["status"] != "running":
                yield sse.sse("finished", run_summary(run), progress["processed"] + 1)
                return
            idle += sse.POLL_S
            if idle >= sse.KEEPALIVE_S:
                idle = 0.0
                yield sse.keepalive()
            await asyncio.sleep(sse.POLL_S)
    return sse.stream(gen())


# -- incidents ------------------------------------------------------------------------------------

@api.get("/incidents")
def list_incidents(status: str | None = None, integration: str | None = None) -> list[dict[str, Any]]:
    """`status` = open | resolved; omitted means all."""
    return Store().incidents(integration, status=status)


@api.get("/incidents/{incident_id}")
def get_incident(incident_id: int) -> dict[str, Any]:
    inc = Store().incident(incident_id)
    if inc is None:
        raise HTTPException(404, f"incident {incident_id} not found")
    return inc


# ---- app ----------------------------------------------------------------------------------------

def create_app(*, agent: Any | None = None, dotenv: bool = True) -> FastAPI:
    """`agent`: an AgentService to use (tests pass a ScriptedAgentService(pace_s=0)); default is
    `agents.get_agent_service()`, resolved on first use."""
    if dotenv:
        load_dotenv()
    runs = RunManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        runs.stop_all()

    app = FastAPI(title="Patchwork control plane", version="0.2.0", lifespan=lifespan)
    app.state.agent = agent
    app.state.settings = Settings()
    app.state.runs = runs
    app.add_middleware(CORSMiddleware, allow_origin_regex=CORS_ORIGIN_REGEX, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.include_router(api)
    app.include_router(recordings.router)
    app.include_router(demo.router)
    return app

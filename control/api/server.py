"""Control-plane API (docs/contracts.md section 4). `python -m control.api --port 38080`.

Wraps control.ops, the runner (background subprocesses), the AgentService and the capture
session. JSON everywhere; errors are {"detail": "..."} with a 4xx status.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, Union

from fastapi.concurrency import run_in_threadpool
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

from control import drafts, ops
from control.api import credentials, demo, drafts as drafts_api, pages, recordings, sse
from control.api.runs import CredentialMissing, RunManager, RunStartError
from control.api.settings import Settings, load_dotenv
from control.autorun import AutoRunner, Busy
from control.page_hooks import PageHooks
from control.pages import PageInbox
from runner.store import Store, workspace_dir

CORS_ORIGIN_REGEX = r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"
EXTENSION_ORIGIN_REGEX = r"chrome-extension://[a-z0-9]+"


# ---- request bodies ---------------------------------------------------------------------------

class SettingsUpdate(BaseModel):
    ai_enabled: bool | None = None
    learn_policy: Literal["auto", "approve"] | None = None


class AirtableDestination(BaseModel):
    """V14: teach a page→form recording into an Airtable table instead of the recorded form. The base may be
    given as its URL (`base_url`): the id in it becomes `base_id`."""
    kind: Literal["airtable"]
    base_id: str | None = None
    base_url: str | None = None
    table: str
    credential: str = "AIRTABLE_TOKEN"

    @model_validator(mode="after")
    def _base(self):
        from control.destinations import base_id_from_url
        if not self.base_id and self.base_url:
            self.base_id = base_id_from_url(self.base_url) or self.base_url
        if not self.base_id:
            raise ValueError("base_id (or base_url) is required")
        self.base_url = None
        return self


class FormDestination(BaseModel):
    kind: Literal["form"]
    url: str


class ApiOperation(BaseModel):
    method: Literal["POST", "PUT", "PATCH", "post", "put", "patch"]
    path: str


class ApiDestination(BaseModel):
    kind: Literal["api"]
    base_url: str
    operation: ApiOperation
    credential: str | None = None


class SqliteDestination(BaseModel):
    kind: Literal["sqlite"]
    path: str
    table: str


Destination = Annotated[Union[AirtableDestination, FormDestination, ApiDestination, SqliteDestination],
                        Field(discriminator="kind")]


class BuildRequest(BaseModel):
    recording_id: str
    name: str | None = None  # default: "jobs_to_tracker" for page→form recordings, else the agent's choice
    form_url: str | None = None  # page→form only: teach against this form instead of the one recorded (V9)
    destination: Destination | None = None  # page→form only: write to this destination instead (V14)


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

def resolve_agent(state):
    """The app's AgentService, created on first use and subscribed to by the page hooks."""
    if state.agent is None:
        from agents import get_agent_service
        state.agent = get_agent_service()
    state.hooks.attach(state.agent)
    return state.agent


def agent_service(request: Request):
    return resolve_agent(request.app.state)


def agent_kind(svc: Any | None) -> str:
    if svc is None:
        return os.environ.get("PATCHWORK_AGENT", "scripted")
    return "scripted" if "Scripted" in type(svc).__name__ else "deep"


PAGE_INTEGRATION = "jobs_to_tracker"


def page_build_name(recording_id: str, recording: dict[str, Any] | None = None) -> str:
    """Default name of a page→form BUILD (V15 item 6): `fixture-jobs` → jobs_to_tracker; any other recording →
    a slug of its intent (`Track product prices` → `track_product_prices`), jobs_to_tracker if it has none."""
    if recording_id == recordings.FIXTURE_JOBS_ID:
        return PAGE_INTEGRATION
    from agents.deep_tools import intent_slug
    cands = None
    try:
        cands = recordings._candidates(recording_id)
    except (OSError, ValueError):
        pass
    intent = ((cands or {}).get("intent") if isinstance(cands, dict) else None) or (recording or {}).get("intent") or ""
    return intent_slug(str(intent)) or PAGE_INTEGRATION


def _accepts_name(fn) -> bool:
    """V10 `start_build(recording_id, name=None)`; agents from before W3 take only the recording id."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "name" in params or any(p.kind is p.VAR_POSITIONAL for p in params.values()) or len(params) >= 2


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
    except CredentialMissing as e:  # V13: names the credential that is not set, never a value
        raise HTTPException(409, str(e)) from None
    except RunStartError as e:
        raise HTTPException(500, f"runner failed to start: {e}") from None
    except RuntimeError as e:  # a run of this integration is already in progress
        raise HTTPException(409, str(e)) from None


# ---- routes -----------------------------------------------------------------------------------

api = APIRouter(prefix="/api")


@api.get("/health")
def health(request: Request) -> dict[str, Any]:
    return {"status": "ok", "workspace": str(workspace_dir()), "agent": agent_kind(request.app.state.agent),
            "ai_enabled": request.app.state.settings.ai_enabled,
            "recovered_runs": getattr(request.app.state, "recovered_runs", {"failed": [], "adopted": []})}


@api.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    kind = agent_kind(request.app.state.agent)
    model = "scripted" if kind == "scripted" else os.environ.get("PATCHWORK_MODEL", "")
    settings = request.app.state.settings
    return {"ai_enabled": settings.ai_enabled, "agent": kind, "model": model, "learn_policy": settings.learn_policy}


@api.put("/settings")
def put_settings(body: SettingsUpdate, request: Request) -> dict[str, Any]:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(422, "send ai_enabled and/or learn_policy")
    request.app.state.settings.update(**changes)
    return get_settings(request)


# -- agent ----------------------------------------------------------------------------------------

def retarget_recording_form(recording_id: str, form_url: str) -> None:
    """Point a page→form recording's candidates at another form (same questions, different address), e.g. the
    fixture-jobs recording at the demo Google Form, or at a fixture form on a free port. Writes
    $WS/recordings/<id>/candidates.json, which the agents read before the shipped fixture (contracts V9)."""
    if not re.match(r"^https?://", form_url):
        raise HTTPException(422, "form_url must be an http(s) URL")
    if not recordings.is_page_form(recording_id):
        raise HTTPException(422, f"recording {recording_id} is not a page→form recording; form_url does not apply")
    cands = recordings._candidates(recording_id)
    if not isinstance(cands, dict) or not isinstance(cands.get("sink"), dict):
        raise HTTPException(409, f"recording {recording_id} has no candidates yet")
    if cands["sink"].get("url") != form_url:
        cands["sink"].setdefault("retargeted_from", cands["sink"].get("url"))
        cands["sink"]["url"] = form_url
    out = recordings.recording_dir(recording_id) / "candidates.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cands, indent=2))


def retarget_recording_destination(recording_id: str, dest: Any | None) -> None:
    """V14: record (or clear) the destination a page→form recording is taught into, as `destination` in
    $WS/recordings/<id>/candidates.json (the agents read it there). The recorded form (`sink.url`) stays as
    is; matching labels to the table's fields is the TEACH agent's job (it asks about every label without
    a field of the same name, never guesses). `dest` is one of the `Destination` kinds."""
    from spec.models import AIRTABLE_BASE_ID
    from runner.credentials import check_name
    if dest is not None:
        if not recordings.is_page_form(recording_id):
            raise HTTPException(422, f"recording {recording_id} is not a page→form recording; destination does not apply")
        if dest.kind == "airtable":
            if not AIRTABLE_BASE_ID.match(dest.base_id):
                raise HTTPException(422, "destination.base_id must look like appXXXXXXXXXXXXXX (from the base URL)")
            if not dest.table.strip():
                raise HTTPException(422, "destination.table must be a table name or id")
            try:
                check_name(dest.credential)
            except ValueError as e:
                raise HTTPException(422, f"destination.credential: {e}") from None
        else:
            from control import destinations
            try:
                destinations.normalize(dest.model_dump(exclude_none=True))
            except destinations.DestinationError as e:
                raise HTTPException(422, f"destination: {e}") from None
    cands = recordings._candidates(recording_id)
    if not isinstance(cands, dict) or not isinstance(cands.get("sink"), dict):
        if dest is None:
            return
        raise HTTPException(409, f"recording {recording_id} has no candidates yet")
    new = dest.model_dump(exclude_none=True) if dest is not None else None
    if cands.get("destination") == new:
        return
    if new is None:
        cands.pop("destination", None)
    else:
        cands["destination"] = new
    out = recordings.recording_dir(recording_id) / "candidates.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cands, indent=2))


@api.post("/integrations/build")
def build(body: BuildRequest, request: Request) -> dict[str, str]:
    require_ai(request)
    rec = recordings.load_recording(body.recording_id)  # 404 unknown, 409 still recording
    name = body.name or (page_build_name(body.recording_id, rec) if recordings.is_page_form(body.recording_id) else None)
    if body.form_url is not None:
        retarget_recording_form(body.recording_id, body.form_url)
    if body.destination is not None or recordings.is_page_form(body.recording_id):
        retarget_recording_destination(body.recording_id, body.destination)  # a build without one clears it
    svc = agent_service(request)
    if name is not None and _accepts_name(svc.start_build):
        return {"thread_id": svc.start_build(body.recording_id, name)}
    return {"thread_id": svc.start_build(body.recording_id)}


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
    return drafts.annotate(ops.list_integrations())


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

class ControlURLMiddleware:
    """Learns the control plane's own base URL from the socket it serves on (V7: the runner resolves
    `patchwork://control` through PATCHWORK_CONTROL_URL). An explicit PATCHWORK_CONTROL_URL wins."""

    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and getattr(self.state, "control_url", None) is None:
            server = scope.get("server")
            if server and server[1]:
                host = server[0]
                host = f"[{host}]" if ":" in host else host
                self.state.control_url = f"http://{host}:{server[1]}"
                ops.CONTROL_URL = os.environ.get("PATCHWORK_CONTROL_URL") or self.state.control_url
        await self.app(scope, receive, send)


def _extension_route(path: str, method: str) -> bool:
    """POST /api/pages (send a page) and GET /api/pages/{id} (poll its outcome); not the html-bearing list."""
    if path == "/api/pages":
        return method == "POST"
    return path.startswith("/api/pages/") and path.count("/") == 3 and method == "GET"


class ExtensionCORSMiddleware:
    """CORS for chrome-extension:// origins on the extension's routes only (POST /api/pages and
    GET /api/pages/{id}); everything else keeps the localhost-only policy."""

    def __init__(self, app):
        self.app = app
        self.cors = CORSMiddleware(app, allow_origin_regex=EXTENSION_ORIGIN_REGEX, allow_credentials=False,
                                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Patchwork-Token"])

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            method = scope.get("method", "")
            if method == "OPTIONS":
                method = headers.get(b"access-control-request-method", b"").decode("latin-1").upper()
            if headers.get(b"origin", b"").startswith(b"chrome-extension://") and _extension_route(
                    scope.get("path", ""), method):
                await self.cors(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(*, agent: Any | None = None, dotenv: bool = True) -> FastAPI:
    """`agent`: an AgentService to use (tests pass a ScriptedAgentService(pace_s=0)); default is
    `agents.get_agent_service()`, resolved on first use."""
    if dotenv:
        load_dotenv()
    runs = RunManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Runs a previous control plane left "running": fail the dead ones, adopt live runners.
        app.state.recovered_runs = await run_in_threadpool(runs.recover)
        yield
        runs.stop_all()

    app = FastAPI(title="Patchwork control plane", version="0.2.0", lifespan=lifespan)
    app.state.agent = agent
    app.state.settings = Settings()
    app.state.runs = runs
    app.state.control_url = None
    # v2: page inbox, run on arrival (one run + one queued follow-up per integration), learn trigger
    app.state.inbox = PageInbox()

    def auto_start(name: str, kind: str) -> int:
        try:
            return runs.start(name, replay=kind == "replay")
        except RunStartError:
            raise
        except RuntimeError as e:  # a run of this integration is already in progress
            raise Busy(str(e)) from None

    app.state.auto = AutoRunner(auto_start, runs.wait)
    app.state.hooks = PageHooks(app.state.inbox, app.state.auto, lambda: app.state.settings.ai_enabled,
                                lambda: resolve_agent(app.state))
    runs.on_finish(app.state.hooks.on_run_finished)
    if agent is not None:
        app.state.hooks.attach(agent)
    app.add_middleware(CORSMiddleware, allow_origin_regex=CORS_ORIGIN_REGEX, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(ExtensionCORSMiddleware)  # outermost: answers extension preflights itself
    app.add_middleware(ControlURLMiddleware, state=app.state)
    app.add_middleware(credentials.RedactErrorsMiddleware)  # V13: error bodies never show a credential value
    app.include_router(api)
    app.include_router(recordings.router)
    app.include_router(demo.router)
    app.include_router(pages.router)
    app.include_router(credentials.router)
    app.include_router(drafts_api.router)
    return app

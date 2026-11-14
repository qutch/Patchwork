"""Page inbox endpoints (docs/contracts.md V9, V15): the extension posts pages here, the page
integrations read them back as their source, and the UI reads the tracker and the learned layouts.

A page goes to one integration (V15): the one the extension sent (`integration`), else the default page
integration (the first one, `jobs_to_tracker` when present). `GET /api/pages?integration=X` is X's source:
X's pages, plus untagged pages when X is the default; without `integration` it is the default's (so specs
compiled before V15 see exactly what they saw). Runs, learns and tracker rows follow the page's integration.

`POST /api/pages` needs the extension token (X-Patchwork-Token), takes at most 5 MB of HTML, and is
the only route (with `GET /api/pages/{id}`, which the extension polls) open to chrome-extension://
origins. Page HTML is never logged or echoed in errors.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from control import autorun, drafts, layouts, ops, tracker
from control.api.settings import extension_token
from runner.store import Store

router = APIRouter(prefix="/api", tags=["pages"])

MAX_HTML_BYTES = 5 * 1024 * 1024
# JSON escaping can grow a body (quotes, newlines); anything beyond this is too large whatever it holds.
MAX_BODY_BYTES = 3 * MAX_HTML_BYTES
DEFAULT_INTEGRATION = "jobs_to_tracker"


def current_agent(state) -> Any:
    """The agent, if there is anything to ask it (learn threads exist); never forces one up otherwise."""
    if state.agent is not None:
        return state.agent
    return state.hooks.get_agent() if state.inbox.learns() else None


def control_url(request: Request) -> str:
    return ops.CONTROL_URL or str(request.base_url).rstrip("/")


async def _read_capped(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(413, f"page too large: the limit is {MAX_HTML_BYTES // (1024 * 1024)} MB of HTML")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(413, f"page too large: the limit is {MAX_HTML_BYTES // (1024 * 1024)} MB of HTML")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_page(raw: bytes) -> tuple[str, str, str, str | None]:
    """422 messages name the problem, never the values (no page HTML in errors or logs)."""
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "body must be JSON: {url, title, html, integration?}") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "body must be a JSON object: {url, title, html, integration?}")
    url, title, html = body.get("url"), body.get("title", ""), body.get("html")
    integration = body.get("integration")
    if integration is not None and not isinstance(integration, str):
        raise HTTPException(422, "integration must be a string (an integration name) or omitted")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(422, "url is required (a string)")
    if not isinstance(html, str):
        raise HTTPException(422, "html is required (a string)")
    if title is None:
        title = ""
    if not isinstance(title, str):
        raise HTTPException(422, "title must be a string")
    if len(html.encode("utf-8", "surrogatepass")) > MAX_HTML_BYTES:
        raise HTTPException(413, f"page too large: the limit is {MAX_HTML_BYTES // (1024 * 1024)} MB of HTML")
    return url.strip(), title, html, (integration or "").strip() or None


def _check_token(request: Request) -> None:
    sent = request.headers.get("x-patchwork-token") or ""
    if not sent or not secrets.compare_digest(sent.encode(), extension_token().encode()):
        raise HTTPException(401, "missing or wrong X-Patchwork-Token (copy it from the UI's extension setup)")


@router.post("/pages", status_code=201)
async def post_page(request: Request) -> JSONResponse:
    _check_token(request)
    url, title, html, integration = _parse_page(await _read_capped(request))
    state = request.app.state
    names = await run_in_threadpool(autorun.page_integrations)
    waiting = await run_in_threadpool(drafts.waiting_names)  # wizard drafts: pages wait, nothing runs
    if integration is not None and integration not in names and integration not in waiting:
        raise HTTPException(422, f"integration {integration!r} is not a page integration"
                                 f" (known: {', '.join(names + waiting) or 'none yet'})")
    page = await run_in_threadpool(state.inbox.add, url, title, html, integration)
    out: dict[str, Any] = {"id": page["id"], "seq": page["seq"], "site": page["site"],
                           "canonical_url": page["canonical_url"], "integration": None, "run_id": None,
                           "queued": False}
    name = integration or (names[0] if names else None)  # V15: only the page's integration runs
    if name is not None and name in waiting:
        out.update(integration=name)
    elif name is not None:
        try:
            started = await run_in_threadpool(state.auto.request, name)
        except Exception as e:  # noqa: BLE001 - the page is stored either way; the next press retries
            started = {"run_id": None, "queued": False, "run_error": f"runner failed to start: {e}"[:500]}
        out.update(integration=name, **started)
    return JSONResponse(out, status_code=201)


def _since(since: str | None) -> int:
    if since in (None, "", "None", "null"):
        return 0
    try:
        return max(0, int(since))
    except ValueError:
        raise HTTPException(422, "since must be a page seq (integer)") from None


@router.get("/pages")
def list_pages(request: Request, since: str | None = None, limit: int = Query(100, ge=1, le=1000),
               integration: str | None = None) -> list[dict]:
    """A page integration's source (V7, V15): `integration`'s pages (+ untagged ones for the default
    integration); without `integration`, the default integration's. No token: localhost only."""
    return request.app.state.inbox.list(since=_since(since), limit=limit,
                                        **autorun.page_filter((integration or "").strip() or None))


@router.get("/pages/{page_id}")
def get_page(page_id: str, request: Request, integration: str | None = None) -> dict[str, Any]:
    state = request.app.state
    page = state.inbox.get(page_id)
    if page is None:
        raise HTTPException(404, f"page {page_id} not found")
    name = integration or autorun.integration_of_page(page) or DEFAULT_INTEGRATION
    row = tracker.page_row(Store(), state.inbox, current_agent(state), name, page_id)
    return {"page": {k: v for k, v in page.items() if k != "html"}, "row": row}


@router.get("/extension")
def extension(request: Request) -> dict[str, Any]:
    """Setup values for the extension (V9) and its integration dropdown (V15): the page integrations
    `[{name, intent}]` (default first) and `default_integration` (null until there is one)."""
    store = Store()
    names = autorun.page_integrations(store)
    waiting = [d for d in drafts.list_all() if d.state != "live" and d.name not in names]
    return {"token": extension_token(), "control_url": control_url(request),
            "integrations": [{"name": n, "intent": autorun.integration_intent(n, store)} for n in names]
            + [{"name": d.name, "intent": " ".join(d.intent.split())} for d in waiting],
            "default_integration": names[0] if names else None}


# ---- tracker and learned layouts ----------------------------------------------------------------

class RevertBody(BaseModel):
    integration: str = DEFAULT_INTEGRATION


@router.get("/tracker")
def get_tracker(request: Request, integration: str = DEFAULT_INTEGRATION,
                limit: int = Query(50, ge=1, le=1000)) -> dict[str, Any]:
    state = request.app.state
    return tracker.tracker_rows(Store(), state.inbox, current_agent(state), integration, limit)


@router.get("/layouts")
def get_layouts(integration: str = DEFAULT_INTEGRATION) -> list[dict[str, Any]]:
    return tracker.layouts(Store(), integration)


@router.post("/layouts/{parser_id}/revert")
def revert_layout(parser_id: str, body: RevertBody | None = None, integration: str | None = None) -> dict[str, Any]:
    """Compile the active spec minus that parser, test, deploy (no AI). 409 if tests fail or it is the last parser.
    The integration comes from `?integration=` (V15), else the body's, else jobs_to_tracker."""
    name = (integration or "").strip() or (body or RevertBody()).integration
    try:
        return layouts.revert_parser(name, parser_id)
    except layouts.LayoutError as e:
        raise HTTPException(e.status, str(e)) from None

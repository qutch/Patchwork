"""Page inbox endpoints (docs/contracts.md V9): the extension posts pages here, the jobs_to_tracker
integration reads them back as its source, and the UI reads the tracker and the learned layouts.

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

from control import autorun, layouts, ops, tracker
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


def _parse_page(raw: bytes) -> tuple[str, str, str]:
    """422 messages name the problem, never the values (no page HTML in errors or logs)."""
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "body must be JSON: {url, title, html}") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "body must be a JSON object: {url, title, html}")
    url, title, html = body.get("url"), body.get("title", ""), body.get("html")
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
    return url.strip(), title, html


def _check_token(request: Request) -> None:
    sent = request.headers.get("x-patchwork-token") or ""
    if not sent or not secrets.compare_digest(sent.encode(), extension_token().encode()):
        raise HTTPException(401, "missing or wrong X-Patchwork-Token (copy it from the UI's extension setup)")


@router.post("/pages", status_code=201)
async def post_page(request: Request) -> JSONResponse:
    _check_token(request)
    url, title, html = _parse_page(await _read_capped(request))
    state = request.app.state
    page = await run_in_threadpool(state.inbox.add, url, title, html)
    out: dict[str, Any] = {"id": page["id"], "seq": page["seq"], "site": page["site"],
                           "canonical_url": page["canonical_url"], "integration": None, "run_id": None,
                           "queued": False}
    names = await run_in_threadpool(autorun.page_integrations)
    for i, name in enumerate(names):
        try:
            started = await run_in_threadpool(state.auto.request, name)
        except Exception as e:  # noqa: BLE001 - the page is stored either way; the next press retries
            started = {"run_id": None, "queued": False, "run_error": f"runner failed to start: {e}"[:500]}
        if i == 0:
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
def list_pages(request: Request, since: str | None = None, limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
    """The jobs_to_tracker source (V7). No token: localhost only, and the runner has none."""
    return request.app.state.inbox.list(since=_since(since), limit=limit)


@router.get("/pages/{page_id}")
def get_page(page_id: str, request: Request, integration: str | None = None) -> dict[str, Any]:
    state = request.app.state
    page = state.inbox.get(page_id)
    if page is None:
        raise HTTPException(404, f"page {page_id} not found")
    name = integration or (autorun.page_integrations() or [DEFAULT_INTEGRATION])[0]
    row = tracker.page_row(Store(), state.inbox, current_agent(state), name, page_id)
    return {"page": {k: v for k, v in page.items() if k != "html"}, "row": row}


@router.get("/extension")
def extension(request: Request) -> dict[str, str]:
    return {"token": extension_token(), "control_url": control_url(request)}


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
def revert_layout(parser_id: str, body: RevertBody) -> dict[str, Any]:
    """Compile the active spec minus that parser, test, deploy (no AI). 409 if tests fail or it is the last parser."""
    try:
        return layouts.revert_parser(body.integration, parser_id)
    except layouts.LayoutError as e:
        raise HTTPException(e.status, str(e)) from None

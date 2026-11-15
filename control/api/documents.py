"""Document inbox endpoints (docs/contracts.md V17): a person (or a connector) uploads a file or an email, the
document integrations read the leaves back as their source, and the UI follows arrivals over SSE.

`POST /api/documents` is multipart (`file`, `integration`) and needs the same token as `POST /api/pages`. The
multipart body is parsed with the standard library, so the control plane needs no extra dependency. File bytes
and document text are never logged or echoed in errors. The same bytes for one integration are one document.
"""

from __future__ import annotations

import asyncio
from email import policy
from email.parser import BytesParser
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from control import autorun, drafts
from control.api import sse
from control.api.pages import _check_token
from control.documents import DocumentsUnavailable, doc_id

router = APIRouter(prefix="/api", tags=["documents"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024  # multipart framing


async def _read_capped(request: Request) -> bytes:
    too_big = HTTPException(413, f"file too large: the limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise too_big
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise too_big
        chunks.append(chunk)
    return b"".join(chunks)


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], tuple[str, str, bytes] | None]:
    """-> (text fields, (filename, content type, bytes) of the `file` part, or None). 422 names the problem only."""
    if not content_type.lower().startswith("multipart/form-data") or "boundary=" not in content_type.lower():
        raise HTTPException(422, "body must be multipart/form-data with a `file` part and an `integration` field")
    msg = BytesParser(policy=policy.HTTP).parsebytes(b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    if not msg.is_multipart():
        raise HTTPException(422, "body must be multipart/form-data with a `file` part and an `integration` field")
    fields: dict[str, str] = {}
    file: tuple[str, str, bytes] | None = None
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        payload = part.get_payload(decode=True) or b""
        if name == "file":
            file = (part.get_filename() or "upload", part.get_content_type() or "", payload)
        else:
            fields[name] = payload.decode("utf-8", "replace").strip()
    return fields, file


@router.post("/documents", status_code=201)
async def post_document(request: Request) -> JSONResponse:
    _check_token(request)
    fields, file = parse_multipart(request.headers.get("content-type", ""), await _read_capped(request))
    if file is None:
        raise HTTPException(422, "a `file` part is required")
    integration = fields.get("integration", "").strip()
    names = await run_in_threadpool(autorun.document_integrations)
    waiting = await run_in_threadpool(drafts.waiting_names)  # wizard drafts: documents wait, nothing runs
    if not integration:
        raise HTTPException(422, "integration is required (the name of a document integration)")
    if integration not in names and integration not in waiting:
        raise HTTPException(422, f"integration {integration!r} is not a document integration"
                                 f" (known: {', '.join(names + waiting) or 'none yet'})")
    filename, content_type, data = file
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file too large: the limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    state = request.app.state
    try:
        parent = await run_in_threadpool(state.documents.add_bytes, data, filename, content_type, integration)
    except DocumentsUnavailable as e:
        raise HTTPException(503, str(e)) from None
    out: dict[str, Any] = {k: parent[k] for k in ("id", "kind", "name", "site", "flags", "meta", "sha256",
                                                    "integration", "leaves", "duplicate")}
    out.update(run_id=None, queued=False)
    if parent["duplicate"]:
        return JSONResponse(out, status_code=200)
    if integration not in waiting:
        try:
            out.update(await run_in_threadpool(state.auto.request, integration))
        except Exception as e:  # noqa: BLE001 - the document is stored either way; the next upload retries
            out.update(run_error=f"runner failed to start: {e}"[:500])
    return JSONResponse(out, status_code=201)


def _since(since: str | None) -> int:
    if since in (None, "", "None", "null"):
        return 0
    try:
        return max(0, int(since))
    except ValueError:
        raise HTTPException(422, "since must be a document seq (integer)") from None


@router.get("/documents")
def list_documents(request: Request, since: str | None = None, limit: int = Query(100, ge=1, le=1000),
                   integration: str | None = None, leaves_only: bool = True) -> list[dict]:
    """A document integration's source: its leaves after `since` (V17). No token: localhost only."""
    return request.app.state.documents.list(since=_since(since), limit=limit,
                                            integration=(integration or "").strip() or None,
                                            leaves_only=leaves_only)


@router.get("/documents/stream")
async def stream_documents(request: Request, after: int | None = None, integration: str | None = None):
    """SSE `document` events (a summary per new leaf, no body), resumable with `?after=` / Last-Event-ID."""
    inbox = request.app.state.documents
    start = sse.resume_from(request, after)

    async def gen():
        seq, idle = start, 0.0
        while not await request.is_disconnected():
            rows = await run_in_threadpool(inbox.list, seq, 100, (integration or "").strip() or None, True)
            for r in rows:
                seq = r["seq"]
                idle = 0.0
                yield sse.sse("document", {k: v for k, v in r.items() if k not in ("text", "html", "tables")}, seq)
            if not rows:
                idle += sse.POLL_S
                if idle >= sse.KEEPALIVE_S:
                    idle = 0.0
                    yield sse.keepalive()
                await asyncio.sleep(sse.POLL_S)

    return sse.stream(gen())


@router.get("/documents/{document_id}")
def get_document(document_id: str, request: Request) -> dict[str, Any]:
    rec = request.app.state.documents.get(document_id)
    if rec is None:
        raise HTTPException(404, f"document {document_id} not found")
    return rec


__all__ = ["router", "doc_id", "parse_multipart"]

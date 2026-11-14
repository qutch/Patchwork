"""Server-sent events helpers: `id: <seq>` + `event: <type>` + `data: <json>`."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import Request
from fastapi.responses import StreamingResponse

POLL_S = 0.2
KEEPALIVE_S = 15.0


def sse(event: str, data: Any, id: int | str | None = None) -> str:
    head = f"id: {id}\n" if id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def keepalive() -> str:
    return ": keepalive\n\n"


def resume_from(request: Request, after: int | None) -> int:
    """`?after=N` or the `Last-Event-ID` header (whichever is larger); 0 = from the start."""
    seq = after or 0
    last = request.headers.get("last-event-id")
    if last:
        try:
            seq = max(seq, int(last))
        except ValueError:
            pass
    return seq


def stream(gen: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

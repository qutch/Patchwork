"""`GET /api/credentials` and the redaction of API error responses (contracts V13).

Credentials are named by specs and valued in `.env`; this API only ever says which names are used, by
which integrations, and whether each is set. `RedactErrorsMiddleware` passes every error response body
(status >= 400) through `control.credentials.redact`, so an error that quotes a destination's reply (which
may echo the token back) never shows a value.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from control.credentials import list_credentials, redact

router = APIRouter(prefix="/api")


@router.get("/credentials")
def get_credentials() -> list[dict[str, Any]]:
    """[{name, set, used_by: [integration, ...]}] for every credential a spec in the workspace references."""
    return list_credentials()


class RedactErrorsMiddleware:
    """ASGI middleware: buffers responses with status >= 400 and redacts credential values from the body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start: dict[str, Any] | None = None
        chunks: list[bytes] = []

        async def wrapped(message):
            nonlocal start
            if message["type"] == "http.response.start":
                if message["status"] < 400:
                    await send(message)
                    return
                start = message
                return
            if message["type"] == "http.response.body" and start is not None:
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                raw = b"".join(chunks)
                try:
                    body = redact(raw.decode("utf-8")).encode("utf-8")
                except UnicodeDecodeError:
                    body = raw
                headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            await send(message)

        await self.app(scope, receive, wrapped)

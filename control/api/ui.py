"""The built UI (`web/dist`) on the control plane's own port, so `./patchwork` needs one port only
(docs/superpowers/specs/2026-11-14-one-command-launcher-design.md).

`mount_ui(app)` does nothing when `web/dist/index.html` does not exist, so the API is exactly as before. When
it exists, GET/HEAD requests that no API route matches are answered from `web/dist`: a file when one exists
at that path, else `index.html` (the UI's own routes, e.g. `/integrations/foo`). `/api/...` is never touched:
an unknown API path still gets FastAPI's 404, and a wrong method on a known one still gets 405, because the
UI is the router's last resort (`router.default`), after every route and partial match.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse

from control.api.settings import REPO_ROOT

DEFAULT_DIST = REPO_ROOT / "web" / "dist"


def ui_dist() -> Path:
    """`PATCHWORK_WEB_DIST` (tests, or a UI built elsewhere), else `web/dist` in the repo."""
    return Path(os.environ.get("PATCHWORK_WEB_DIST") or DEFAULT_DIST)


def mount_ui(app: FastAPI, dist: Path | None = None) -> bool:
    """Serve the built UI from `dist` (default `ui_dist()`) as the app's fallback. Returns whether it did."""
    root = (dist or ui_dist()).resolve()
    index = root / "index.html"
    if not index.is_file():
        return False
    not_found = app.router.default

    async def serve_ui(scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or scope.get("method") not in ("GET", "HEAD") \
                or path == "/api" or path.startswith("/api/"):
            await not_found(scope, receive, send)
            return
        rel = path.lstrip("/")
        try:
            target = (root / rel).resolve() if rel else index
            servable = target.is_relative_to(root) and target.is_file()
        except (ValueError, OSError):  # e.g. a NUL byte in the path: no such file
            await not_found(scope, receive, send)
            return
        if servable:
            headers = {"Cache-Control": "no-cache"} if target == index else None
            await FileResponse(target, headers=headers)(scope, receive, send)
            return
        if rel.startswith("assets/"):  # a missing script or stylesheet is a 404, never the page
            await not_found(scope, receive, send)
            return
        await FileResponse(index, headers={"Cache-Control": "no-cache"})(scope, receive, send)

    app.router.default = serve_ui
    return True

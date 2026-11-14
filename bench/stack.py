"""In-process demo stack for the benchmark: Inbox, Stockroom live + staging and (optionally) the
control plane with the scripted agent, each a uvicorn server in a thread on a free port.

Never uses the live-demo ports (8000-8003, 5180) and points PATCHWORK_WORKSPACE at its own dir.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

RESERVED_PORTS = {5180, 5181, 8000, 8001, 8002, 8003}


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in RESERVED_PORTS:
            return port


class Server:
    def __init__(self, app: Any):
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "Server":
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                httpx.get(self.url + "/openapi.json", timeout=0.5)
                return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


class Stack:
    """`with Stack(workspace, control=True) as st:` -> st.inbox, st.live, st.staging, st.control URLs.

    Sets the env vars the control plane and runner read (process-wide, restored on exit)."""

    def __init__(self, workspace: Path, control: bool = True):
        self.workspace = Path(workspace)
        self.control_enabled = control
        self._servers: list[Server] = []
        self._old_env: dict[str, str | None] = {}
        self.inbox = self.live = self.staging = self.control = ""

    def _setenv(self, **kw: str) -> None:
        for k, v in kw.items():
            self._old_env.setdefault(k, os.environ.get(k))
            os.environ[k] = v

    def _start(self, app: Any) -> str:
        srv = Server(app).__enter__()
        self._servers.append(srv)
        return srv.url

    def __enter__(self) -> "Stack":
        from demo_apps.inbox.app import create_app as inbox_app
        from demo_apps.stockroom.app import create_app as stockroom_app

        self.workspace.mkdir(parents=True, exist_ok=True)
        self._setenv(PATCHWORK_WORKSPACE=str(self.workspace), PATCHWORK_NO_DOTENV="1",
                     PATCHWORK_RUN_PACE_MS="0", PATCHWORK_AI_ENABLED="1", PATCHWORK_AGENT="scripted")
        self.inbox = self._start(inbox_app())
        self.live = self._start(stockroom_app())
        self.staging = self._start(stockroom_app())
        self._setenv(PATCHWORK_SOURCE_BASE_URL=self.inbox, PATCHWORK_SINK_BASE_URL=self.live,
                     PATCHWORK_STAGING_BASE_URL=self.staging, PATCHWORK_INBOX_URL=self.inbox,
                     PATCHWORK_STOCKROOM_URL=self.live, PATCHWORK_STAGING_URL=self.staging)
        if self.control_enabled:
            from agents.scripted import ScriptedAgentService
            from control.api import create_app

            self.agent = ScriptedAgentService(pace_s=0)
            self.control = self._start(create_app(agent=self.agent, dotenv=False))
        return self

    def __exit__(self, *exc: Any) -> None:
        for srv in reversed(self._servers):
            srv.__exit__(*exc)
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

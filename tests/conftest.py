from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from demo_apps.inbox.app import create_app as inbox_app
from demo_apps.stockroom.app import create_app as stockroom_app

REPO = Path(__file__).resolve().parent.parent
FIXTURE_SPEC = REPO / "fixtures" / "spec.yaml"
FIXTURE_EXAMPLES = REPO / "fixtures" / "examples.yaml"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, app):
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                httpx.get(self.url + "/openapi.json", timeout=0.5)
                return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCHWORK_WORKSPACE", str(tmp_path))
    return tmp_path


@pytest.fixture
def apps(workspace, monkeypatch):
    """Inbox, Stockroom live and Stockroom staging on free ports, wired in via env overrides."""
    with _Server(inbox_app()) as inbox, _Server(stockroom_app()) as live, _Server(stockroom_app()) as staging:
        monkeypatch.setenv("PATCHWORK_SOURCE_BASE_URL", inbox.url)
        monkeypatch.setenv("PATCHWORK_SINK_BASE_URL", live.url)
        monkeypatch.setenv("PATCHWORK_STAGING_BASE_URL", staging.url)
        yield {"inbox": inbox.url, "live": live.url, "staging": staging.url}

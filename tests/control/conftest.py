"""Control-plane API fixtures: the real app on a free port (uvicorn in a thread), demo apps on free
ports, and the scripted agent with pace 0. Never uses 8000-8003."""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx
import pytest

from agents.scripted import ScriptedAgentService
from control.api import create_app
from tests.conftest import _Server


@pytest.fixture
def agent(workspace):
    return ScriptedAgentService(pace_s=0)


@pytest.fixture
def api(apps, agent, monkeypatch):
    """httpx client for the control plane, wired to the test demo apps."""
    monkeypatch.setenv("PATCHWORK_INBOX_URL", apps["inbox"])
    monkeypatch.setenv("PATCHWORK_STOCKROOM_URL", apps["live"])
    monkeypatch.setenv("PATCHWORK_STAGING_URL", apps["staging"])
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")
    monkeypatch.delenv("PATCHWORK_AI_ENABLED", raising=False)
    app = create_app(agent=agent, dotenv=False)
    with _Server(app) as srv, httpx.Client(base_url=srv.url, timeout=60) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


def read_sse(client: httpx.Client, url: str, *, headers: dict | None = None,
             until: Callable[[dict], bool] | None = None, timeout: float = 90) -> list[dict[str, Any]]:
    """Read an SSE stream into [{id, event, data}] until the server closes it or `until(ev)`."""
    events: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    deadline = time.time() + timeout
    with client.stream("GET", url, headers=headers or {}, timeout=timeout) as resp:
        assert resp.status_code == 200, resp.read()
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if time.time() > deadline:
                raise AssertionError(f"SSE timeout; got {events[-3:]}")
            if line.startswith(":"):
                continue
            if line == "":
                if cur:
                    events.append(cur)
                    if until and until(cur):
                        break
                    cur = {}
                continue
            key, _, value = line.partition(": ")
            cur[key] = json.loads(value) if key == "data" else (int(value) if key == "id" else value)
    return events


def wait_thread(client: httpx.Client, tid: str, state: str, timeout: float = 90) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/agent/{tid}").json()
        if s["state"] == state:
            return s
        if s["state"] == "failed":
            raise AssertionError(client.get(f"/api/agent/{tid}/stream").text[-2000:])
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {state}")


def finish_run(client: httpx.Client, run_id: int) -> dict[str, Any]:
    events = read_sse(client, f"/api/runs/{run_id}/stream")
    assert events[-1]["event"] == "finished", events[-3:]
    return events[-1]["data"]


def build_and_deploy(client: httpx.Client) -> str:
    """BUILD through the scripted agent and approve v1. Returns the thread id."""
    tid = client.post("/api/integrations/build", json={"recording_id": "fixture"}).json()["thread_id"]
    s = wait_thread(client, tid, "waiting")
    client.post(f"/api/agent/{tid}/resume", json={"interrupt_id": s["pending"]["interrupt_id"],
                                                  "answer": "Quarantine them for review"}).raise_for_status()
    s = wait_thread(client, tid, "waiting")
    assert s["pending"]["type"] == "request_deploy"
    client.post(f"/api/agent/{tid}/resume", json={"interrupt_id": s["pending"]["interrupt_id"],
                                                  "decision": "approve"}).raise_for_status()
    wait_thread(client, tid, "done")
    return tid

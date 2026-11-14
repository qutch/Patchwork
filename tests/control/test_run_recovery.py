"""Control-plane startup reconciles runs a previous instance left `running`."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest

from agents.scripted import ScriptedAgentService
from control import ops
from control.api import create_app
from runner.pipeline import Outcome
from runner.store import Store
from tests.conftest import FIXTURE_SPEC, _Server

NAME = "po_to_stockroom"


@contextlib.contextmanager
def control_plane():
    """Start a fresh control plane (its lifespan runs recovery) and yield an httpx client."""
    app = create_app(agent=ScriptedAgentService(pace_s=0), dotenv=False)
    with _Server(app) as srv, httpx.Client(base_url=srv.url, timeout=60) as client:
        yield client


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def stale_run(store: Store, pid: int | None, n_records: int = 2) -> int:
    run_id = store.start_run(NAME, 1, False, pid=pid)
    for i in range(n_records):
        store.add_record(run_id, Outcome(record_id=f"msg-{i:04d}", status="success", key=f"PO-{i}"), {"id": i})
    return run_id


def wait_until(pred, timeout=30.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def deployed(apps, monkeypatch):
    monkeypatch.setenv("PATCHWORK_INBOX_URL", apps["inbox"])
    monkeypatch.setenv("PATCHWORK_STOCKROOM_URL", apps["live"])
    monkeypatch.setenv("PATCHWORK_STAGING_URL", apps["staging"])
    monkeypatch.delenv("PATCHWORK_AI_ENABLED", raising=False)
    ops.compile_and_test(FIXTURE_SPEC)
    ops.deploy(NAME, 1)
    return apps


def test_runs_whose_runner_is_gone_are_marked_failed_on_startup(workspace, monkeypatch):
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")
    store = Store()
    gone = stale_run(store, dead_pid(), n_records=2)
    legacy = stale_run(store, None, n_records=0)  # row from before pids were recorded
    sleeper = subprocess.Popen(["sleep", "30"])  # a live pid that is not a runner (pid reuse)
    try:
        reused = stale_run(store, sleeper.pid, n_records=1)
        with control_plane() as api:
            health = api.get("/api/health").json()
            assert sorted(health["recovered_runs"]["failed"]) == sorted([gone, legacy, reused])
            assert health["recovered_runs"]["adopted"] == []
            for rid, n in ((gone, 2), (legacy, 0), (reused, 1)):
                run = api.get(f"/api/runs/{rid}").json()["run"]
                assert run["status"] == "failed"
                assert "control plane restarted" in run["message"]
                assert run["counts"].get("success", 0) == n and run["finished_at"]
            assert api.get("/api/runs").json()[0]["status"] != "running"
    finally:
        sleeper.kill()


def _start_external_runner(pace_ms: int) -> subprocess.Popen:
    """A runner the control plane didn't launch (e.g. from the CLI, or a crashed API's child)."""
    env = {**ops.runner_env(), "PATCHWORK_RUN_PACE_MS": str(pace_ms)}
    return subprocess.Popen(ops.runner_command(NAME, version="active", dry_run=False, limit=None, replay=False),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=ops.REPO_ROOT)


def _running_run_id() -> int:
    wait_until(lambda: Store().running_runs(), what="runner to register its run")
    return Store().running_runs()[0]["id"]


def test_a_live_runner_is_adopted_blocks_new_runs_and_finishes_itself(deployed, workspace):
    proc = _start_external_runner(pace_ms=20)  # 150 records x 20 ms: alive for ~3 s
    try:
        run_id = _running_run_id()
        with control_plane() as api:
            assert api.get("/api/health").json()["recovered_runs"] == {"failed": [], "adopted": [run_id]}
            r = api.post("/api/runs", json={"integration": NAME})
            assert r.status_code == 409, "an adopted runner must block a concurrent run"
            assert api.get(f"/api/runs/{run_id}").json()["run"]["status"] == "running"
            proc.wait(timeout=60)
            wait_until(lambda: api.get(f"/api/runs/{run_id}").json()["run"]["status"] != "running", what="finish")
            run = api.get(f"/api/runs/{run_id}").json()["run"]
            assert run["status"] == "completed", "the adopted runner writes its own summary; we don't fail it"
            assert sum(run["counts"].get(k, 0) for k in ("success", "duplicate", "quarantined")) == 150
            wait_until(lambda: api.post("/api/runs", json={"integration": NAME, "limit": 1}).status_code == 200,
                       what="integration to be released")
    finally:
        if proc.poll() is None:
            proc.kill()


def test_an_adopted_runner_that_dies_without_a_summary_is_marked_failed(deployed, workspace):
    proc = _start_external_runner(pace_ms=200)  # slow enough to kill mid-run
    try:
        run_id = _running_run_id()
        with control_plane() as api:
            assert api.get("/api/health").json()["recovered_runs"]["adopted"] == [run_id]
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
            wait_until(lambda: api.get(f"/api/runs/{run_id}").json()["run"]["status"] == "failed",
                       what="adopted run to be marked failed")
            run = api.get(f"/api/runs/{run_id}").json()["run"]
            assert "without a summary" in run["message"]
            assert api.post("/api/runs", json={"integration": NAME, "limit": 1}).status_code == 200
    finally:
        if proc.poll() is None:
            proc.kill()

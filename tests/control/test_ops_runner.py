"""Runner and control.ops additions: started line, pace, replay mode, rollback, incident
resolution on deploy, .env loading."""

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from compiler.compile import compile_spec, load_examples
from control import ops
from control.api.settings import load_dotenv
from demo_apps.stockroom.app import create_app
from runner import run as run_mod
from runner.store import Store
from runner.testing import FakeHttp, FakeResponse
from spec import load_spec
from tests.conftest import FIXTURE_EXAMPLES, FIXTURE_SPEC

NAME = "po_to_stockroom"


def test_runner_cli_prints_started_first_and_summary_last(apps, monkeypatch):
    ops.compile_and_test(FIXTURE_SPEC)
    ops.deploy(NAME, 1)
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "50")
    proc = subprocess.run([*ops.runner_command(NAME, limit=4)], capture_output=True, text=True,
                          env=ops.runner_env(), timeout=60)
    lines = [json.loads(ln) for ln in proc.stdout.splitlines()]
    assert lines[0] == {"event": "started", "run_id": 1}
    assert lines[1] == {"event": "fetched", "run_id": 1, "total": 4}
    assert lines[-1]["status"] == "completed" and lines[-1]["kind"] == "run"
    assert lines[-1]["duration_s"] >= 0.2, "PATCHWORK_RUN_PACE_MS slows each record"
    assert ops.parse_summary(proc.stdout) == lines[-1]


def test_run_sandboxed_reports_failure_when_runner_dies_after_starting():
    out = "\n".join([json.dumps({"event": "started", "run_id": 3}), "Traceback ..."])
    assert ops.parse_summary(out) == {}


class RunnerFake(FakeHttp):
    def __init__(self, openapi, **kw):
        super().__init__(**kw)
        self.openapi = openapi

    def request(self, target, method, path, *, params=None, json=None):
        if path == "/openapi.json":
            return FakeResponse(200, self.openapi)
        return super().request(target, method, path, params=params, json=json)

    def close(self):
        pass


def _msg(i, domain, body):
    return {"id": f"m{i:03d}", "sender": f"x@{domain}", "sender_domain": domain, "subject": "s",
            "body": body, "received_at": f"2026-11-02T08:{i:02d}:00Z"}


@pytest.fixture
def deployed(workspace):
    openapi = TestClient(create_app()).get("/openapi.json").json()
    compile_spec(load_spec(FIXTURE_SPEC), version=1, out_root=workspace / "integrations",
                 examples=load_examples(FIXTURE_EXAMPLES), sink_openapi=openapi)
    store = Store()
    store.add_version(NAME, 1, "h", None)
    store.set_tests(NAME, 1, True, "ok")
    store.set_active(NAME, 1)
    return store, openapi


def test_replay_takes_quarantined_records_from_the_db(monkeypatch, deployed):
    store, openapi = deployed
    good = _msg(1, "acme.com", "PO Number: PO-1001\nSKU: WID-101\nQty: 5")
    new_format = [_msg(i, "initech.com", f"Item code: WID-101 / 2 cases (12/case) PO-{3000 + i}") for i in (2, 3, 4)]
    fake = RunnerFake(openapi, records=[good, *new_format])
    monkeypatch.setattr(run_mod, "Http", lambda config, staging=False: fake)
    first = run_mod.run(NAME, store=store, log=lambda s: None)
    assert first["counts"] == {"success": 1, "quarantined": 3, "quarantined:PARSE_NO_MATCH": 3}
    assert first["incident_id"] and first["kind"] == "run"
    cursor = store.get_cursor(NAME, "live")
    assert [r["id"] for r in store.quarantined_raw(NAME)] == ["m002", "m003", "m004"]

    # the source now "fixes" one record's format; replay must read from the DB, not the source
    fake.records = []
    new_format[0]["body"] = "ORDER REF=PO-3002 | PART=WID-102 | UNITS=24"
    store.db.execute("UPDATE records SET raw=? WHERE record_id='m002'", (json.dumps(new_format[0]),))
    lines = []
    replay = run_mod.run(NAME, replay=True, store=store, log=lines.append)
    assert json.loads(lines[0]) == {"event": "started", "run_id": replay["run_id"]}
    assert replay["kind"] == "replay" and store.run(replay["run_id"])["kind"] == "replay"
    assert replay["counts"] == {"success": 1, "quarantined": 2, "quarantined:PARSE_NO_MATCH": 2}
    assert replay["incident_id"] is None, "a replay never reopens NEW_SOURCE_FORMAT"
    assert store.get_cursor(NAME, "live") == cursor, "a replay never moves the cursor"
    assert [r["id"] for r in store.quarantined_raw(NAME)] == ["m003", "m004"]
    assert {r["record_id"]: r["status"] for r in store.latest_outcomes(NAME)} == {
        "m001": "success", "m002": "success", "m003": "quarantined", "m004": "quarantined"}

    # replaying again is safe: nothing that succeeded is re-sent
    writes = len(fake.sink_writes)
    again = run_mod.run(NAME, replay=True, store=store, log=lambda s: None)
    assert again["counts"].get("success", 0) == 0 and len(fake.sink_writes) == writes


def test_replay_quarantined_needs_an_active_version(workspace):
    with pytest.raises(ops.DeployRefused):
        ops.replay_quarantined(NAME)


def test_deploy_resolves_older_incidents_and_rollback_does_not(deployed):
    store, _ = deployed
    store.add_version(NAME, 2, "h2", None)
    store.set_tests(NAME, 2, True, "ok")
    inc = store.open_incident(name=NAME, version=1, run_id=None, type="CONTRACT_CHANGED", reason_codes=[], summary="x")
    out = ops.deploy(NAME, 2, store=store)
    assert out["resolved_incidents"] == [inc] and store.incident(inc)["status"] == "resolved"
    inc2 = store.open_incident(name=NAME, version=2, run_id=None, type="CIRCUIT_BREAKER", reason_codes=[], summary="y")
    rb = ops.rollback(NAME, 1, store=store)
    assert rb == {"name": NAME, "active_version": 1, "previous_version": 2, "resolved_incidents": [],
                  "rolled_back": True}
    assert store.incident(inc2)["status"] == "open"
    with pytest.raises(ops.DeployRefused):
        ops.rollback(NAME, 2, store=store)


def test_store_migrates_runs_kind_column(workspace):
    import sqlite3
    db = sqlite3.connect(workspace / "patchwork.db")
    db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, integration TEXT NOT NULL,"
               " version INTEGER NOT NULL, dry_run INTEGER NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,"
               " finished_at TEXT, duration_s REAL, counts TEXT, llm_calls INTEGER NOT NULL DEFAULT 0,"
               " incident_id INTEGER, message TEXT)")
    db.execute("INSERT INTO runs(integration, version, dry_run, status, started_at) VALUES ('a', 1, 0, 'completed', 't')")
    db.commit()
    db.close()
    assert Store().run(1)["kind"] == "run"


def test_load_dotenv_never_overrides_and_runner_env_strips_keys(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nOPENROUTER_API_KEY='sk-test'\nexport PATCHWORK_MODEL=\"m/x\"\nPATCHWORK_AGENT=deep\n")
    monkeypatch.setenv("PATCHWORK_AGENT", "scripted")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("PATCHWORK_MODEL", raising=False)
    monkeypatch.delenv("PATCHWORK_NO_DOTENV", raising=False)
    try:
        assert sorted(load_dotenv(env)) == ["OPENROUTER_API_KEY", "PATCHWORK_MODEL"]
        import os
        assert os.environ["OPENROUTER_API_KEY"] == "sk-test" and os.environ["PATCHWORK_MODEL"] == "m/x"
        assert os.environ["PATCHWORK_AGENT"] == "scripted"
        assert "OPENROUTER_API_KEY" not in ops.runner_env()
    finally:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("PATCHWORK_MODEL", raising=False)


def test_python_m_control_api_help():
    out = subprocess.run([sys.executable, "-m", "control.api", "--help"], capture_output=True, text=True,
                         cwd=ops.REPO_ROOT, timeout=30)
    assert out.returncode == 0 and "--port" in out.stdout

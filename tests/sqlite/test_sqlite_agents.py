"""REPAIR of a renamed SQLite column (contracts V16), scripted path only (no langchain in this module): a column
is renamed in the file, the next run is held before any write (CONTRACT_CHANGED), diff_openapi pairs the rename,
a person approves the `move` patch, and the held page is written by the repaired version."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from agents.scripted import ScriptedAgentService
from control import ops
from demo_apps.sqlite_tracker.db import create_db, rename_column, rows
from runner.run import run
from runner.store import Store
from tests.conftest import _Server
from tests.sqlite.helpers import DB, JOBS, NAME, sqlite_spec, write_spec

NEW_URL = "https://jobs.lever.co/palantir/00000000-new0-4000-8000-000000000001"


@pytest.fixture
def stack(workspace, monkeypatch):
    from control.api import create_app as control_app
    from tests.engine.fake_inbox import create_app as inbox_app
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")
    monkeypatch.delenv("PATCHWORK_AI_ENABLED", raising=False)
    agent = ScriptedAgentService(pace_s=0)
    with _Server(inbox_app()) as inbox, _Server(control_app(agent=agent, dotenv=False)) as api, \
            httpx.Client(base_url=api.url, timeout=60) as c:
        monkeypatch.setenv("PATCHWORK_CONTROL_URL", inbox.url)
        yield {"api": c, "inbox": inbox.url, "db": create_db(workspace / DB)}


def wait(c, tid, states, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = c.get(f"/api/agent/{tid}").json()
        if s["state"] in states:
            return s
        assert s["state"] != "failed", c.get(f"/api/agent/{tid}/events").text[-1500:]
        time.sleep(0.05)
    raise AssertionError(f"timeout: {s}")


def test_rename_then_repair_then_the_held_page_is_written(stack, workspace):
    from fixtures.jobs.pages import PAGES
    c, db = stack["api"], stack["db"]
    built = ops.compile_and_test(write_spec(workspace, sqlite_spec()), examples_path=JOBS / "examples.yaml")
    assert built["tests_passed"], built["tests_summary"]
    ops.deploy(NAME, built["version"])

    summary = run(NAME, limit=2, log=lambda _: None)
    assert summary["status"] == "completed" and summary["counts"] == {"success": 2}, summary
    assert len(rows(db)) == 2

    # the owner renames a column: the next run is held before any write
    rename_column(db, "Company", "Employer")
    r = httpx.post(stack["inbox"] + "/api/pages", json={"url": NEW_URL, "html": (PAGES / "lever-1.html").read_text()})
    assert r.status_code == 201, r.text
    blocked = run(NAME, log=lambda _: None)
    assert blocked["status"] == "blocked", blocked
    inc = Store().incident(blocked["incident_id"])
    assert inc["type"] == "CONTRACT_CHANGED" and any("body.Company: not accepted" in e for e in inc["detail"]["contract_errors"])
    assert len(rows(db)) == 2

    # REPAIR: diff_openapi pairs Company -> Employer; a person approves
    rid = c.post(f"/api/incidents/{inc['id']}/repair", json={}).json()["thread_id"]
    p = wait(c, rid, {"waiting"})["pending"]
    assert p["type"] == "request_deploy" and p["tests"]["passed"], p
    assert {"op": "move", "from": "/sink/fields/Company", "path": "/sink/fields/Employer"} in p["spec_patch"]
    c.post(f"/api/agent/{rid}/resume", json={"interrupt_id": p["interrupt_id"], "decision": "approve"})
    wait(c, rid, {"done"})
    assert Store().active_version(NAME) == 2
    gold = json.loads((ops.version_dir(NAME, 2) / "golden.json").read_text())
    assert all("Employer" in g["expect"]["body"] and "Company" not in g["expect"]["body"] for g in gold)

    # every page the blocked run held (it never moved the cursor): the 4 left over from the first batch's inbox
    # plus the new one are written by the repaired version, under the new column name
    again = run(NAME, log=lambda _: None)
    assert again["status"] == "completed" and again["counts"]["success"] == 5, again
    got = rows(db)
    assert len(got) == 2 + 5 and all("Company" not in r for r in got)  # the rename applies to every row
    new_row = next(r for r in got if r["Link"] == NEW_URL)
    assert new_row["Employer"] == "Palantir Technologies"
    assert all(r["Employer"] == "Palantir Technologies" for r in got[:2])  # written before the rename, renamed by SQLite


def test_repair_cannot_repoint_the_database_file(stack, workspace):
    """/sink/path is protected like Airtable's base/table: a patch cannot send the integration to another file."""
    from agents.deep_tools import ToolContext, tool_functions
    from agents.interface import _Thread
    built = ops.compile_and_test(write_spec(workspace, sqlite_spec()), examples_path=JOBS / "examples.yaml")
    assert built["tests_passed"]
    svc = ScriptedAgentService(pace_s=0)
    t = _Thread(id="repair-sqlite01", mode="repair", subject="x", integration=NAME)
    svc._threads[t.id] = t
    ctx = ToolContext(svc, t, name=NAME)
    tools = tool_functions(ctx)
    for op in ({"op": "replace", "path": "/sink/path", "value": "other.db"},
               {"op": "replace", "path": "/sink/table", "value": "Other"},
               {"op": "remove", "path": "/sink/path"}):
        assert tools["propose_spec_patch"](patch=[op]).startswith("REFUSED")

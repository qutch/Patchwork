"""DeepAgentService with an Airtable destination (contracts V14), driven by the scripted fake model (offline):
TEACH from `fixture-jobs` retargeted to a table without Email (the code asks; skip drops it), a run, a renamed
field held before any write, REPAIR's `move` patch, approve, and the held pages written.

langchain is imported inside the tests (tests/airtable/conftest.py runs this module last)."""

from __future__ import annotations

import httpx
import pytest

from control import ops
from demo_apps.fake_airtable.app import REAL_BASE_FIELDS, TOKEN, create_app
from runner.store import Store
from tests.airtable.helpers import NAME
from tests.airtable.test_airtable_agents import NEW_URL, post_new_lever_page, retarget
from tests.conftest import _Server


def teach_script():
    from tests.agents.fake_model import call, turn
    return [
        turn(call("write_todos", todos=[{"content": "Draft the spec from the recording", "status": "in_progress"},
                                        {"content": "Compile and test", "status": "pending"},
                                        {"content": "Request deploy", "status": "pending"}])),
        turn(call("task", subagent_type="interpreter", description="Recording fixture-jobs -> spec.yaml")),
        # interpreter
        turn(call("get_candidates")),
        turn(call("draft_spec")),  # the code asks about Email (no such field in the table) inside this tool
        turn(call("test_parser")),
        turn(call("validate_spec")),
        turn(text="Drafted a Lever parser into the Airtable table; Email skipped."),
        # supervisor -> builder
        turn(call("task", subagent_type="builder", description="Compile the spec")),
        turn(call("compile_spec")),
        turn(text="v1 compiled; tests passed."),
        turn(call("request_deploy", version=1, summary="Taught from fixture-jobs into Airtable.")),
        turn(text="v1 deployed."),
    ]


def repair_script(version: int):
    from tests.agents.fake_model import call, turn
    move = [{"op": "move", "from": "/sink/fields/Company", "path": "/sink/fields/Employer"}]
    return [
        turn(call("write_todos", todos=[{"content": "Diagnose", "status": "in_progress"}])),
        turn(call("task", subagent_type="repairer", description="Repair the Airtable incident")),
        turn(call("get_incident")),
        turn(call("diff_openapi")),
        turn(call("propose_spec_patch", patch=move, reason="the Airtable field Company is now Employer")),
        turn(call("migrate_golden_examples")),
        turn(text="Moved sink.fields.Company to Employer; goldens migrated."),
        turn(call("task", subagent_type="builder", description="Compile the patched spec")),
        turn(call("compile_spec")),
        turn(text=f"v{version} compiled; tests passed."),
        turn(call("request_deploy", version=version, summary="Follow the table's rename Company -> Employer.")),
        turn(text=f"v{version} deployed."),
    ]


@pytest.fixture
def env(workspace, monkeypatch):
    from tests.engine.fake_inbox import create_app as inbox_app
    monkeypatch.setenv("AIRTABLE_TOKEN", TOKEN)  # the control plane's .env; runs get PATCHWORK_CRED__AIRTABLE_TOKEN
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")
    with _Server(create_app(fields=REAL_BASE_FIELDS)) as fake, _Server(inbox_app()) as inbox:
        monkeypatch.setenv("PATCHWORK_AIRTABLE_API_URL", fake.url + "/v0")
        monkeypatch.setenv("PATCHWORK_CONTROL_URL", inbox.url)
        yield fake.url, inbox.url


def test_deep_teach_and_repair_into_airtable(env, monkeypatch):
    from agents.deep import DeepAgentService
    from tests.agents.fake_model import ScriptedChatModel
    from tests.agents.test_deep_agent import check_events, wait_for
    fake, inbox = env
    retarget()
    model = ScriptedChatModel(teach_script())
    svc = DeepAgentService(model=model)
    tid = svc.start_build("fixture-jobs", NAME)
    s = wait_for(svc, tid, "waiting")
    p = s["pending"]
    assert p["type"] == "ask_user" and p["options"] == ["Skip this field"] and "'Email'" in p["question"]
    svc.resume(tid, p["interrupt_id"], {"answer": "Skip this field"})
    s = wait_for(svc, tid, "waiting")
    p = s["pending"]
    assert p["type"] == "request_deploy" and p["version"] == 1 and p["tests"]["passed"], p
    assert "kind: airtable" in p["spec_diff"] and "Email" not in p["spec_diff"].split("fields:", 1)[1].split("typecast")[0]
    svc.resume(tid, p["interrupt_id"], {"decision": "approve"})
    wait_for(svc, tid, "done")
    assert model.remaining == 0 and Store().active_version(NAME) == 1
    check_events(svc, tid)
    assert TOKEN not in str(svc.events(tid))

    s1 = ops.run_sandboxed(NAME)
    assert s1["status"] == "completed" and s1["counts"]["success"] == 2, s1
    httpx.post(fake + "/admin/rename", json={"from": "Company", "to": "Employer"})
    post_new_lever_page(inbox)
    held = ops.run_sandboxed(NAME)
    assert held["status"] == "blocked" and len(httpx.get(fake + "/admin/records").json()) == 2

    replays = []
    monkeypatch.setattr(ops, "replay_quarantined", lambda name, **kw: replays.append(name) or {})
    model = ScriptedChatModel(repair_script(2))
    svc = DeepAgentService(model=model)
    rid = svc.start_repair(held["incident_id"])
    s = wait_for(svc, rid, "waiting")
    p = s["pending"]
    assert p["type"] == "request_deploy" and p["version"] == 2 and p["tests"]["passed"], p
    assert {"op": "move", "from": "/sink/fields/Company", "path": "/sink/fields/Employer"} in p["spec_patch"]
    svc.resume(rid, p["interrupt_id"], {"decision": "approve"})
    wait_for(svc, rid, "done")
    assert model.remaining == 0 and Store().active_version(NAME) == 2
    assert not any(e["type"] == "ask_user" for e in svc.events(rid)), "a unique rename needs no question"
    assert replays == []  # page integrations: the control plane replays held pages

    after = ops.run_sandboxed(NAME)  # the blocked run never moved the cursor: the held page is written now
    assert after["status"] == "completed" and after["counts"] == {"success": 1}, after
    rows = httpx.get(fake + "/admin/records").json()
    assert len(rows) == 3 and rows[-1]["Employer"] == "Palantir Technologies" and rows[-1]["Link"] == NEW_URL

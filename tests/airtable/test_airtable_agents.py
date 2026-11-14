"""Agents with an Airtable destination (contracts V14), scripted path only (no langchain in this module):
TEACH retargets the page->form recording `fixture-jobs` to a table (labels matched to field names by normalised
name; every unmatched label is asked, never guessed), and REPAIR of a renamed field shares the form's path."""

from __future__ import annotations

import copy
import json
import threading
import time

import httpx
import pytest
import yaml

from agents.scripted import FIXTURES, ScriptedAgentService
from control import ops
from demo_apps.fake_airtable.app import BASE_ID, REAL_BASE_FIELDS, SEED_FIELDS, TABLE, TOKEN, create_app
from runner.store import Store, workspace_dir
from spec import validate_spec
from tests.airtable.helpers import NAME
from tests.conftest import _Server

DEST = {"kind": "airtable", "base_id": BASE_ID, "table": TABLE, "credential": "AIRTABLE_TOKEN"}
LINK = "https://jobs.lever.co/palantir/0ccbe620-a3ef-41d1-a5c4-68e56b3c91d0"


def serve(fields=None):
    return _Server(create_app(fields=fields))


@pytest.fixture
def token(monkeypatch):
    """The control plane reads the plain credential from its env (.env); the runner gets PATCHWORK_CRED__*."""
    monkeypatch.setenv("AIRTABLE_TOKEN", TOKEN)
    monkeypatch.setenv("PATCHWORK_CRED__AIRTABLE_TOKEN", TOKEN)


def start_fake(monkeypatch, fields=None):
    srv = serve(fields).__enter__()
    monkeypatch.setenv("PATCHWORK_AIRTABLE_API_URL", srv.url + "/v0")
    return srv


def retarget(dest=DEST):
    """What POST /api/integrations/build {destination} writes: the recording's candidates plus `destination`."""
    c = json.loads((FIXTURES / "recording_jobs" / "candidates.json").read_text())
    c["destination"] = dict(dest)
    path = workspace_dir() / "recordings" / "fixture-jobs" / "candidates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c))


def make_ctx(mode="build", **kw):
    from agents.deep_tools import ToolContext, tool_functions
    from agents.interface import _Thread
    svc = ScriptedAgentService(pace_s=0)
    t = _Thread(id=f"{mode}-airtable01", mode=mode, subject="x", integration=NAME)
    svc._threads[t.id] = t
    ctx = ToolContext(svc, t, name=NAME, recording_id=kw.pop("recording_id", "fixture-jobs"), **kw)
    return svc, t, ctx, tool_functions(ctx)


def call_answering(svc, t, fn, answers, timeout=60):
    """Run a tool in a thread; answer each ask_user from `answers` (in order). Returns (result, questions)."""
    out, asked = {}, []
    th = threading.Thread(target=lambda: out.setdefault("r", fn()), daemon=True)
    th.start()
    deadline = time.time() + timeout
    while th.is_alive() and time.time() < deadline:
        p = t.pending
        if p is not None:
            asked.append({"question": p["question"], "options": p["options"]})
            svc.resume(t.id, p["interrupt_id"], {"answer": answers.pop(0)})
        time.sleep(0.01)
    th.join(1)
    assert "r" in out, "tool did not finish"
    return out["r"], asked


def test_draft_spec_into_the_fake_table(workspace, token, monkeypatch):
    srv = start_fake(monkeypatch)
    try:
        retarget()
        svc, t, ctx, tools = make_ctx()
        out, asked = call_answering(svc, t, tools["draft_spec"], [])
    finally:
        srv.__exit__(None)
    assert asked == [] and out.endswith("valid") and "Airtable table 'Applications'" in out, out
    spec = yaml.safe_load(ctx.spec_path.read_text())
    assert validate_spec(ctx.spec_path.read_text()) == []
    assert spec["sink"] == {"kind": "airtable", "base_id": BASE_ID, "table": TABLE,
                            "auth": {"scheme": "bearer", "credential": "AIRTABLE_TOKEN"},
                            "fields": {"Email": {"const": "demo@example.com"}, "Job Title": {"from": "title"},
                                       "Company": {"from": "company"}, "Link": {"from": "record.canonical_url"},
                                       "Date Applied": {"from": "record.captured_date"}},
                            "typecast": False, "verify": {"read_back": True}}
    (ex,) = yaml.safe_load((ctx.wd / "examples.yaml").read_text())
    assert ex["expect"]["body"] == {"Email": "demo@example.com", "Job Title": "American Tech Fellowship",
                                    "Company": "Palantir Technologies", "Link": LINK, "Date Applied": "2026-11-14"}
    assert spec["extract"]["parsers"][0]["id"] == "lever"


def test_teach_asks_about_email_when_the_table_has_no_such_field(workspace, token, monkeypatch):
    """Like the real demo base (Job Title, Company, Link, Date Applied): Email is asked, never guessed."""
    srv = start_fake(monkeypatch, REAL_BASE_FIELDS)
    try:
        retarget()
        svc, t, ctx, tools = make_ctx()
        out, asked = call_answering(svc, t, tools["draft_spec"], ["Skip this field"])
    finally:
        srv.__exit__(None)
    assert asked == [{"question": "The form question 'Email' has no field with that name in the Airtable table "
                                  "'Applications'. Which field should get it?", "options": ["Skip this field"]}]
    assert out.endswith("valid") and "skipped: ['Email']" in out, out
    spec = yaml.safe_load(ctx.spec_path.read_text())
    assert list(spec["sink"]["fields"]) == ["Job Title", "Company", "Link", "Date Applied"]
    assert any("'Email' is not sent" in a["text"] and a["status"] == "confirmed" for a in spec["assumptions"])
    (ex,) = yaml.safe_load((ctx.wd / "examples.yaml").read_text())
    assert "Email" not in ex["expect"]["body"]


def test_an_unmatched_label_can_go_to_another_field(workspace, token, monkeypatch):
    fields = copy.deepcopy(SEED_FIELDS)
    next(f for f in fields if f["name"] == "Email")["name"] = "Contact"
    next(f for f in fields if f["name"] == "Job Title")["name"] = "job  TITLE"  # normalised: still a match
    srv = start_fake(monkeypatch, fields)
    try:
        retarget()
        svc, t, ctx, tools = make_ctx()
        out, asked = call_answering(svc, t, tools["draft_spec"], ["Contact"])
        assert [q["options"] for q in asked] == [["Contact", "Skip this field"]]
        spec = yaml.safe_load(ctx.spec_path.read_text())
        assert spec["sink"]["fields"]["Contact"] == {"const": "demo@example.com"}
        assert spec["sink"]["fields"]["job  TITLE"] == {"from": "title"} and "Email" not in spec["sink"]["fields"]
        # an answer that is neither a field nor skip writes nothing
        ctx.spec_path.unlink()
        out, _ = call_answering(svc, t, tools["draft_spec"], ["the email column I guess"])
        assert out.startswith("ERROR") and not ctx.spec_path.exists()
    finally:
        srv.__exit__(None)


def test_unreadable_table_is_an_error_without_the_token(workspace, monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", "sentinel-wrong-airtable-token")
    srv = start_fake(monkeypatch)
    try:
        retarget()
        svc, t, ctx, tools = make_ctx()
        out, asked = call_answering(svc, t, tools["draft_spec"], [])
    finally:
        srv.__exit__(None)
    assert out.startswith("ERROR") and "AUTH" in out and "sentinel-wrong-airtable-token" not in out


def test_read_openapi_before_draft_reads_the_table(workspace, token, monkeypatch):
    srv = start_fake(monkeypatch)
    try:
        retarget()
        _, _, _, tools = make_ctx()
        out = tools["read_openapi"]()
    finally:
        srv.__exit__(None)
    assert "/records" in out and "Company" in out and "Summary" not in out


def test_propose_spec_patch_cannot_retarget_the_table(workspace, token, monkeypatch):
    from tests.airtable.helpers import airtable_spec
    from agents.learn import ydump
    svc, t, ctx, tools = make_ctx("repair")
    ctx.spec_path.write_text(ydump(airtable_spec()))
    for op in ({"op": "replace", "path": "/sink/base_id", "value": "appEEEEEEEEEEEEEE"},
               {"op": "replace", "path": "/sink/table", "value": "Other"},
               {"op": "replace", "path": "/sink/auth/credential", "value": "OTHER_TOKEN"},
               {"op": "replace", "path": "/sink/kind", "value": "form"}):
        assert tools["propose_spec_patch"](patch=[op]).startswith("REFUSED")


# ---- control plane: POST /api/integrations/build {destination} -> scripted TEACH -> run -> REPAIR ----------

@pytest.fixture
def stack(workspace, token, monkeypatch):
    from control.api import create_app as control_app
    from tests.engine.fake_inbox import create_app as inbox_app
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")
    monkeypatch.delenv("PATCHWORK_AI_ENABLED", raising=False)
    agent = ScriptedAgentService(pace_s=0)
    with serve(REAL_BASE_FIELDS) as fake, _Server(inbox_app()) as inbox, \
            _Server(control_app(agent=agent, dotenv=False)) as api, httpx.Client(base_url=api.url, timeout=60) as c:
        monkeypatch.setenv("PATCHWORK_AIRTABLE_API_URL", fake.url + "/v0")
        monkeypatch.setenv("PATCHWORK_CONTROL_URL", inbox.url)  # the runner's page source: the fixture pages
        yield {"api": c, "fake": fake.url, "agent": agent}


def wait(c, tid, states, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = c.get(f"/api/agent/{tid}").json()
        if s["state"] in states:
            return s
        assert s["state"] not in ("failed",), c.get(f"/api/agent/{tid}/events").text[-1500:]
        time.sleep(0.05)
    raise AssertionError(f"timeout: {s}")


def test_build_rejects_a_bad_destination(stack):
    c = stack["api"]
    for dest, msg in (({**DEST, "base_id": "nope"}, "base_id"), ({**DEST, "credential": "OPENAI_API_KEY"}, "credential"),
                      ({**DEST, "table": " "}, "table")):
        r = c.post("/api/integrations/build", json={"recording_id": "fixture-jobs", "destination": dest})
        assert r.status_code == 422 and msg in r.text, r.text
    r = c.post("/api/integrations/build", json={"recording_id": "fixture-jobs", "destination": {**DEST, "kind": "sql"}})
    assert r.status_code == 422


def test_teach_into_airtable_then_run_rename_and_repair(stack):
    from runner.run import run
    c, fake = stack["api"], stack["fake"]
    tid = c.post("/api/integrations/build", json={"recording_id": "fixture-jobs", "destination": DEST}).json()["thread_id"]
    cands = json.loads((workspace_dir() / "recordings" / "fixture-jobs" / "candidates.json").read_text())
    assert cands["destination"] == DEST and cands["sink"]["kind"] == "form"  # the recorded form stays as is
    s = wait(c, tid, {"waiting"})
    assert s["pending"]["type"] == "ask_user" and s["pending"]["options"] == ["Skip this field"]
    assert "'Email'" in s["pending"]["question"]
    c.post(f"/api/agent/{tid}/resume", json={"interrupt_id": s["pending"]["interrupt_id"], "answer": "Skip this field"})
    s = wait(c, tid, {"waiting"})
    assert s["pending"]["type"] == "request_deploy" and s["pending"]["tests"]["passed"], s["pending"]
    c.post(f"/api/agent/{tid}/resume", json={"interrupt_id": s["pending"]["interrupt_id"], "decision": "approve"})
    wait(c, tid, {"done"})
    assert Store().active_version(NAME) == 1
    vdir = ops.version_dir(NAME, 1)
    assert json.loads((vdir / "manifest.json").read_text())["contract_kind"] == "airtable"

    # page runs (in process; the runner reads PATCHWORK_CRED__AIRTABLE_TOKEN): the taught Lever parser writes rows
    summary = run(NAME, log=lambda _: None)
    assert summary["status"] == "completed" and summary["counts"]["success"] == 2, summary
    rows = httpx.get(fake + "/admin/records").json()
    assert {r["Company"] for r in rows} == {"Palantir Technologies"} and all("Email" not in r for r in rows)

    # the owner renames a field: the next run is held before any write
    httpx.post(fake + "/admin/rename", json={"from": "Company", "to": "Employer"})
    blocked = run(NAME, log=lambda _: None)
    assert blocked["status"] == "blocked" and len(httpx.get(fake + "/admin/records").json()) == 2
    inc = Store().incident(blocked["incident_id"])
    assert any("body.Company: not accepted" in e for e in inc["detail"]["contract_errors"])

    # REPAIR: diff_openapi pairs Company -> Employer; a person approves
    rid = c.post(f"/api/incidents/{inc['id']}/repair", json={}).json()["thread_id"]
    s = wait(c, rid, {"waiting"})
    p = s["pending"]
    assert p["type"] == "request_deploy" and p["tests"]["passed"]
    assert {"op": "move", "from": "/sink/fields/Company", "path": "/sink/fields/Employer"} in p["spec_patch"]
    c.post(f"/api/agent/{rid}/resume", json={"interrupt_id": p["interrupt_id"], "decision": "approve"})
    wait(c, rid, {"done"})
    assert Store().active_version(NAME) == 2
    gold = json.loads((ops.version_dir(NAME, 2) / "golden.json").read_text())
    assert all("Employer" in g["expect"]["body"] and "Company" not in g["expect"]["body"] for g in gold)

    # a run after the reset of the processed keys is not needed: blocked runs never moved the cursor
    httpx.post(fake + "/admin/reset")
    httpx.post(fake + "/admin/rename", json={"from": "Company", "to": "Employer"})
    again = run(NAME, log=lambda _: None)
    assert again["status"] == "completed", again
    assert stack["agent"] is not None


def test_build_without_destination_clears_it(stack):
    c = stack["api"]
    retarget()
    path = workspace_dir() / "recordings" / "fixture-jobs" / "candidates.json"
    tid = c.post("/api/integrations/build", json={"recording_id": "fixture-jobs"}).json()["thread_id"]
    assert "destination" not in json.loads(path.read_text())
    c.delete(f"/api/agent/{tid}")

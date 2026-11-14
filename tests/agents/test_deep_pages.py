"""TEACH for page -> form (draft_spec from V11 candidates) and REPAIR for a renamed form question."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

from agents.learn import run_page_parser
from runner.store import Store
from tests.agents.jobs import NAME, W1_READY, FakeEngine, active_spec, golden, jobs_spec, open_form_incident
from tests.agents.test_deep_agent import check_events, wait_for


def make_ctx(mode, **kw):
    from agents.deep_tools import ToolContext, tool_functions
    from agents.interface import _Thread
    from agents.scripted import ScriptedAgentService
    svc = ScriptedAgentService(pace_s=0)
    t = _Thread(id=f"{mode}-pages01", mode=mode, subject="x", integration=NAME)
    svc._threads[t.id] = t
    ctx = ToolContext(svc, t, name=NAME, **kw)
    return svc, t, ctx, tool_functions(ctx)


def test_default_build_name(workspace):
    from agents.deep_tools import default_build_name
    assert default_build_name("fixture-jobs", "po_to_stockroom") == "jobs_to_tracker"
    assert default_build_name("fixture", "po_to_stockroom") == "po_to_stockroom"
    assert default_build_name("rec-nope", "po_to_stockroom") == "po_to_stockroom"


def test_draft_spec_from_fixture_jobs(workspace, monkeypatch):
    FakeEngine().install(monkeypatch)
    svc, t, ctx, tools = make_ctx("build", recording_id="fixture-jobs")
    out = tools["draft_spec"]()
    assert out.endswith("valid") and "parser lever for jobs.lever.co" in out, out
    spec = yaml.safe_load(ctx.spec_path.read_text())
    (p,) = spec["extract"]["parsers"]
    assert p["id"] == "lever" and p["match"] == {"site": "jobs.lever.co"}
    # The taught reader follows what the person copied in the recording (W2's fixture-jobs candidates),
    # so derive the expectation from the candidates, and check that it reads both Lever pages.
    assert p["fields"]["title"] == {"css": ".posting-headline h2"}
    from agents.learn import run_page_parser
    from runner.html import visible_text
    index = {e["file"]: e for e in json.loads((REPO / "fixtures/pages/index.json").read_text())}
    for name in ("lever-1.html", "lever-2.html"):
        html = (REPO / "fixtures/pages" / name).read_text()
        got = run_page_parser(p, {"html": html, "text": visible_text(html)})
        assert got == index[name]["expected"], (name, got)
    assert p["origin"]["kind"] == "taught" and p["origin"]["thread_id"] == t.id
    assert spec["source"]["base_url"] == "patchwork://control" and spec["source"]["html_field"] == "html"
    assert spec["source"]["cursor_field"] == "seq" and spec["dedupe"] == {"key": ["record.canonical_url"]}
    sink = spec["sink"]
    assert sink["kind"] == "form" and list(sink["fields"]) == ["Email", "Job Title", "Company", "Link", "Date Applied"]
    assert sink["fields"]["Link"] == {"from": "record.canonical_url"}
    assert sink["fields"]["Date Applied"] == {"from": "record.captured_date"}
    assert sink["fields"]["Email"] == {"const": "demo@example.com"}
    assert sink["fields"]["Job Title"] == {"from": "title"} and sink["fields"]["Company"] == {"from": "company"}
    assert sink["verify"] == {"confirmation_text": "Your response has been recorded"}
    assert spec["validate"] == [{"field": "title", "rule": "required"}, {"field": "company", "rule": "required"}]
    (ex,) = yaml.safe_load((ctx.wd / "examples.yaml").read_text())
    assert ex["expect"]["body"] == {"Email": "demo@example.com", "Job Title": "American Tech Fellowship",
                                    "Company": "Palantir Technologies",
                                    "Link": "https://jobs.lever.co/palantir/0ccbe620-a3ef-41d1-a5c4-68e56b3c91d0",
                                    "Date Applied": "2026-11-14"}
    # the golden's expected values are the recorded ones; the taught parser reproduces them
    assert run_page_parser(p, ex["record"]) == {"title": "American Tech Fellowship", "company": "Palantir Technologies"}
    tested = tools["test_parser"]()
    assert tested.startswith("OK: 1/1"), tested


@pytest.mark.xfail(not W1_READY, reason="needs W1 spec models", strict=False)
def test_drafted_jobs_spec_validates(workspace):
    from spec import validate_spec
    svc, t, ctx, tools = make_ctx("build", recording_id="fixture-jobs")
    assert tools["draft_spec"]().endswith("valid")
    assert validate_spec(ctx.spec_path.read_text()) == []


@pytest.fixture
def repaired(workspace, monkeypatch):
    eng = FakeEngine().install(monkeypatch)
    eng.deploy_baseline(jobs_spec(), [golden("lever-1"), golden("ashby-1")])
    return eng


def test_diff_openapi_form_rename(repaired):
    from agents.deep_tools import seed_repair_workdir
    inc = open_form_incident({"Company": "Employer"}, required=("Email", "Employer"))
    seed_repair_workdir(NAME)
    svc, t, ctx, tools = make_ctx("repair", incident_id=inc)
    d = json.loads(tools["diff_openapi"]())
    assert d["likely_renames"] == {"Company": "Employer"}
    assert d["suggested_questions"] == [] and d["new_required_unmapped"] == {}
    assert d["patch_hint"] == [{"op": "move", "from": "/sink/fields/Company", "path": "/sink/fields/Employer"}]
    assert d["operation"] == "post /form"


def test_diff_openapi_asks_when_the_pairing_is_not_unique(repaired):
    from agents.deep_tools import seed_repair_workdir
    inc = open_form_incident({"Company": "Employer", "Link": "Posting URL"})
    seed_repair_workdir(NAME)
    svc, t, ctx, tools = make_ctx("repair", incident_id=inc)
    d = json.loads(tools["diff_openapi"]())
    assert d["likely_renames"] == {}
    qs = {q["field"]: q for q in d["suggested_questions"]}
    assert set(qs) == {"Company", "Link"}
    assert set(qs["Company"]["options"]) == {"Employer", "Posting URL", "None: stop sending it"}


def test_propose_spec_patch_cannot_retarget_the_form(repaired):
    from agents.deep_tools import seed_repair_workdir
    seed_repair_workdir(NAME)
    svc, t, ctx, tools = make_ctx("repair")
    for op in ({"op": "replace", "path": "/sink/url", "value": "https://evil.example/form"},
               {"op": "move", "from": "/sink/url", "path": "/sink/fields/x"},
               {"op": "replace", "path": "/sink/kind", "value": "api"}):
        assert tools["propose_spec_patch"](patch=[op]).startswith("REFUSED")


def repair_form_script():
    from tests.agents.fake_model import call, turn
    move = [{"op": "move", "from": "/sink/fields/Company", "path": "/sink/fields/Employer"}]
    return [
        turn(call("write_todos", todos=[{"content": "Diagnose the form change", "status": "in_progress"},
                                        {"content": "Patch and migrate goldens", "status": "pending"},
                                        {"content": "Compile, test, request deploy", "status": "pending"}])),
        turn(call("task", subagent_type="repairer", description="Repair the form incident")),
        # repairer
        turn(call("get_incident")),
        turn(call("diff_openapi")),
        turn(call("propose_spec_patch", patch=move, reason="the form question Company is now Employer")),
        turn(call("migrate_golden_examples")),
        turn(text="Moved sink.fields.Company to Employer; goldens migrated."),
        # supervisor
        turn(call("task", subagent_type="builder", description="Compile the patched spec")),
        turn(call("compile_spec")),
        turn(text="v2 compiled; tests passed."),
        turn(call("request_deploy", version=2, summary="Follow the form's rename Company -> Employer.")),
        turn(text="v2 deployed."),
    ]


def test_deep_repair_form_rename(repaired, monkeypatch):
    from agents.deep import DeepAgentService
    from control import ops
    from tests.agents.fake_model import ScriptedChatModel
    replays = []
    monkeypatch.setattr(ops, "replay_quarantined", lambda name, **kw: replays.append(name) or {})
    inc = open_form_incident({"Company": "Employer"}, required=("Email", "Employer"))
    model = ScriptedChatModel(repair_form_script())
    svc = DeepAgentService(model=model)
    rid = svc.start_repair(inc)
    s = wait_for(svc, rid, "waiting")
    p = s["pending"]
    assert p["type"] == "request_deploy" and p["version"] == 2 and p["tests"]["passed"]
    assert {"op": "move", "from": "/sink/fields/Company", "path": "/sink/fields/Employer"} in p["spec_patch"]
    assert "Employer" in p["spec_diff"]
    svc.resume(rid, p["interrupt_id"], {"decision": "approve"})
    done = wait_for(svc, rid, "done")
    assert done["state"] == "done" and model.remaining == 0
    evs = check_events(svc, rid)
    assert not any(e["type"] == "ask_user" for e in evs), "a unique rename needs no question"
    assert Store().active_version(NAME) == 2
    fields = active_spec()["sink"]["fields"]
    assert "Company" not in fields and fields["Employer"] == {"from": "company"}
    gold = json.loads((Path(repaired.compiled[-1]["path"]) / "golden.json").read_text())
    assert all("Employer" in g["expect"]["body"] and "Company" not in g["expect"]["body"] for g in gold)
    assert gold[0]["expect"]["body"]["Employer"] == "Palantir Technologies"
    assert replays == [], "page integrations are replayed by the control plane, not the agent"


def test_page_goldens_are_not_readable_by_agents(repaired):
    """A live form REPAIR spent its 80-call budget paging through /examples.yaml (whole pages): page
    integrations deny reading it, and their repairs get a smaller call cap."""
    from agents.deep import DeepAgentService
    from tests.agents.fake_model import ScriptedChatModel, call, turn
    inc = open_form_incident({"Company": "Employer"})
    model = ScriptedChatModel([turn(call("read_file", file_path="/examples.yaml")),
                               turn(call("read_file", file_path="/spec.yaml")), turn(text="stop")])
    svc = DeepAgentService(model=model, max_nudges=0)
    rid = svc.start_repair(inc)
    wait_for(svc, rid, "failed")
    res = [e for e in svc.events(rid) if e["type"] == "tool_result" and e["tool"] == "read_file"]
    assert [r["ok"] for r in res] == [False, True], res
    assert svc.max_page_repair_calls == 30
    first = model.seen[0][-1].content
    assert "Don't read files" in first and "patch_hint" in first


def test_read_openapi_before_draft_reads_the_form_contract(workspace, monkeypatch):
    """Live TEACH (2026-11-14) called read_openapi before draft_spec: a page→form recording's sink has a url,
    not a base_url, so the tool must read the form contract instead of failing with KeyError."""
    svc, t, ctx, tools = make_ctx("build", recording_id="fixture-jobs")
    assert not ctx.spec_path.exists()
    seen = []
    doc = {"openapi": "3.0.3", "info": {"title": "Job tracker", "x-patchwork-kind": "form"},
           "paths": {"/form": {"post": {"requestBody": {"content": {"application/json": {"schema": {
               "type": "object", "required": ["Email"],
               "properties": {"Email": {"type": "string"}, "Company": {"type": "string"}}}}}}}}}}
    import runner.forms
    monkeypatch.setattr(runner.forms, "fetch_form_contract", lambda url: seen.append(url) or doc)
    out = tools["read_openapi"]()
    assert seen == ["http://127.0.0.1:18010/"]  # the recorded form (fixture-jobs)
    assert "Company" in out and "/form" in out


def test_tool_exception_goes_back_to_the_model(workspace):
    """A tool that raises must not fail the whole thread: the model gets an error result and can recover."""
    from types import SimpleNamespace

    from agents.deep import EventMiddleware
    svc, t, ctx, tools = make_ctx("build", recording_id="fixture-jobs")
    mw = EventMiddleware(svc, t, "supervisor")
    req = SimpleNamespace(tool_call={"name": "read_openapi", "args": {}, "id": "call_1"})

    def boom(_):
        raise KeyError("base_url")

    msg = mw.wrap_tool_call(req, boom)
    assert getattr(msg, "status", None) == "error" and "KeyError" in msg.content and msg.tool_call_id == "call_1"
    last = [e for e in svc.events(t.id) if e["type"] == "tool_result"][-1]
    assert last["ok"] is False and "KeyError" in last["summary"]

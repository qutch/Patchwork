"""Scripted LEARN / TEACH (page -> form) / REPAIR (form rename): the stage fallback, no model.

They call the same tool functions as the deep agent (agents.deep_tools.tool_functions), so the checks are
real. W1's validator/compiler are stood in by tests.agents.jobs.FakeEngine (versions in the real Store,
real control.ops.deploy). This module must not import langchain (it runs mid-suite)."""

from __future__ import annotations

import sys
import time

import pytest

from agents.scripted import ScriptedAgentService
from runner.store import Store
from tests.agents.jobs import (NAME, READERS, FakeEngine, active_spec, golden, jobs_spec, open_form_incident, page,
                               set_learn_policy)

LEARN_TYPES = {"status", "todos", "message", "subagent_start", "tool_call", "tool_result", "evidence", "artifact",
               "learned", "deployed", "subagent_end", "done"}


def wait_state(svc, tid, states, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = svc.status(tid)
        if s["state"] in states:
            return s
        time.sleep(0.02)
    raise AssertionError(svc.status(tid))


@pytest.fixture
def engine(workspace, monkeypatch):
    return FakeEngine().install(monkeypatch)


def baseline(engine, platforms=("lever", "greenhouse", "ashby")):
    engine.deploy_baseline(jobs_spec(platforms), [golden("lever-1")])


def test_no_ai_modules_loaded():
    assert not [m for m in sys.modules if m.split(".")[0] in ("langchain", "langchain_core", "langgraph", "deepagents")]


def test_scripted_learn_branded_greenhouse(engine):
    baseline(engine)
    engine.add_page(page("greenhouse_branded-1", seq=5))
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_learn("pg-000005")
    assert wait_state(svc, tid, ("done", "failed"))["state"] == "done", svc.events(tid)[-3:]
    evs = svc.events(tid)
    assert {e["type"] for e in evs} == LEARN_TYPES
    result = next(e for e in evs if e["type"] == "done")["result"]
    assert {k: result[k] for k in ("model_calls", "page_id", "parser_id", "site", "version", "deployed", "approval")} == {
        "model_calls": 0, "page_id": "pg-000005", "parser_id": "airbnb", "site": "careers.airbnb.com", "version": 2,
        "deployed": True, "approval": "auto"}
    p = next(p for p in active_spec()["extract"]["parsers"] if p["id"] == "airbnb")
    assert p["fields"] == {"title": {"css": "h1"}, "company": {"jsonld": "name", "jsonld_type": "Organization"}}
    calls = [e["tool"] for e in evs if e["type"] == "tool_call"]
    assert calls == ["get_page", "test_page_parser", "save_parser"]


@pytest.mark.parametrize("name,platform,pid", [("greenhouse-2", "greenhouse", "greenhouse"),
                                                ("lever-2", "lever", "lever"), ("ashby-2", "ashby", "ashbyhq")])
def test_scripted_learn_known_platforms_from_scratch(engine, name, platform, pid):
    others = tuple(p for p in ("lever", "greenhouse", "ashby") if p != platform)
    baseline(engine, others)
    rec = engine.add_page(page(name, seq=9))
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_learn(rec["id"])
    assert wait_state(svc, tid, ("done", "failed"))["state"] == "done", svc.events(tid)[-3:]
    result = next(e for e in svc.events(tid) if e["type"] == "done")["result"]
    assert result["approval"] == "auto" and result["parser_id"] == pid
    ev = [e for e in svc.events(tid) if e["type"] == "evidence"][-1]
    from tests.agents.jobs import INDEX
    assert {f["name"]: f["value"] for f in ev["fields"]} == INDEX[name]["expected"]


def test_scripted_learn_uses_real_checks(engine, monkeypatch):
    """A fabricated value (here: hidden in <noscript>) is refused by the real tools; nothing compiles."""
    baseline(engine, ("lever", "ashby"))
    from agents.learn import make_page_record
    rec = page("greenhouse-1", seq=4)
    html = rec["html"].replace("</h1>", '</h1><noscript><p class="ns-co">Acme Inc</p></noscript>', 1)
    engine.add_page(make_page_record(rec["url"], html, seq=4))
    fake = {"id": "greenhouse", "fields": {"title": READERS["greenhouse"]["title"], "company": {"css": ".ns-co"}}}
    monkeypatch.setattr(ScriptedAgentService, "propose_parser", lambda self, rec, spec: fake)
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_learn("pg-000004")
    assert wait_state(svc, tid, ("done", "failed"))["state"] == "failed"
    evs = svc.events(tid)
    assert next(e for e in evs if e["type"] == "evidence")["ok"] is False
    assert "failed its test" in next(e for e in evs if e["type"] == "error")["message"]
    assert Store().latest_version(NAME) == 1


def test_scripted_learn_page_chrome_asks_a_person(engine, monkeypatch):
    baseline(engine, ("greenhouse", "ashby"))
    engine.add_page(page("lever-1", seq=2))
    chrome = {"id": "lever", "fields": {"title": READERS["lever"]["title"], "company": {"css": "[class*=cookie]"}}}
    monkeypatch.setattr(ScriptedAgentService, "propose_parser", lambda self, rec, spec: chrome)
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_learn("pg-000002")
    s = wait_state(svc, tid, ("waiting", "failed", "done"))
    assert s["state"] == "waiting" and s["pending"]["type"] == "request_deploy"
    assert "not corroborated" in s["pending"]["summary"] and Store().active_version(NAME) == 1
    svc.resume(tid, s["pending"]["interrupt_id"], {"decision": "reject", "comment": "cookie banner"})
    assert wait_state(svc, tid, ("done", "failed"))["state"] == "done"
    result = next(e for e in svc.events(tid) if e["type"] == "done")["result"]
    assert result["deployed"] is False and result["approval"] == "human"


def test_scripted_learn_respects_approve_policy(engine):
    baseline(engine, ("lever", "ashby"))
    engine.add_page(page("greenhouse-1", seq=3))
    set_learn_policy("approve")
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_learn("pg-000003")
    s = wait_state(svc, tid, ("waiting", "failed", "done"))
    assert s["pending"]["type"] == "request_deploy"
    svc.resume(tid, s["pending"]["interrupt_id"], {"decision": "approve"})
    assert wait_state(svc, tid, ("done", "failed"))["state"] == "done"
    assert Store().active_version(NAME) == 2


def test_scripted_learn_without_a_reader_fails_and_holds(engine):
    baseline(engine)
    from agents.learn import make_page_record
    engine.add_page(make_page_record("https://example.org/jobs/1", "<html><body><p>Hello</p></body></html>", seq=8))
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_learn("pg-000008")
    assert wait_state(svc, tid, ("done", "failed"))["state"] == "failed"
    assert "no checked reader" in next(e for e in svc.events(tid) if e["type"] == "error")["message"]
    assert Store().latest_version(NAME) == 1


def test_scripted_teach_fixture_jobs(engine):
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_build("fixture-jobs")
    s = wait_state(svc, tid, ("waiting", "failed"))
    assert s["state"] == "waiting" and s["pending"]["type"] == "ask_user", svc.events(tid)[-3:]
    assert "demo@example.com" in s["pending"]["question"]
    svc.resume(tid, s["pending"]["interrupt_id"], {"answer": s["pending"]["options"][0]})
    s = wait_state(svc, tid, ("waiting", "failed"))
    assert s["pending"]["type"] == "request_deploy" and s["pending"]["name"] == NAME and s["pending"]["tests"]["passed"]
    svc.resume(tid, s["pending"]["interrupt_id"], {"decision": "approve"})
    assert wait_state(svc, tid, ("done", "failed"))["state"] == "done"
    assert Store().active_version(NAME) == 1
    spec = active_spec()
    (p,) = spec["extract"]["parsers"]
    assert p["id"] == "lever" and p["origin"]["kind"] == "taught" and spec["sink"]["kind"] == "form"
    tools = [e["tool"] for e in svc.events(tid) if e["type"] == "tool_call"]
    assert tools[:4] == ["get_recording_summary", "get_candidates", "draft_spec", "test_parser"]
    assert "compile_spec" in tools and tools[-1] == "request_deploy"
    assert all(e["ok"] for e in svc.events(tid) if e["type"] == "tool_result"), \
        [e for e in svc.events(tid) if e["type"] == "tool_result" and not e["ok"]]


def test_scripted_form_repair(engine, monkeypatch):
    from control import ops
    replays = []
    monkeypatch.setattr(ops, "replay_quarantined", lambda name, **kw: replays.append(name))
    baseline(engine)
    inc = open_form_incident({"Company": "Employer"}, required=("Email", "Employer"))
    svc = ScriptedAgentService(pace_s=0)
    rid = svc.start_repair(inc)
    s = wait_state(svc, rid, ("waiting", "failed"))
    assert s["state"] == "waiting" and s["pending"]["type"] == "request_deploy", svc.events(rid)[-3:]
    assert {"op": "move", "from": "/sink/fields/Company", "path": "/sink/fields/Employer"} in s["pending"]["spec_patch"]
    svc.resume(rid, s["pending"]["interrupt_id"], {"decision": "approve"})
    assert wait_state(svc, rid, ("done", "failed"))["state"] == "done"
    assert Store().active_version(NAME) == 2
    assert active_spec()["sink"]["fields"]["Employer"] == {"from": "company"}
    assert not [e for e in svc.events(rid) if e["type"] == "ask_user"] and replays == []


def test_scripted_form_repair_asks_when_the_pairing_is_not_unique(engine):
    baseline(engine)
    inc = open_form_incident({"Company": "Employer", "Link": "Posting URL"})
    svc = ScriptedAgentService(pace_s=0)
    rid = svc.start_repair(inc)
    answers = {"Company": "Employer", "Link": "Posting URL"}
    for _ in range(2):
        s = wait_state(svc, rid, ("waiting", "failed"))
        p = s["pending"]
        assert p["type"] == "ask_user"
        field = next(f for f in answers if f"'{f}'" in p["question"])
        svc.resume(rid, p["interrupt_id"], {"answer": answers.pop(field)})
        time.sleep(0.05)
    s = wait_state(svc, rid, ("waiting", "failed"))
    assert s["pending"]["type"] == "request_deploy"
    svc.resume(rid, s["pending"]["interrupt_id"], {"decision": "approve"})
    assert wait_state(svc, rid, ("done", "failed"))["state"] == "done"
    fields = active_spec()["sink"]["fields"]
    assert fields["Employer"] == {"from": "company"} and fields["Posting URL"] == {"from": "record.canonical_url"}

"""LEARN mode on DeepAgentService, driven by a scripted tool-calling model (offline).

The page is the branded Greenhouse posting (careers.airbnb.com): the fingerprint routes it to the
greenhouse parser, which can't read it, so it is held and a learn thread starts (contracts V9/V10)."""

from __future__ import annotations

import pytest

from runner.store import Store
from tests.agents.jobs import NAME, FakeEngine, active_spec, golden, jobs_spec, page
from tests.agents.test_deep_agent import check_events, wait_for

PAGE = "pg-000005"


def deps():
    from agents.deep import DeepAgentService
    from tests.agents.fake_model import ScriptedChatModel, call, turn
    from tests.agents.scripts import learn_script
    return DeepAgentService, ScriptedChatModel, call, turn, learn_script


@pytest.fixture
def engine(workspace, monkeypatch):
    eng = FakeEngine().install(monkeypatch)
    eng.add_page(page("greenhouse_branded-1", seq=5))
    eng.deploy_baseline(jobs_spec(), [golden("lever-1"), golden("greenhouse-1"), golden("ashby-1")])
    return eng


def test_deep_learn_branded_greenhouse(engine):
    DeepAgentService, ScriptedChatModel, call, turn, learn_script = deps()
    model = ScriptedChatModel(learn_script(PAGE))
    svc = DeepAgentService(model=model)
    tid = svc.start_learn(PAGE)
    assert tid.startswith("learn-")
    wait_for(svc, tid, "done")
    assert model.remaining == 0
    evs = check_events(svc, tid)

    def key(e):
        if e["type"] == "status":
            return f"status({e['mode']})"
        if e["type"] == "subagent_start":
            return f"subagent_start({e['name']})"
        if e["type"] in ("tool_call", "tool_result"):
            return f"{e['type']} {e['tool']}"
        if e["type"] == "learned":
            return f"learned({e['approval']})"
        return e["type"]

    seq = [key(e) for e in evs]
    want = ["status(learn)", "todos", "subagent_start(learner)", "tool_call get_page", "tool_call test_page_parser",
            "evidence", "tool_call save_parser", "learned(auto)", "deployed", "done"]
    pos = [seq.index(w, seq.index(want[i - 1]) + 1 if i else 0) for i, w in enumerate(want)]
    assert pos == sorted(pos), seq
    done = next(e for e in evs if e["type"] == "done")["result"]
    assert done["model_calls"] <= 8 and done["model_calls"] == len(learn_script(PAGE))
    assert {k: done[k] for k in ("page_id", "parser_id", "site", "version", "deployed", "approval")} == {
        "page_id": PAGE, "parser_id": "airbnb", "site": "careers.airbnb.com", "version": 2, "deployed": True,
        "approval": "auto"}
    assert done["seconds"] >= 0
    assert Store().active_version(NAME) == 2
    parser = next(p for p in active_spec()["extract"]["parsers"] if p["id"] == "airbnb")
    assert parser["origin"]["approved_by"] == "auto" and parser["origin"]["thread_id"] == tid
    # the learner never saw the page's HTML: only the outline from get_page
    for msgs in model.seen:
        assert all(len(str(getattr(m, "content", ""))) < 6000 for m in msgs)
    ev = next(e for e in evs if e["type"] == "evidence")
    assert [(f["name"], f["value"]) for f in ev["fields"]] == [
        ("title", "AirCover Specialist, Claims Experience"), ("company", "Airbnb")]


def test_learn_tools_per_agent(engine):
    DeepAgentService, ScriptedChatModel, call, turn, learn_script = deps()
    model = ScriptedChatModel([turn(call("task", subagent_type="learner", description="learn")),
                               turn(call("get_page")), turn(text="cannot"), turn(text="stop")])
    svc = DeepAgentService(model=model, max_nudges=0)
    tid = svc.start_learn(PAGE)
    wait_for(svc, tid, "failed")  # nothing saved: the thread fails and the page stays held
    sup = set(model._bound[0])
    learner = set(next(b for b in model._bound if "save_parser" in b))
    assert {"task", "write_todos", "ls", "read_file"} <= sup
    assert not {"save_parser", "request_deploy", "write_file", "edit_file", "ask_user", "get_page"} & sup, \
        "the LEARN supervisor plans and delegates; it has no write tools"
    assert {"get_page", "test_page_parser", "ask_user", "save_parser", "add_test_case"} <= learner
    assert not {"request_deploy", "task", "compile_spec", "propose_spec_patch"} & learner
    desc = model._descriptions["task"]
    assert "- learner:" in desc and "- interpreter:" not in desc and "- general-purpose:" not in desc
    err = next(e for e in svc.events(tid) if e["type"] == "error")
    assert "without saving a parser" in err["message"]
    assert Store().active_version(NAME) == 1


@pytest.mark.parametrize("path,allowed", [("/spec.yaml", True), ("/examples.yaml", False), ("/transforms.py", False)])
def test_learn_writes_only_spec(engine, path, allowed):
    DeepAgentService, ScriptedChatModel, call, turn, learn_script = deps()
    model = ScriptedChatModel([turn(call("task", subagent_type="learner", description="write")),
                               turn(call("write_file", file_path=path, content="x: 1\n")),
                               turn(text="done"), turn(text="done")])
    svc = DeepAgentService(model=model, max_nudges=0)
    tid = svc.start_learn(PAGE)
    wait_for(svc, tid, "failed")
    res = next(e for e in svc.events(tid) if e["type"] == "tool_result" and e["tool"] == "write_file")
    assert res["ok"] is allowed, res


def test_learn_budget_is_capped(engine, monkeypatch):
    DeepAgentService, ScriptedChatModel, call, turn, learn_script = deps()
    monkeypatch.setenv("PATCHWORK_LEARN_MAX_CALLS", "3")
    model = ScriptedChatModel([turn(call("task", subagent_type="learner", description="x"))]
                              + [turn(call("get_page")) for _ in range(10)])
    svc = DeepAgentService(model=model, max_nudges=0)
    tid = svc.start_learn(PAGE)
    wait_for(svc, tid, "failed")
    err = next(e for e in svc.events(tid) if e["type"] == "error")
    assert "budget exhausted (3 calls)" in err["message"]

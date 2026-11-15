"""G6 (contracts V17, V18): LEARN on a document through DeepAgentService, driven by a scripted tool-calling model
(offline). The learner gets the document prompt, the same tools, and at most six model calls."""

from __future__ import annotations

import pytest

from runner.store import Store
from tests.agents.jobs import NAME, FakeEngine, active_spec, set_learn_policy
from tests.agents.test_deep_agent import check_events, wait_for
from tests.agents.test_learn_rows import GOOD, doc, rows_spec

DOC = "doc-000009"


def deps():
    from agents.deep import DeepAgentService
    from tests.agents.fake_model import ScriptedChatModel, call, turn
    return DeepAgentService, ScriptedChatModel, call, turn


@pytest.fixture
def engine(workspace, monkeypatch):
    set_learn_policy("auto")
    eng = FakeEngine().install(monkeypatch)
    eng.add_page(doc())
    eng.deploy_baseline(rows_spec(), [])
    return eng


def doc_script(parser: dict = GOOD) -> list:
    DeepAgentService, ScriptedChatModel, call, turn = deps()
    return [
        turn(call("task", subagent_type="learner", description=f"Learn document {DOC} (site south.example)")),
        turn(call("get_page")),
        turn(call("test_page_parser", parser=parser)),
        turn(call("save_parser", parser=parser, reason="a new sender's layout")),
        turn(text="Saved parser south (2 rows); deployed automatically."),
        turn(text="Learned south.example: parser south, deployed automatically after every check passed."),
    ]


def test_deep_learn_on_a_document_auto_deploys_within_six_model_calls(engine):
    DeepAgentService, ScriptedChatModel, call, turn = deps()
    model = ScriptedChatModel(doc_script())
    svc = DeepAgentService(model=model)
    tid = svc.start_learn(DOC)
    wait_for(svc, tid, "done")
    assert model.remaining == 0
    evs = check_events(svc, tid)
    ev = next(e for e in evs if e["type"] == "evidence")
    assert ev["rows"]["count"] == 2 and all(c["ok"] for c in ev["rows"]["checks"])
    assert {c["name"] for c in ev["rows"]["checks"]} >= {"sum_equals", "values_in_own_row", "row_count"}
    learned = next(e for e in evs if e["type"] == "learned")
    assert learned["approval"] == "auto" and learned["deployed"] is True and learned["site"] == "south.example"
    # the same event types the scripted agent emits (tests/agents/test_scripted_learn_documents.py)
    assert [e["type"] for e in evs if e["type"] in ("evidence", "learned", "deployed")] == [
        "evidence", "evidence", "learned", "deployed"]
    done = next(e for e in evs if e["type"] == "done")["result"]
    assert done["model_calls"] == 6 and done["parser_id"] == "south" and done["approval"] == "auto"
    assert Store().active_version(NAME) == 2
    parser = next(p for p in active_spec()["extract"]["parsers"] if p["id"] == "south")
    assert parser["rows"]["select"]["table"] and parser["origin"]["approved_by"] == "auto"


def test_the_learner_is_given_the_document_prompt_and_the_document_task(engine):
    DeepAgentService, ScriptedChatModel, call, turn = deps()
    model = ScriptedChatModel(doc_script())
    svc = DeepAgentService(model=model)
    tid = svc.start_learn(DOC)
    wait_for(svc, tid, "done")
    text = "\n".join(str(getattr(m, "content", "")) for msgs in model.seen for m in msgs)
    assert "A document arrived (a file, or" in text and "The record is a document" in text
    assert "the document fields `ref`, `total`" in text and "the row fields `item`, `qty`, `line`" in text
    assert "declared cross-checks (sum_equals on total)" in text
    assert "MODE: LEARN" in text and DOC in text
    learner_tools = set(next(b for b in model._bound if "save_parser" in b))
    assert {"get_page", "test_page_parser", "save_parser"} <= learner_tools
    assert not {"request_deploy", "task", "compile_spec"} & learner_tools


def test_a_page_learn_keeps_the_page_prompt(workspace, monkeypatch):
    from tests.agents.jobs import golden, jobs_spec, page

    DeepAgentService, ScriptedChatModel, call, turn = deps()
    eng = FakeEngine().install(monkeypatch)
    eng.add_page(page("greenhouse_branded-1", seq=5))
    eng.deploy_baseline(jobs_spec(), [golden("lever-1"), golden("greenhouse-1"), golden("ashby-1")])
    model = ScriptedChatModel([turn(call("task", subagent_type="learner", description="learn")), turn(text="no"),
                               turn(text="stop")])
    svc = DeepAgentService(model=model, max_nudges=0)
    tid = svc.start_learn("pg-000005")
    wait_for(svc, tid, "failed")
    text = "\n".join(str(getattr(m, "content", "")) for msgs in model.seen for m in msgs)
    assert "A document arrived" not in text and "You are the learner" in text

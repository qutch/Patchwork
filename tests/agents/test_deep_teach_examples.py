"""The deep agent's TEACH from example pages, offline: a scripted fake chat model drives mapper -> learner ->
builder -> request_deploy on two product sites into a fake Airtable table, within 10 model calls."""

from __future__ import annotations

import pytest

from demo_apps.fake_airtable.app import create_app
from runner.store import Store
from tests.agents.teach_examples_helpers import (PRODUCT_FIELDS, PRODUCT_LABELS, TOKEN, airtable_destination,
                                                 product_pages, write_teach_draft)
from tests.agents.test_deep_agent import check_events, wait_for
from tests.conftest import _Server

NAME = "track_product_prices"
MAPPING = {"Product": {"field": "name", "reader": {"css": "h1.product-title"}},
           "Price": {"field": "price", "reader": {"jsonld": "offers.price", "jsonld_type": "Product"}},
           "Link": {"record": "canonical_url"}, "Date Added": {"today": True}, "Store": {"skip": True}}
SHOP_B = {"fields": {"name": {"jsonld": "name", "jsonld_type": "Product"},
                     "price": {"jsonld": "offers.price", "jsonld_type": "Product"}}}
TODOS = [{"content": "Map the destination fields", "status": "in_progress"},
         {"content": "Add a parser for each other example site", "status": "pending"},
         {"content": "Compile and test", "status": "pending"},
         {"content": "Request deploy", "status": "pending"}]


def teach_script(pids: list[str]) -> list:
    from tests.agents.fake_model import call, turn
    return [
        # supervisor: the plan and the first delegation in one turn
        turn(call("write_todos", todos=TODOS),
             call("task", subagent_type="mapper", description=f"Map the destination fields of {NAME} on page {pids[0]}")),
        # mapper (the first example page's outline is in its system prompt)
        turn(call("propose_mapping", mapping=MAPPING)),
        turn(text="Mapped: Product <- name, Price <- price, Link <- address, Date Added <- today, Store skipped. "
                  f"Fields: name, price. Next: {pids[1]}."),
        # supervisor
        turn(call("task", subagent_type="learner", description=f"Add a parser for page {pids[1]} reading name, price")),
        # learner
        turn(call("add_site_parser", page_id=pids[1], parser=SHOP_B)),
        turn(text="Added parser lumen_supply for lumen-supply.example.org."),
        # supervisor
        turn(call("task", subagent_type="builder", description="Compile spec.yaml and run its tests")),
        # builder
        turn(call("compile_spec")),
        turn(text="v1 compiled; tests passed."),
        # supervisor: request_deploy ends the run (no closing model call)
        turn(call("request_deploy", version=1, summary="Taught from 2 example pages (shop-a, lumen-supply).")),
    ]


@pytest.fixture
def products_draft_teach(workspace, monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", TOKEN)
    monkeypatch.setenv("PATCHWORK_CRED__AIRTABLE_TOKEN", TOKEN)
    with _Server(create_app(fields=PRODUCT_FIELDS)) as srv:
        monkeypatch.setenv("PATCHWORK_AIRTABLE_API_URL", srv.url + "/v0")
        rid, pids = write_teach_draft(NAME, product_pages(), airtable_destination(), "Track product prices")
        yield {"recording_id": rid, "page_ids": pids, "airtable": srv.url}


@pytest.fixture
def fake_model_service(products_draft_teach):
    from agents.deep import DeepAgentService
    from tests.agents.fake_model import ScriptedChatModel
    model = ScriptedChatModel(teach_script(products_draft_teach["page_ids"]))
    svc = DeepAgentService(model=model, max_nudges=0)
    svc.fake_model = model
    return svc


def run_to_interrupt(svc, draft) -> list[dict]:
    tid = svc.start_build(draft["recording_id"], NAME)
    svc.tid = tid
    s = wait_for(svc, tid, "waiting", timeout=180)
    assert s["pending"]["type"] == "request_deploy", s["pending"]
    return svc.events(tid)


def test_examples_teach_runs_mapper_learner_builder(fake_model_service, products_draft_teach):
    ev = run_to_interrupt(fake_model_service, products_draft_teach)
    names = [e["name"] for e in ev if e["type"] == "subagent_start"]
    assert names[:1] == ["mapper"] and "learner" in names and "builder" in names
    deploy = next(e for e in ev if e["type"] == "request_deploy")
    assert {r["dest_field"] for r in deploy["review"]} == {"Product", "Price", "Store", "Link", "Date Added"}
    assert sum(1 for e in ev if e["type"] == "model_call") <= 10


def test_examples_teach_end_to_end(fake_model_service, products_draft_teach):
    svc, model = fake_model_service, fake_model_service.fake_model
    ev = run_to_interrupt(svc, products_draft_teach)
    pids = products_draft_teach["page_ids"]
    calls = [e for e in ev if e["type"] == "model_call"]
    assert len(calls) == 10 and model.remaining == 0
    assert [e["name"] for e in ev if e["type"] == "subagent_start"] == ["mapper", "learner", "builder"]
    deploy = next(e for e in ev if e["type"] == "request_deploy")
    rows = {r["dest_field"]: r for r in deploy["review"]}
    assert list(rows) == PRODUCT_LABELS
    assert rows["Price"]["values"] == {pids[0]: 24.99, pids[1]: 89} and rows["Store"]["status"] == "needs_you"
    assert all(r["ai_proposed"] for r in deploy["review"])
    # the subagents got their own tools only (no save_parser, no draft_spec in this mode)
    by_agent = {(e["agent"], e["tool"]) for e in ev if e["type"] == "tool_call"}
    assert by_agent == {("mapper", "propose_mapping"), ("learner", "add_site_parser"), ("builder", "compile_spec"),
                        ("supervisor", "request_deploy")}
    # the mapper saw the destination fields and the first page's outline without calling a tool
    first_prompt = " ".join(str(m.content) for m in model.seen[1])
    assert "Product (text)" in first_prompt and "h1.product-title" in first_prompt
    svc.resume(svc.tid, deploy["interrupt_id"], {"decision": "approve"})
    done = wait_for(svc, svc.tid, "done", timeout=120)
    assert done["state"] == "done" and Store().active_version(NAME) == 1
    evs = check_events(svc, svc.tid)
    result = next(e for e in evs if e["type"] == "done")["result"]
    assert result["deployed"] is True and result["model_calls"] == 10
    assert svc.status(svc.tid)["integration"] == NAME


def test_examples_tools_bound_per_agent(fake_model_service, products_draft_teach):
    from agents.deep import EXAMPLES_SUBAGENTS, EXAMPLES_SUPERVISOR_TOOLS
    assert EXAMPLES_SUPERVISOR_TOOLS == ["request_deploy"]
    assert EXAMPLES_SUBAGENTS["mapper"][2] == ["get_candidates", "get_page", "test_page_parser", "propose_mapping", "ask_user"]
    assert EXAMPLES_SUBAGENTS["learner"][2] == ["get_page", "test_page_parser", "add_site_parser"]
    assert EXAMPLES_SUBAGENTS["builder"][2] == ["compile_spec", "run_tests"]

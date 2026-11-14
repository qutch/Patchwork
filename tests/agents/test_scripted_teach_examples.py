"""The scripted agent's TEACH from example pages (no model): the same subagents and tools as the deep agent's
(mapper -> learner per other site -> builder -> request_deploy with review), on two product sites into a fake
Airtable table; then a run writes both pages with zero AI."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

from demo_apps.fake_airtable.app import create_app
from runner.store import Store, workspace_dir
from tests.agents.teach_examples_helpers import (PRODUCT_FIELDS, PRODUCT_LABELS, TOKEN, airtable_destination,
                                                 product_pages, write_teach_draft)
from tests.agents.test_deep_agent import check_events, wait_for
from tests.conftest import _Server

NAME = "track_product_prices"


@pytest.fixture
def stack(workspace, monkeypatch):
    from fixtures.products.pages import page_record
    from tests.engine.fake_inbox import create_app as inbox_app
    monkeypatch.setenv("AIRTABLE_TOKEN", TOKEN)  # the control plane's .env; runs get PATCHWORK_CRED__AIRTABLE_TOKEN
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")
    records = [page_record("shop-a.html", 1), page_record("shop-b.html", 2)]
    with _Server(create_app(fields=PRODUCT_FIELDS)) as fake, _Server(inbox_app(records=records)) as inbox:
        monkeypatch.setenv("PATCHWORK_AIRTABLE_API_URL", fake.url + "/v0")
        monkeypatch.setenv("PATCHWORK_CONTROL_URL", inbox.url)
        rid, pids = write_teach_draft(NAME, product_pages(), airtable_destination(), "Track product prices")
        yield {"recording_id": rid, "page_ids": pids, "airtable": fake.url}


def test_scripted_examples_teach_into_airtable(stack):
    from agents.scripted import ScriptedAgentService
    from control import ops
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_build(stack["recording_id"], NAME)

    # Store: nothing on the page clearly fills it, so the code asks (checked alternatives + skip)
    s = wait_for(svc, tid, "waiting")
    p = s["pending"]
    assert p["type"] == "ask_user" and "'Store'" in p["question"], p
    assert p["options"][-1] == "Skip this field" and len(p["options"]) >= 2
    svc.resume(tid, p["interrupt_id"], {"answer": "Skip this field"})

    s = wait_for(svc, tid, "waiting")
    p = s["pending"]
    assert p["type"] == "request_deploy" and p["version"] == 1 and p["tests"]["passed"], p
    rows = {r["dest_field"]: r for r in p["review"]}
    assert list(rows) == PRODUCT_LABELS
    pids = stack["page_ids"]
    assert rows["Price"]["values"] == {pids[0]: 24.99, pids[1]: 89} and rows["Price"]["ai_proposed"] is True
    assert rows["Store"]["source"]["skip"] is True and rows["Store"]["status"] == "checked"  # a person chose it
    assert rows["Store"]["ai_proposed"] is False
    svc.resume(tid, p["interrupt_id"], {"decision": "approve"})
    wait_for(svc, tid, "done")
    assert Store().active_version(NAME) == 1

    spec = yaml.safe_load((workspace_dir() / "integrations" / NAME / "v1" / "spec.yaml").read_text())
    assert {x["match"]["site"] for x in spec["extract"]["parsers"]} == {"shop-a.example.com",
                                                                         "lumen-supply.example.org"}
    ev = check_events(svc, tid)
    assert [e["name"] for e in ev if e["type"] == "subagent_start"] == ["mapper", "learner", "builder"]
    calls = [(e["agent"], e["tool"]) for e in ev if e["type"] == "tool_call"]
    assert ("mapper", "propose_mapping") in calls and ("learner", "add_site_parser") in calls
    assert ("builder", "compile_spec") in calls and calls[-1] == ("supervisor", "request_deploy")
    assert not any(t in ("save_parser", "draft_spec") for _, t in calls)
    result = next(e for e in ev if e["type"] == "done")["result"]
    assert result["model_calls"] == 0 and result["deployed"] is True
    assert svc.status(tid)["integration"] == NAME

    summary = ops.run_sandboxed(NAME)
    assert summary["status"] == "completed" and summary["counts"]["success"] == 2, summary
    assert summary["llm_calls"] == 0  # ai_calls == 0: the integration runs with zero AI
    records = httpx.get(stack["airtable"] + "/admin/records").json()
    assert sorted(r["Price"] for r in records) == [24.99, 89.0]
    assert {r["Product"] for r in records} and all("Store" not in r for r in records)
    assert TOKEN not in json.dumps(svc.events(tid))

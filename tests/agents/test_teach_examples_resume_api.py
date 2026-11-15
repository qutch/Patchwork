"""POST /api/agent/{tid}/resume with review edits (TEACH from example pages): a refused edit is answered with 422
naming the field and the reason, before the agent thread sees it, so the deploy request stays pending and the
person can fix the edit and approve again. No model, nothing deployed until a good edit."""

from __future__ import annotations

import time

import httpx
import pytest
import yaml

from demo_apps.fake_airtable.app import create_app
from runner.store import Store, workspace_dir
from tests.agents.teach_examples_helpers import (PRODUCT_FIELDS, TOKEN, airtable_destination, product_pages,
                                                 write_teach_draft)
from tests.conftest import _Server

NAME = "track_product_prices"


@pytest.fixture
def api(workspace, monkeypatch):
    from agents.scripted import ScriptedAgentService
    from control.api import create_app as control_app
    monkeypatch.setenv("AIRTABLE_TOKEN", TOKEN)
    monkeypatch.delenv("PATCHWORK_AI_ENABLED", raising=False)
    agent = ScriptedAgentService(pace_s=0)
    with _Server(create_app(fields=PRODUCT_FIELDS)) as fake, \
            _Server(control_app(agent=agent, dotenv=False)) as srv, httpx.Client(base_url=srv.url, timeout=60) as c:
        monkeypatch.setenv("PATCHWORK_AIRTABLE_API_URL", fake.url + "/v0")
        rid, _ = write_teach_draft(NAME, product_pages(), airtable_destination(required=("Product",)),
                                   "Track product prices")
        yield c, agent, rid


def wait(c, tid, pending_type=None, states=("waiting",), timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = c.get(f"/api/agent/{tid}").json()
        assert s["state"] != "failed", c.get(f"/api/agent/{tid}/events").text[-1500:]
        if s["state"] in states and (pending_type is None or (s.get("pending") or {}).get("type") == pending_type):
            return s
        time.sleep(0.05)
    raise AssertionError(f"timeout: {s}")


def test_a_refused_review_edit_is_422_and_the_review_stays_open(api):
    c, agent, rid = api
    tid = agent.start_build(rid, NAME)
    s = wait(c, tid, "ask_user")
    c.post(f"/api/agent/{tid}/resume", json={"interrupt_id": s["pending"]["interrupt_id"], "answer": "Skip this field"})
    s = wait(c, tid, "request_deploy")
    p = s["pending"]
    rows = {r["dest_field"]: r for r in p["review"]}
    url = f"/api/agent/{tid}/resume"

    bad = [({"dest_field": "Product", "choice": {"skip": True}}, "Product is required"),
           ({"dest_field": "Price", "choice": {"const": "cheap"}}, "Price: the constant 'cheap' does not fit"),
           ({"dest_field": "Store", "choice": {"field": "store", "reader": {"css": "body"}}}, "Store: "),
           ({"dest_field": "Nope", "choice": {"skip": True}}, "'Nope' is not a row of the review")]
    for edit, why in bad:
        r = c.post(url, json={"interrupt_id": p["interrupt_id"], "decision": "approve", "edits": [edit]})
        assert r.status_code == 422 and why in r.json()["detail"], (edit, r.text)
        again = c.get(f"/api/agent/{tid}").json()  # the same deploy request is still waiting for the person
        assert again["state"] == "waiting" and again["pending"]["interrupt_id"] == p["interrupt_id"]
    assert Store().active_version(NAME) is None  # nothing deployed

    shown = next(a for a in rows["Store"]["alternatives"] if "reader" in a)  # echoed as shown, label and values too
    r = c.post(url, json={"interrupt_id": p["interrupt_id"], "decision": "approve",
                          "edits": [{"dest_field": "Store", "choice": shown}]})
    assert r.status_code == 200, r.text
    done = wait(c, tid, states=("done",))
    assert done["state"] == "done" and Store().active_version(NAME) == 2
    spec = yaml.safe_load((workspace_dir() / "integrations" / NAME / "v2" / "spec.yaml").read_text())
    assert spec["sink"]["fields"]["Store"]["from"] == shown["field"]
    result = next(e for e in agent.events(tid) if e["type"] == "done")["result"]
    assert result["model_calls"] == 0 and result["deployed"] is True

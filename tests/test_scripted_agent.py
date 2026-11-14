"""The scripted agent drives the real BUILD -> approve -> run -> Break 1 -> REPAIR -> approve flow."""

import time

import httpx

from agents.scripted import ScriptedAgentService
from control import ops
from runner.store import Store


def wait_for(svc, tid, state, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = svc.status(tid)
        if s["state"] == state:
            return s
        if s["state"] == "failed":
            raise AssertionError(svc.events(tid)[-3:])
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {state}: {svc.status(tid)}")


def test_build_then_repair(apps, workspace):
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_build("fixture")
    s = wait_for(svc, tid, "waiting")
    assert s["pending"]["type"] == "ask_user"
    svc.resume(tid, s["pending"]["interrupt_id"], {"answer": "Quarantine them for review"})
    s = wait_for(svc, tid, "waiting")
    assert s["pending"]["type"] == "request_deploy" and s["pending"]["tests"]["passed"]
    svc.resume(tid, s["pending"]["interrupt_id"], {"decision": "approve"})
    wait_for(svc, tid, "done")
    types = [e["type"] for e in svc.events(tid)]
    assert {"todos", "subagent_start", "tool_call", "tool_result", "deployed", "done"} <= set(types)
    assert [e["seq"] for e in svc.events(tid)] == list(range(1, len(types) + 1))

    assert ops.run_sandboxed("po_to_stockroom")["status"] == "completed"
    httpx.post(apps["live"] + "/admin/schema", json={"version": 2})
    httpx.post(apps["staging"] + "/admin/schema", json={"version": 2})
    blocked = ops.run_sandboxed("po_to_stockroom")
    assert blocked["status"] == "blocked"

    rid = svc.start_repair(blocked["incident_id"])
    s = wait_for(svc, rid, "waiting")
    svc.resume(rid, s["pending"]["interrupt_id"], {"answer": "By supplier (acme→BOS, globex→NYC, initech→BOS)"})
    s = wait_for(svc, rid, "waiting")
    assert s["pending"]["version"] == 2 and "warehouse_id" in s["pending"]["spec_diff"]
    svc.resume(rid, s["pending"]["interrupt_id"], {"decision": "approve"})
    wait_for(svc, rid, "done")
    assert Store().active_version("po_to_stockroom") == 2
    # v2 runs clean against the v2 contract (nothing new in the inbox, but preflight passes)
    assert ops.run_sandboxed("po_to_stockroom")["status"] == "completed"


def _repair(svc, incident_id, answers):
    rid = svc.start_repair(incident_id)
    asked = []
    while True:
        s = wait_for(svc, rid, "waiting")
        p = s["pending"]
        if p["type"] == "request_deploy":
            return rid, p, asked
        asked.append(p["question"])
        svc.resume(rid, p["interrupt_id"], {"answer": answers(p)})


def test_repair_new_supplier_format_after_contract_repair(apps, workspace):
    """Break 2 on top of v2: the agent adds an umbrella parser (units = cases x per case),
    maps its warehouse, deploys v3 and replays; the bench scores every record correct."""
    from bench.accuracy import score

    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_build("fixture")
    s = wait_for(svc, tid, "waiting")
    svc.resume(tid, s["pending"]["interrupt_id"], {"answer": "Quarantine them for review"})
    s = wait_for(svc, tid, "waiting")
    svc.resume(tid, s["pending"]["interrupt_id"], {"decision": "approve"})
    wait_for(svc, tid, "done")
    assert ops.run_sandboxed("po_to_stockroom")["status"] == "completed"

    for app in ("live", "staging"):
        httpx.post(apps[app] + "/admin/schema", json={"version": 2})
    blocked = ops.run_sandboxed("po_to_stockroom")
    rid, p, _ = _repair(svc, blocked["incident_id"], lambda p: "By supplier (acme→BOS, globex→NYC, initech→BOS)")
    svc.resume(rid, p["interrupt_id"], {"decision": "approve"})
    wait_for(svc, rid, "done")

    httpx.post(apps["inbox"] + "/admin/new_supplier", json={"count": 4}).raise_for_status()
    run = ops.run_sandboxed("po_to_stockroom")
    assert run["status"] == "completed" and run["incident_id"], run
    inc = Store().incident(run["incident_id"])
    assert inc["type"] == "NEW_SOURCE_FORMAT"

    rid, p, asked = _repair(svc, inc["id"], lambda p: p["options"][0])
    assert any("case" in q for q in asked) and any("warehouse" in q.lower() for q in asked), asked
    assert p["version"] == 3 and p["tests"]["passed"]
    assert "umbrella_v1" in p["spec_diff"] and "combine: product" in p["spec_diff"]
    svc.resume(rid, p["interrupt_id"], {"decision": "approve"})
    wait_for(svc, rid, "done")
    assert Store().active_version("po_to_stockroom") == 3
    assert Store().incident(inc["id"])["status"] == "resolved"

    truth = httpx.get(apps["inbox"] + "/admin/ground_truth").json()
    latest = Store().latest_outcomes("po_to_stockroom")
    umbrella = [r for r in latest if truth[r["record_id"]].get("new_supplier")]
    assert len(umbrella) == 4 and all(r["status"] == "success" for r in umbrella), umbrella
    result = score(latest, truth)
    assert result["accuracy"] == 1.0, result["mismatches"]


def test_repair_refuses_incident_types_it_has_no_script_for(apps, workspace):
    svc = ScriptedAgentService(pace_s=0)
    store = Store()
    iid = store.open_incident(name="po_to_stockroom", version=1, run_id=None, type="AUTH", reason_codes=["AUTH"],
                              summary="401 from the sink", samples=[])
    rid = svc.start_repair(iid)
    deadline = time.time() + 10
    while svc.status(rid)["state"] != "failed" and time.time() < deadline:
        time.sleep(0.05)
    err = [e for e in svc.events(rid) if e["type"] == "error"]
    assert err and "AUTH" in err[0]["message"]

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

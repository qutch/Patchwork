"""Real-model runs of DeepAgentService (opt in: `pytest -m live tests/agents -s`).

Needs OPENROUTER_API_KEY (and optionally PATCHWORK_MODEL / PATCHWORK_MODEL_FALLBACK) in the environment.
Same scenario as tests/test_scripted_agent.py: BUILD -> approve -> run -> Break 1 -> REPAIR -> approve.
A human stand-in answers questions: warehouse questions get the by-supplier answer, others the first option.
"""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest

from control import ops
from runner.store import Store

pytestmark = pytest.mark.live
BY_SUPPLIER = "By supplier (acme→BOS, globex→NYC, initech→BOS)"


def drive(svc, tid, timeout=1500):
    """Answer interrupts until the thread ends; approve deploys whose tests passed."""
    deadline, answered = time.time() + timeout, set()
    while time.time() < deadline:
        s = svc.status(tid)
        if s["state"] in ("done", "failed"):
            return s
        p = s.get("pending")
        if s["state"] == "waiting" and p and p["interrupt_id"] not in answered:
            answered.add(p["interrupt_id"])
            if p["type"] == "ask_user":
                q = p["question"].lower()
                opts = p.get("options") or []
                ans = BY_SUPPLIER if ("warehouse" in q or "supplier" in q) else \
                    next((o for o in opts if "quarantine" in o.lower()), opts[0] if opts else "Yes")
                print(f"\n  ASK {p['question']!r} -> {ans!r}", flush=True)
                svc.resume(tid, p["interrupt_id"], {"answer": ans})
            else:
                ok = bool(p["tests"]["passed"])
                print(f"\n  DEPLOY v{p['version']} tests={p['tests']['summary']!r} -> {'approve' if ok else 'reject'}", flush=True)
                svc.resume(tid, p["interrupt_id"], {"decision": "approve" if ok else "reject", "comment": ""})
        time.sleep(0.2)
    raise AssertionError(f"timeout: {svc.status(tid)}")


def report(svc, tid, label):
    evs = svc.events(tid)
    end = next((e for e in reversed(evs) if e["type"] in ("done", "error")), {})
    print(f"\n[{label}] {len(evs)} events; result={json.dumps(end.get('result') or end.get('message'))}")
    for e in evs:
        if e["type"] in ("subagent_start", "tool_call", "tool_result", "ask_user", "deployed", "error"):
            brief = {k: v for k, v in e.items() if k not in ("seq", "t", "thread_id", "type")}
            print(f"  {e['seq']:>3} {e['type']:<15} {json.dumps(brief, ensure_ascii=False)[:220]}")
    return end


@pytest.fixture
def svc():
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")
    from agents.deep import DeepAgentService
    return DeepAgentService()


def break_stockroom(apps):
    httpx.post(apps["live"] + "/admin/schema", json={"version": 2})
    httpx.post(apps["staging"] + "/admin/schema", json={"version": 2})
    blocked = ops.run_sandboxed("po_to_stockroom")
    assert blocked["status"] == "blocked"
    return blocked["incident_id"]


def repair_and_check(svc, apps):
    t0 = time.time()
    rid = svc.start_repair(break_stockroom(apps))
    s = drive(svc, rid)
    end = report(svc, rid, f"REPAIR {time.time() - t0:.0f}s")
    assert s["state"] == "done", end
    assert end["result"]["deployed"] is True
    v = Store().active_version("po_to_stockroom")
    assert v == 2
    spec = (ops.version_dir("po_to_stockroom", v) / "spec.yaml").read_text() if hasattr(ops, "version_dir") else ""
    assert "warehouse_id" in spec and "lookup" in spec
    assert ops.run_sandboxed("po_to_stockroom")["status"] == "completed"


def test_live_build_then_repair(apps, workspace, svc):
    t0 = time.time()
    tid = svc.start_build("fixture")
    s = drive(svc, tid)
    end = report(svc, tid, f"BUILD {time.time() - t0:.0f}s")
    assert s["state"] == "done", end
    assert end["result"]["deployed"] is True
    run = ops.run_sandboxed("po_to_stockroom")
    print(f"\n  first live run: {run['status']} {run['counts']}")
    assert run["status"] == "completed" and run["counts"].get("success", 0) >= 100
    repair_and_check(svc, apps)


def test_live_repair_only(apps, workspace, svc):
    """REPAIR on Break 1 starting from a v1 built without a model (cheaper to iterate on)."""
    from agents.scripted import ScriptedAgentService
    sc = ScriptedAgentService(pace_s=0)
    tid = sc.start_build("fixture")
    for _ in range(2):
        while sc.status(tid)["state"] != "waiting":
            time.sleep(0.05)
        p = sc.status(tid)["pending"]
        sc.resume(tid, p["interrupt_id"], {"answer": "Quarantine them for review"} if p["type"] == "ask_user"
                  else {"decision": "approve"})
    while sc.status(tid)["state"] != "done":
        time.sleep(0.05)
    assert ops.run_sandboxed("po_to_stockroom")["status"] == "completed"
    repair_and_check(svc, apps)

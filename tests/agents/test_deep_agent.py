"""DeepAgentService driven by a scripted tool-calling model (offline): the same BUILD -> approve ->
run -> Break 1 -> REPAIR -> approve scenario as tests/test_scripted_agent.py, plus event mapping."""

from __future__ import annotations

import time

import httpx
import pytest

from agents.interface import EVENT_TYPES
from control import ops
from runner.store import Store

# langchain/deepagents are imported lazily, inside the tests: importing them at collection time would put
# AI modules in the pytest process before tests that run the runner in-process (which then refuses).


def deps():
    from agents.deep import DeepAgentService
    from tests.agents.fake_model import ScriptedChatModel, call, turn
    from tests.agents.scripts import build_script, repair_script
    return DeepAgentService, ScriptedChatModel, call, turn, build_script, repair_script


def wait_for(svc, tid, state, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = svc.status(tid)
        if s["state"] == state:
            return s
        if s["state"] in ("failed", "done") and state not in ("failed", "done"):
            raise AssertionError([e for e in svc.events(tid)][-6:])
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {state}: {svc.status(tid)}")


def check_events(svc, tid):
    evs = svc.events(tid)
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    for e in evs:
        assert e["type"] in EVENT_TYPES
        missing = [k for k in EVENT_TYPES[e["type"]] if k not in e]
        assert not missing, (e, missing)
    return evs


def test_build_then_repair(apps, workspace):
    DeepAgentService, ScriptedChatModel, call, turn, build_script, repair_script = deps()
    model = ScriptedChatModel(build_script())
    svc = DeepAgentService(model=model)
    tid = svc.start_build("fixture")
    s = wait_for(svc, tid, "waiting")
    assert s["pending"]["type"] == "ask_user" and "Quarantine them for review" in s["pending"]["options"]
    svc.resume(tid, s["pending"]["interrupt_id"], {"answer": "Quarantine them for review"})
    s = wait_for(svc, tid, "waiting")
    assert s["pending"]["type"] == "request_deploy" and s["pending"]["tests"]["passed"]
    assert s["pending"]["version"] == 1 and "globex_v1" in s["pending"]["spec_diff"]
    svc.resume(tid, s["pending"]["interrupt_id"], {"decision": "approve"})
    done = wait_for(svc, tid, "done")
    assert model.remaining == 0

    evs = check_events(svc, tid)
    types = [e["type"] for e in evs]
    assert {"todos", "subagent_start", "subagent_end", "tool_call", "tool_result", "message", "artifact",
            "ask_user", "resumed", "request_deploy", "deployed", "done"} <= set(types)
    starts = [e["name"] for e in evs if e["type"] == "subagent_start"]
    assert starts == ["interpreter", "builder", "tester"]
    by_tool = {(e["agent"], e["tool"]) for e in evs if e["type"] == "tool_call"}
    assert {("interpreter", "draft_spec"), ("builder", "compile_spec"), ("tester", "dry_run"),
            ("supervisor", "request_deploy")} <= by_tool
    results = {e["tool"]: e for e in evs if e["type"] == "tool_result"}
    assert results["compile_spec"]["ok"] and results["validate_spec"]["ok"]
    first_test = next(e for e in evs if e["type"] == "tool_result" and e["tool"] == "test_parser")
    assert not first_test["ok"] and "NO PARSER" in first_test["summary"]  # acme-only draft misses 2 senders
    assert evs[-2]["type"] == "done" and evs[-2]["result"]["deployed"] is True
    assert evs[-2]["result"]["model_calls"] == len(build_script())
    assert done["state"] == "done"

    # the deployed integration runs for real, then Break 1 blocks it
    assert ops.run_sandboxed("po_to_stockroom")["status"] == "completed"
    httpx.post(apps["live"] + "/admin/schema", json={"version": 2})
    httpx.post(apps["staging"] + "/admin/schema", json={"version": 2})
    blocked = ops.run_sandboxed("po_to_stockroom")
    assert blocked["status"] == "blocked"

    model2 = ScriptedChatModel(repair_script())
    svc2 = DeepAgentService(model=model2)
    rid = svc2.start_repair(blocked["incident_id"])
    s = wait_for(svc2, rid, "waiting")
    assert s["pending"]["type"] == "ask_user" and "by supplier" in s["pending"]["question"]
    svc2.resume(rid, s["pending"]["interrupt_id"], {"answer": "By supplier (acme→BOS, globex→NYC, initech→BOS)"})
    s = wait_for(svc2, rid, "waiting")
    assert s["pending"]["type"] == "request_deploy"
    assert s["pending"]["version"] == 2 and "warehouse_id" in s["pending"]["spec_diff"]
    patch_ops = s["pending"]["spec_patch"]
    assert any("/sink/body/product_id" in (op.get("from"), op.get("path")) for op in patch_ops)
    assert any(op["path"] == "/sink/body/warehouse_id" for op in patch_ops)
    svc2.resume(rid, s["pending"]["interrupt_id"], {"decision": "approve"})
    wait_for(svc2, rid, "done")
    check_events(svc2, rid)
    diff = next(e for e in svc2.events(rid) if e["type"] == "tool_result" and e["tool"] == "diff_openapi")
    assert '"product_id": "sku"' in diff["summary"] and '"quantity": "quantity_delta"' in diff["summary"]
    assert Store().active_version("po_to_stockroom") == 2
    assert ops.run_sandboxed("po_to_stockroom")["status"] == "completed"


def test_tools_offered_per_agent(apps, workspace):
    DeepAgentService, ScriptedChatModel, call, turn, build_script, repair_script = deps()
    """Built-in tools are trimmed (no execute/glob/grep/delete), no general-purpose subagent, and each
    subagent is bound only to its own tools."""
    model = ScriptedChatModel([turn(call("task", subagent_type="builder", description="compile")),
                               turn(text="nothing to compile"), turn(text="stop")])
    svc = DeepAgentService(model=model, max_nudges=0)
    tid = svc.start_build("fixture")
    wait_for(svc, tid, "failed")  # finishing without a deploy request is an error, by design
    sup, builder = model._bound[0], model._bound[1]
    assert {"task", "write_todos", "ask_user", "request_deploy", "read_file", "edit_file"} <= set(sup)
    assert not {"execute", "glob", "grep", "delete", "compile_spec"} & set(sup)
    assert {"compile_spec", "validate_spec", "run_tests"} <= set(builder)
    assert not {"request_deploy", "task", "propose_spec_patch"} & set(builder)
    task_desc = model._descriptions["task"]
    assert "- repairer:" in task_desc and "- general-purpose:" not in task_desc
    err = [e for e in svc.events(tid) if e["type"] == "error"]
    assert err and "without requesting a deploy" in err[0]["message"]


@pytest.mark.parametrize("path,allowed", [("/transforms.py", True), ("/integration.py", False), ("/v1/parsers.py", False)])
def test_filesystem_only_spec_files_writable(apps, workspace, path, allowed):
    DeepAgentService, ScriptedChatModel, call, turn, build_script, repair_script = deps()
    model = ScriptedChatModel([
        turn(call("write_file", file_path=path, content="def f(x):\n    return x\n")),
        turn(text="done"),
    ])
    svc = DeepAgentService(model=model, max_nudges=0)
    tid = svc.start_build("fixture")
    wait_for(svc, tid, "failed")
    res = next(e for e in svc.events(tid) if e["type"] == "tool_result" and e["tool"] == "write_file")
    assert res["ok"] is allowed, res
    from agents.scripted import work_dir
    assert (work_dir() / path.lstrip("/")).exists() is allowed


def test_reset_stops_a_thread_waiting_on_ask_user(apps, workspace):
    DeepAgentService, ScriptedChatModel, call, turn, build_script, repair_script = deps()
    model = ScriptedChatModel([turn(call("ask_user", question="Which?", options=["a", "b"])),
                               turn(call("draft_spec")), turn(text="never")])
    svc = DeepAgentService(model=model, max_nudges=0)
    tid = svc.start_build("fixture")
    wait_for(svc, tid, "waiting")
    worker = svc._threads[tid].worker
    n_events = len(svc.events(tid))

    out = svc.reset(timeout=5)

    assert out == {"cancelled": [tid], "still_running": []}
    assert not worker.is_alive()
    time.sleep(0.3)
    assert len(svc.events(tid)) == n_events  # nothing written after the reset (read back from JSONL)
    assert model.remaining == 2  # no further model call
    from agents.scripted import work_dir
    assert not (work_dir() / "spec.yaml").exists()  # the next tool (draft_spec) never ran

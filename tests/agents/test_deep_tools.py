"""Guardrails live in the tools, so test them directly (no model at all)."""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import yaml
from fastapi.openapi.utils import get_openapi

from runner.store import Store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def make_ctx(mode="build", incident_id=None):
    # lazy: keep langchain out of the pytest process until these tests run (see conftest)
    from agents.deep import DeepAgentService
    from agents.deep_tools import ToolContext, build_tools
    from agents.interface import _Thread
    from agents.scripted import work_dir
    svc = DeepAgentService(model=object())  # never called
    t = _Thread(id=f"{mode}-test", mode=mode, subject="x")
    svc._threads[t.id] = t
    ctx = ToolContext(svc, t, incident_id=incident_id)
    wd = work_dir()
    for f in ("spec.yaml", "examples.yaml"):
        shutil.copy(FIXTURES / f, wd / f)
    return svc, t, ctx, {name: tool.func for name, tool in build_tools(ctx).items()}


def test_compile_refuses_invalid_spec_and_request_deploy_refuses_failing_version(apps, workspace):
    svc, t, ctx, tools = make_ctx()
    spec = yaml.safe_load(ctx.spec_path.read_text())
    good = ctx.spec_path.read_text()
    spec["sink"]["call"]["method"] = "GET"
    ctx.spec_path.write_text(yaml.safe_dump(spec))
    assert tools["compile_spec"]().startswith("REFUSED: spec is invalid")
    assert Store().latest_version("po_to_stockroom") == 0

    ctx.spec_path.write_text(good)
    ex = yaml.safe_load((ctx.wd / "examples.yaml").read_text())
    ex[0]["expect"]["body"]["quantity"] = 999  # wrong golden body -> failing suite
    (ctx.wd / "examples.yaml").write_text(yaml.safe_dump(ex))
    out = tools["compile_spec"]()
    assert out.startswith("FAILED: v1 compiled but tests failed")
    assert tools["request_deploy"](version=1, summary="x").startswith("REFUSED: v1 has not passed")
    assert tools["request_deploy"](version=7, summary="x").startswith("REFUSED")
    assert t.pending is None and Store().active_version("po_to_stockroom") is None  # never asked, never deployed


def test_propose_spec_patch_is_spec_only_and_validated(apps, workspace):
    svc, t, ctx, tools = make_ctx()
    before = ctx.spec_path.read_text()
    assert tools["propose_spec_patch"](patch=[{"op": "replace", "path": "/sink/call/method", "value": "GET"}]).startswith("REFUSED")
    assert tools["propose_spec_patch"](patch=[{"op": "replace", "path": "/sink/base_url", "value": "http://evil"}]).startswith("REFUSED")
    assert tools["propose_spec_patch"](patch=[{"op": "replace", "path": "/sink/staging_base_url",
                                               "value": "http://localhost:8002"}]).startswith("REFUSED")
    lookup = {"transform": "lookup", "from": "record.sender_domain", "args": {"table": {"acme": "BOS", "globex": "NYC"}}}
    out = tools["propose_spec_patch"](patch=[{"op": "add", "path": "/sink/body/warehouse_id", "value": lookup}])
    assert out.startswith("REFUSED") and "do not cover" in out
    assert tools["propose_spec_patch"](patch=[{"op": "remove", "path": "/nope"}]).startswith("REFUSED")
    assert ctx.spec_path.read_text() == before  # nothing written on refusal
    ok = tools["propose_spec_patch"](patch=[{"op": "add", "path": "/assumptions/-",
                                             "value": {"id": "a9", "text": "t", "status": "confirmed"}}])
    assert ok.startswith("applied")


def test_dry_run_never_targets_live(apps, workspace, monkeypatch):
    svc, t, ctx, tools = make_ctx()
    assert tools["dry_run"]().startswith("REFUSED: no compiled version")
    assert tools["compile_spec"]().startswith("compiled v1")
    monkeypatch.setenv("PATCHWORK_STAGING_BASE_URL", apps["live"])
    assert tools["dry_run"]().startswith("REFUSED: no separate staging sink")
    monkeypatch.setenv("PATCHWORK_STAGING_BASE_URL", apps["staging"])
    assert "completed" in tools["dry_run"](limit=3)


def test_repair_attempts_capped_then_escalated(apps, workspace):
    svc, t, ctx, tools = make_ctx(mode="repair")
    ctx.failed_attempts = 3
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", tools["compile_spec"]()))
    th.start()
    deadline = time.time() + 10
    while t.pending is None and time.time() < deadline:
        time.sleep(0.02)
    assert t.pending["type"] == "ask_user" and "3 repair attempts" in t.pending["question"]
    svc.resume(t.id, t.pending["interrupt_id"], {"answer": "Stop the repair; I'll take it from here"})
    th.join(10)
    assert out["r"].startswith("REFUSED: user stopped")
    assert tools["compile_spec"]().startswith("REFUSED")
    assert Store().latest_version("po_to_stockroom") == 0


def test_diff_openapi_maps_renames_and_suggests_the_question(apps, workspace):
    from demo_apps.stockroom.app import _schema_router
    docs = {v: get_openapi(title="Stockroom", version=f"{v}.0.0", routes=_schema_router(v).routes) for v in (1, 2)}
    inc = Store().open_incident(name="po_to_stockroom", version=1, run_id=None, type="CONTRACT_CHANGED",
                                reason_codes=["SINK_CONTRACT"], summary="changed", old_openapi=docs[1], new_openapi=docs[2])
    svc, t, ctx, tools = make_ctx(mode="repair", incident_id=inc)
    import json
    d = json.loads(tools["diff_openapi"]())
    assert d["likely_renames"] == {"product_id": "sku", "quantity": "quantity_delta"}
    assert list(d["new_required_unmapped"]) == ["warehouse_id"]
    q = d["suggested_questions"][0]
    assert q["question"] == "Stockroom now requires warehouse_id. Always BOS, or by supplier?"
    assert q["options"][0] == "Always BOS"
    assert d["routed_values"] == {"sender_domain": ["acme.com", "globex.com", "initech.com"]}


def _ask_ctx(monkeypatch, fields):
    """ask_user with a stubbed destination: fields = {label: {type, required}}."""
    import agents.deep_tools as dt
    svc, t, ctx, tools = make_ctx()
    monkeypatch.setattr(dt, "examples_dest_fields", lambda c, k: fields)
    monkeypatch.setattr(ctx, "examples_mode", lambda: True)
    monkeypatch.setattr(ctx, "dest_contract", lambda: {})
    return svc, t, ctx, tools


def _refused_without_asking(t, ctx, tools, options, field):
    out = tools["ask_user"]("What should it be?", options, field)
    assert out.startswith("REFUSED:")
    assert t.pending is None and ctx.answers == {}
    return out


def _ask_in_thread(t, tools, options, field):
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", tools["ask_user"]("Which?", options, field)))
    th.start()
    deadline = time.time() + 10
    while t.pending is None and time.time() < deadline:
        time.sleep(0.02)
    return th, out


def test_ask_user_refuses_zero_option_for_number_field(monkeypatch, apps, workspace):
    svc, t, ctx, tools = _ask_ctx(monkeypatch, {"Price": {"type": "number", "required": False}})
    for opts in (["0", "12.50"], ["$0.00"], ["0,0"]):
        out = _refused_without_asking(t, ctx, tools, opts, "Price")
        assert "Price" in out and "Leave it for me" in out


def test_ask_user_refuses_empty_option_for_required_text_field(monkeypatch, apps, workspace):
    svc, t, ctx, tools = _ask_ctx(monkeypatch, {"Name": {"type": "text", "required": True}})
    _refused_without_asking(t, ctx, tools, ["Acme", "  "], "Name")


def test_ask_user_allows_zero_for_optional_text_field(monkeypatch, apps, workspace):
    svc, t, ctx, tools = _ask_ctx(monkeypatch, {"Note": {"type": "text", "required": False}})
    th, out = _ask_in_thread(t, tools, ["0", "none"], "Note")
    assert t.pending and t.pending["type"] == "ask_user"
    svc.resume(t.id, t.pending["interrupt_id"], {"answer": "0"})
    th.join(10)
    assert out["r"] == "user answered: 0"


def test_ask_user_real_options_interrupt_and_free_text_zero_is_accepted(monkeypatch, apps, workspace):
    svc, t, ctx, tools = _ask_ctx(monkeypatch, {"Price": {"type": "number", "required": True}})
    th, out = _ask_in_thread(t, tools, ["12.50", "Leave it for me to fill in at review"], "Price")
    assert t.pending and t.pending["type"] == "ask_user"
    svc.resume(t.id, t.pending["interrupt_id"], {"answer": "0"})  # the person's own free-text choice
    th.join(10)
    assert out["r"] == "user answered: 0" and ctx.answers == {"Price": "0"}


def test_ask_user_without_dest_field_checks_the_field_named_in_the_question(monkeypatch, apps, workspace):
    svc, t, ctx, tools = _ask_ctx(monkeypatch, {"Price": {"type": "number", "required": False},
                                                "Note": {"type": "text", "required": False}})
    out = tools["ask_user"]("What should Price be?", ["0", "Leave it for me to fill in at review"])
    assert out.startswith("REFUSED:") and "Price" in out
    assert t.pending is None and ctx.answers == {}

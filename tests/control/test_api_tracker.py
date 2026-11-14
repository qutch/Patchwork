"""W4a-3: learn trigger, replay after deploy, the tracker and the learned layouts (docs/contracts.md V9)."""

import threading
import time

import pytest

from control import ops, tracker
from control.layouts import LayoutError, revert_parser
from control.pages import PageInbox
from runner.pipeline import Outcome
from runner.store import Store
from tests.conftest import FIXTURE_SPEC
from tests.control.v2_fakes import (GREENHOUSE, LEVER, NAME, fake_runner, gate, make_page_integration,  # noqa: F401
                                    page_spec, post_page, records_by_page, v2agent, v2api, wait_idle)


def _wait(cond, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("timeout")


def _rows(api):
    return {r["page_id"]: r for r in api.get("/api/tracker").json()["rows"]}


def test_unknown_layout_starts_one_learn_per_site(v2api):
    make_page_integration([LEVER])
    agent = v2api.agent
    agent.hold, agent.learn = threading.Event(), False  # learn threads stay running, then fail
    post_page(v2api, "greenhouse-1.html")
    wait_idle(v2api)
    post_page(v2api, "greenhouse-2.html")
    post_page(v2api, "ashby-1.html")
    post_page(v2api, "lever-1.html")
    wait_idle(v2api)
    assert agent.learn_calls == [("pg-000001", NAME), ("pg-000003", NAME)]  # one per site, never for known layouts
    rows = _rows(v2api)
    assert rows["pg-000001"]["status"] == "learning" and rows["pg-000001"]["learn_thread_id"].startswith("learn-")
    assert rows["pg-000002"]["status"] == "held" and rows["pg-000002"]["learn_thread_id"] is None
    assert rows["pg-000004"]["label"] == "code"
    agent.hold.set()  # the model is down: learns fail, rows stay held, nothing is written
    _wait(lambda: all(agent.status(t)["state"] == "failed" for t in agent._threads))
    rows = _rows(v2api)
    assert [rows[p]["label"] for p in ("pg-000001", "pg-000002", "pg-000003")] == ["held"] * 3
    assert all(rows[p]["reason"] == "PARSE_NO_MATCH" for p in ("pg-000001", "pg-000002", "pg-000003"))


def test_unknown_layout_with_ai_off_is_held(v2api):
    make_page_integration([LEVER])
    assert v2api.put("/api/settings", json={"ai_enabled": False}).json()["ai_enabled"] is False
    pid = post_page(v2api, "greenhouse-1.html").json()["id"]
    wait_idle(v2api)
    assert v2api.agent.learn_calls == []
    row = v2api.get(f"/api/pages/{pid}").json()["row"]
    assert row["status"] == "held" and row["label"] == "held" and row["reason"] == "PARSE_NO_MATCH"
    assert row["learn_thread_id"] is None and row["ai_calls"] == 0
    assert v2api.get("/api/tracker").json()["totals"]["held"] == 1
    # switching AI on and replaying learns it
    v2api.put("/api/settings", json={"ai_enabled": True})
    v2api.post(f"/api/integrations/{NAME}/replay").raise_for_status()
    _wait(lambda: v2api.get(f"/api/pages/{pid}").json()["row"]["label"] == "learned_now")
    assert v2api.agent.learn_calls == [(pid, NAME)]


def test_deployed_event_replays_then_runs(v2api):
    make_page_integration([LEVER])
    pid = post_page(v2api, "greenhouse-1.html").json()["id"]
    _wait(lambda: len(Store().runs(NAME)) >= 3)
    wait_idle(v2api)
    runs = list(reversed(Store().runs(NAME)))
    assert [r["kind"] for r in runs] == ["run", "replay", "run"]
    assert runs[0]["version"] == 1 and runs[1]["version"] == 2 and runs[2]["version"] == 2
    recs = records_by_page()[pid]
    assert [(r["kind"], r["status"]) for r in recs] == [("run", "quarantined"), ("replay", "success")]
    row = v2api.get(f"/api/pages/{pid}").json()["row"]
    assert row["label"] == "learned_now" and row["ai_calls"] == 5 and row["version"] == 2
    assert row["parser"] == "job_boards_learned" and row["parser_origin"]["thread_id"] == row["learn_thread_id"]
    # the second posting of the learned platform is plain code, 0 AI calls
    pid2 = post_page(v2api, "greenhouse-2.html").json()["id"]
    wait_idle(v2api)
    row2 = v2api.get(f"/api/pages/{pid2}").json()["row"]
    assert row2["label"] == "code" and row2["ai_calls"] == 0 and row2["parser"] == "job_boards_learned"
    assert v2api.agent.learn_calls == [(pid, NAME)]
    totals = v2api.get("/api/tracker").json()["totals"]
    assert totals == {"pages": 2, "ai_calls": 5, "code": 1, "learned_now": 1, "held": 0, "duplicate": 0}


class FakeThreads:
    """Just enough of AgentService for the tracker: status + events of known threads."""

    def __init__(self, threads):
        self.threads = threads

    def status(self, tid):
        return {"thread_id": tid, "state": self.threads[tid]["state"]}

    def events(self, tid, after=0):
        t = self.threads[tid]
        return [{"seq": 1, "type": "done", "result": {"model_calls": t["calls"]}}] if t["state"] == "done" else []


def test_labels(workspace):
    learned = {**GREENHOUSE, "id": "gh_learned",
               "origin": {"kind": "learned", "at": "2026-11-14T15:00:00+00:00", "thread_id": "learn-aaa",
                          "approved_by": "auto"}}
    make_page_integration([LEVER, learned])
    store, inbox = Store(), PageInbox()
    pages = [inbox.add(f"https://jobs.lever.co/p/{i}", f"Page {i}", "<h1>x</h1>") for i in range(6)]
    run = store.start_run(NAME, 1, False)

    def rec(i, status, parser=None, reason=None, body=None):
        store.add_record(run, Outcome(record_id=pages[i]["id"], status=status, parser=parser, reason=reason,
                                      detail="no parser" if reason else None, body=body), {"id": pages[i]["id"]})

    rec(0, "success", "lever", body={"Job Title": "Engineer", "Company": "Palantir", "Email": "demo@example.com"})
    rec(1, "success", "gh_learned", body={"Job Title": "AE", "Company": "Discord"})
    rec(2, "quarantined", reason="PARSE_NO_MATCH")
    rec(3, "duplicate", "lever")
    rec(4, "quarantined", reason="PARSE_NO_MATCH")
    store.finish_run(run, "completed", {}, 0.1)
    inbox.add_learn(pages[1]["id"], "learn-aaa", "job-boards.greenhouse.io", NAME)
    inbox.add_learn(pages[4]["id"], "learn-bbb", "example.org", NAME)
    agent = FakeThreads({"learn-aaa": {"state": "done", "calls": 5}, "learn-bbb": {"state": "running", "calls": 0}})
    # page 5 arrives, then a run is blocked by preflight before fetching it
    blocked = store.start_run(NAME, 1, False)
    inc = store.open_incident(name=NAME, version=1, run_id=blocked, type="CONTRACT_CHANGED",
                              reason_codes=["SINK_CONTRACT"], summary="Destination contract changed")
    store.finish_run(blocked, "blocked", {}, 0.1, inc, "preflight: destination contract changed")

    out = tracker.tracker_rows(store, inbox, agent, NAME, 50)
    rows = {r["page_id"]: r for r in out["rows"]}
    assert [r["seq"] for r in out["rows"]] == [6, 5, 4, 3, 2, 1]  # newest first
    r0, r1, r2, r3, r4, r5 = (rows[p["id"]] for p in pages)
    assert (r0["label"], r0["ai_calls"], r0["title"], r0["company"]) == ("code", 0, "Engineer", "Palantir")
    assert r0["parser"] == "lever" and r0["version"] == 1 and r0["parser_origin"]["kind"] == "fixture"
    assert (r1["label"], r1["ai_calls"], r1["learn_thread_id"]) == ("learned_now", 5, "learn-aaa")
    assert (r2["status"], r2["label"], r2["reason"], r2["title"], r2["company"]) == (
        "held", "held", "PARSE_NO_MATCH", "Page 2", None)
    assert (r3["status"], r3["label"]) == ("duplicate", "duplicate")
    assert (r4["status"], r4["label"], r4["ai_calls"]) == ("learning", None, 0)
    assert (r5["status"], r5["label"], r5["reason"], r5["incident_id"]) == ("held", "held", "CONTRACT_CHANGED", inc)
    assert out["totals"] == {"pages": 6, "ai_calls": 5, "code": 1, "learned_now": 1, "held": 2, "duplicate": 1}
    assert set(r0) == {"page_id", "seq", "captured_at", "site", "url", "title", "company", "status", "label", "reason",
                       "detail", "parser", "version", "routed_by", "parser_origin", "ai_calls", "run_id",
                       "learn_thread_id", "incident_id"}
    assert len(tracker.tracker_rows(store, inbox, agent, NAME, 2)["rows"]) == 2
    no_agent = tracker.page_row(store, inbox, None, NAME, pages[1]["id"])  # the inbox knows the thread; calls unknown
    assert (no_agent["label"], no_agent["ai_calls"]) == ("learned_now", 0)
    assert tracker.page_row(store, inbox, agent, NAME, "pg-999999") is None


def test_layouts_from_manifests(v2api):
    learned = {**GREENHOUSE, "id": "gh_learned",
               "origin": {"kind": "learned", "at": "2026-11-14T15:00:00+00:00", "thread_id": "learn-aaa",
                          "approved_by": "human"}}
    make_page_integration([LEVER])
    make_page_integration([LEVER, learned])
    make_page_integration([learned], deploy=False)  # v3 drops lever, not deployed
    out = v2api.get("/api/layouts", params={"integration": NAME}).json()
    assert [(l["parser_id"], l["active"], l["introduced_in"]) for l in out] == [("lever", True, 1), ("gh_learned", True, 2)]
    gh = out[1]
    assert gh == {"integration": NAME, "parser_id": "gh_learned", "site": "job-boards.greenhouse.io",
                  "fingerprint": GREENHOUSE["fingerprint"], "fields": GREENHOUSE["fields"], "origin": learned["origin"],
                  "introduced_in": 2, "active": True}
    v2api.post(f"/api/integrations/{NAME}/deploy", json={"version": 3}).raise_for_status()
    out = tracker.layouts(Store(), NAME)
    assert [(l["parser_id"], l["active"]) for l in out] == [("gh_learned", True), ("lever", False)]
    assert v2api.get("/api/layouts", params={"integration": "nope"}).json() == []


@pytest.fixture
def v1_compiled(apps):
    built = ops.compile_and_test(FIXTURE_SPEC)
    assert built["tests_passed"], built["tests_summary"]
    ops.deploy("po_to_stockroom", 1)
    return built


def test_revert_parser_compiles_and_deploys(v1_compiled):
    out = revert_parser("po_to_stockroom", "globex_v1")
    assert out["version"] == 2 and out["deployed"] is True and out["previous_version"] == 1
    assert out["dropped_examples"] and all("globex" in n for n in out["dropped_examples"])
    store = Store()
    assert store.active_version("po_to_stockroom") == 2 and store.version("po_to_stockroom", 2)["tests_passed"]
    spec = ops.version_detail("po_to_stockroom", 2)["spec_yaml"]
    assert "globex_v1" not in spec and "acme_v1" in spec and "initech_v1" in spec
    layouts = {l["parser_id"]: l["active"] for l in tracker.layouts(store, "po_to_stockroom")}
    assert layouts == {"acme_v1": True, "initech_v1": True, "globex_v1": False}


def test_revert_last_parser_409(v1_compiled, v2api):
    revert_parser("po_to_stockroom", "globex_v1")
    r = v2api.post("/api/layouts/initech_v1/revert", json={"integration": "po_to_stockroom"})
    assert r.status_code == 200 and r.json()["version"] == 3 and r.json()["deployed"] is True
    r = v2api.post("/api/layouts/acme_v1/revert", json={"integration": "po_to_stockroom"})
    assert r.status_code == 409 and "last parser" in r.json()["detail"]
    assert v2api.post("/api/layouts/nope/revert", json={"integration": "po_to_stockroom"}).status_code == 404
    assert v2api.post("/api/layouts/x/revert", json={"integration": "nope"}).status_code == 404
    assert Store().active_version("po_to_stockroom") == 3
    with pytest.raises(LayoutError):
        revert_parser("po_to_stockroom", "acme_v1")


def test_revert_refuses_when_tests_fail(v1_compiled, monkeypatch):
    real = ops.compile_and_test

    def failing(*a, **kw):
        out = real(*a, **kw)
        Store().set_tests(out["name"], out["version"], False, "1 failed")
        return {**out, "tests_passed": False, "tests_summary": "1 failed"}

    monkeypatch.setattr(ops, "compile_and_test", failing)
    with pytest.raises(LayoutError, match="tests failed"):
        revert_parser("po_to_stockroom", "globex_v1")
    assert Store().active_version("po_to_stockroom") == 1


def test_page_spec_helper_is_a_page_integration(workspace):
    from control.autorun import is_page_spec, page_integrations
    assert is_page_spec(page_spec([LEVER]))
    make_page_integration([LEVER])
    assert page_integrations() == [NAME]

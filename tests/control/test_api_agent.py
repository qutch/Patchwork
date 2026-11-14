"""Settings, AI on/off gating, agent threads, SSE resume-from-seq and resume errors."""

import json

from control import ops
from tests.conftest import FIXTURE_SPEC
from tests.control.conftest import finish_run, read_sse, wait_thread


def test_health_and_settings_persist(api, workspace):
    assert api.get("/api/health").json()["status"] == "ok"
    s = api.get("/api/settings").json()
    assert s == {"ai_enabled": True, "agent": "scripted", "model": "scripted", "learn_policy": "auto"}  # V9 adds learn_policy
    s = api.put("/api/settings", json={"ai_enabled": False}).json()
    assert s["ai_enabled"] is False
    assert api.get("/api/settings").json()["ai_enabled"] is False
    assert json.loads((workspace / "settings.json").read_text())["ai_enabled"] is False
    assert api.put("/api/settings", json={"ai_enabled": "maybe"}).status_code == 422


def test_ai_off_gates_build_and_repair_but_never_runs(api, apps):
    built = ops.compile_and_test(FIXTURE_SPEC)
    ops.deploy("po_to_stockroom", built["version"])
    api.put("/api/settings", json={"ai_enabled": False})

    r = api.post("/api/integrations/build", json={"recording_id": "fixture"})
    assert r.status_code == 409 and "AI is off" in r.json()["detail"]
    assert api.get("/api/agent/threads").json() == []

    run = finish_run(api, api.post("/api/runs", json={"integration": "po_to_stockroom", "limit": 5}).json()["run_id"])
    assert run["status"] == "completed" and run["llm_calls"] == 0

    api.post("/api/demo/stockroom/schema", json={"version": 2})
    blocked = finish_run(api, api.post("/api/runs", json={"integration": "po_to_stockroom"}).json()["run_id"])
    r = api.post(f"/api/incidents/{blocked['incident_id']}/repair")
    assert r.status_code == 409 and "AI is off" in r.json()["detail"]

    api.put("/api/settings", json={"ai_enabled": True})
    assert api.post(f"/api/incidents/{blocked['incident_id']}/repair").status_code == 200
    assert api.post("/api/incidents/999/repair").status_code == 404


def test_agent_stream_resumes_from_seq(api, apps):
    tid = api.post("/api/integrations/build", json={"recording_id": "fixture"}).json()["thread_id"]
    s = wait_thread(api, tid, "waiting")
    all_events = read_sse(api, f"/api/agent/{tid}/stream", until=lambda e: e["event"] == "ask_user")
    assert [e["id"] for e in all_events] == list(range(1, len(all_events) + 1))
    for e in all_events:
        assert e["event"] == e["data"]["type"] and e["id"] == e["data"]["seq"] and e["data"]["thread_id"] == tid

    tail = read_sse(api, f"/api/agent/{tid}/stream?after=5", until=lambda e: e["event"] == "ask_user")
    assert tail[0]["id"] == 6 and tail == all_events[5:]
    tail = read_sse(api, f"/api/agent/{tid}/stream", headers={"Last-Event-ID": "7"},
                    until=lambda e: e["event"] == "ask_user")
    assert tail[0]["id"] == 8 and tail == all_events[7:]

    # a stream on a waiting thread stays open and delivers events as they happen
    api.post(f"/api/agent/{tid}/resume", json={"interrupt_id": s["pending"]["interrupt_id"], "answer": "Skip"})
    more = read_sse(api, f"/api/agent/{tid}/stream?after={s['last_seq']}", until=lambda e: e["event"] == "request_deploy")
    assert more[0]["id"] == s["last_seq"] + 1

    threads = api.get("/api/agent/threads").json()
    assert [t["thread_id"] for t in threads] == [tid] and threads[0]["mode"] == "build"


def test_resume_errors(api, apps):
    assert api.get("/api/agent/nope").status_code == 404
    assert api.get("/api/agent/nope/stream").status_code == 404
    assert api.post("/api/agent/nope/resume", json={"interrupt_id": "x", "answer": "a"}).status_code == 404

    tid = api.post("/api/integrations/build", json={"recording_id": "fixture"}).json()["thread_id"]
    s = wait_thread(api, tid, "waiting")
    iid = s["pending"]["interrupt_id"]
    r = api.post(f"/api/agent/{tid}/resume", json={"interrupt_id": "int-wrong", "answer": "a"})
    assert r.status_code == 409 and "no pending interrupt" in r.json()["detail"]
    assert api.post(f"/api/agent/{tid}/resume", json={"interrupt_id": iid}).status_code == 422
    assert api.post(f"/api/agent/{tid}/resume", json={"interrupt_id": iid, "decision": "maybe"}).status_code == 422

    api.post(f"/api/agent/{tid}/resume", json={"interrupt_id": iid, "answer": "Quarantine them for review"})
    s = wait_thread(api, tid, "waiting")
    assert s["pending"]["type"] == "request_deploy"
    r = api.post(f"/api/agent/{tid}/resume", json={"interrupt_id": s["pending"]["interrupt_id"], "answer": "yes"})
    assert r.status_code == 422, "request_deploy needs a decision"
    r = api.post(f"/api/agent/{tid}/resume", json={"interrupt_id": s["pending"]["interrupt_id"],
                                                   "decision": "reject", "comment": "not yet"})
    assert r.status_code == 200
    done = wait_thread(api, tid, "done")
    assert done["pending"] is None
    assert api.get("/api/integrations/po_to_stockroom").json()["active_version"] is None

    # a finished thread's stream replays everything and closes
    evs = read_sse(api, f"/api/agent/{tid}/stream")
    assert evs[-1]["data"]["state"] == "done" and len(evs) == done["last_seq"]


def test_build_needs_a_finished_recording(api, workspace):
    assert api.post("/api/integrations/build", json={"recording_id": "rec-nope"}).status_code == 404
    (workspace / "recordings" / "rec-live").mkdir(parents=True)
    r = api.post("/api/integrations/build", json={"recording_id": "rec-live"})
    assert r.status_code == 409 and "stop it first" in r.json()["detail"]


def test_cors_allows_the_vite_dev_server(api):
    r = api.options("/api/runs", headers={"Origin": "http://localhost:5180", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://localhost:5180"
    r = api.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers

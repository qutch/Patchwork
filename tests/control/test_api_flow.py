"""The whole demo through the HTTP API only: build (scripted agent) -> ask_user -> approve ->
run 100 with live progress -> accuracy 1.0 -> Break 1 -> run blocked -> repair -> approve ->
v2 active -> replay -> diff, versions, rollback."""

import httpx
import jsonpatch
import yaml

from tests.control.conftest import finish_run, read_sse, wait_thread

NAME = "po_to_stockroom"


def test_full_flow_through_the_api(api, apps, monkeypatch):
    # BUILD, watching the agent over SSE
    tid = api.post("/api/integrations/build", json={"recording_id": "fixture"}).json()["thread_id"]
    evs = read_sse(api, f"/api/agent/{tid}/stream", until=lambda e: e["event"] == "ask_user")
    ask = evs[-1]["data"]
    assert ask["options"] and [e["id"] for e in evs] == list(range(1, len(evs) + 1))
    r = api.post(f"/api/agent/{tid}/resume", json={"interrupt_id": ask["interrupt_id"], "answer": ask["options"][0]})
    assert r.status_code == 200 and r.json()["state"] in ("running", "waiting", "done")

    last = evs[-1]["id"]
    evs = read_sse(api, f"/api/agent/{tid}/stream?after={last}", until=lambda e: e["event"] == "request_deploy")
    assert evs[0]["id"] == last + 1 and "resumed" in [e["event"] for e in evs]
    deploy_req = evs[-1]["data"]
    assert deploy_req["version"] == 1 and deploy_req["tests"]["passed"]
    api.post(f"/api/agent/{tid}/resume", json={"interrupt_id": deploy_req["interrupt_id"],
                                               "decision": "approve"}).raise_for_status()
    rest = read_sse(api, f"/api/agent/{tid}/stream?after={evs[-1]['id']}")  # ends when the thread is done
    assert "deployed" in [e["event"] for e in rest] and rest[-1]["data"]["state"] == "done"

    [integ] = api.get("/api/integrations").json()
    assert integ == {"name": NAME, "active_version": 1, "latest_version": 1, "open_incidents": 0,
                     "state": "live"}

    # Run 100 with live progress (paced so several progress snapshots are visible)
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "15")
    run_id = api.post("/api/runs", json={"integration": NAME, "limit": 100}).json()["run_id"]
    events = read_sse(api, f"/api/runs/{run_id}/stream")
    progress = [e["data"] for e in events if e["event"] == "progress"]
    assert len(progress) >= 3, progress
    assert [p["processed"] for p in progress] == sorted(p["processed"] for p in progress)
    assert any(0 < p["processed"] < 100 for p in progress), "should see counts while the run proceeds"
    assert progress[-1]["total"] == 100
    finished = events[-1]
    assert finished["event"] == "finished"
    summary = finished["data"]
    assert summary["status"] == "completed" and summary["llm_calls"] == 0 and summary["kind"] == "run"
    assert sum(summary["counts"].get(k, 0) for k in ("success", "duplicate", "quarantined")) == 100
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")

    detail = api.get(f"/api/runs/{run_id}").json()
    assert len(detail["records"]) == 100 and detail["incident"] is None and detail["run"]["total"] == 100

    acc = api.get("/api/bench/accuracy", params={"integration": NAME}).json()
    assert acc["records"] == 100 and acc["accuracy"] == 1.0, acc["mismatches"]

    # Break 1: contract switch -> the run is blocked before any write
    r = api.post("/api/demo/stockroom/schema", json={"version": 2})
    assert r.status_code == 200 and r.json()["stockroom"]["version"] == 2 and r.json()["staging"]["version"] == 2
    writes_before = len(httpx.get(apps["live"] + "/api/incoming").json())
    blocked = finish_run(api, api.post("/api/runs", json={"integration": NAME}).json()["run_id"])
    assert blocked["status"] == "blocked" and blocked["incident_id"]
    assert len(httpx.get(apps["live"] + "/api/incoming").json()) == writes_before
    incident = api.get(f"/api/incidents/{blocked['incident_id']}").json()
    assert incident["type"] == "CONTRACT_CHANGED" and incident["status"] == "open"
    assert [i["id"] for i in api.get("/api/incidents", params={"status": "open"}).json()] == [incident["id"]]
    assert api.get("/api/integrations").json()[0]["open_incidents"] == 1

    # REPAIR through the agent, approved from the API
    rid = api.post(f"/api/incidents/{incident['id']}/repair").json()["thread_id"]
    s = wait_thread(api, rid, "waiting")
    assert s["pending"]["type"] == "ask_user"
    api.post(f"/api/agent/{rid}/resume", json={"interrupt_id": s["pending"]["interrupt_id"],
                                               "answer": "By supplier (acme→BOS, globex→NYC, initech→BOS)"})
    s = wait_thread(api, rid, "waiting")
    assert s["pending"]["type"] == "request_deploy" and s["pending"]["version"] == 2
    assert s["pending"]["spec_patch"]
    api.post(f"/api/agent/{rid}/resume", json={"interrupt_id": s["pending"]["interrupt_id"], "decision": "approve",
                                               "comment": "looks right"})
    wait_thread(api, rid, "done")

    status = api.get(f"/api/integrations/{NAME}").json()
    assert status["active_version"] == 2
    assert api.get("/api/incidents", params={"status": "open"}).json() == []
    assert api.get(f"/api/incidents/{incident['id']}").json()["status"] == "resolved"
    runs = api.get("/api/runs", params={"integration": NAME}).json()
    assert runs[0]["kind"] == "replay" and runs[0]["version"] == 2, "the agent replays after a repair deploy"

    # Replay from the API: quarantined records re-run through v2, nothing new succeeds or is written twice
    quarantined_before = sum(1 for r in api.get(f"/api/runs/{run_id}").json()["records"]
                             if r["status"] == "quarantined")
    replay = finish_run(api, api.post(f"/api/integrations/{NAME}/replay").json()["run_id"])
    assert replay["kind"] == "replay" and replay["status"] == "completed" and replay["version"] == 2
    assert replay["counts"]["quarantined"] == quarantined_before and "success" not in replay["counts"]

    # The rest of the inbox on v2, then accuracy over everything
    rest_run = finish_run(api, api.post("/api/runs", json={"integration": NAME}).json()["run_id"])
    assert rest_run["status"] == "completed" and rest_run["llm_calls"] == 0
    acc = api.get("/api/bench/accuracy", params={"integration": NAME}).json()
    assert acc["records"] == 150 and acc["accuracy"] == 1.0, acc["mismatches"]

    # Versions and diffs
    v2 = api.get(f"/api/integrations/{NAME}/versions/2").json()
    assert v2["tests_passed"] and v2["active"] and "integration.py" in v2["files"]
    assert "warehouse_id" in v2["spec_yaml"] and v2["manifest"]["version"] == 2
    diff = api.get(f"/api/integrations/{NAME}/diff", params={"from": 1, "to": 2}).json()
    v1 = api.get(f"/api/integrations/{NAME}/versions/1").json()
    assert jsonpatch.apply_patch(yaml.safe_load(v1["spec_yaml"]), diff["spec_patch"]) == yaml.safe_load(v2["spec_yaml"])
    assert any(op["path"] == "/sink/body/warehouse_id" for op in diff["spec_patch"])
    assert "+    warehouse_id:" in diff["spec_diff"] and diff["spec_diff"].startswith("--- v1/spec.yaml")
    assert diff["code_diff"] and all(d.startswith("--- v1/") for d in diff["code_diff"].values())
    assert api.get(f"/api/integrations/{NAME}/diff").json()["spec_patch"] == diff["spec_patch"]  # defaults

    # Rollback moves the pointer back; deploy moves it forward again
    rb = api.post(f"/api/integrations/{NAME}/rollback", json={"version": 1}).json()
    assert rb["active_version"] == 1 and rb["previous_version"] == 2 and rb["rolled_back"]
    assert api.post(f"/api/integrations/{NAME}/deploy", json={"version": 2}).json()["active_version"] == 2

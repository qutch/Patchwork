"""Runs, integrations and incidents endpoints: happy paths not in the flow test, and error codes."""

import time

import pytest

from control import ops
from runner.store import Store
from tests.conftest import FIXTURE_SPEC
from tests.control.conftest import finish_run, read_sse

NAME = "po_to_stockroom"


@pytest.fixture
def compiled(api):
    built = ops.compile_and_test(FIXTURE_SPEC)
    assert built["tests_passed"], built["tests_summary"]
    return api


def test_run_requires_a_deployed_integration(compiled):
    api = compiled
    r = api.post("/api/runs", json={"integration": "nope"})
    assert r.status_code == 404 and r.json()["detail"]
    r = api.post("/api/runs", json={"integration": NAME})
    assert r.status_code == 409 and "no active version" in r.json()["detail"]
    assert api.post("/api/runs", json={"integration": NAME, "version": 9}).status_code == 404
    assert api.post(f"/api/integrations/{NAME}/replay").status_code == 409
    assert api.post("/api/integrations/nope/replay").status_code == 404

    # an explicit, tested version runs without being active
    run = finish_run(api, api.post("/api/runs", json={"integration": NAME, "version": 1, "dry_run": True,
                                                      "limit": 3}).json()["run_id"])
    assert run["dry_run"] is True and run["status"] == "completed" and run["version"] == 1


def test_dry_run_list_and_detail(compiled):
    api = compiled
    api.post(f"/api/integrations/{NAME}/deploy", json={"version": 1}).raise_for_status()
    run = finish_run(api, api.post("/api/runs", json={"integration": NAME, "dry_run": True, "limit": 5}).json()["run_id"])
    assert run["status"] == "completed" and run["dry_run"] is True
    runs = api.get("/api/runs", params={"integration": NAME, "limit": 5}).json()
    assert runs[0]["id"] == run["run_id"] and runs[0]["dry_run"] is True and runs[0]["llm_calls"] == 0
    assert api.get("/api/runs", params={"integration": "other"}).json() == []
    detail = api.get(f"/api/runs/{run['run_id']}").json()
    assert set(detail) == {"run", "records", "incident"} and len(detail["records"]) == 5
    assert api.get("/api/runs/999").status_code == 404
    assert api.get("/api/runs/999/stream").status_code == 404
    # stream of an already-finished run: one progress snapshot + finished, then it closes
    evs = read_sse(api, f"/api/runs/{run['run_id']}/stream")
    assert [e["event"] for e in evs] == ["progress", "finished"]
    assert evs[0]["data"]["processed"] == 5 and evs[0]["id"] == 5


def test_one_run_per_integration_at_a_time_and_dead_runner_is_marked_failed(compiled, monkeypatch):
    api = compiled
    api.post(f"/api/integrations/{NAME}/deploy", json={"version": 1}).raise_for_status()
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "100")
    run_id = api.post("/api/runs", json={"integration": NAME}).json()["run_id"]
    r = api.post("/api/runs", json={"integration": NAME})
    assert r.status_code == 409 and "already in progress" in r.json()["detail"]
    assert api.post(f"/api/integrations/{NAME}/replay").status_code == 409

    deadline = time.time() + 30
    while api.get(f"/api/runs/{run_id}").json()["run"]["counts"].get("success", 0) < 2:
        assert time.time() < deadline
        time.sleep(0.05)
    mid = api.get(f"/api/runs/{run_id}").json()
    assert mid["run"]["status"] == "running" and mid["run"]["total"] == 150

    api.app.state.runs.get(run_id).proc.kill()  # the runner dies mid-run
    summary = finish_run(api, run_id)
    assert summary["status"] == "failed" and "runner" in summary["message"]
    assert 2 <= sum(summary["counts"].get(k, 0) for k in ("success", "duplicate", "quarantined")) < 150

    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")
    again = finish_run(api, api.post("/api/runs", json={"integration": NAME}).json()["run_id"])
    assert again["status"] == "completed"


def test_runner_that_cannot_start_is_a_500(compiled):
    api = compiled
    built = ops.compile_and_test(FIXTURE_SPEC)  # v2, then break its package
    (ops.version_dir(NAME, built["version"]) / "manifest.json").unlink()
    r = api.post("/api/runs", json={"integration": NAME, "version": built["version"]})
    assert r.status_code == 500 and "runner failed to start" in r.json()["detail"]


def test_integration_endpoints_errors(compiled):
    api = compiled
    assert api.get("/api/integrations/nope").status_code == 404
    assert api.get(f"/api/integrations/{NAME}/versions/9").status_code == 404
    assert api.get(f"/api/integrations/{NAME}/diff", params={"from": 1, "to": 9}).status_code == 404
    assert api.get("/api/integrations/nope/diff").status_code == 404
    assert api.post(f"/api/integrations/{NAME}/deploy", json={"version": 9}).status_code == 404

    v1 = api.get(f"/api/integrations/{NAME}/versions/1").json()
    assert set(v1) >= {"version", "manifest", "spec_yaml", "files", "tests_passed", "tests_summary"}
    assert v1["active"] is False and not any("__pycache__" in f for f in v1["files"])

    ops.compile_and_test(FIXTURE_SPEC)  # v2
    Store().set_tests(NAME, 2, False, "1 failed")
    r = api.post(f"/api/integrations/{NAME}/deploy", json={"version": 2})
    assert r.status_code == 409 and "tests have not passed" in r.json()["detail"]
    assert api.post(f"/api/integrations/{NAME}/deploy", json={"version": 1}).json()["active_version"] == 1
    r = api.post(f"/api/integrations/{NAME}/rollback", json={"version": 1})
    assert r.status_code == 409 and "not older" in r.json()["detail"]
    status = api.get(f"/api/integrations/{NAME}").json()
    assert status["active_version"] == 1 and len(status["versions"]) == 2

    # identical specs: empty patch, only version-stamped files differ
    diff = api.get(f"/api/integrations/{NAME}/diff", params={"from": 1, "to": 2}).json()
    assert diff["spec_patch"] == [] and diff["spec_diff"] == "" and "manifest.json" in diff["code_diff"]


def test_incidents_endpoints(compiled, apps):
    api = compiled
    api.post(f"/api/integrations/{NAME}/deploy", json={"version": 1})
    assert api.get("/api/incidents").json() == []
    api.post("/api/demo/stockroom/schema", json={"version": 2})
    blocked = finish_run(api, api.post("/api/runs", json={"integration": NAME}).json()["run_id"])
    inc = api.get(f"/api/incidents/{blocked['incident_id']}").json()
    assert inc["type"] == "CONTRACT_CHANGED" and inc["old_openapi"] and inc["new_openapi"]
    assert len(api.get("/api/incidents").json()) == 1
    assert len(api.get("/api/incidents", params={"status": "open", "integration": NAME}).json()) == 1
    assert api.get("/api/incidents", params={"status": "resolved"}).json() == []
    assert api.get("/api/incidents/999").status_code == 404
    detail = api.get(f"/api/runs/{blocked['run_id']}").json()
    assert detail["incident"]["id"] == inc["id"] and detail["records"] == []

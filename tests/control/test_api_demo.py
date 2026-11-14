"""Demo proxies, demo reset and the accuracy benchmark."""

import httpx
import pytest
from fastapi import FastAPI

from demo_apps.seed import generate
from tests.conftest import _Server, _free_port
from tests.control.conftest import build_and_deploy, finish_run

NAME = "po_to_stockroom"


def fake_inbox(truth: dict | None) -> FastAPI:
    """An Inbox with /admin/deliver and (optionally) /admin/ground_truth, but no /admin/new_supplier."""
    app = FastAPI()
    calls = []

    @app.post("/admin/deliver")
    def deliver(body: dict) -> dict:
        calls.append(body)
        return {"delivered": body["count"]}

    if truth is not None:
        @app.get("/admin/ground_truth")
        def ground_truth() -> dict:
            return {"ground_truth": truth}

    app.state.calls = calls
    return app


def test_inbox_proxies_pass_status_through(api, monkeypatch):
    app = fake_inbox(None)
    with _Server(app) as inbox:
        monkeypatch.setenv("PATCHWORK_INBOX_URL", inbox.url)
        r = api.post("/api/demo/inbox/deliver", json={"count": 5})
        assert r.status_code == 200 and r.json() == {"delivered": 5} and app.state.calls == [{"count": 5}]
        r = api.post("/api/demo/inbox/new_supplier", json={"count": 3})
        assert r.status_code == 404 and "/admin/new_supplier" in r.json()["detail"]
        assert api.post("/api/demo/inbox/deliver", json={"count": 0}).status_code == 422
    monkeypatch.setenv("PATCHWORK_INBOX_URL", f"http://127.0.0.1:{_free_port()}")
    r = api.post("/api/demo/inbox/deliver", json={"count": 1})
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]


def test_stockroom_schema_switch_and_bad_version(api, apps):
    r = api.post("/api/demo/stockroom/schema", json={"version": 2})
    assert r.status_code == 200
    assert httpx.get(apps["live"] + "/admin/schema").json() == {"version": 2}
    assert httpx.get(apps["staging"] + "/admin/schema").json() == {"version": 2}
    assert api.post("/api/demo/stockroom/schema", json={"version": 7}).status_code == 400


def test_reset_clears_workspace_and_apps_but_keeps_recordings(api, apps, workspace):
    build_and_deploy(api)
    finish_run(api, api.post("/api/runs", json={"integration": NAME, "limit": 10}).json()["run_id"])
    api.post("/api/demo/stockroom/schema", json={"version": 2})
    (workspace / "recordings" / "rec-keep").mkdir(parents=True)
    api.put("/api/settings", json={"ai_enabled": False})
    assert httpx.get(apps["live"] + "/api/incoming").json()

    api.post("/api/demo/inbox/deliver", json={"count": 4}).raise_for_status()
    assert len(httpx.get(apps["inbox"] + "/api/messages").json()) == 154

    r = api.post("/api/demo/reset").json()
    assert r["ok"] and all(r["apps"][a]["status"] == 200 for a in ("inbox", "stockroom", "staging"))
    assert len(httpx.get(apps["inbox"] + "/api/messages").json()) == 150
    assert {"integrations", "runs", "agent_threads", "patchwork.db"} <= set(r["removed"])
    assert api.get("/api/integrations").json() == []
    assert api.get("/api/runs").json() == []
    assert api.get("/api/agent/threads").json() == []
    assert (workspace / "recordings" / "rec-keep").is_dir()
    assert api.get("/api/settings").json()["ai_enabled"] is False, "settings survive a reset"
    assert httpx.get(apps["live"] + "/admin/schema").json() == {"version": 1}
    assert httpx.get(apps["live"] + "/api/incoming").json() == []

    api.put("/api/settings", json={"ai_enabled": True})
    build_and_deploy(api)  # the demo can start over
    assert api.get("/api/integrations").json()[0]["active_version"] == 1


@pytest.fixture
def ran_ten(api):
    from control import ops
    from tests.conftest import FIXTURE_SPEC
    ops.compile_and_test(FIXTURE_SPEC)
    ops.deploy(NAME, 1)
    finish_run(api, api.post("/api/runs", json={"integration": NAME, "limit": 10}).json()["run_id"])
    finish_run(api, api.post("/api/runs", json={"integration": NAME, "dry_run": True}).json()["run_id"])
    return api


def test_accuracy_uses_inbox_ground_truth_when_available(ran_ten, monkeypatch):
    api = ran_ten
    acc = api.get("/api/bench/accuracy", params={"integration": NAME}).json()
    assert acc["records"] == 10 and acc["accuracy"] == 1.0, "dry runs don't count"
    assert acc["truth_source"] in ("seed", "inbox")
    assert api.get("/api/bench/accuracy").json()["records"] == 10
    assert api.get("/api/bench/accuracy", params={"integration": "nope"}).status_code == 404

    with _Server(fake_inbox(generate().ground_truth)) as inbox:
        monkeypatch.setenv("PATCHWORK_INBOX_URL", inbox.url)
        acc = api.get("/api/bench/accuracy", params={"integration": NAME}).json()
        assert acc["truth_source"] == "inbox" and acc["accuracy"] == 1.0
    with _Server(fake_inbox({"msg-x": {"outcome": "success", "reason": None}})) as inbox:
        monkeypatch.setenv("PATCHWORK_INBOX_URL", inbox.url)
        acc = api.get("/api/bench/accuracy", params={"integration": NAME}).json()
        assert acc["truth_source"] == "inbox" and acc["accuracy"] == 0.0
    with _Server(fake_inbox(None)) as inbox:
        monkeypatch.setenv("PATCHWORK_INBOX_URL", inbox.url)
        assert api.get("/api/bench/accuracy", params={"integration": NAME}).json()["truth_source"] == "seed"


def test_real_inbox_deliveries_and_new_supplier(ran_ten, apps):
    """Delivered messages are scored with the Inbox's ground truth; Break 2 messages quarantine."""
    api = ran_ten
    r = api.post("/api/demo/inbox/deliver", json={"count": 6})
    assert r.status_code == 200 and len(r.json()["delivered"]) == 6
    run = finish_run(api, api.post("/api/runs", json={"integration": NAME}).json()["run_id"])
    assert run["status"] == "completed"
    acc = api.get("/api/bench/accuracy", params={"integration": NAME}).json()
    assert acc["truth_source"] == "inbox" and acc["records"] == 156 and acc["accuracy"] == 1.0, acc["mismatches"]

    r = api.post("/api/demo/inbox/new_supplier", json={"count": 4})
    assert r.status_code == 200 and len(r.json()["delivered"]) == 4
    run = finish_run(api, api.post("/api/runs", json={"integration": NAME}).json()["run_id"])
    assert run["counts"].get("quarantined:PARSE_NO_MATCH") == 4 and run["incident_id"]
    assert api.get(f"/api/incidents/{run['incident_id']}").json()["type"] == "NEW_SOURCE_FORMAT"

"""Contracts V13 over the API: `GET /api/credentials` (names, set, used_by; never values), background runs
refuse to start without their credential, and error responses never show a credential value."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import HTTPException

from agents.scripted import ScriptedAgentService
from control import ops
from control.api import create_app
from runner.store import Store
from tests.conftest import _Server
from tests.control.conftest import finish_run
from tests.test_credentials import KEY, NAME, TOKEN, _no_sentinel, secure_app, write_spec


@pytest.fixture
def cred_api(workspace, monkeypatch):
    """The control plane plus the secure fake (source + sink on one free port), cred_demo v1 deployed."""
    monkeypatch.setenv("SINK_TOKEN", TOKEN)
    monkeypatch.setenv("SOURCE_KEY", KEY)
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")
    for var in ("PATCHWORK_SOURCE_BASE_URL", "PATCHWORK_SINK_BASE_URL", "PATCHWORK_STAGING_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    fake = secure_app()
    app = create_app(agent=ScriptedAgentService(pace_s=0), dotenv=False)

    @app.get("/api/_test/echo-error")
    def echo_error():  # an upstream error quoted back to the client: the worst case
        raise HTTPException(502, f"destination answered 401: invalid token Bearer {TOKEN}")

    with _Server(fake) as sink, _Server(app) as srv, httpx.Client(base_url=srv.url, timeout=60) as client:
        built = ops.compile_and_test(write_spec(workspace, sink.url))
        assert built["tests_passed"], built["tests_summary"]
        ops.deploy(NAME, 1)
        client.fake = fake  # type: ignore[attr-defined]
        yield client


def test_get_credentials_lists_names_and_whether_set(cred_api, monkeypatch):
    resp = cred_api.get("/api/credentials")
    assert resp.status_code == 200
    assert resp.json() == [{"name": "SINK_TOKEN", "set": True, "used_by": [NAME]},
                           {"name": "SOURCE_KEY", "set": True, "used_by": [NAME]}]
    monkeypatch.delenv("SOURCE_KEY")
    body = cred_api.get("/api/credentials").json()
    assert {c["name"]: c["set"] for c in body} == {"SINK_TOKEN": True, "SOURCE_KEY": False}
    _no_sentinel(cred_api.get("/api/credentials").text)


def test_background_run_sends_credentials_and_shows_none(cred_api):
    run_id = cred_api.post("/api/runs", json={"integration": NAME}).json()["run_id"]
    summary = finish_run(cred_api, run_id)
    assert summary["status"] == "completed", summary
    assert cred_api.fake.state.items == [{"title": "foo", "n": 3}]
    detail = cred_api.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    recs = {r["record_id"]: r for r in detail.json()["records"]}
    assert recs["r2"]["reason"] == "SINK_CONTRACT" and "***" in recs["r2"]["detail"]
    _no_sentinel(detail.text + cred_api.get("/api/runs").text + cred_api.get("/api/incidents").text)


def test_background_run_and_replay_refuse_to_start_without_the_credential(cred_api, monkeypatch):
    monkeypatch.delenv("SINK_TOKEN")
    before = len(Store().runs(NAME, limit=100))
    resp = cred_api.post("/api/runs", json={"integration": NAME})
    assert resp.status_code == 409
    assert resp.json()["detail"].startswith("credential SINK_TOKEN is not set: add SINK_TOKEN=... to .env")
    replay = cred_api.post(f"/api/integrations/{NAME}/replay")
    assert replay.status_code == 409 and "SINK_TOKEN" in replay.json()["detail"]
    assert len(Store().runs(NAME, limit=100)) == before  # nothing started


def test_error_responses_are_redacted(cred_api):
    resp = cred_api.get("/api/_test/echo-error")
    assert resp.status_code == 502
    assert resp.json() == {"detail": "destination answered 401: invalid token Bearer ***"}
    assert int(resp.headers["content-length"]) == len(resp.content)
    _no_sentinel(resp.text)
    ok = cred_api.get("/api/health")  # success responses pass through untouched
    assert ok.status_code == 200 and json.loads(ok.text)["status"] == "ok"


# ---- adding a credential from the UI ------------------------------------------------------------

@pytest.fixture
def client(workspace, monkeypatch, tmp_path):
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")
    monkeypatch.setattr("control.credentials.ENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("SHOP_TOKEN", "placeholder")  # so monkeypatch restores os.environ afterwards
    monkeypatch.delenv("SHOP_TOKEN")
    app = create_app(agent=ScriptedAgentService(pace_s=0), dotenv=False)
    with _Server(app) as srv, httpx.Client(base_url=srv.url, timeout=60) as c:
        yield c


def test_post_credential_sets_env_and_never_echoes(client, tmp_path, monkeypatch):
    import os
    env = tmp_path / ".env"; env.write_text("OTHER=1\n")
    r = client.post("/api/credentials", json={"name": "SHOP_TOKEN", "value": "sentinel-cred-123456"})
    assert r.status_code == 201 and "sentinel-cred-123456" not in r.text
    assert r.json() == {"name": "SHOP_TOKEN", "set": True, "used_by": []}
    assert os.environ["SHOP_TOKEN"] == "sentinel-cred-123456"
    assert "SHOP_TOKEN=sentinel-cred-123456" in env.read_text() and "OTHER=1" in env.read_text()
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert {"name": "SHOP_TOKEN", "set": True}.items() <= next(
        c for c in client.get("/api/credentials").json() if c["name"] == "SHOP_TOKEN").items()


def test_post_credential_refuses_model_and_process_names(client):
    for bad in ("ANTHROPIC_API_KEY", "PATH", "lower"):
        assert client.post("/api/credentials", json={"name": bad, "value": "x" * 12}).status_code == 422


def test_new_credential_is_redacted_immediately(client):
    client.post("/api/credentials", json={"name": "SHOP_TOKEN", "value": "sentinel-cred-123456"})
    from runner.credentials import redact
    assert redact("x sentinel-cred-123456 y") == "x *** y"


def test_replacing_a_credential_rewrites_its_line(client, tmp_path):
    env = tmp_path / ".env"; env.write_text("SHOP_TOKEN=old-value-0001\n")
    client.post("/api/credentials", json={"name": "SHOP_TOKEN", "value": "new-value-0002"})
    assert env.read_text().count("SHOP_TOKEN=") == 1 and "old-value-0001" not in env.read_text()

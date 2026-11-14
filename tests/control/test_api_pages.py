"""W4a-2: /api/pages, the extension token and CORS, run on arrival (docs/contracts.md V9)."""

import threading

import httpx
import pytest

from control import ops
from runner.store import Store
from tests.control.v2_fakes import (INDEX, NAME, fake_runner, gate, make_page_integration,  # noqa: F401
                                    make_v1_integration, page_html, post_page, records_by_page, v2agent, v2api,
                                    wait_idle)


def test_post_requires_token(v2api, workspace):
    body = {"url": "https://jobs.lever.co/x/1", "title": "t", "html": "<h1>x</h1>"}
    assert v2api.post("/api/pages", json=body).status_code == 401
    r = v2api.post("/api/pages", json=body, headers={"X-Patchwork-Token": "wrong"})
    assert r.status_code == 401 and "X-Patchwork-Token" in r.json()["detail"]
    token = v2api.get("/api/extension").json()["token"]
    assert token == (workspace / "extension_token").read_text().strip()
    r = v2api.post("/api/pages", json=body, headers={"X-Patchwork-Token": token})
    assert r.status_code == 201, r.text
    out = r.json()
    assert set(out) >= {"id", "seq", "site", "canonical_url", "integration", "run_id", "queued"}
    assert out["id"] == "pg-000001" and out["site"] == "jobs.lever.co"
    assert out["integration"] is None and out["run_id"] is None and out["queued"] is False  # no page integration yet


def test_post_too_large_and_bad_bodies(v2api):
    token = v2api.get("/api/extension").json()["token"]
    h = {"X-Patchwork-Token": token}
    big = "<p>" + "a" * (5 * 1024 * 1024) + "</p>"
    r = v2api.post("/api/pages", json={"url": "https://x.org/1", "title": "t", "html": big}, headers=h)
    assert r.status_code == 413 and "5 MB" in r.json()["detail"]
    secret_html = "<h1>SECRET-PAGE-CONTENT</h1>"
    for body in ({"title": "t", "html": secret_html}, {"url": "https://x.org", "title": "t"},
                 {"url": "https://x.org", "title": 3, "html": secret_html}, [1, 2]):
        r = v2api.post("/api/pages", json=body, headers=h)
        assert r.status_code == 422, body
        assert "SECRET-PAGE-CONTENT" not in r.text  # never echo page HTML
    r = v2api.post("/api/pages", content=b"not json", headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 422
    assert v2api.get("/api/pages").json() == []  # nothing stored


def test_get_pages_since(v2api):
    for i in range(3):
        assert post_page(v2api, "lever-1.html", url=f"https://jobs.lever.co/palantir/{i}").status_code == 201
    pages = v2api.get("/api/pages").json()
    assert [p["seq"] for p in pages] == [1, 2, 3]
    assert set(pages[0]) == {"id", "seq", "url", "canonical_url", "site", "title", "html", "text", "captured_at",
                             "captured_date"}
    assert [p["seq"] for p in v2api.get("/api/pages", params={"since": ""}).json()] == [1, 2, 3]
    assert [p["seq"] for p in v2api.get("/api/pages", params={"since": "1"}).json()] == [2, 3]
    assert [p["seq"] for p in v2api.get("/api/pages", params={"since": 1, "limit": 1}).json()] == [2]
    assert v2api.get("/api/pages", params={"since": "abc"}).status_code == 422
    one = v2api.get("/api/pages/pg-000002").json()
    assert "html" not in one["page"] and one["page"]["text"] and one["row"]["page_id"] == "pg-000002"
    assert v2api.get("/api/pages/pg-000099").status_code == 404


def test_post_starts_run_for_page_integrations(v2api):
    make_page_integration()
    make_v1_integration()
    r = post_page(v2api, "lever-1.html").json()
    assert r["integration"] == NAME and isinstance(r["run_id"], int) and r["queued"] is False
    wait_idle(v2api)
    store = Store()
    assert [run["id"] for run in store.runs(NAME)] == [r["run_id"]]
    assert store.runs("po_to_stockroom") == []  # v1 integrations are not run on arrival
    # the runner fetched the page from the control plane: PATCHWORK_CONTROL_URL reached it
    recs = records_by_page()
    assert list(recs) == [r["id"]] and recs[r["id"]][0]["status"] == "success"
    assert recs[r["id"]][0]["parser"] == "lever"
    row = v2api.get(f"/api/pages/{r['id']}").json()["row"]
    assert row["status"] == "success" and row["label"] == "code" and row["ai_calls"] == 0


def test_presses_during_run_are_queued_once(v2api):
    make_page_integration()
    with gate():
        first = post_page(v2api, "lever-1.html").json()
        assert first["run_id"] and not first["queued"]
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(
            post_page(v2api, "lever-2.html", url=f"https://jobs.lever.co/palantir/press-{i}").json()))
            for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(r["queued"] and r["run_id"] is None for r in results), results
    wait_idle(v2api)
    runs = Store().runs(NAME)
    assert len(runs) == 2, runs  # the busy run + exactly one follow-up
    recs = records_by_page()
    assert sorted(recs) == [f"pg-00000{i}" for i in range(1, 5)]
    assert all(len(v) == 1 and v[0]["status"] == "success" for v in recs.values()), recs  # each exactly once


def test_manual_run_in_progress_is_followed_up(v2api):
    make_page_integration()
    with gate():
        run_id = v2api.post("/api/runs", json={"integration": NAME}).json()["run_id"]
        r = post_page(v2api, "lever-1.html").json()
        assert r["queued"] is True and r["run_id"] is None
    wait_idle(v2api)
    runs = Store().runs(NAME)
    assert len(runs) == 2 and runs[-1]["id"] == run_id
    assert records_by_page()["pg-000001"][0]["status"] == "success"


def test_same_posting_twice_is_duplicate(workspace, monkeypatch):
    """End to end with W1's engine: fixtures/jobs compiled against the fixture tracker form, the real
    runner, the same posting sent twice with a different query string and #fragment → one row."""
    from demo_apps.tracker_form.app import create_app as form_app  # W1-3
    import yaml

    from agents.scripted import ScriptedAgentService
    from control.api import create_app
    from tests.conftest import REPO, _Server
    from tests.control.v2_fakes import wait_idle as idle

    monkeypatch.delenv("PATCHWORK_CONTROL_URL", raising=False)
    with _Server(form_app()) as form:
        spec = yaml.safe_load((REPO / "fixtures" / "jobs" / "spec.yaml").read_text())
        spec["sink"]["url"] = form.url + "/"
        (workspace / "jobs").mkdir()
        (workspace / "jobs" / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
        (workspace / "jobs" / "examples.yaml").write_text((REPO / "fixtures" / "jobs" / "examples.yaml").read_text())
        built = ops.compile_and_test(workspace / "jobs" / "spec.yaml")
        assert built["tests_passed"], built["tests_summary"]
        ops.deploy(spec["name"], built["version"])
        app = create_app(agent=ScriptedAgentService(pace_s=0), dotenv=False)
        with _Server(app) as srv, httpx.Client(base_url=srv.url, timeout=60) as api:
            api.app = app
            url = INDEX["lever-1.html"]["url"]
            a = post_page(api, "lever-1.html", url=url)
            idle(api, spec["name"], timeout=90)
            b = post_page(api, "lever-1.html", url=url + "?utm_source=x#top")
            idle(api, spec["name"], timeout=90)
            rows = api.get("/api/tracker", params={"integration": spec["name"]}).json()["rows"]
            by_id = {r["page_id"]: r for r in rows}
            assert by_id[a.json()["id"]]["label"] == "code"
            assert by_id[b.json()["id"]]["label"] == "duplicate"
            assert len(httpx.get(form.url + "/admin/rows").json()) == 1


def test_extension_setup_endpoint(v2api, workspace):
    ext = v2api.get("/api/extension").json()
    assert set(ext) == {"token", "control_url"}
    assert ext["token"] == (workspace / "extension_token").read_text().strip() and len(ext["token"]) >= 20
    assert ext["control_url"] == str(v2api.base_url).rstrip("/")
    assert v2api.get("/api/extension").json()["token"] == ext["token"]  # stable
    assert ops.runner_env()["PATCHWORK_CONTROL_URL"] == ext["control_url"]
    assert not any(k.endswith("_TOKEN") for k in ops.runner_env())


def test_cors_for_extension_only_on_page_routes(v2api):
    ext = {"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"}
    pre = v2api.options("/api/pages", headers={**ext, "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "content-type,x-patchwork-token"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == ext["Origin"]
    assert "x-patchwork-token" in pre.headers["access-control-allow-headers"].lower()
    token = v2api.get("/api/extension").json()["token"]
    r = v2api.post("/api/pages", json={"url": "https://x.org/1", "title": "", "html": "<p>x</p>"},
                   headers={**ext, "X-Patchwork-Token": token})
    assert r.status_code == 201 and r.headers["access-control-allow-origin"] == ext["Origin"]
    assert v2api.get("/api/pages/pg-000001", headers=ext).headers.get("access-control-allow-origin") == ext["Origin"]
    # other routes (and the html-bearing list) keep the localhost-only policy
    assert "access-control-allow-origin" not in v2api.get("/api/pages", headers=ext).headers
    assert "access-control-allow-origin" not in v2api.get("/api/integrations", headers=ext).headers
    assert "access-control-allow-origin" not in v2api.get("/api/extension", headers=ext).headers
    local = {"Origin": "http://localhost:5181"}
    assert v2api.get("/api/integrations", headers=local).headers["access-control-allow-origin"] == local["Origin"]

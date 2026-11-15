"""The control plane serves the built UI (web/dist) on its own port (one-command launcher design): files as files,
the UI's routes as index.html, /api untouched; with no build, nothing changes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.scripted import ScriptedAgentService
from control.api import create_app

INDEX = "<!doctype html><html><body><div id=root></div><script src=/assets/x.js></script></body></html>"


@pytest.fixture
def dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(INDEX)
    (d / "assets" / "x.js").write_text("console.log('ui')")
    (d / "favicon.svg").write_text("<svg/>")
    return d


def _client(monkeypatch, tmp_path, dist_dir) -> TestClient:
    monkeypatch.setenv("PATCHWORK_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("PATCHWORK_WEB_DIST", str(dist_dir))
    return TestClient(create_app(agent=ScriptedAgentService(pace_s=0), dotenv=False))


def test_serves_index_assets_and_ui_routes(monkeypatch, tmp_path, dist):
    with _client(monkeypatch, tmp_path, dist) as c:
        r = c.get("/")
        assert r.status_code == 200 and r.text == INDEX and r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-cache"
        r = c.get("/assets/x.js")
        assert r.status_code == 200 and r.text == "console.log('ui')"
        assert "javascript" in r.headers["content-type"]
        assert c.get("/favicon.svg").text == "<svg/>"
        for route in ("/integrations/foo", "/integrations/foo/", "/new"):
            r = c.get(route)
            assert r.status_code == 200 and r.text == INDEX, route
        assert c.head("/integrations/foo").status_code == 200


def test_api_is_untouched(monkeypatch, tmp_path, dist):
    with _client(monkeypatch, tmp_path, dist) as c:
        r = c.get("/api/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"
        for path in ("/api", "/api/", "/api/nope", "/api/nope/deeper"):
            r = c.get(path)
            assert r.status_code == 404 and r.json() == {"detail": "Not Found"}, path
        assert c.post("/api/nope").status_code == 404
        assert c.delete("/api/health").status_code == 405  # a wrong method on a real route is still 405
        assert "/{path}" not in str(c.get("/openapi.json").json()["paths"])


def test_no_escape_and_no_page_for_missing_assets(monkeypatch, tmp_path, dist):
    (tmp_path / "secret.txt").write_text("outside dist")
    with _client(monkeypatch, tmp_path, dist) as c:
        assert c.get("/assets/missing.js").status_code == 404
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"):
            r = c.get(path)
            assert "outside dist" not in r.text, path
        assert c.post("/integrations/foo").status_code == 404  # only GET/HEAD get the UI


def test_without_a_build_nothing_changes(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, tmp_path / "no-dist") as c:
        assert c.get("/").status_code == 404
        assert c.get("/integrations/foo").status_code == 404
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/nope").json() == {"detail": "Not Found"}

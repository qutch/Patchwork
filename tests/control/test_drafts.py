"""Draft integrations and full delete (wizard, Task 2): create/list, pages wait without a run or a learn,
duplicate names, and delete removes everything but credentials."""

from __future__ import annotations

import pytest

from fixtures.products.pages import NAME as PRODUCT, PAGES as PRODUCT_PAGES, html as product_html
from runner.store import Store, workspace_dir
from tests.control.test_pages_routing import post_product, product_spec
from tests.control.v2_fakes import fake_runner, make_page_integration, v2agent, v2api, wait_idle  # noqa: F401


@pytest.fixture
def client(v2api):
    return v2api


@pytest.fixture
def TOKEN(client):
    return {"X-Patchwork-Token": client.get("/api/extension").json()["token"]}


class Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return {"run_id": None, "queued": False}


@pytest.fixture
def page_runner_spy(client, monkeypatch):
    spy = Spy()
    monkeypatch.setattr(client.app.state.runs, "start", lambda *a, **kw: spy(*a, **kw) or 1)
    return spy


@pytest.fixture
def learn_spy(client, monkeypatch):
    spy = Spy()
    monkeypatch.setattr(client.agent, "start_learn", lambda *a, **kw: spy(*a, **kw) or "learn-x", raising=False)
    return spy


def test_create_lists_draft_everywhere(client, TOKEN):
    d = client.post("/api/integrations/new", json={"intent": "Track upcoming events"}).json()
    assert d["name"] == "track_upcoming_events" and d["state"] == "draft"
    assert any(i["name"] == d["name"] and i["state"] == "draft" for i in client.get("/api/integrations").json())
    assert d["name"] in [i["name"] for i in client.get("/api/extension", headers=TOKEN).json()["integrations"]]
    assert client.get(f"/api/integrations/{d['name']}/draft").json()["intent"] == "Track upcoming events"
    assert (workspace_dir() / "drafts" / f"{d['name']}.json").exists()


def test_page_to_draft_waits_without_run_or_learn(client, TOKEN, page_runner_spy, learn_spy):
    client.post("/api/integrations/new", json={"intent": "Track upcoming events"})
    r = client.post("/api/pages", json={"url": "https://ev.example.com/e/1", "title": "t", "html": "<h1>x</h1>",
                                        "integration": "track_upcoming_events"}, headers=TOKEN)
    assert r.status_code == 201 and r.json()["queued"] is False and r.json()["run_id"] is None
    assert r.json()["integration"] == "track_upcoming_events"
    assert page_runner_spy.calls == [] and learn_spy.calls == []
    assert len(client.get("/api/pages?integration=track_upcoming_events").json()) == 1
    # the hook that would learn a held page also leaves a draft alone
    assert client.app.state.hooks.maybe_learn("track_upcoming_events", ["pg-000001"]) == []
    assert learn_spy.calls == []


def test_duplicate_name_is_409(client):
    client.post("/api/integrations/new", json={"intent": "Track upcoming events"})
    assert client.post("/api/integrations/new", json={"intent": "Track upcoming events"}).status_code == 409


def test_name_of_an_existing_integration_is_409(client):
    make_page_integration(name=PRODUCT, spec=product_spec())
    assert client.post("/api/integrations/new", json={"name": PRODUCT, "intent": "x y"}).status_code == 409


def test_bad_name_and_empty_intent_are_422(client):
    assert client.post("/api/integrations/new", json={"name": "../x", "intent": "x"}).status_code == 422
    assert client.post("/api/integrations/new", json={"intent": "   "}).status_code == 422


@pytest.fixture
def built_products_integration(client):
    make_page_integration(name=PRODUCT, spec=product_spec())
    post_product(client)
    wait_idle(client, PRODUCT)
    assert Store().runs(PRODUCT)
    return PRODUCT


def test_delete_removes_everything_but_credentials(client, built_products_integration, monkeypatch, tmp_path):
    monkeypatch.setattr("control.credentials.ENV_PATH", tmp_path / ".env")
    client.post("/api/credentials", json={"name": "SHOP_TOKEN", "value": "sentinel-cred-123456"})
    monkeypatch.delenv("SHOP_TOKEN", raising=False)
    name = built_products_integration
    r = client.delete(f"/api/integrations/{name}?delete_rows=false")
    assert r.status_code == 200 and r.json()["rows_deleted"] == 0
    assert name not in [i["name"] for i in client.get("/api/integrations").json()]
    assert client.get(f"/api/pages?integration={name}").json() == []
    assert not (workspace_dir() / "integrations" / name).exists()
    store = Store()
    assert store.runs(name) == [] and store.versions(name) == []
    assert "SHOP_TOKEN" in [c["name"] for c in client.get("/api/credentials").json()]
    assert client.delete(f"/api/integrations/{name}").status_code == 404


def test_delete_a_draft_removes_it_and_its_pages(client, TOKEN):
    client.post("/api/integrations/new", json={"intent": "Track upcoming events"})
    client.post("/api/pages", json={"url": "https://ev.example.com/e/1", "title": "t", "html": "<h1>x</h1>",
                                    "integration": "track_upcoming_events"}, headers=TOKEN)
    assert client.delete("/api/integrations/track_upcoming_events").status_code == 200
    assert client.get("/api/integrations/track_upcoming_events/draft").status_code == 404
    assert client.get("/api/pages?integration=track_upcoming_events").json() == []
    assert "track_upcoming_events" not in [i["name"] for i in client.get("/api/integrations").json()]


def test_deploying_a_version_makes_the_draft_live(client):
    from control import drafts, ops

    drafts.create(PRODUCT, "Track product prices")
    drafts.update(PRODUCT, state="teaching")
    assert [i["state"] for i in client.get("/api/integrations").json() if i["name"] == PRODUCT] == ["teaching"]
    make_page_integration(name=PRODUCT, spec=product_spec(), deploy=False)
    assert [i["state"] for i in client.get("/api/integrations").json() if i["name"] == PRODUCT] == ["teaching"]
    ops.deploy(PRODUCT, 1)
    assert drafts.get(PRODUCT).state == "live"
    assert [i["state"] for i in client.get("/api/integrations").json() if i["name"] == PRODUCT] == ["live"]


def test_draft_endpoint_keeps_answering_once_live(client):
    from control import drafts, ops

    drafts.create(PRODUCT, "Track product prices")
    make_page_integration(name=PRODUCT, spec=product_spec(), deploy=False)
    ops.deploy(PRODUCT, 1)
    r = client.get(f"/api/integrations/{PRODUCT}/draft")
    assert r.status_code == 200 and r.json()["state"] == "live" and r.json()["name"] == PRODUCT

"""Choose a destination and check the connection (wizard, Task 3): Airtable (fake), form (fixture tracker),
API (a minimal app defined here), SQLite (a file in the workspace). No real network."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from control import destinations
from demo_apps.fake_airtable.app import BASE_ID, TABLE, TOKEN, create_app as fake_airtable_app
from tests.conftest import _Server
from tests.control.v2_fakes import fake_runner, v2agent, v2api  # noqa: F401

FIELDS = [{"id": "fldProduct0000001", "name": "Product", "type": "singleLineText"},
          {"id": "fldPrice000000001", "name": "Price", "type": "number", "options": {"precision": 2}},
          {"id": "fldStore000000001", "name": "Store", "type": "singleSelect",
           "options": {"choices": [{"name": "Acme"}, {"name": "Lumen"}]}}]


@pytest.fixture
def client(v2api):
    return v2api


@pytest.fixture
def draft(client):
    return client.post("/api/integrations/new", json={"intent": "Track product prices"}).json()["name"]


@pytest.fixture
def fake_airtable(monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", TOKEN)
    with _Server(fake_airtable_app(fields=FIELDS)) as srv:
        monkeypatch.setenv("PATCHWORK_AIRTABLE_API_URL", srv.url + "/v0")
        yield srv


@pytest.fixture
def fake_airtable_rejecting(monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", "a-token-the-base-does-not-accept")
    with _Server(fake_airtable_app(fields=FIELDS)) as srv:
        monkeypatch.setenv("PATCHWORK_AIRTABLE_API_URL", srv.url + "/v0")
        yield srv


class Venue(BaseModel):
    name: str
    city: str | None = None


class Author(BaseModel):
    name: str


class Paper(BaseModel):
    title: str
    venue: Venue
    year: int
    authors: list[Author] = []
    note: str | None = None


def papers_app() -> FastAPI:
    app = FastAPI(title="Papers")

    @app.post("/papers", summary="Add a paper")
    def add(p: Paper):
        return p

    @app.put("/papers/{pid}", summary="Replace a paper")
    def put(pid: str, p: Paper):
        return p

    @app.get("/papers", summary="List papers")
    def listing():
        return []

    return app


@pytest.fixture
def papers_api():
    with _Server(papers_app()) as srv:
        yield srv.url


def put_destination(client, name, body):
    return client.put(f"/api/integrations/{name}/destination", json=body)


# ---- Airtable -------------------------------------------------------------------------------------

def test_airtable_url_parsed_and_fields_typed(fake_airtable, client, draft):
    r = put_destination(client, draft, {"kind": "airtable", "base_url": f"https://airtable.com/{BASE_ID}/shrX",
                                        "table": TABLE, "credential": "AIRTABLE_TOKEN"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "airtable" and {"name": "Price", "type": "number"}.items() <= body["fields"][1].items()
    assert body["fields"][2]["options"] == ["Acme", "Lumen"]
    saved = client.get(f"/api/integrations/{draft}/draft").json()["destination"]
    assert saved == {"kind": "airtable", "base_id": BASE_ID, "table": TABLE, "credential": "AIRTABLE_TOKEN"}


def test_rejected_credential_says_what_to_do(fake_airtable_rejecting, client, draft):
    r = put_destination(client, draft, {"kind": "airtable", "base_id": BASE_ID, "table": TABLE,
                                        "credential": "AIRTABLE_TOKEN"})
    assert r.status_code == 422 and "rejected the credential AIRTABLE_TOKEN" in r.json()["detail"]
    assert "a-token-the-base-does-not-accept" not in r.text
    assert client.get(f"/api/integrations/{draft}/draft").json()["destination"] is None


def test_a_token_id_instead_of_the_token_is_named(fake_airtable_rejecting, client, draft, monkeypatch):
    # Airtable's token list shows the token's ID (pat + 14 characters); the token itself adds a dot and a secret
    monkeypatch.setenv("AIRTABLE_TOKEN", "patAbCdEfGhIjKlMn")
    r = put_destination(client, draft, {"kind": "airtable", "base_id": BASE_ID, "table": TABLE,
                                        "credential": "AIRTABLE_TOKEN"})
    detail = r.json()["detail"]
    assert r.status_code == 422 and "looks like the token's ID" in detail and "patAbCdEfGhIjKlMn" not in detail
    r = client.get(f"/api/destinations/airtable/tables?base_id={BASE_ID}&credential=AIRTABLE_TOKEN")
    assert r.status_code == 422 and "looks like the token's ID" in r.json()["detail"]


def test_a_full_token_that_is_rejected_gets_no_id_hint(fake_airtable_rejecting, client, draft):
    r = put_destination(client, draft, {"kind": "airtable", "base_id": BASE_ID, "table": TABLE,
                                        "credential": "AIRTABLE_TOKEN"})
    assert r.status_code == 422 and "token's ID" not in r.json()["detail"]


def test_missing_table_names_the_tables(fake_airtable, client, draft):
    r = put_destination(client, draft, {"kind": "airtable", "base_id": BASE_ID, "table": "Nope",
                                        "credential": "AIRTABLE_TOKEN"})
    assert r.status_code == 422 and "not found" in r.json()["detail"] and TABLE in r.json()["detail"]


def test_unset_credential_is_422(fake_airtable, client, draft, monkeypatch):
    monkeypatch.delenv("AIRTABLE_TOKEN")
    r = put_destination(client, draft, {"kind": "airtable", "base_id": BASE_ID, "table": TABLE,
                                        "credential": "AIRTABLE_TOKEN"})
    assert r.status_code == 422 and "AIRTABLE_TOKEN is not set" in r.json()["detail"]


def test_bad_base_url_is_422(client, draft):
    r = put_destination(client, draft, {"kind": "airtable", "base_url": "https://example.com/x", "table": TABLE})
    assert r.status_code == 422 and "base" in r.json()["detail"].lower()


def test_airtable_tables_listed(fake_airtable, client):
    r = client.get(f"/api/destinations/airtable/tables?base_id={BASE_ID}&credential=AIRTABLE_TOKEN")
    assert r.status_code == 200 and {"name": TABLE} .items() <= r.json()[0].items() and r.json()[0]["id"]


def test_airtable_tables_with_rejected_credential(fake_airtable_rejecting, client):
    r = client.get(f"/api/destinations/airtable/tables?base_id={BASE_ID}&credential=AIRTABLE_TOKEN")
    assert r.status_code == 422 and "rejected the credential AIRTABLE_TOKEN" in r.json()["detail"]


# ---- API ----------------------------------------------------------------------------------------------

def test_api_operations_flatten_nested_body(papers_api, client):
    ops = client.get(f"/api/destinations/api/operations?base_url={papers_api}").json()
    post = next(o for o in ops if o["method"] == "POST")
    assert "authors.0.name" not in post["fields"] and {"title", "venue.name", "year"} <= set(post["fields"])
    assert "authors[]" in post["fields"] and "authors.name" not in post["fields"]  # arrays stay unflattened
    assert {"title", "venue.name", "year"} <= set(post["required"]) and "venue.city" not in post["required"]
    assert post["summary"] == "Add a paper"


def test_get_operations_are_not_offered(papers_api, client):
    ops = client.get(f"/api/destinations/api/operations?base_url={papers_api}").json()
    assert ops and all(o["method"] in ("POST", "PUT", "PATCH") for o in ops)
    assert {o["method"] for o in ops} == {"POST", "PUT"}


def test_api_destination_checks_the_chosen_operation(papers_api, client, draft):
    r = put_destination(client, draft, {"kind": "api", "base_url": papers_api,
                                        "operation": {"method": "post", "path": "/papers"}})
    assert r.status_code == 200, r.text
    body = r.json()
    by_name = {f["name"]: f for f in body["fields"]}
    assert by_name["venue.name"]["required"] is True and by_name["note"]["required"] is False
    assert by_name["year"]["type"] == "integer" and {o["path"] for o in body["operations"]} == {"/papers", "/papers/{pid}"}
    assert client.get(f"/api/integrations/{draft}/draft").json()["destination"]["operation"] == {
        "method": "POST", "path": "/papers"}


def test_api_unknown_operation_and_unreachable_host(papers_api, client, draft):
    r = put_destination(client, draft, {"kind": "api", "base_url": papers_api,
                                        "operation": {"method": "POST", "path": "/nope"}})
    assert r.status_code == 422 and "/nope" in r.json()["detail"]
    r = client.get("/api/destinations/api/operations?base_url=http://127.0.0.1:9")
    assert r.status_code == 422 and "couldn't reach 127.0.0.1:9" in r.json()["detail"]


# ---- SQLite and form -------------------------------------------------------------------------------------

def test_sqlite_destination(client, draft, workspace):
    with sqlite3.connect(workspace / "tracker.db") as db:
        db.execute("CREATE TABLE events (title TEXT NOT NULL, starts DATE, seats INTEGER)")
    r = put_destination(client, draft, {"kind": "sqlite", "path": "tracker.db", "table": "events"})
    assert r.status_code == 200, r.text
    fields = {f["name"]: f for f in r.json()["fields"]}
    assert fields["title"]["required"] is True and fields["seats"]["type"] == "integer"
    r = put_destination(client, draft, {"kind": "sqlite", "path": "tracker.db", "table": "missing"})
    assert r.status_code == 422 and "missing" in r.json()["detail"]


def test_form_destination(client, draft):
    from demo_apps.tracker_form.app import create_app

    with _Server(create_app(questions=[{"label": "Product", "required": True}, {"label": "Price"}])) as form:
        r = put_destination(client, draft, {"kind": "form", "url": form.url + "/"})
    assert r.status_code == 200, r.text
    assert [(f["name"], f["required"]) for f in r.json()["fields"]] == [("Product", True), ("Price", False)]


def test_unknown_kind_is_422(client, draft):
    assert put_destination(client, draft, {"kind": "carrier-pigeon"}).status_code == 422


def test_destination_unit_errors_never_carry_the_value(monkeypatch):
    monkeypatch.setenv("SHOP_TOKEN", "sentinel-cred-123456")
    with pytest.raises(destinations.DestinationError) as e:
        destinations.check({"kind": "api", "base_url": "http://127.0.0.1:9", "credential": "SHOP_TOKEN",
                            "operation": {"method": "POST", "path": "/x"}})
    assert "sentinel-cred-123456" not in str(e.value)


def test_a_credential_is_only_sent_when_the_request_names_it(client, draft, monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", "sentinel-airtable-123456")
    seen: list[str | None] = []
    app = papers_app()

    @app.middleware("http")
    async def spy(request, call_next):
        seen.append(request.headers.get("authorization"))
        return await call_next(request)

    with _Server(app) as srv:
        assert client.get(f"/api/destinations/api/operations?base_url={srv.url}").status_code == 200
        assert seen[-1] is None
        r = put_destination(client, draft, {"kind": "api", "base_url": srv.url,
                                            "operation": {"method": "POST", "path": "/papers"}})
        assert r.status_code == 200 and seen[-1] is None  # nothing named, nothing sent
        assert client.get(f"/api/destinations/api/operations?base_url={srv.url}&credential=AIRTABLE_TOKEN"
                          ).status_code == 200
        assert seen[-1] == "Bearer sentinel-airtable-123456"


def test_airtable_needs_an_explicit_credential(client, draft, fake_airtable):
    r = put_destination(client, draft, {"kind": "airtable", "base_id": BASE_ID, "table": TABLE})
    assert r.status_code == 422 and "credential" in r.json()["detail"]
    assert client.get(f"/api/destinations/airtable/tables?base_id={BASE_ID}").status_code == 422

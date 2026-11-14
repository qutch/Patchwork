"""V15 item 2-4: pages go to one integration. A product integration (fixtures/products) next to
jobs_to_tracker on the real control plane with the fake page runner and the fake learning agent:
product pages never trigger job runs/learns/rows and vice versa, untagged pages are jobs_to_tracker's, and
a jobs spec compiled before V15 (no `integration` param) sees exactly the jobs pages."""

from __future__ import annotations

import sqlite3

import yaml

from control.pages import PageInbox
from fixtures.products.pages import NAME as PRODUCT, SPEC, html as product_html, PAGES as PRODUCT_PAGES
from runner.store import Store
from tests.control.v2_fakes import (INDEX, LEVER, NAME, fake_runner, make_page_integration, page_html,  # noqa: F401
                                    records_by_page, v2agent, v2api, wait_idle)

SHOP_A = {"id": "shop_a", "match": {"site": "shop-a.example.com"}, "origin": {"kind": "fixture"},
          "fields": {"name": {"css": "h1.product-title"}, "price": {"jsonld": "offers.price", "jsonld_type": "Product"},
                     "released": {"jsonld": "releaseDate", "jsonld_type": "Product"},
                     "image": {"jsonld": "image", "jsonld_type": "Product"}}}


def product_spec() -> dict:
    spec = yaml.safe_load(SPEC.read_text())
    spec["sink"]["url"] = "http://127.0.0.1:9/"
    return spec


def setup_both() -> None:
    make_page_integration([LEVER])  # jobs_to_tracker as compiled before V15: no `integration` param
    make_page_integration(name=PRODUCT, spec=product_spec())


def token(api) -> str:
    return api.get("/api/extension").json()["token"]


def post(api, *, url: str, html: str, integration: str | None = None):
    body = {"url": url, "title": "t", "html": html, **({"integration": integration} if integration else {})}
    return api.post("/api/pages", json=body, headers={"X-Patchwork-Token": token(api)})


def post_job(api, file: str = "lever-1.html", url: str | None = None, integration: str | None = None) -> dict:
    r = post(api, url=url or INDEX[file]["url"], html=page_html(file), integration=integration)
    assert r.status_code == 201, r.text
    return r.json()


def post_product(api, file: str = "shop-a.html", integration: str | None = PRODUCT) -> dict:
    r = post(api, url=PRODUCT_PAGES[file]["url"], html=product_html(file), integration=integration)
    assert r.status_code == 201, r.text
    return r.json()


def test_extension_lists_page_integrations_with_their_intent(v2api):
    assert v2api.get("/api/extension").json()["integrations"] == []
    assert v2api.get("/api/extension").json()["default_integration"] is None
    setup_both()
    ext = v2api.get("/api/extension").json()
    assert ext["integrations"] == [{"name": NAME, "intent": "Add every job posting I send to my tracker"},
                                   {"name": PRODUCT, "intent": "Track product prices"}]
    assert ext["default_integration"] == NAME


def test_pages_route_to_their_integration_only(v2api):
    setup_both()
    job = post_job(v2api)
    prod = post_product(v2api)
    tagged_job = post_job(v2api, "lever-2.html", integration=NAME)
    assert job["integration"] == NAME and prod["integration"] == PRODUCT and tagged_job["integration"] == NAME
    wait_idle(v2api, NAME)
    wait_idle(v2api, PRODUCT)
    job_pages, prod_pages = set(records_by_page(NAME)), set(records_by_page(PRODUCT))
    assert job_pages == {job["id"], tagged_job["id"]}  # the jobs spec without the param: exactly the jobs pages
    assert prod_pages == {prod["id"]}
    assert records_by_page(PRODUCT)[prod["id"]][-1]["status"] == "success"
    # each integration's runs were triggered only by its own pages
    assert all(r["kind"] == "run" for r in Store().runs(PRODUCT))
    # GET /api/pages: no param = the default integration's pages (tagged + untagged)
    ids = lambda **p: [x["id"] for x in v2api.get("/api/pages", params=p).json()]  # noqa: E731
    assert ids() == [job["id"], tagged_job["id"]]
    assert ids(integration=NAME) == [job["id"], tagged_job["id"]]
    assert ids(integration=PRODUCT) == [prod["id"]]
    assert ids(integration="nope") == []
    pages = {p["id"]: p for p in v2api.get("/api/pages", params={"integration": PRODUCT}).json()}
    assert pages[prod["id"]]["integration"] == PRODUCT
    # tracker rows per integration, generic fields
    jobs_t = v2api.get("/api/tracker", params={"integration": NAME}).json()
    prod_t = v2api.get("/api/tracker", params={"integration": PRODUCT}).json()
    assert {r["page_id"] for r in jobs_t["rows"]} == {job["id"], tagged_job["id"]}
    assert [r["page_id"] for r in prod_t["rows"]] == [prod["id"]]
    assert jobs_t["totals"]["pages"] == 2 and prod_t["totals"]["pages"] == 1
    assert jobs_t["field_names"] == ["title", "company"] and prod_t["field_names"] == ["name", "price", "released", "image"]
    row = prod_t["rows"][0]
    assert row["status"] == "success" and list(row["fields"]) == ["name", "price", "released", "image"]
    assert row["fields"]["price"] == "price at shop-a.example.com"  # the fake runner's value, via sink.fields
    jrow = next(r for r in jobs_t["rows"] if r["page_id"] == job["id"])
    assert jrow["fields"]["company"] == jrow["company"] == "Company at jobs.lever.co"  # title/company stay
    assert v2api.get("/api/tracker").json()["rows"] == jobs_t["rows"]  # default: jobs_to_tracker
    # GET /api/pages/{id} answers for the page's own integration
    assert v2api.get(f"/api/pages/{prod['id']}").json()["row"]["status"] == "success"


def test_unknown_integration_is_refused(v2api):
    setup_both()
    r = post(v2api, url="https://x.org/1", html="<h1>x</h1>", integration="po_to_stockroom")
    assert r.status_code == 422 and "not a page integration" in r.json()["detail"]
    assert post(v2api, url="https://x.org/1", html="<h1>x</h1>", integration="").status_code == 201  # = untagged
    bad = v2api.post("/api/pages", json={"url": "https://x.org", "title": "t", "html": "<p>x</p>", "integration": 3},
                     headers={"X-Patchwork-Token": token(v2api)})
    assert bad.status_code == 422


def test_learns_follow_the_page_integration(v2api, v2agent):
    v2agent.learn = False  # record the calls only
    setup_both()
    unknown_shop = post(v2api, url="https://lumen-supply.example.org/lamps/bolt", html=product_html("shop-b.html"),
                        integration=PRODUCT).json()
    unknown_job = post_job(v2api, "ashby-1.html")
    wait_idle(v2api, NAME)
    wait_idle(v2api, PRODUCT)
    import time
    deadline = time.time() + 10
    while len(v2agent.learn_calls) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert sorted(v2agent.learn_calls) == sorted([(unknown_shop["id"], PRODUCT), (unknown_job["id"], NAME)])
    # a page of the other integration is never learned for this one
    assert v2api.app.state.hooks.maybe_learn(NAME, [unknown_shop["id"]]) == []
    assert v2api.app.state.hooks.maybe_learn(PRODUCT, [unknown_job["id"]]) == []
    assert set(PageInbox().learns(PRODUCT)) == {unknown_shop["id"]}


def test_demo_reset_per_integration(v2api, workspace):
    setup_both()
    for name in (NAME, PRODUCT):
        r = v2api.post("/api/demo/v2/save-baseline", params={"integration": name})
        assert r.status_code == 200 and r.json()["integration"] == name
    job = post_job(v2api)
    prod = post_product(v2api)
    wait_idle(v2api, NAME)
    wait_idle(v2api, PRODUCT)
    out = v2api.post("/api/demo/v2/reset", params={"integration": PRODUCT}).json()
    assert out["integration"] == PRODUCT and out["removed"]["pages"] == 1
    assert [p["id"] for p in PageInbox().list()] == [job["id"]]  # the jobs page is untouched
    assert Store().runs(PRODUCT) == [] and Store().runs(NAME)
    out = v2api.post("/api/demo/v2/reset").json()  # default: jobs_to_tracker
    assert out["integration"] == NAME and out["removed"]["pages"] == 1
    assert PageInbox().list() == [] and prod["id"]
    assert (workspace / "v2_baseline" / "baseline.json").exists()  # jobs' baseline where it always was
    assert v2api.post("/api/demo/v2/reset", params={"integration": "never_saved"}).status_code == 409
    assert v2api.post("/api/demo/v2/save-baseline", params={"integration": "Bad Name"}).status_code == 422
    assert v2api.get("/api/layouts", params={"integration": PRODUCT}).json()[0]["parser_id"] == "shop_a"


def test_old_pages_db_gains_the_integration_column(workspace):
    db = sqlite3.connect(workspace / "pages.db")
    db.executescript("""CREATE TABLE pages (seq INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
        canonical_url TEXT NOT NULL, site TEXT NOT NULL, title TEXT NOT NULL, html TEXT NOT NULL, text TEXT NOT NULL,
        captured_at TEXT NOT NULL, captured_date TEXT NOT NULL);
        INSERT INTO pages(url, canonical_url, site, title, html, text, captured_at, captured_date)
        VALUES ('https://jobs.lever.co/x/1', 'https://jobs.lever.co/x/1', 'jobs.lever.co', 't', '<h1>x</h1>', 'x',
                '2026-11-14T10:00:00.000+00:00', '2026-11-14');""")
    db.commit()
    db.close()
    inbox = PageInbox()
    old = inbox.list()
    assert len(old) == 1 and old[0]["integration"] is None
    new = inbox.add("https://shop-a.example.com/p/1", "t", "<h1>k</h1>", integration=PRODUCT)
    assert new["integration"] == PRODUCT and new["seq"] == 2
    assert [p["seq"] for p in inbox.list(integration=NAME, include_untagged=True)] == [1]
    assert [p["seq"] for p in inbox.list(integration=PRODUCT, include_untagged=False)] == [2]
    assert [p["seq"] for p in inbox.summaries(integration=PRODUCT, include_untagged=False)] == [2]
    assert inbox.clear(integration=PRODUCT, include_untagged=False) == 1 and len(inbox.list()) == 1

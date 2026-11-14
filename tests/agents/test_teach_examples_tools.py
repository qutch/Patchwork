"""TEACH from example pages (wizard approach A): `propose_mapping` and `add_site_parser` checked in code, the
review rows of `request_deploy`, and review edits applied by code (`apply_review_edits`). Product pages from two
sites (fixtures/products) into a fake Airtable table. No model, no langchain."""

from __future__ import annotations

import json

import pytest

from agents.deep_tools import apply_review_edits
from agents.learn import guess_reader
from demo_apps.fake_airtable.app import create_app
from fixtures.products.pages import page_record
from tests.agents.teach_examples_helpers import (PRODUCT_FIELDS, PRODUCT_LABELS, TOKEN, TeachCtx, airtable_destination,
                                                 product_pages, write_teach_draft)
from tests.conftest import _Server

NAME = "track_product_prices"
FULL_MAPPING = {"Product": {"field": "name", "reader": {"css": "h1.product-title"}},
                "Price": {"field": "price", "reader": {"jsonld": "offers.price", "jsonld_type": "Product"}},
                "Link": {"record": "canonical_url"}, "Date Added": {"today": True}, "Store": {"skip": True}}


@pytest.fixture
def fake_airtable(workspace, monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", TOKEN)
    monkeypatch.setenv("PATCHWORK_CRED__AIRTABLE_TOKEN", TOKEN)
    with _Server(create_app(fields=PRODUCT_FIELDS)) as srv:
        monkeypatch.setenv("PATCHWORK_AIRTABLE_API_URL", srv.url + "/v0")
        yield srv.url


def _teach_ctx(required=()) -> TeachCtx:
    rid, pids = write_teach_draft(NAME, product_pages(), airtable_destination(required=required), "Track product prices")
    return TeachCtx(NAME, rid, pids)


@pytest.fixture
def teach_ctx(fake_airtable):
    return _teach_ctx()


@pytest.fixture
def teach_ctx_required_store(fake_airtable):
    return _teach_ctx(required=("Store",))


@pytest.fixture
def teach_ctx_mapped(teach_ctx):
    assert teach_ctx.call("propose_mapping", mapping=FULL_MAPPING).startswith("OK")
    return teach_ctx


@pytest.fixture
def shop_b_pid(teach_ctx):
    return teach_ctx.page_ids[1]


@pytest.fixture
def shop_a_page():
    return page_record("shop-a.html", 1)


@pytest.fixture
def mapped_two_sites(teach_ctx_mapped, shop_b_pid):
    out = teach_ctx_mapped.call("add_site_parser", page_id=shop_b_pid, parser={"fields": {
        "name": {"jsonld": "name", "jsonld_type": "Product"},
        "price": {"jsonld": "offers.price", "jsonld_type": "Product"}}})
    assert out.startswith("OK"), out
    return teach_ctx_mapped


def _review_of(tc: TeachCtx) -> list[dict]:
    """The rows request_deploy shows, for the work spec compiled into a version (FakeEngine-free: real compile)."""
    from control import ops
    built = ops.compile_and_test(tc.ctx.spec_path)
    assert built["tests_passed"], built["tests_summary"]
    out = {}
    import threading
    th = threading.Thread(target=lambda: out.setdefault("r", tc.call("request_deploy", version=built["version"],
                                                                     summary="review")), daemon=True)
    th.start()
    import time
    deadline = time.time() + 60
    while tc.t.pending is None and time.time() < deadline:
        time.sleep(0.01)
    pending = tc.t.pending
    tc.svc.resume(tc.t.id, pending["interrupt_id"], {"decision": "reject", "comment": "test only"})
    th.join(10)
    return pending["review"]


@pytest.fixture
def review(mapped_two_sites):
    return _review_of(mapped_two_sites)


@pytest.fixture
def spec(mapped_two_sites, review):
    return mapped_two_sites.spec()


# ---- the brief's tests ------------------------------------------------------------------------------

def test_mapping_reader_checked_on_first_page(teach_ctx):
    out = teach_ctx.call("propose_mapping", mapping={
        "Product": {"field": "name", "reader": {"css": "h1.product-title"}},
        "Price": {"field": "price", "reader": {"jsonld": "offers.price", "jsonld_type": "Product"}},
        "Link": {"record": "canonical_url"}, "Date Added": {"today": True}, "Store": {"skip": True}})
    assert out.startswith("OK") and teach_ctx.spec()["extract"]["parsers"][0]["match"] == {"site": "shop-a.example.com"}


def test_reader_not_on_page_is_refused(teach_ctx):
    out = teach_ctx.call("propose_mapping", mapping={"Product": {"field": "name", "reader": {"css": "h1.nope"}}})
    assert out.startswith("REFUSED") and "Product" in out


def test_second_site_gets_its_own_parser_never_catch_all(teach_ctx_mapped, shop_b_pid):
    assert teach_ctx_mapped.call("add_site_parser", page_id=shop_b_pid,
                                 parser={"fields": {"name": {"css": "h1.product-title"}}}).startswith("REFUSED")
    assert teach_ctx_mapped.call("add_site_parser", page_id=shop_b_pid,
                                 parser={"match": {}, "fields": {"name": {"css": "h2.name"}}}).startswith("REFUSED")
    out = teach_ctx_mapped.call("add_site_parser", page_id=shop_b_pid, parser={"fields": {"name": {"css": "h2.name"},
                                "price": {"jsonld": "offers.price", "jsonld_type": "Product"}}})
    assert out.startswith("OK") and [p["match"] for p in teach_ctx_mapped.spec()["extract"]["parsers"]][1] == {"site": "lumen-supply.example.org"}


def test_required_field_skipped_is_refused(teach_ctx_required_store):
    out = teach_ctx_required_store.call("propose_mapping", mapping={"Store": {"skip": True}})
    assert "Store is required by the destination" in out


def test_review_edit_must_be_an_offered_alternative(review, spec):
    with pytest.raises(ValueError):
        apply_review_edits(spec, review, [{"dest_field": "Product", "choice": {"css": "body"}}])
    assert apply_review_edits(spec, review, [{"dest_field": "Store", "choice": {"const": "Acme"}}])["sink"]["fields"]["Store"] == {"const": "Acme"}


def test_guess_reader_finds_product_price(shop_a_page):
    assert guess_reader("Price", "number", shop_a_page) == {"jsonld": "offers.price", "jsonld_type": "Product"}


# ---- more checks --------------------------------------------------------------------------------------

def test_mapping_writes_a_valid_checked_spec_and_golden(teach_ctx_mapped):
    from spec import validate_spec
    tc = teach_ctx_mapped
    spec = tc.spec()
    assert validate_spec(tc.ctx.spec_path.read_text()) == []
    (p,) = spec["extract"]["parsers"]
    assert p["id"] == "shop_a" and p["origin"]["kind"] == "taught" and set(p["fields"]) == {"name", "price"}
    assert spec["sink"]["kind"] == "airtable" and spec["sink"]["auth"] == {"scheme": "bearer", "credential": "AIRTABLE_TOKEN"}
    assert spec["sink"]["fields"] == {"Product": {"from": "name"}, "Price": {"from": "price"},
                                      "Link": {"from": "record.canonical_url"}, "Date Added": {"from": "record.captured_date"}}
    assert {"field": "price", "rule": "number"} in spec["validate"]
    assert spec["source"]["list"]["params"]["integration"] == NAME
    import yaml
    (ex,) = yaml.safe_load((tc.ctx.wd / "examples.yaml").read_text())
    assert ex["record"]["id"] == tc.page_ids[0]
    assert ex["expect"]["body"]["Price"] == 24.99 and ex["expect"]["body"]["Product"] == "Acme Steel Kettle 1.7 L"
    assert any(e["type"] == "evidence" and e["ok"] for e in tc.events())


def test_wrong_type_and_literal_capture_are_refused(teach_ctx):
    out = teach_ctx.call("propose_mapping", mapping={
        "Product": {"field": "name", "reader": {"css": "h1.product-title"}},
        "Price": {"field": "price", "reader": {"css": "h1.product-title"}}})  # text where a number goes
    assert out.startswith("REFUSED") and "Price" in out and "Product" not in out.split("REFUSED: ")[1][:8]
    out = teach_ctx.call("propose_mapping", mapping={
        "Product": {"field": "name", "reader": {"css": "head title", "regex": "(Acme Steel Kettle)"}}})
    assert out.startswith("REFUSED") and "fixed text" in out
    out = teach_ctx.call("propose_mapping", mapping={"Product": {"field": "name", "reader": {"css": "p"}}})
    assert out.startswith("REFUSED") and "several values" in out
    out = teach_ctx.call("propose_mapping", mapping={"Price": {"const": "cheap"}, "Product": {"field": "name", "reader": {"css": "h1"}}})
    assert out.startswith("REFUSED") and "Price" in out
    assert not teach_ctx.ctx.spec_path.exists(), "a refused mapping writes nothing"


def test_unknown_destination_field_and_wrong_mode(teach_ctx, workspace):
    out = teach_ctx.call("propose_mapping", mapping={"Nope": {"skip": True}, "Product": {"field": "name", "reader": {"css": "h1"}}})
    assert out.startswith("REFUSED") and "Nope: not a destination field" in out
    from agents.deep_tools import ToolContext, tool_functions
    other = ToolContext(teach_ctx.svc, teach_ctx.t, name=NAME, recording_id="fixture-jobs")
    assert tool_functions(other)["propose_mapping"](mapping={"x": {}}).startswith("REFUSED")


def test_add_site_parser_refuses_other_pages_and_duplicates(teach_ctx_mapped, shop_b_pid):
    tc = teach_ctx_mapped
    assert tc.call("add_site_parser", page_id="pg-999999", parser={"fields": {}}).startswith("REFUSED")
    assert "already reads" in tc.call("add_site_parser", page_id=tc.page_ids[0], parser={"fields": {
        "name": {"css": "h1.product-title"}, "price": {"jsonld": "offers.price", "jsonld_type": "Product"}}})
    # the value must be on that page: a reader that only works on shop-a fails on shop-b
    out = tc.call("add_site_parser", page_id=shop_b_pid, parser={"fields": {
        "name": {"css": 'meta[property="og:site_name"]', "attr": "content", "regex": "(Lumen Supply)"},
        "price": {"jsonld": "offers.price", "jsonld_type": "Product"}}})
    assert out.startswith("REFUSED"), out
    assert len(tc.spec()["extract"]["parsers"]) == 1


def test_review_rows(review, mapped_two_sites):
    tc = mapped_two_sites
    a, b = tc.page_ids
    rows = {r["dest_field"]: r for r in review}
    assert list(rows) == PRODUCT_LABELS
    assert rows["Price"]["values"] == {a: 24.99, b: 89} and rows["Price"]["status"] == "checked"
    assert rows["Product"]["readers"] == {a: {"css": "h1.product-title"}, b: {"jsonld": "name", "jsonld_type": "Product"}}
    assert rows["Link"]["source"]["record"] == "canonical_url" and rows["Date Added"]["source"]["today"] is True
    assert rows["Store"]["source"]["skip"] is True and rows["Store"]["status"] == "needs_you"
    assert all(r["ai_proposed"] is True for r in review)
    assert {"skip": True, "label": "skip this field"} in rows["Product"]["alternatives"]
    # og:site_name reads the store on both example pages: offered for Store, checked on both
    og_site = {"field": "store", "reader": {"css": 'meta[property="og:site_name"]', "attr": "content"}}
    assert og_site in [{k: v for k, v in a.items() if k not in ("label", "values")} for a in rows["Store"]["alternatives"]]
    assert all(a["label"] for r in review for a in r["alternatives"]) and rows["Link"]["source"]["label"] == "the page's address"
    for alt in rows["Price"]["alternatives"]:
        assert "reader" not in alt or alt["reader"].get("jsonld") is not None


def test_review_edits_then_compile(review, spec, mapped_two_sites):
    og_site = {"field": "store", "reader": {"css": 'meta[property="og:site_name"]', "attr": "content"}}
    new = apply_review_edits(spec, review, [{"dest_field": "Store", "choice": og_site},
                                            {"dest_field": "Date Added", "choice": {"skip": True}}])
    assert new["sink"]["fields"]["Store"] == {"from": "store"} and "Date Added" not in new["sink"]["fields"]
    assert all(p["fields"]["store"] == og_site["reader"] for p in new["extract"]["parsers"])
    with pytest.raises(ValueError):  # a constant must fit the field's type
        apply_review_edits(spec, review, [{"dest_field": "Price", "choice": {"const": "cheap"}}])
    with pytest.raises(ValueError):
        apply_review_edits(spec, review, [{"dest_field": "Nope", "choice": {"skip": True}}])
    # skipping the only page fields leaves nothing to read
    with pytest.raises(ValueError):
        apply_review_edits(spec, review, [{"dest_field": "Product", "choice": {"skip": True}},
                                          {"dest_field": "Price", "choice": {"skip": True}}])
    # a skipped page field disappears from the parsers and the rules
    new = apply_review_edits(spec, review, [{"dest_field": "Price", "choice": {"skip": True}}])
    assert all(set(p["fields"]) == {"name"} for p in new["extract"]["parsers"])
    assert all(r["field"] != "price" for r in new["validate"])


def test_approve_with_edits_compiles_and_deploys_without_a_model(mapped_two_sites):
    from control import ops
    from runner.store import Store
    tc = mapped_two_sites
    built = ops.compile_and_test(tc.ctx.spec_path)
    assert built["tests_passed"]
    og_site = {"field": "store", "reader": {"css": 'meta[property="og:site_name"]', "attr": "content"}}
    import threading
    import time
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", tc.call("request_deploy", version=built["version"],
                                                                     summary="Taught from 2 example pages")))
    th.start()
    deadline = time.time() + 60
    while tc.t.pending is None and time.time() < deadline:
        time.sleep(0.01)
    tc.svc.resume(tc.t.id, tc.t.pending["interrupt_id"], {"decision": "approve", "edits": [
        {"dest_field": "Store", "choice": og_site}]})
    th.join(120)
    assert out["r"].startswith("deployed v2"), out["r"]
    assert Store().active_version(NAME) == 2
    v2 = ops.version_dir(NAME, 2)
    import yaml
    spec2 = yaml.safe_load((v2 / "spec.yaml").read_text())
    assert spec2["sink"]["fields"]["Store"]["from"] == "store"
    gold = {g["record"]["id"]: g["expect"]["body"] for g in json.loads((v2 / "golden.json").read_text())}
    assert gold[tc.page_ids[0]]["Store"] == "Acme Home Store" and gold[tc.page_ids[1]]["Store"] == "Lumen Supply"
    deployed = [e for e in tc.events() if e["type"] == "deployed"]
    assert deployed and deployed[-1]["version"] == 2


def test_approve_with_an_edit_not_offered_deploys_nothing(mapped_two_sites):
    from control import ops
    from runner.store import Store
    tc = mapped_two_sites
    built = ops.compile_and_test(tc.ctx.spec_path)
    import threading
    import time
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", tc.call("request_deploy", version=built["version"], summary="x")))
    th.start()
    deadline = time.time() + 60
    while tc.t.pending is None and time.time() < deadline:
        time.sleep(0.01)
    tc.svc.resume(tc.t.id, tc.t.pending["interrupt_id"], {"decision": "approve", "edits": [
        {"dest_field": "Product", "choice": {"field": "name", "reader": {"css": "body"}}}]})
    th.join(60)
    assert out["r"].startswith("REFUSED") and Store().active_version(NAME) is None


def test_tool_results_are_redacted(teach_ctx, monkeypatch):
    """Tool results pass through the redaction wrapper like every other tool's."""
    from agents import deep_tools
    assert deep_tools.tool_functions(teach_ctx.ctx)["propose_mapping"].__wrapped__ is not None
    assert "propose_mapping" in deep_tools.TOOL_DOCS and "add_site_parser" in deep_tools.TOOL_DOCS


def test_an_echoed_alternative_is_applied_but_a_tampered_one_is_refused(review, spec, mapped_two_sites):
    """The UI sends back the alternative exactly as shown (with `label` and `values`): those keys are ignored when
    matching; everything else must be an offered alternative."""
    a, b = mapped_two_sites.page_ids
    rows = {r["dest_field"]: r for r in review}
    shown = next(alt for alt in rows["Store"]["alternatives"]
                 if (alt.get("reader") or {}).get("css", "").startswith("meta"))
    assert shown["label"].startswith("page: meta") and set(shown["values"]) == {a, b}
    new = apply_review_edits(spec, review, [{"dest_field": "Store", "choice": shown}])
    assert new["sink"]["fields"]["Store"] == {"from": shown["field"]}
    tampered = {**shown, "reader": {**shown["reader"], "regex": "(.+)"}}
    with pytest.raises(ValueError):
        apply_review_edits(spec, review, [{"dest_field": "Store", "choice": tampered}])
    today = next(alt for alt in rows["Product"]["alternatives"] if "skip" in alt)
    assert "Product" not in apply_review_edits(spec, review, [{"dest_field": "Product", "choice": {
        **today, "values": {"x": 1}}}])["sink"]["fields"]
    assert rows["Link"]["source"]["label"] == "the page's address" and rows["Link"]["values"][a].startswith("https://shop-a")
    with pytest.raises(ValueError):  # display keys alone are no choice
        apply_review_edits(spec, review, [{"dest_field": "Store", "choice": {"label": "x", "values": {}}}])

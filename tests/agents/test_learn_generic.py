"""V15 items 1, 5, 6 on the agent side: LEARN/TEACH for an integration that is not about jobs (products).

Prompts and tools name the integration's own intent and fields (never job wording), get_page lists every
JSON-LD type, learned readers carry an explicit jsonld_type, the new validation rules are part of the
evidence check, TEACH writes the integration param, and a page recording's default build name is its intent
slug. No model: the scripted agent and tests.agents.jobs.FakeEngine (real Store, real deploy)."""

from __future__ import annotations

import inspect
import json
import re

import pytest
import yaml

from fixtures.products.pages import FORM_QUESTIONS, NAME as PRODUCT, SPEC, examples, page_record
from runner.store import Store
from tests.agents.jobs import NAME as JOBS, READERS, FakeEngine, active_spec, form_contract, page

JOB_WORDS = re.compile(r"\bjob|posting|hiring|company", re.I)


def product_contract() -> dict:
    return form_contract(labels=[q["label"] for q in FORM_QUESTIONS], required=("Product",))


class ProductHarness:
    def __init__(self, monkeypatch, file: str = "shop-b.html", spec: dict | None = None):
        from agents.deep_tools import ToolContext, seed_learn_workdir, tool_functions
        from agents.interface import _Thread
        from agents.learn import ydump
        from agents.scripted import ScriptedAgentService, work_dir
        from control import ops
        self.engine = FakeEngine().install(monkeypatch)
        self.engine.contract = product_contract()
        self.rec = self.engine.add_page(page_record(file, seq=7))
        self.spec = spec or yaml.safe_load(SPEC.read_text())
        wd = work_dir(PRODUCT)
        (wd / "spec.yaml").write_text(ydump(self.spec, sort_keys=False, width=200))
        (wd / "examples.yaml").write_text(ydump(examples(), sort_keys=False, width=200))
        ops.deploy(PRODUCT, self.engine.compile(wd / "spec.yaml")["version"])
        self.svc = ScriptedAgentService(pace_s=0)
        self.t = _Thread(id="learn-prod0001", mode="learn", subject=self.rec["id"], integration=PRODUCT)
        self.svc._threads[self.t.id] = self.t
        seed_learn_workdir(PRODUCT)
        self.ctx = ToolContext(self.svc, self.t, name=PRODUCT, page_id=self.rec["id"])
        self.tools = tool_functions(self.ctx)

    def active(self) -> dict:
        v = Store().active_version(PRODUCT)
        from runner.store import workspace_dir
        return yaml.safe_load((workspace_dir() / "integrations" / PRODUCT / f"v{v}" / "spec.yaml").read_text())


def test_get_page_names_the_integration_fields_and_every_jsonld_type(workspace, monkeypatch):
    h = ProductHarness(monkeypatch)
    out = h.tools["get_page"]()
    assert "json-ld types on this page: WebSite, Product" in out
    assert "fields to extract (exactly these): name, price, released, image" in out
    assert "integration: track_product_prices (Track product prices)" in out
    assert "price: number (min 0, max 100000)" in out and "image: url" in out and "released: date" in out
    assert '{jsonld: "offers.price", jsonld_type: Product} → "89.00"' in out
    assert not JOB_WORDS.search(out), JOB_WORDS.search(out)


def test_learn_prompts_have_no_job_wording_for_a_product_integration():
    from agents import prompts
    spec = yaml.safe_load(SPEC.read_text())
    texts = [prompts.learner_prompt(spec, PRODUCT), prompts.learner_description(spec), prompts.LEARN_SUPERVISOR,
             prompts.learn_task(page_id="pg-000007", name=PRODUCT, site="lumen-supply.example.org", active=1, spec=spec),
             prompts.LEARN_NUDGE]
    for text in texts:
        assert not JOB_WORDS.search(text), (JOB_WORDS.search(text), text)
    learner = prompts.learner_prompt(spec, PRODUCT)
    assert "`name`, `price`, `released`, `image`" in learner and "Track product prices" in learner
    assert '"jsonld_type": "Product"' in learner
    # the job integration keeps its tuned prompt
    from tests.agents.jobs import jobs_spec
    assert "job title and company" in prompts.learner_prompt(jobs_spec(), JOBS)


def test_learn_tool_descriptions_have_no_job_wording(workspace, monkeypatch):
    h = ProductHarness(monkeypatch)
    for tool in ("get_page", "test_page_parser", "save_parser"):
        doc = inspect.getdoc(h.tools[tool]) or ""
        assert not JOB_WORDS.search(doc), (tool, doc)


def test_scripted_learner_reads_a_new_shop_and_saves_typed_readers(workspace, monkeypatch):
    h = ProductHarness(monkeypatch)
    parser = h.svc.propose_parser(h.rec, h.active())
    assert parser is not None and parser["id"] == "example"
    assert parser["fields"]["price"] == {"jsonld": "offers.price", "jsonld_type": "Product"}
    tested = h.tools["test_page_parser"](parser=parser)
    assert tested.startswith("OK"), tested
    out = h.tools["save_parser"](parser=parser, reason="new shop")
    assert out.startswith("deployed v2 automatically"), out
    learned = next(p for p in h.active()["extract"]["parsers"] if p["id"] == "example")
    assert learned["match"] == {"site": "lumen-supply.example.org"}
    assert all(f.get("jsonld_type") == "Product" for f in learned["fields"].values() if "jsonld" in f)
    gold = yaml.safe_load((h.ctx.wd / "examples.yaml").read_text())
    ex = next(e for e in gold if e["name"].startswith("learned_example"))
    assert ex["expect"]["body"]["Product"] == "Bolt Desk Lamp" and ex["expect"]["body"]["Price"] == "89.00"


def test_save_parser_fills_a_missing_jsonld_type_from_the_page(workspace, monkeypatch):
    h = ProductHarness(monkeypatch)
    untyped = {"id": "lumen", "fields": {"name": {"css": "h2.name"}, "price": {"jsonld": "offers.price"},
                                         "released": {"jsonld": "releaseDate"}, "image": {"jsonld": "image"}}}
    assert h.tools["test_page_parser"](parser=untyped).startswith("OK")  # read from Product, not JobPosting
    assert h.tools["save_parser"](parser=untyped).startswith("deployed v2")
    learned = next(p for p in h.active()["extract"]["parsers"] if p["id"] == "lumen")
    assert learned["fields"]["price"] == {"jsonld": "offers.price", "jsonld_type": "Product"}
    assert learned["fields"]["name"] == {"css": "h2.name"}


def test_job_learn_writes_jobposting_explicitly(workspace, monkeypatch):
    from tests.agents.test_learn_tools import Harness, learned_parser
    h = Harness(monkeypatch, "ashby-1", baseline=("lever", "greenhouse"))
    assert READERS["ashby"]["title"] == {"jsonld": "title"}
    out = h.tools["save_parser"](parser={"id": "ashby", "fields": READERS["ashby"]})
    assert out.startswith("deployed"), out
    p = learned_parser(active_spec(), "ashby")
    assert p["fields"]["title"] == {"jsonld": "title", "jsonld_type": "JobPosting"}


def test_evidence_runs_the_new_rules(workspace, monkeypatch):
    h = ProductHarness(monkeypatch)
    bad = {"id": "lumen", "fields": {"name": {"css": "h2.name"}, "price": {"css": ".cost"},
                                     "released": {"jsonld": "releaseDate", "jsonld_type": "Product"},
                                     "image": {"css": ".gallery img", "attr": "alt"}}}
    out = h.tools["test_page_parser"](parser=bad)
    assert out.startswith("FAILED")
    report = json.loads(out.split(": ", 1)[1].split("\nnext:")[0])
    assert "fails validation: expected a number" in report["fields"]["price"]["problem"]
    assert "fails validation: expected an absolute http(s) URL" in report["fields"]["image"]["problem"]
    assert "problem" not in report["fields"]["released"]
    assert h.tools["save_parser"](parser=bad).startswith("REFUSED: evidence check failed")


def test_check_rules_positive_and_negative():
    from agents.learn import _check_rules
    rules = [{"field": "price", "rule": "number", "min": 0, "max": 100}, {"field": "d", "rule": "date"},
             {"field": "u", "rule": "url"}]
    assert _check_rules("price", "99.5", rules) is None
    assert "above the maximum" in _check_rules("price", "101", rules)
    assert "below the minimum" in _check_rules("price", -1, rules)
    assert "expected a number" in _check_rules("price", "cheap", rules)
    assert _check_rules("d", "2026-11-14", rules) is None
    assert _check_rules("d", "Nov 14", rules)
    assert _check_rules("u", "https://x.org/a", rules) is None
    assert _check_rules("u", "x.org/a", rules)


def test_outline_lists_preferred_types_first():
    from agents.learn import infer_jsonld_type, jsonld_types, page_outline
    rec = page_record("shop-a.html")
    assert jsonld_types(rec["html"]) == ["BreadcrumbList", "Product", "Organization"]
    out = page_outline(rec, prefer_types=["Product"])
    assert "json-ld types on this page: BreadcrumbList, Product, Organization" in out
    first = next(line for line in out.splitlines() if line.strip().startswith("{jsonld:"))
    assert "jsonld_type: Product" in first
    assert infer_jsonld_type(rec["html"], "offers.price") == "Product"
    assert infer_jsonld_type(rec["html"], "itemListElement[0].name") == "BreadcrumbList"
    assert infer_jsonld_type(rec["html"], "nope") is None
    job = page("ashby-1")
    assert infer_jsonld_type(job["html"], "title") == "JobPosting"


# ---- TEACH ----------------------------------------------------------------------------------------

def _product_candidates(url: str) -> dict:
    return {"recording_id": "rec-prod", "intent": "Track product prices",
            "source": {"kind": "page", "site": "shop-a.example.com", "url": url, "snapshot": "snapshots/1.html"},
            "sink": {"kind": "form", "url": "http://127.0.0.1:9/", "labels": ["Product", "Price", "Link"],
                     "submit": "Submit", "confirmation_text": "Your response has been recorded"},
            "mappings": [
                {"dest_field": "Product", "value": "Acme Steel Kettle 1.7 L", "kind": "css", "css": "h1.product-title"},
                {"dest_field": "Price", "value": "24.99", "kind": "jsonld", "jsonld": "offers.price"},
                {"dest_field": "Link", "value": "x", "kind": "record", "from": "record.canonical_url"}],
            "unexplained": []}


def test_teach_writes_the_integration_param_and_typed_readers():
    from agents.deep_tools import draft_page_spec
    from fixtures.products.pages import PAGES, html
    c = _product_candidates(PAGES["shop-a.html"]["url"])
    spec, exs, problems = draft_page_spec(c, name="track_product_prices", thread_id="build-1", html=html("shop-a.html"))
    assert problems == []
    assert spec["source"]["list"]["params"] == {"since": "{cursor}", "integration": "track_product_prices"}
    assert spec["extract"]["parsers"][0]["fields"]["price"] == {"jsonld": "offers.price", "jsonld_type": "Product"}
    assert exs[0]["expect"]["body"]["Price"] == "24.99"
    assert not JOB_WORDS.search(json.dumps(spec["assumptions"]))


@pytest.mark.parametrize("intent,slug", [("Track product prices", "track_product_prices"),
                                         ("  Save recipes I like!  ", "save_recipes_i_like"),
                                         ("3D printers to buy", "p_3d_printers_to_buy"),
                                         ("Keep every paper I read about protein folding and its applications",
                                          "keep_every_paper_i_read_about_protein"),
                                         ("", "")])
def test_intent_slug(intent, slug):
    from agents.deep_tools import intent_slug
    assert intent_slug(intent) == slug
    assert len(slug) <= 40


def test_default_build_name_for_page_recordings(workspace):
    from agents.deep_tools import default_build_name
    from runner.store import workspace_dir
    d = workspace_dir() / "recordings" / "rec-prod"
    d.mkdir(parents=True)
    (d / "candidates.json").write_text(json.dumps(_product_candidates("https://shop-a.example.com/p/1")))
    assert default_build_name("rec-prod", "po_to_stockroom") == "track_product_prices"
    assert default_build_name("fixture-jobs", "po_to_stockroom") == JOBS
    assert default_build_name("fixture", "po_to_stockroom") == "po_to_stockroom"

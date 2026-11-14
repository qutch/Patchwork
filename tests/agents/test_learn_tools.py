"""LEARN tools and the D2 guardrail (contracts V10), called directly: no model, no langchain.

The engine pieces W1 owns (spec validation for css/form specs, the compiler) are replaced by
tests.agents.jobs.FakeEngine, which still registers real versions in the Store, so `control.ops.deploy`
and request_deploy's "tests passed on this exact version" check run for real."""

from __future__ import annotations

import json
import threading
import time

import pytest
import yaml

from runner.store import Store
from tests.agents.jobs import (NAME, READERS, W1_READY, FakeEngine, active_spec, golden, jobs_spec, page,
                              set_learn_policy)


class Harness:
    def __init__(self, monkeypatch, page_name: str, baseline=("lever", "ashby"), html_patch=None):
        from agents.deep_tools import ToolContext, seed_learn_workdir, tool_functions
        from agents.interface import _Thread
        from agents.scripted import ScriptedAgentService
        self.engine = FakeEngine().install(monkeypatch)
        rec = page(page_name, seq=7) if isinstance(page_name, str) else page_name
        if html_patch:
            from agents.learn import make_page_record
            rec = make_page_record(rec["url"], html_patch(rec["html"]), seq=7, captured_at=rec["captured_at"])
        self.rec = self.engine.add_page(rec)
        gold = [golden(f"{p}-1") for p in baseline if p != "greenhouse_branded"]
        self.v1 = self.engine.deploy_baseline(jobs_spec(baseline), gold)
        self.svc = ScriptedAgentService(pace_s=0)
        self.t = _Thread(id="learn-test0001", mode="learn", subject=rec["id"], integration=NAME)
        self.svc._threads[self.t.id] = self.t
        seed_learn_workdir(NAME)
        self.ctx = ToolContext(self.svc, self.t, name=NAME, page_id=rec["id"])
        self.tools = tool_functions(self.ctx)

    def events(self, type_=None):
        return [e for e in self.svc.events(self.t.id) if type_ is None or e["type"] == type_]

    def call_async(self, tool, **kw):
        box = {}
        th = threading.Thread(target=lambda: box.setdefault("out", self.tools[tool](**kw)), daemon=True)
        th.start()
        return th, box

    def wait_pending(self, th, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.t.pending is not None:
                return self.t.pending
            if not th.is_alive():
                return None
            time.sleep(0.02)
        raise AssertionError("no interrupt")

    def answer(self, payload):
        self.svc.resume(self.t.id, self.t.pending["interrupt_id"], payload)


def learned_parser(spec, pid):
    return next(p for p in spec["extract"]["parsers"] if p["id"] == pid)


def test_get_page_is_an_outline_with_the_fields_to_extract(workspace, monkeypatch):
    h = Harness(monkeypatch, "greenhouse-1")
    out = h.tools["get_page"]()
    assert len(out) < 4600 and "<div" not in out
    assert "Account Executive, Mobile Gaming" in out and "Discord" in out
    assert "fields to extract (exactly these): title, company" in out and "lever (jobs.lever.co)" in out


def test_test_page_parser_reports_checks_and_emits_evidence(workspace, monkeypatch):
    h = Harness(monkeypatch, "greenhouse-1")
    out = h.tools["test_page_parser"](parser={"id": "greenhouse", "fields": READERS["greenhouse"]})
    assert out.startswith("OK") and '"corroborated": true' in out
    ev = h.events("evidence")[-1]
    assert ev["ok"] and ev["page_id"] == "pg-000007" and ev["parser_id"] == "greenhouse"
    assert [(f["name"], f["value"], f["ok"]) for f in ev["fields"]] == [
        ("title", "Account Executive, Mobile Gaming", True), ("company", "Discord", True)]
    miss = h.tools["test_page_parser"](parser={"fields": {"title": {"css": ".nope"}, "company": {"jsonld": "x"}}})
    assert miss.startswith("FAILED")


def test_save_parser_auto_deploys_when_clean(workspace, monkeypatch):
    h = Harness(monkeypatch, "greenhouse-1")
    out = h.tools["save_parser"](parser={"id": "greenhouse", "fields": READERS["greenhouse"]}, reason="new site")
    assert out.startswith("deployed v2 automatically"), out
    assert Store().active_version(NAME) == 2
    p = learned_parser(active_spec(), "greenhouse")
    assert p["match"] == {"site": "job-boards.greenhouse.io"}
    assert p["origin"]["kind"] == "learned" and p["origin"]["approved_by"] == "auto"
    assert p["origin"]["thread_id"] == h.t.id and p["origin"]["at"]
    types = [e["type"] for e in h.events()]
    assert types.index("evidence") < types.index("learned") < types.index("deployed")
    learned = h.events("learned")[-1]
    assert learned["approval"] == "auto" and learned["deployed"] is True and learned["version"] == 2
    assert learned["site"] == "job-boards.greenhouse.io" and learned["parser_id"] == "greenhouse"
    assert h.events("request_deploy") == [] and h.events("ask_user") == []
    # the new version carries a golden example for the page, computed from evidenced values
    gold = yaml.safe_load((h.ctx.wd / "examples.yaml").read_text())
    ex = next(e for e in gold if e["name"].startswith("learned_greenhouse"))
    assert ex["expect"]["body"]["Company"] == "Discord" and ex["expect"]["body"]["Link"] == h.rec["canonical_url"]
    assert h.ctx.learn_result == {"page_id": "pg-000007", "parser_id": "greenhouse", "site": "job-boards.greenhouse.io",
                                  "version": 2, "deployed": True, "approval": "auto"}
    assert h.tools["save_parser"](parser={"id": "g2", "fields": READERS["greenhouse"]}).startswith("REFUSED")


def test_save_parser_asks_when_ambiguous(workspace, monkeypatch):
    h = Harness(monkeypatch, "ashby-1", baseline=("lever", "greenhouse"))
    th, box = h.call_async("save_parser", parser={"id": "ashby", "fields": {
        "title": {"css": "h1"}, "company": {"jsonld": "hiringOrganization.name"}}})
    ask = h.wait_pending(th)
    assert ask["type"] == "ask_user" and "title" in ask["question"]
    assert {"Software Engineer, Security, Cloud", "About Ramp", "About the Role"} <= set(ask["options"])
    h.answer({"answer": "Software Engineer, Security, Cloud"})
    time.sleep(0.05)
    req = h.wait_pending(th)
    assert req["type"] == "request_deploy" and "ambiguous: title" in req["summary"]
    assert req["tests"]["passed"] and req["version"] == 2
    assert Store().active_version(NAME) == 1  # not deployed while a person decides
    assert h.events("learned")[-1]["approval"] == "pending"
    h.answer({"decision": "reject", "comment": "use the heading class"})
    th.join(10)
    assert box["out"].startswith("REJECTED") and Store().active_version(NAME) == 1
    last = h.events("learned")[-1]
    assert last["approval"] == "human" and last["deployed"] is False


def test_wrong_answer_to_ambiguity_refuses(workspace, monkeypatch):
    h = Harness(monkeypatch, "ashby-1", baseline=("lever", "greenhouse"))
    th, box = h.call_async("save_parser", parser={"id": "ashby", "fields": {
        "title": {"css": "h1"}, "company": {"jsonld": "hiringOrganization.name"}}})
    h.wait_pending(th)
    h.answer({"answer": "About Ramp"})
    th.join(10)
    assert box["out"].startswith("REFUSED: the user says title is 'About Ramp'")
    assert Store().latest_version(NAME) == 1


def test_disagreeing_candidates_make_a_field_ambiguous(workspace, monkeypatch):
    h = Harness(monkeypatch, "lever-1", baseline=("greenhouse", "ashby"))
    assert h.tools["test_page_parser"](parser={"fields": READERS["lever"]}).startswith("OK")
    other = {"title": {"css": ".posting-categories .location"}, "company": READERS["lever"]["company"]}
    h.tools["test_page_parser"](parser={"fields": other})
    assert set(h.ctx.ambiguous["title"]) == {"American Tech Fellowship", "North America"}
    th, box = h.call_async("save_parser", parser={"id": "lever", "fields": READERS["lever"]})
    assert h.wait_pending(th)["type"] == "ask_user"
    h.answer({"answer": "American Tech Fellowship"})
    time.sleep(0.05)
    assert h.wait_pending(th)["type"] == "request_deploy"
    h.answer({"decision": "approve"})
    th.join(10)
    assert Store().active_version(NAME) == 2
    assert learned_parser(active_spec(), "lever")["origin"]["approved_by"] == "human"


def test_save_parser_asks_when_not_corroborated(workspace, monkeypatch):
    """Review focus 4: a reader that picks page chrome (here the cookie banner link) is never auto-used."""
    h = Harness(monkeypatch, "lever-1", baseline=("greenhouse", "ashby"))
    chrome = {"title": READERS["lever"]["title"], "company": {"css": "[class*=cookie]"}}
    th, box = h.call_async("save_parser", parser={"id": "lever", "fields": chrome})
    req = h.wait_pending(th)
    assert req["type"] == "request_deploy" and "not corroborated" in req["summary"]
    ev = h.events("evidence")[-1]
    assert ev["ok"] is True and [f["corroborated"] for f in ev["fields"]] == [True, False]
    assert Store().active_version(NAME) == 1
    h.answer({"decision": "reject"})
    th.join(10)
    assert Store().active_version(NAME) == 1


def test_policy_approve_always_asks(workspace, monkeypatch):
    h = Harness(monkeypatch, "greenhouse-1")
    set_learn_policy("approve")
    th, box = h.call_async("save_parser", parser={"id": "greenhouse", "fields": READERS["greenhouse"]})
    req = h.wait_pending(th)
    assert req["type"] == "request_deploy" and "learn_policy is 'approve'" in req["summary"]
    assert any(op["op"] == "add" and op["path"].startswith("/extract/parsers/") for op in req["spec_patch"])
    h.answer({"decision": "approve"})
    th.join(10)
    assert box["out"].startswith("deployed v2")
    assert Store().active_version(NAME) == 2
    assert learned_parser(active_spec(), "greenhouse")["origin"]["approved_by"] == "human"
    assert [e["approval"] for e in h.events("learned")] == ["pending", "human"]
    assert h.events("deployed")


def test_evidence_failure_refuses_compile(workspace, monkeypatch):
    """A value the visitor can't see (inside <noscript>) has no evidence on the page: refused, nothing compiled."""
    def hidden(html):
        return html.replace("</h1>", '</h1><noscript><p class="ns-co">Acme Inc</p></noscript>', 1)
    h = Harness(monkeypatch, "greenhouse-1", html_patch=hidden)
    fake = {"title": READERS["greenhouse"]["title"], "company": {"css": ".ns-co"}}
    out = h.tools["save_parser"](parser={"id": "greenhouse", "fields": fake})
    assert out.startswith("REFUSED: evidence check failed") and "Acme Inc" in out
    assert Store().latest_version(NAME) == 1 and h.engine.compiled[-1]["version"] == 1
    assert h.events("evidence")[-1]["ok"] is False


def test_save_parser_rejects_non_additive(workspace, monkeypatch):
    h = Harness(monkeypatch, "greenhouse-1")
    out = h.tools["save_parser"](parser={"id": "greenhouse", "fields": {"title": READERS["greenhouse"]["title"]}})
    assert out.startswith("REFUSED: parser fields ['title'] must be exactly") and "company" in out
    out = h.tools["save_parser"](parser={"id": "lever", "fields": READERS["greenhouse"]})
    assert out.startswith("REFUSED: parser id 'lever' already exists")
    out = h.tools["save_parser"](parser={"id": "greenhouse", "match": {"site": "example.org"}, "fields": READERS["greenhouse"]})
    assert out.startswith("REFUSED: match")
    assert Store().latest_version(NAME) == 1


def test_validator_errors_are_returned(workspace, monkeypatch):
    h = Harness(monkeypatch, "greenhouse-1")
    monkeypatch.setattr(type(h.ctx), "validate_spec_text",
                        lambda self, text: ["sink.fields.Company references field 'company', which not every parser extracts"])
    out = h.tools["save_parser"](parser={"id": "greenhouse", "fields": READERS["greenhouse"]})
    assert out.startswith("REFUSED (nothing saved)") and "sink.fields.Company" in out
    assert Store().latest_version(NAME) == 1


def test_failing_tests_never_deploy(workspace, monkeypatch):
    h = Harness(monkeypatch, "greenhouse-1")
    h.engine.fail_tests = True
    out = h.tools["save_parser"](parser={"id": "greenhouse", "fields": READERS["greenhouse"]})
    assert out.startswith("FAILED: v2 compiled but its tests failed")
    assert Store().active_version(NAME) == 1 and not h.events("learned")


def test_unknown_policy_fails_safe(workspace):
    from agents.deep_tools import learn_policy
    assert learn_policy() == "auto"
    set_learn_policy("approve")
    assert learn_policy() == "approve"
    set_learn_policy("yolo")
    assert learn_policy() == "approve"


def test_test_parser_reads_css_and_jsonld(workspace, monkeypatch):
    from agents.deep_tools import run_parser
    rec = page("lever-1")
    assert run_parser({"id": "lever", "fields": READERS["lever"]}, "text", rec, "html") == {
        "title": "American Tech Fellowship", "company": "Palantir Technologies"}


@pytest.mark.parametrize("platform,name", [("greenhouse", "greenhouse-1"), ("greenhouse_branded", "greenhouse_branded-2")])
def test_spike_readers_learn_cleanly(workspace, monkeypatch, platform, name):
    baseline = ("lever", "ashby") if platform == "greenhouse" else ("lever", "greenhouse", "ashby")
    h = Harness(monkeypatch, name, baseline=baseline)
    out = h.tools["save_parser"](parser={"fields": READERS[platform]})
    assert out.startswith("deployed v2 automatically"), out
    pid = json.loads(json.dumps(h.ctx.learn_result))["parser_id"]
    assert pid == {"greenhouse": "greenhouse", "greenhouse_branded": "airbnb"}[platform]


@pytest.mark.xfail(not W1_READY, reason="needs W1 spec models, compiler and fixture tracker form", strict=False)
def test_learn_with_the_real_engine(workspace, monkeypatch):
    """Same as test_save_parser_auto_deploys_when_clean, but W1's validator, compiler, generated tests and
    form contract are real (fixture tracker form on a free port). Only the page inbox is faked."""
    from agents.deep_tools import ToolContext, seed_learn_workdir, tool_functions
    from agents.interface import _Thread
    from agents.scripted import ScriptedAgentService, work_dir
    from control import ops
    from demo_apps.tracker_form.app import create_app  # W1-3
    from tests.conftest import _Server
    rec = page("greenhouse-1", seq=3)
    monkeypatch.setattr(ToolContext, "get_page_record", lambda self, pid: rec)
    with _Server(create_app()) as form:
        wd = work_dir(NAME)
        spec = jobs_spec(("lever", "ashby"), form_url=form.url + "/")
        (wd / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
        (wd / "examples.yaml").write_text(yaml.safe_dump([golden("lever-1"), golden("ashby-1")], sort_keys=False))
        built = ops.compile_and_test(wd / "spec.yaml")
        assert built["tests_passed"], built["tests_summary"]
        ops.deploy(NAME, built["version"])
        svc = ScriptedAgentService(pace_s=0)
        t = _Thread(id="learn-real0001", mode="learn", subject=rec["id"], integration=NAME)
        svc._threads[t.id] = t
        seed_learn_workdir(NAME)
        tools = tool_functions(ToolContext(svc, t, name=NAME, page_id=rec["id"]))
        out = tools["save_parser"](parser={"id": "greenhouse", "fields": READERS["greenhouse"]})
        assert out.startswith("deployed v2 automatically"), out
        assert Store().active_version(NAME) == 2


# ---- multi-tenant platforms (Workday: one layout, a host per company) ------------------------------

def test_workday_tenant_learns_the_platform(workspace, monkeypatch):
    """A parser that also reads the other stored Workday pages is saved for *.myworkdayjobs.com, so the next
    company on Workday is read by code instead of starting another learn."""
    from tests.workday_pages import WORKDAY_READERS, workday_page
    h = Harness(monkeypatch, workday_page("tmobile", seq=7))
    h.engine.add_page(workday_page("nvidia", seq=8))
    assert "platform: tmobile.wd1.myworkdayjobs.com is one company on *.myworkdayjobs.com" in h.tools["get_page"]()
    out = h.tools["save_parser"](parser={"id": "tmobile", "fields": WORKDAY_READERS})
    assert out.startswith("deployed v2 automatically") and "platform *.myworkdayjobs.com" in out, out
    assert "parser workday for *.myworkdayjobs.com" in out  # named after the platform, not the first company
    assert learned_parser(active_spec(), "workday")["match"] == {"site": "*.myworkdayjobs.com"}


def test_one_odd_tenant_does_not_block_the_platform_parser(workspace, monkeypatch):
    """Real stage data: Visa leaves hiringOrganization empty. The T-Mobile reader reads NVIDIA but not Visa, so it
    still becomes the platform parser (1 of 2); Visa's pages are held and learn their own exact-host parser."""
    from tests.workday_pages import WORKDAY_READERS, workday_page
    h = Harness(monkeypatch, workday_page("tmobile", seq=7))
    h.engine.add_page(workday_page("nvidia", seq=8))
    h.engine.add_page(workday_page("visa", seq=9))
    out = h.tools["save_parser"](parser={"id": "workday", "fields": WORKDAY_READERS})
    assert "platform *.myworkdayjobs.com: also reads 1 other company; 1 that it misses will learn their own" in out, out
    assert learned_parser(active_spec(), "workday")["match"] == {"site": "*.myworkdayjobs.com"}


def test_parser_that_misses_other_tenants_stays_company_specific(workspace, monkeypatch):
    """Visa leaves hiringOrganization empty, so its reader cuts the company out of the description: that
    reader does not read T-Mobile's page, so it is saved for Visa's host only, never for the whole platform."""
    from tests.workday_pages import workday_page
    h = Harness(monkeypatch, workday_page("visa", seq=7))
    h.engine.add_page(workday_page("tmobile", seq=8))
    visa = {"title": {"jsonld": "title", "jsonld_type": "JobPosting"},
            "company": {"css": 'meta[property="og:description"]', "attr": "content", "regex": r"^About Us (\w+) is"}}
    out = h.tools["save_parser"](parser={"id": "visa", "fields": visa})
    assert out.startswith("deployed v2 automatically") and "for visa.wd5.myworkdayjobs.com only" in out, out
    assert "misses 1 of 1" in out
    assert learned_parser(active_spec(), "visa")["match"] == {"site": "visa.wd5.myworkdayjobs.com"}


def test_platform_match_asked_for_must_read_every_tenant(workspace, monkeypatch):
    from tests.workday_pages import workday_page
    h = Harness(monkeypatch, workday_page("visa", seq=7))
    h.engine.add_page(workday_page("tmobile", seq=8))
    visa = {"title": {"jsonld": "title", "jsonld_type": "JobPosting"},
            "company": {"css": 'meta[property="og:description"]', "attr": "content", "regex": r"^About Us (\w+) is"}}
    out = h.tools["save_parser"](parser={"id": "visa", "match": {"site": "*.myworkdayjobs.com"}, "fields": visa})
    assert out.startswith("REFUSED: a platform match must read at least half") and "tmobile" in out, out
    assert Store().active_version(NAME) == 1


def test_regex_that_hard_codes_the_value_is_refused(workspace, monkeypatch):
    """`About Us (Visa) is` captures a fixed word: it would write Visa for every page. A code check, not the prompt."""
    from tests.workday_pages import workday_page
    h = Harness(monkeypatch, workday_page("visa", seq=7))
    fixed = {"title": {"jsonld": "title", "jsonld_type": "JobPosting"},
             "company": {"css": 'meta[property="og:description"]', "attr": "content", "regex": "About Us (Visa) is"}}
    out = h.tools["test_page_parser"](parser={"id": "visa", "fields": fixed})
    assert out.startswith("FAILED") and "fixed text 'Visa'" in out, out
    assert h.tools["save_parser"](parser={"id": "visa", "fields": fixed}).startswith("REFUSED: evidence check failed")
    assert Store().active_version(NAME) == 1

"""agents/learn.py: the deterministic checks behind LEARN, on the 8 rendered pages of spike b."""

from __future__ import annotations

import copy
import sys

import pytest

from agents.learn import (additive_parser_patch, ambiguity, corroborated, d2_decision, evidence, page_outline,
                          run_page_parser)
from tests.agents.jobs import INDEX, READERS, jobs_spec, page, parser

VALIDATE = [{"field": "title", "rule": "required"}, {"field": "company", "rule": "required"}]
ALL = sorted(INDEX)


def reader(name: str) -> dict:
    return {"id": INDEX[name]["platform"], "fields": READERS[INDEX[name]["platform"]]}


def test_no_langchain_at_import():
    assert not any(m.split(".")[0] in ("langchain", "langchain_core", "deepagents", "langgraph")
                   for m in sys.modules if m.startswith("agents.learn")), "agents.learn is pure"


@pytest.mark.parametrize("name", ALL)
def test_outline_is_small_and_has_candidates(name):
    rec = page(name)
    out = page_outline(rec)
    assert len(out) <= 4000
    exp = INDEX[name]["expected"]
    assert exp["title"] in out and exp["company"] in out
    assert "<script" not in out and "<div" not in out


def test_outline_gives_working_extractors():
    out = page_outline(page("lever-1"))
    assert '{css: ".posting-headline h2"} → "American Tech Fellowship"' in out
    assert '{jsonld: "hiringOrganization.name", jsonld_type: JobPosting} → "Palantir Technologies"' in out
    ashby = page_outline(page("ashby-1"))
    assert '{css: "h1.ashby-job-posting-heading"}' in ashby  # a selector that reads only the title
    branded = page_outline(page("greenhouse_branded-1"))
    assert '{css: "meta[property=\\"og:site_name\\"]", attr: content} → "Careers at Airbnb"' in branded


@pytest.mark.parametrize("name", ALL)
def test_evidence_ok_for_spike_readers(name):
    rec = page(name)
    p = reader(name)
    fields = run_page_parser(p, rec)
    assert fields == INDEX[name]["expected"]
    items = evidence(rec, fields, VALIDATE, parser=p)
    assert [i["name"] for i in items] == ["title", "company"]
    assert all(i["ok"] and i["where"] in ("text", "jsonld", "html") for i in items), items
    assert items[0]["extractor"].startswith(("css", "jsonld"))


def test_fabricated_value_fails_evidence():
    rec = page("lever-1")
    items = evidence(rec, {"title": "American Tech Fellowship", "company": "Acme Inc"}, VALIDATE)
    by = {i["name"]: i for i in items}
    assert by["title"]["ok"] and not by["company"]["ok"] and by["company"]["where"] is None
    empty = evidence(rec, {"title": "", "company": "Palantir Technologies"}, VALIDATE)
    assert not empty[0]["ok"]


def test_run_page_parser_reports_misses():
    rec = page("greenhouse-1")
    out = run_page_parser({"id": "x", "fields": {"title": {"css": ".nope"}, "company": {"jsonld": "hiringOrganization.name"}}}, rec)
    assert "error" in out and "x.title" in out["error"] and "x.company" in out["error"]
    bad = run_page_parser({"id": "x", "fields": {"title": {"css": "h1", "regex": "("}}}, rec)
    assert "error" in bad


@pytest.mark.parametrize("name", ALL)
def test_page_chrome_is_not_corroborated(name):
    rec = page(name)
    exp = INDEX[name]["expected"]
    assert corroborated(rec, exp["title"]) and corroborated(rec, exp["company"])
    for chrome in ("Apply", "Apply for this job", "Accept", "Deny", "Cookie Policy"):
        assert not corroborated(rec, chrome), chrome


def test_cookie_banner_sentence_is_not_corroborated():
    rec = page("lever-1")
    banner = next(ln for ln in rec["text"].splitlines() if "uses cookies" in ln)
    assert not corroborated(rec, banner)
    branded = page("greenhouse_branded-1")
    assert not corroborated(branded, next(ln for ln in branded["text"].splitlines() if "We use cookies" in ln))


def test_ashby_h1_is_ambiguous():
    rec = page("ashby-1")
    amb = ambiguity(rec, {"id": "a", "fields": {"title": {"css": "h1"}, "company": {"jsonld": "hiringOrganization.name"}}})
    assert set(amb) == {"title"} and len(amb["title"]) == 3 and "Software Engineer, Security, Cloud" in amb["title"]
    assert ambiguity(rec, reader("ashby-1")) == {}
    # a refine regex narrows the matches: branded pages have 3 <title> elements, only one is the page title
    branded = page("greenhouse_branded-1")
    assert ambiguity(branded, {"fields": {"company": {"css": "title", "regex": r"Careers at (.+)$"}}}) == {}
    assert "company" in ambiguity(branded, {"fields": {"company": {"css": "title"}}})


def test_additive_patch_detection():
    active = jobs_spec(("lever", "ashby"))
    new = copy.deepcopy(active)
    new["extract"]["parsers"].append(parser("greenhouse"))
    assert additive_parser_patch(active, new)
    both = copy.deepcopy(new)
    both["sink"]["url"] = "http://elsewhere/"
    assert not additive_parser_patch(active, both)
    changed = copy.deepcopy(active)
    changed["extract"]["parsers"][0]["fields"]["title"] = {"css": "h1"}
    assert not additive_parser_patch(active, changed)
    two = copy.deepcopy(new)
    two["extract"]["parsers"].append(parser("greenhouse_branded", "airbnb"))
    assert not additive_parser_patch(active, two)
    assert not additive_parser_patch(active, active)


def test_d2_decision():
    ok = dict(policy="auto", tests_passed=True, evidence_ok=True, all_corroborated=True, ambiguous={}, additive=True)
    assert d2_decision(**ok) == (True, [])
    for k, v in [("policy", "approve"), ("tests_passed", False), ("evidence_ok", False), ("all_corroborated", False),
                 ("ambiguous", {"title": ["a", "b"]}), ("additive", False)]:
        auto, reasons = d2_decision(**{**ok, k: v})
        assert not auto and len(reasons) == 1, (k, reasons)

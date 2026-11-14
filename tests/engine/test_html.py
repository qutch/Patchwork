"""runner/html.py helpers and runner.lib page extractors, on the rendered pages from spike b."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from runner.html import (canonical_url, css_all, css_first, fingerprint_present, jsonld_get, jsonld_objects,
                         normalize_ws, site_of, visible_text)
from runner.lib import Quarantine, extract_css, extract_jsonld, refine_regex

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "pages"
INDEX = json.loads((PAGES / "index.json").read_text())

# The readers spike b found (scripts/spike_extract_pages.py), one per platform.
READERS = {
    "lever": {"title": {"css": ".posting-headline h2"}, "company": {"jsonld": "hiringOrganization.name"}},
    "greenhouse": {"title": {"css": ".job__title h1"},
                   "company": {"css": "title", "regex": r"^Job Application for .* at (.+)$"}},
    "greenhouse_branded": {"title": {"css": "h1"},
                           "company": {"css": 'meta[property="og:site_name"]', "attr": "content",
                                       "regex": r"^Careers at (.+)$"}},
    "ashby": {"title": {"jsonld": "title"}, "company": {"jsonld": "hiringOrganization.name"}},
}


def _html(entry: dict) -> str:
    return (PAGES / entry["file"]).read_text()


def _apply(reader: dict, html: str) -> str:
    where = "test.field"
    if "css" in reader:
        value = extract_css(html, reader["css"], reader.get("attr"), where)
    else:
        value = extract_jsonld(html, reader["jsonld"], reader.get("jsonld_type", "JobPosting"), where)
    if "regex" in reader:
        value = refine_regex(str(value), re.compile(reader["regex"]), where)
    return value


@pytest.mark.parametrize("entry", INDEX, ids=[e["file"] for e in INDEX])
def test_expected_values_extract(entry):
    html = _html(entry)
    reader = READERS[entry["platform"]]
    assert _apply(reader["title"], html) == entry["expected"]["title"]
    assert _apply(reader["company"], html) == entry["expected"]["company"]


def test_site_and_canonical():
    assert site_of("https://www.Jobs.Lever.co:443/palantir/x") == "jobs.lever.co"
    assert site_of("not a url") == ""
    url = "https://jobs.lever.co/palantir/abc?lever-source=LinkedIn&utm_source=x&keep=1#apply"
    assert canonical_url(url, "<html></html>") == "https://jobs.lever.co/palantir/abc?keep=1"
    html = '<html><head><link rel="canonical" href="https://example.com/jobs/1"></head></html>'
    assert canonical_url("https://example.com/jobs/1?ref=home", html) == "https://example.com/jobs/1"
    relative = '<html><head><link rel="canonical" href="/jobs/1"></head></html>'
    assert canonical_url("https://example.com/jobs/1#x", relative) == "https://example.com/jobs/1"


@pytest.mark.parametrize("entry", INDEX, ids=[e["file"] for e in INDEX])
def test_visible_text_skips_script_and_style(entry):
    text = visible_text(_html(entry))
    assert entry["expected"]["title"] in text
    assert "@font-face" not in text and "function(" not in text
    assert all(line == normalize_ws(line) and line for line in text.splitlines())


def test_ashby_h1_is_ambiguous():
    html = (PAGES / "ashby-1.html").read_text()
    assert len(set(css_all(html, "h1"))) == 3


def test_jsonld_objects_and_fingerprints():
    lever = (PAGES / "lever-1.html").read_text()
    assert any(o.get("@type") == "JobPosting" for o in jsonld_objects(lever))
    assert jsonld_get(lever, "hiringOrganization.name") == "Palantir Technologies"
    assert jsonld_get(lever, "nope.missing") is None
    assert fingerprint_present(lever, ".posting-headline")
    assert not fingerprint_present(lever, '[class*="ashby-job-posting"]')
    assert css_first(lever, ".posting-headline h2") == "American Tech Fellowship"


def test_bad_html_never_raises():
    assert css_all("<<<", "h1") == []
    assert css_first("", "h1") is None
    assert jsonld_objects("<script type=application/ld+json>{bad</script>") == []
    assert visible_text("") == ""
    assert not fingerprint_present("<p>x</p>", "[[[not a selector")


def test_misses_quarantine():
    html = (PAGES / "lever-1.html").read_text()
    with pytest.raises(Quarantine) as e:
        extract_css(html, ".nope", None, "lever.title")
    assert e.value.code == "PARSE_NO_MATCH" and "lever.title" in e.value.detail
    with pytest.raises(Quarantine):
        extract_jsonld(html, "hiringOrganization.nope", "JobPosting", "lever.company")
    with pytest.raises(Quarantine):
        refine_regex("Palantir", re.compile(r"^Careers at (.+)$"), "lever.company")
    with pytest.raises(Quarantine):
        extract_css("<h1>   </h1>", "h1", None, "x.title")

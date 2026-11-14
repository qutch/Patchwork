"""fixtures/postings: real rendered postings and their answer key (W5-1, docs/contracts.md V12)."""

import gzip
import json
from pathlib import Path

import pytest

from runner.html import normalize_ws, site_of, visible_text

ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "postings"
KEY = json.loads((ROOT / "answer_key.json").read_text())
KINDS = {"lever", "greenhouse", "greenhouse_branded", "ashby", "smartrecruiters"}


def _html(entry):
    return gzip.decompress((ROOT / entry["file"]).read_bytes()).decode("utf-8")


def test_answer_key_shape():
    assert 50 <= len(KEY) <= 70
    assert {e["platform"] for e in KEY} == KINDS
    assert len({e["file"] for e in KEY}) == len(KEY)
    assert len({e["url"] for e in KEY}) == len(KEY)
    for e in KEY:
        for k in ("file", "platform", "site", "url", "fetched_at", "title", "company"):
            assert e.get(k), (e["file"], k)
        assert e["site"] == site_of(e["url"])
        assert e["file"].startswith(e["platform"] + "/") and e["file"].endswith(".html.gz")
        assert e["method"]["title"] and e["method"]["company"], e["file"]
        assert not any(n.startswith("UNRESOLVED") for n in e["notes"]), e["file"]


@pytest.mark.parametrize("entry", KEY, ids=[e["file"] for e in KEY])
def test_answer_key_matches_files(entry):
    html = _html(entry)
    assert len(html) > 10_000, "rendered DOM, not an error page"
    text = normalize_ws(visible_text(html)).casefold()
    assert normalize_ws(entry["title"]).casefold() in text
    assert entry["company"].casefold() in (text + " " + html.casefold())


def test_sites_per_platform():
    sites = {}
    for e in KEY:
        sites.setdefault(e["platform"], set()).add(e["site"])
    assert sites == {"lever": {"jobs.lever.co"}, "greenhouse": {"job-boards.greenhouse.io"},
                     "greenhouse_branded": {"careers.airbnb.com"}, "ashby": {"jobs.ashbyhq.com"},
                     "smartrecruiters": {"jobs.smartrecruiters.com"}}


def test_known_traps_are_recorded():
    """Two real traps for a learned reader, kept in the key's notes: an HTML-escaped JSON-LD title on Lever, and
    SmartRecruiters' 'Internet Explorer 11' banner as the first h1."""
    by = {e["file"]: e for e in KEY}
    assert any("&amp;" in n for n in by["lever/9.html.gz"]["notes"])
    assert by["lever/9.html.gz"]["title"] == "Head of Measurement & Advertising Effectiveness"
    sr = [e for e in KEY if e["platform"] == "smartrecruiters"]
    assert all(any("Internet Explorer" in n for n in e["notes"]) for e in sr)

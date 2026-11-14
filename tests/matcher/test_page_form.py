"""Page -> form candidates (docs/contracts.md V11) from fixtures/recording_jobs/ (scripts/make_recording_jobs.py)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from matcher import match
from runner.html import css_first, jsonld_get

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "fixtures" / "recording_jobs"
LEVER_URL = "https://jobs.lever.co/palantir/0ccbe620-a3ef-41d1-a5c4-68e56b3c91d0"


@pytest.fixture(scope="module")
def recording():
    return json.loads((FIX / "recording.json").read_text())


@pytest.fixture(scope="module")
def cands(recording):
    return match(recording, base_dir=FIX)


@pytest.fixture(scope="module")
def snapshot():
    return (FIX / "snapshots" / "1.html").read_text()


def _m(cands):
    return {m["dest_field"]: m for m in cands["mappings"]}


def test_sink_is_form(cands):
    sink = cands["sink"]
    assert sink["kind"] == "form"
    assert sink["labels"] == ["Email", "Job Title", "Company", "Link", "Date Applied"]
    assert sink["url"] == "http://127.0.0.1:18010/"
    assert sink["submit"] == "Submit"
    assert sink["confirmation_text"] == "Your response has been recorded"
    assert [q["required"] for q in sink["questions"]] == [True, False, False, False, False]
    assert cands["unexplained"] == []


def test_source_is_the_page(cands):
    assert cands["source"] == {"kind": "page", "site": "jobs.lever.co", "url": LEVER_URL,
                               "canonical_url": LEVER_URL, "snapshot": "snapshots/1.html",
                               "title": "Palantir Technologies - American Tech Fellowship"}


def test_title_mapping_is_css(cands, snapshot):
    m = _m(cands)["Job Title"]
    assert m["kind"] == "css" and m["value"] == "American Tech Fellowship"
    assert m["css"] == ".posting-headline h2"
    assert css_first(snapshot, m["css"]) == m["value"]
    assert m["copied"] is True and m["attr"] is None and m["regex"] is None
    assert {"jsonld": "title"} in m["alternatives"]


def test_company_mapping_refines_the_copied_element(cands, snapshot):
    import re
    m = _m(cands)["Company"]
    assert m["kind"] == "css" and m["copied"] is True and m["value"] == "Palantir Technologies"
    whole = css_first(snapshot, m["css"])
    assert whole == "Palantir Technologies Home Page"
    assert re.search(m["regex"], whole).group(1) == "Palantir Technologies"
    assert {"jsonld": "hiringOrganization.name"} in m["alternatives"]
    assert jsonld_get(snapshot, "hiringOrganization.name") == m["value"]


def test_link_and_date_are_record_fields(cands, recording):
    m = _m(cands)
    assert {k: m["Link"][k] for k in ("kind", "from", "value")} == \
        {"kind": "record", "from": "record.canonical_url", "value": LEVER_URL}
    assert m["Date Applied"]["kind"] == "record" and m["Date Applied"]["from"] == "record.captured_date"
    assert m["Email"] == {"dest_field": "Email", "value": "demo@example.com", "kind": "const"}
    assert [x["dest_field"] for x in cands["mappings"]] == ["Email", "Job Title", "Company", "Link", "Date Applied"]


def test_typed_page_value_is_found_without_a_copy(recording):
    """A value typed by hand that appears on the page still gets a reader (JSON-LD first)."""
    rec = copy.deepcopy(recording)
    rec["events"] = [e for e in rec["events"] if not (e["type"] == "copy" and e["text"] == "Palantir Technologies")]
    for e in rec["events"]:
        if e["type"] == "paste" and e["value"] == "Palantir Technologies":
            e["type"] = "input"
    m = _m(match(rec, base_dir=FIX))["Company"]
    assert m["kind"] == "jsonld" and m["jsonld"] == "hiringOrganization.name" and m.get("copied") is None


def test_without_snapshots_mappings_fall_back_to_the_recorded_path(recording):
    m = _m(match(recording))
    assert m["Job Title"]["kind"] == "css" and m["Job Title"]["css"]
    assert m["Link"]["kind"] == "record" and m["Email"]["kind"] == "const"


def test_unknown_pasted_value_is_unexplained(recording):
    rec = copy.deepcopy(recording)
    for e in rec["events"]:
        if e["type"] == "paste" and e["value"] == "Palantir Technologies":
            e["value"] = "Somewhere Else Inc"
    c = match(rec, base_dir=FIX)
    assert c["unexplained"] == [{"dest_field": "Company", "value": "Somewhere Else Inc"}]


def test_candidates_file_matches_matcher(cands):
    assert json.loads((FIX / "candidates.json").read_text()) == json.loads(json.dumps(cands))


def test_cli_reads_snapshots_next_to_the_recording():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-m", "matcher", str(FIX / "recording.json")], capture_output=True,
                         text=True, cwd=REPO, check=True).stdout
    assert _m(json.loads(out))["Job Title"]["css"] == ".posting-headline h2"

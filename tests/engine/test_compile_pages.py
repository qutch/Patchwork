"""W1-2: compiled page extractors, fingerprint routing and `routed_by` (contracts V3, V4)."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from compiler.compile import compile_spec, load_examples
from fixtures.jobs.pages import INDEX, fixture_records, page_record
from runner.loader import load_integration
from runner.pipeline import MemoryStore, process_record
from runner.store import Store
from runner.testing import FakeHttp
from spec import load_spec

REPO = Path(__file__).resolve().parents[2]
JOBS = REPO / "fixtures" / "jobs"


@pytest.fixture(scope="module")
def jobs_pkg(tmp_path_factory):
    out = tmp_path_factory.mktemp("integrations")
    result = compile_spec(load_spec(JOBS / "spec.yaml"), version=1, out_root=out,
                          examples=load_examples(JOBS / "examples.yaml"))
    integ = load_integration(result.path)
    parsers = importlib.import_module(integ.__package__ + ".parsers")
    return result, integ, parsers


@pytest.fixture(scope="module")
def records():
    return fixture_records()


def _by_platform(records, platform):
    return [(r, e) for r, e in zip(records, INDEX) if e["platform"] == platform]


@pytest.mark.parametrize("platform", ["lever", "greenhouse", "ashby"])
def test_fixture_pages_extract(jobs_pkg, records, platform):
    _, integ, parsers = jobs_pkg
    pairs = _by_platform(records, platform)
    assert len(pairs) == 2
    for rec, entry in pairs:
        parser_id, fields = integ.extract(rec)
        assert parser_id == platform
        assert fields == entry["expected"]
        assert parsers.route(rec)[0::2] == (platform, "match")
        out = process_record(integ, rec, FakeHttp(), MemoryStore())
        assert (out.status, out.parser, out.routed_by) == ("success", platform, "match"), out.detail
        assert out.body["Job Title"] == entry["expected"]["title"]
        assert out.body["Link"] == rec["canonical_url"]


def test_branded_page_routes_by_fingerprint_then_holds(jobs_pkg, records):
    _, integ, parsers = jobs_pkg
    for rec, _ in _by_platform(records, "greenhouse_branded"):
        assert rec["site"] == "careers.airbnb.com"
        assert parsers.route(rec)[0::2] == ("greenhouse", "fingerprint")
        http = FakeHttp()
        out = process_record(integ, rec, http, MemoryStore())
        assert (out.status, out.reason, out.routed_by) == ("quarantined", "PARSE_NO_MATCH", "fingerprint")
        assert "greenhouse" in out.detail and "fingerprint" in out.detail
        assert http.sink_writes == []


def test_unknown_site_no_fingerprint(jobs_pkg):
    _, integ, _ = jobs_pkg
    html = "<html><head><title>Jobs at Example</title></head><body><h1>Engineer</h1></body></html>"
    rec = page_record(99, "https://example.org/jobs/1", html, captured_at="2026-11-14T10:00:00+00:00")
    assert rec["site"] == "example.org"
    out = process_record(integ, rec, FakeHttp(), MemoryStore())
    assert (out.status, out.reason, out.routed_by) == ("quarantined", "PARSE_NO_MATCH", None)
    assert "no fingerprint matched" in out.detail and "example.org" in out.detail


def test_match_wins_over_fingerprint_and_routed_parser_is_not_retried(jobs_pkg, records):
    """A lever-hosted page that also carries an ashby marker still routes by site; a page whose
    routed parser fails is held, never retried on another parser."""
    _, integ, parsers = jobs_pkg
    lever = dict(records[0])
    lever["html"] = lever["html"].replace("<body", '<body><div class="ashby-job-posting-x"></div', 1)
    assert parsers.route(lever)[0::2] == ("lever", "match")
    broken = dict(records[0])
    broken["html"] = broken["html"].replace("posting-headline", "posting-head")  # lever reader misses
    broken["html"] = broken["html"].replace("<body", '<body><div class="ashby-job-posting-x"></div', 1)
    out = process_record(integ, broken, FakeHttp(), MemoryStore())
    assert (out.reason, out.routed_by) == ("PARSE_NO_MATCH", "match")
    assert "lever.title" in out.detail


def test_manifest_lists_parsers_with_origin(jobs_pkg):
    result, _, _ = jobs_pkg
    manifest = json.loads((result.path / "manifest.json").read_text())
    assert manifest["parsers"] == [
        {"id": "lever", "match": {"site": "jobs.lever.co"}, "fingerprint": ".posting-headline",
         "origin": {"kind": "fixture", "at": None, "thread_id": None, "approved_by": None}},
        {"id": "greenhouse", "match": {"site": "job-boards.greenhouse.io"}, "fingerprint": '[src*=".greenhouse.io/"]',
         "origin": {"kind": "fixture", "at": None, "thread_id": None, "approved_by": None}},
        {"id": "ashby", "match": {"site": "jobs.ashbyhq.com"}, "fingerprint": '[class*="ashby-job-posting"]',
         "origin": {"kind": "fixture", "at": None, "thread_id": None, "approved_by": None}},
    ]
    assert manifest["contract_kind"] == "form"
    assert manifest["sink_call"] == {"method": "POST", "path": "/form"}


def test_generated_package_is_deterministic(tmp_path):
    spec, examples = load_spec(JOBS / "spec.yaml"), load_examples(JOBS / "examples.yaml")
    a = compile_spec(spec, version=1, out_root=tmp_path / "a", examples=examples)
    b = compile_spec(spec, version=1, out_root=tmp_path / "b", examples=examples)
    for rel in ("parsers.py", "integration.py", "adapters.py", "golden.json"):
        assert (a.path / rel).read_text() == (b.path / rel).read_text(), rel


def test_generated_tests_pass(jobs_pkg):
    import os
    import subprocess
    import sys
    result, _, _ = jobs_pkg
    proc = subprocess.run([sys.executable, "-m", "pytest", str(result.path / "tests"), "-q", "-p", "no:cacheprovider",
                           "--rootdir", str(result.path), "-c", os.devnull],
                          capture_output=True, text=True, cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)})
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert "failed" not in proc.stdout


def test_v1_routing_unchanged(tmp_path):
    from tests.conftest import FIXTURE_EXAMPLES, FIXTURE_SPEC
    result = compile_spec(load_spec(FIXTURE_SPEC), version=1, out_root=tmp_path,
                          examples=load_examples(FIXTURE_EXAMPLES))
    integ = load_integration(result.path)
    manifest = json.loads((result.path / "manifest.json").read_text())
    assert manifest["contract_kind"] == "openapi"
    assert [p["id"] for p in manifest["parsers"]] == ["acme_v1", "globex_v1", "initech_v1"]
    rec = {"id": "m1", "sender_domain": "acme.com", "body": "PO Number: PO-1\nSKU: WID-101\nQty: 5"}
    out = process_record(integ, rec, FakeHttp(), MemoryStore())
    assert (out.status, out.parser, out.routed_by) == ("success", "acme_v1", "match")
    rec = {"id": "m2", "sender_domain": "umbrella.com", "body": "x"}
    out = process_record(integ, rec, FakeHttp(), MemoryStore())
    assert (out.status, out.reason, out.parser) == ("quarantined", "PARSE_NO_MATCH", None)
    assert out.detail == "no parser for sender_domain='umbrella.com'"
    # a v1 parser that fails keeps parser=None, as before
    rec = {"id": "m3", "sender_domain": "acme.com", "body": "nothing here"}
    out = process_record(integ, rec, FakeHttp(), MemoryStore())
    assert (out.reason, out.parser, out.routed_by) == ("PARSE_NO_MATCH", None, "match")


class _OldStyleInteg:
    """The surface of a package compiled by the v1 templates: extract() returns a 2-tuple, no PIPELINE_API."""
    RECORD_ID, ROUTE_BY = "id", "k"

    def __init__(self, n):
        self.n = n

    def extract(self, record):
        return ("p", {"x": 1}) if self.n == 2 else ("p", {"x": 1}, "fingerprint")

    def validate(self, fields, record, http):
        return None

    def dedupe_key(self, fields, record):
        return str(record["id"])

    def build_body(self, fields, record):
        return dict(fields)

    def send(self, http, body):
        return http.request("sink", "POST", "/x", json=body)

    def verify(self, http, fields, record):
        return None


@pytest.mark.parametrize("n", [2, 3])
def test_pipeline_accepts_old_and_new_extract_tuples(n):
    out = process_record(_OldStyleInteg(n), {"id": "r1", "k": "v"}, FakeHttp(), MemoryStore())
    assert (out.status, out.parser) == ("success", "p")
    assert out.routed_by == (None if n == 2 else "fingerprint")


def test_store_keeps_routed_by(workspace):
    from runner.pipeline import Outcome
    store = Store()
    run_id = store.start_run("jobs_to_tracker", 1, False)
    store.add_record(run_id, Outcome(record_id="pg-1", status="quarantined", reason="PARSE_NO_MATCH",
                                     parser=None, routed_by="fingerprint"), {"id": "pg-1"})
    assert store.records(run_id)[0]["routed_by"] == "fingerprint"

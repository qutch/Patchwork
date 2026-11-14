"""W1-1: spec models for page extractors, fingerprint routing and form sinks (contracts V3-V5, V7)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from spec import load_spec, spec_hash, validate_spec
from spec.models import ApiSink, FormSink

REPO = Path(__file__).resolve().parents[2]
JOBS = REPO / "fixtures" / "jobs"


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load((JOBS / "spec.yaml").read_text())


def _errors(data: dict) -> list[str]:
    return validate_spec(yaml.safe_dump(data, sort_keys=False))


def test_jobs_fixture_validates():
    spec = load_spec(JOBS / "spec.yaml")
    assert spec.name == "jobs_to_tracker"
    assert isinstance(spec.sink, FormSink)
    assert spec.source.html_field == "html"
    assert [p.id for p in spec.extract.parsers] == ["lever", "greenhouse", "ashby"]
    assert all(p.origin is not None and p.origin.kind == "fixture" for p in spec.extract.parsers)
    assert [p.fingerprint for p in spec.extract.parsers] == [
        ".posting-headline", '[src*=".greenhouse.io/"]', '[class*="ashby-job-posting"]']
    assert list(spec.sink.fields) == ["Email", "Job Title", "Company", "Link", "Date Applied"]
    assert spec.sink.submit == "Submit"
    assert spec.sink.verify.confirmation_text == "Your response has been recorded"
    assert set(spec.sink_values()) == set(spec.sink.fields)
    assert spec.dedupe.key == ["record.canonical_url"]


def test_v1_spec_hash_unchanged():
    spec = load_spec(REPO / "fixtures" / "spec.yaml")
    assert isinstance(spec.sink, ApiSink)
    # printed by the code at the W1 branch point, before any spec change
    assert spec_hash(spec) == "88d4fced8f023b4a860b28c2f450c7a65ff8314e60ae352d6a19b688ff893454"
    assert set(spec.sink_values()) == {"product_id", "quantity", "reference"}


def test_v1_error_text_unchanged():
    raw = yaml.safe_load((REPO / "fixtures" / "spec.yaml").read_text())
    raw["sink"]["call"]["method"] = "GET"
    errs = _errors(raw)
    assert errs == ["sink.call: sink.call must be a mutating request (POST/PUT/PATCH)"]
    raw = yaml.safe_load((REPO / "fixtures" / "spec.yaml").read_text())
    raw["extract"]["parsers"][0]["fields"]["qty"] = {"cast": "int"}
    errs = _errors(raw)
    assert errs == ["extract.parsers.0.fields.qty: field extract needs exactly one of "
                    "regex/json_path/between, got none"]


EXTRACTOR_CASES = {
    "css_without_html_field": (None, {"css": "h1"}, "html_field"),
    "attr_without_css": ("title", {"jsonld": "title", "attr": "content"}, "attr"),
    "jsonld_type_without_jsonld": ("title", {"css": "h1", "jsonld_type": "Organization"}, "jsonld_type"),
    "combine_with_css": ("title", {"css": "h1", "regex": r"(\d+)x(\d+)", "combine": "product", "cast": "int"},
                         "combine"),
    "refine_regex_without_group": ("title", {"css": "h1", "regex": r"^Careers at .+$"}, "capture group"),
    "css_and_json_path": ("title", {"css": "h1", "json_path": "$.title"}, "exactly one"),
}


@pytest.mark.parametrize("case", list(EXTRACTOR_CASES), ids=list(EXTRACTOR_CASES))
def test_extractor_errors(raw, case):
    html_field, extract, needle = EXTRACTOR_CASES[case]
    data = copy.deepcopy(raw)
    if html_field is None:
        data["source"].pop("html_field")
        for p in data["extract"]["parsers"]:
            p.pop("fingerprint", None)
            p["fields"] = {"title": {"regex": "(.+)"}, "company": {"regex": "(.+)"}}
        data["extract"]["parsers"][0]["fields"]["title"] = extract
    else:
        data["extract"]["parsers"][0]["fields"]["title"] = extract
    errs = _errors(data)
    assert len(errs) == 1, errs
    assert needle in errs[0], errs
    assert "title" in errs[0], errs


def test_form_sink_reference_checks(raw):
    raw["sink"]["fields"]["Company"] = {"from": "employer"}
    errs = _errors(raw)
    assert len(errs) == 1, errs
    assert "sink.fields.Company" in errs[0] and "'employer'" in errs[0], errs


def test_form_sink_template_reference_checks(raw):
    raw["sink"]["fields"]["Job Title"] = {"template": "{title} at {employer}"}
    errs = _errors(raw)
    assert any("sink.fields.Job Title.template" in e for e in errs), errs


def test_fingerprint_needs_html_field(raw):
    raw["source"].pop("html_field")
    for p in raw["extract"]["parsers"]:
        p["fields"] = {"title": {"regex": "(.+)"}, "company": {"regex": "(.+)"}}
    errs = _errors(raw)
    assert len(errs) == 1, errs
    assert "fingerprint" in errs[0] and "source.html_field" in errs[0], errs


def test_unknown_sink_kind_is_rejected(raw):
    raw["sink"]["kind"] = "spreadsheet"
    errs = _errors(raw)
    assert errs and any("sink" in e for e in errs), errs


def test_form_sink_has_no_api_keys(raw):
    raw["sink"]["base_url"] = "http://x"
    errs = _errors(raw)
    assert any("base_url" in e for e in errs), errs


def test_origin_is_optional_and_checked(raw):
    raw["extract"]["parsers"][0]["origin"] = {"kind": "learned", "at": "2026-11-14T14:02:11+00:00",
                                              "thread_id": "learn-3f9a2c1b0d", "approved_by": "auto"}
    assert _errors(raw) == []
    raw["extract"]["parsers"][0]["origin"] = {"kind": "guessed"}
    assert any("origin" in e for e in _errors(raw))
    del raw["extract"]["parsers"][0]["origin"]
    assert _errors(raw) == []


def test_custom_transforms_cover_form_fields(raw):
    raw["sink"]["fields"]["Company"] = {"transform": "custom:clean_company", "from": "company"}
    spec = load_spec_from(raw)
    assert spec.custom_transforms() == {"clean_company"}


def load_spec_from(data: dict):
    from spec import parse_spec
    return parse_spec(data)


def test_examples_cover_three_platforms():
    examples = yaml.safe_load((JOBS / "examples.yaml").read_text())
    assert len(examples) == 6
    sites = sorted({ex["record"]["site"] for ex in examples})
    assert sites == ["job-boards.greenhouse.io", "jobs.ashbyhq.com", "jobs.lever.co"]
    for ex in examples:
        assert set(ex["expect"]["body"]) == {"Email", "Job Title", "Company", "Link", "Date Applied"}

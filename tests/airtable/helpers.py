"""Shared bits for the Airtable tests (contracts V14): the jobs spec retargeted to the fake Airtable table,
and the fake's coordinates. No langchain at import."""

from __future__ import annotations

from pathlib import Path

import yaml

from demo_apps.fake_airtable.app import BASE_ID, TABLE, TOKEN

REPO = Path(__file__).resolve().parents[2]
JOBS = REPO / "fixtures" / "jobs"
NAME = "jobs_to_tracker"
# runner_env (control.ops) strips *_TOKEN variables until A1's credential pass-through lands, so the tests that
# launch a runner subprocess name a credential the strip keeps; in-process tests use AIRTABLE_TOKEN (V14).
SUBPROCESS_CREDENTIAL = "FAKE_AIRTABLE_PAT"

__all__ = ["BASE_ID", "TABLE", "TOKEN", "NAME", "airtable_sink", "airtable_spec", "write_spec"]


def airtable_sink(credential: str = "AIRTABLE_TOKEN", table: str = TABLE, read_back: bool = True) -> dict:
    return {"kind": "airtable", "base_id": BASE_ID, "table": table,
            "auth": {"scheme": "bearer", "credential": credential},
            "fields": {"Email": {"const": "demo@example.com"}, "Job Title": {"from": "title"},
                       "Company": {"from": "company"}, "Link": {"from": "record.canonical_url"},
                       "Date Applied": {"from": "record.captured_date"}},
            "typecast": False, "verify": {"read_back": read_back}}


def airtable_spec(**kw) -> dict:
    """fixtures/jobs/spec.yaml (V7, three platforms) with its form sink replaced by the fake Airtable table."""
    raw = yaml.safe_load((JOBS / "spec.yaml").read_text())
    raw["sink"] = airtable_sink(**kw)
    return raw


def write_spec(dir_: Path, raw: dict) -> Path:
    path = Path(dir_) / "spec.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path

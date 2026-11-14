"""Shared bits for the SQLite tests (contracts V16): the jobs spec retargeted to a table in a database file under
the workspace. No langchain at import."""

from __future__ import annotations

from pathlib import Path

import yaml

from demo_apps.sqlite_tracker.db import TABLE

REPO = Path(__file__).resolve().parents[2]
JOBS = REPO / "fixtures" / "jobs"
NAME = "jobs_to_tracker"
DB = "tracker.db"  # relative to the workspace

__all__ = ["NAME", "DB", "TABLE", "JOBS", "sqlite_sink", "sqlite_spec", "write_spec"]


def sqlite_sink(path: str = DB, table: str = TABLE, read_back: bool = True) -> dict:
    return {"kind": "sqlite", "path": path, "table": table,
            "fields": {"Email": {"const": "demo@example.com"}, "Job Title": {"from": "title"},
                       "Company": {"from": "company"}, "Link": {"from": "record.canonical_url"},
                       "Date Applied": {"from": "record.captured_date"}},
            "verify": {"read_back": read_back}}


def sqlite_spec(**kw) -> dict:
    """fixtures/jobs/spec.yaml (V7, three platforms) with its form sink replaced by the SQLite table."""
    raw = yaml.safe_load((JOBS / "spec.yaml").read_text())
    raw["sink"] = sqlite_sink(**kw)
    return raw


def write_spec(dir_: Path, raw: dict) -> Path:
    path = Path(dir_) / "spec.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path

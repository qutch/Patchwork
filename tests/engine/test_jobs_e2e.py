"""W1-5: jobs_to_tracker end to end through the real runner subprocess (contracts V5-V7).

Fake page inbox (fixtures/pages) + the fixture tracker form on free ports, a temp workspace, the
scripted path only: no model calls anywhere."""

from __future__ import annotations

import json
import shutil
import sys
import types
from pathlib import Path

import httpx
import pytest
import yaml

from control import ops
from demo_apps.tracker_form.app import create_app as tracker_form_app
from fixtures.jobs.pages import PAGES
from runner.store import Store
from tests.conftest import _Server
from tests.engine.fake_inbox import create_app as inbox_app

REPO = Path(__file__).resolve().parents[2]
JOBS = REPO / "fixtures" / "jobs"
NAME = "jobs_to_tracker"
EXPECTED_ROWS = 6  # lever x2, greenhouse x2, ashby x2; the 2 branded Greenhouse pages are held


@pytest.fixture(scope="module")
def tracker_form():
    with _Server(tracker_form_app()) as srv:
        yield srv.url


@pytest.fixture(scope="module")
def built(tmp_path_factory, tracker_form):
    """Compile + test + deploy v1 once (the contract is fetched from the live fixture form)."""
    ws = tmp_path_factory.mktemp("built-ws")
    raw = yaml.safe_load((JOBS / "spec.yaml").read_text())
    raw["sink"]["url"] = tracker_form + "/"
    spec_path = ws / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATCHWORK_WORKSPACE", str(ws))
        built = ops.compile_and_test(spec_path, examples_path=JOBS / "examples.yaml")
        assert built["tests_passed"], built["tests_summary"]
        assert built["contract_errors"] == []
        ops.deploy(NAME, 1)
    return ws


@pytest.fixture
def stack(built, tracker_form, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    shutil.copytree(built, ws)  # db + WAL together: consistent while nothing writes
    monkeypatch.setenv("PATCHWORK_WORKSPACE", str(ws))
    httpx.post(tracker_form + "/admin/reset")
    with _Server(inbox_app()) as inbox:
        monkeypatch.setenv("PATCHWORK_CONTROL_URL", inbox.url)
        yield {"form": tracker_form, "inbox": inbox.url, "ws": ws}
    httpx.post(tracker_form + "/admin/reset")


def _rows(url: str) -> list[dict]:
    return httpx.get(url + "/admin/rows").json()


def _repost(inbox: str, file: str, url: str) -> dict:
    return httpx.post(inbox + "/api/pages", json={"url": url, "html": (PAGES / file).read_text()}).json()


def test_jobs_run_end_to_end(stack):
    s = ops.run_sandboxed(NAME)
    assert s["status"] == "completed", s
    assert s["exit_code"] == 0 and s["llm_calls"] == 0
    assert s["counts"] == {"success": EXPECTED_ROWS, "quarantined": 2, "quarantined:PARSE_NO_MATCH": 2}, s
    rows = _rows(stack["form"])
    assert len(rows) == EXPECTED_ROWS
    by_title = {r["Job Title"]: r for r in rows}
    assert by_title["American Tech Fellowship"]["Company"] == "Palantir Technologies"
    assert by_title["Software Engineer, Android"]["Company"] == "Ramp"
    assert by_title["Account Executive, Mobile Gaming"]["Link"] == "http://job-boards.greenhouse.io/discord/jobs/8927168002"
    assert all(r["Email"] == "demo@example.com" and r["Date Applied"] == "2026-11-14" for r in rows)
    records = {r["record_id"]: r for r in Store().records(s["run_id"])}
    held = [r for r in records.values() if r["status"] == "quarantined"]
    assert sorted(r["raw"]["site"] for r in held) == ["careers.airbnb.com", "careers.airbnb.com"]
    assert all(r["routed_by"] == "fingerprint" and r["reason"] == "PARSE_NO_MATCH" for r in held)
    assert {r["routed_by"] for r in records.values() if r["status"] == "success"} == {"match"}
    log = [json.loads(ln) for ln in Path(s["log"]).read_text().splitlines()]
    assert log[0]["event"] == "preflight" and log[0]["ok"] and not log[0]["changed"]


def test_rerun_writes_nothing(stack):
    first = ops.run_sandboxed(NAME)
    assert first["counts"]["success"] == EXPECTED_ROWS
    again = ops.run_sandboxed(NAME)
    assert again["status"] == "completed" and again["counts"] == {}
    assert len(_rows(stack["form"])) == EXPECTED_ROWS
    # the same posting sent again with tracking params and a fragment -> one row, shown as duplicate
    _repost(stack["inbox"], "lever-1.html",
            "https://jobs.lever.co/palantir/0ccbe620-a3ef-41d1-a5c4-68e56b3c91d0?utm_source=x#top")
    _repost(stack["inbox"], "greenhouse-1.html", "https://job-boards.greenhouse.io/discord/jobs/8927168002?gh_src=abc")
    dup = ops.run_sandboxed(NAME)
    assert dup["counts"] == {"duplicate": 2}, dup
    assert len(_rows(stack["form"])) == EXPECTED_ROWS


def test_rename_blocks_before_any_write(stack):
    httpx.post(stack["form"] + "/admin/rename", json={"from": "Company", "to": "Employer"})
    s = ops.run_sandboxed(NAME)
    assert s["status"] == "blocked" and s["exit_code"] == 2, s
    assert _rows(stack["form"]) == []
    incident = Store().incident(s["incident_id"])
    assert incident["type"] == "CONTRACT_CHANGED" and incident["reason_codes"] == ["SINK_CONTRACT"]
    errors = incident["detail"]["contract_errors"]
    assert errors and all(e.endswith("body.Company: not accepted by destination (no such property)") for e in errors)
    assert incident["detail"]["sink_call"] == {"method": "POST", "path": "/form"}
    old_props = incident["old_openapi"]["paths"]["/form"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
    new_props = incident["new_openapi"]["paths"]["/form"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "Company" in old_props and "Employer" in new_props and "Company" not in new_props
    # blocked runs never move the cursor: after the form is fixed, every page is still processed
    httpx.post(stack["form"] + "/admin/reset")
    ok = ops.run_sandboxed(NAME)
    assert ok["counts"]["success"] == EXPECTED_ROWS


def test_dry_run_form_writes_nothing(stack):
    s = ops.run_sandboxed(NAME, dry_run=True)
    assert s["status"] == "completed" and s["dry_run"] is True, s
    assert s["counts"]["success"] == EXPECTED_ROWS
    assert _rows(stack["form"]) == []
    details = {r["detail"] for r in Store().records(s["run_id"]) if r["status"] == "success"}
    assert details == {"dry run: filled, not submitted"}
    live = ops.run_sandboxed(NAME)  # dry runs use their own idempotency scope
    assert live["counts"]["success"] == EXPECTED_ROWS


def test_global_overrides_do_not_leak(stack, monkeypatch):
    monkeypatch.setenv("PATCHWORK_SOURCE_BASE_URL", "http://127.0.0.1:9/junk")
    monkeypatch.setenv("PATCHWORK_SINK_BASE_URL", "http://127.0.0.1:9/junk")
    monkeypatch.setenv("PATCHWORK_STAGING_BASE_URL", "http://127.0.0.1:9/junk")
    s = ops.run_sandboxed(NAME)
    assert s["status"] == "completed" and s["counts"]["success"] == EXPECTED_ROWS, s


def test_control_url_missing_fails_clearly(stack, monkeypatch):
    monkeypatch.delenv("PATCHWORK_CONTROL_URL")
    s = ops.run_sandboxed(NAME)
    assert s["status"] == "failed" and s["exit_code"] == 1
    assert "PATCHWORK_CONTROL_URL" in s["message"]
    assert _rows(stack["form"]) == []


def test_runner_still_refuses_ai_modules(stack, monkeypatch):
    from runner import run as run_mod
    monkeypatch.setitem(sys.modules, "langchain", types.ModuleType("langchain"))
    with pytest.raises(SystemExit) as e:
        run_mod.run(NAME, log=lambda s: None)
    assert "refusing to run" in str(e.value)
    assert _rows(stack["form"]) == []


def test_fetch_contract_api_and_form(stack, tracker_form):
    from spec import load_spec
    raw = yaml.safe_load((JOBS / "spec.yaml").read_text())
    raw["sink"]["url"] = tracker_form + "/"
    path = stack["ws"] / "spec2.yaml"
    path.write_text(yaml.safe_dump(raw))
    doc = ops.fetch_contract(load_spec(path))
    assert doc["info"]["x-patchwork-kind"] == "form" and doc["info"]["x-form-url"] == tracker_form + "/"
    assert ops.fetch_sink_openapi is not None  # v1 name kept

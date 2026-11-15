"""An API destination in the wizard: one write operation, a nested body built from dotted destination fields.
TEACH from two paper pages (two sites, no shared layout) into the offline fake papers API, then a run with
zero AI writes records whose JSON nests `venue.name`."""

from __future__ import annotations

import httpx
import pytest
import yaml

from runner.store import Store, workspace_dir
from tests.agents.teach_examples_helpers import write_teach_draft
from tests.agents.test_deep_agent import check_events, wait_for
from tests.conftest import _Server

NAME = "collect_papers"


def test_dotted_to_body_round_trips():
    from agents.deep_tools import body_to_dotted, dotted_to_body
    flat = {"title": {"from": "title"}, "venue.name": {"from": "venue"}, "venue.city": {"const": "Oslo"},
            "year": {"transform": "int", "from": "year"}}
    tree = dotted_to_body(flat)
    assert tree == {"title": {"from": "title"}, "year": {"transform": "int", "from": "year"},
                    "venue": {"name": {"from": "venue"}, "city": {"const": "Oslo"}}}
    assert body_to_dotted(tree) == flat


def test_api_sink_accepts_a_nested_body():
    from pathlib import Path

    from spec import parse_spec, spec_to_dict
    data = yaml.safe_load((Path(__file__).resolve().parents[2] / "fixtures" / "spec.yaml").read_text())
    data["sink"]["body"] = {"product_id": {"from": "sku"}, "line": {"quantity": {"from": "qty"},
                                                                     "ref": {"name": {"from": "po_id"}}}}
    data["sink"].pop("verify", None)
    s = parse_spec(data)
    assert set(s.sink_values()) == {"product_id", "line.quantity", "line.ref.name"}  # flattened, dotted
    dumped = spec_to_dict(s)["sink"]["body"]  # round-trips nested (values gain their defaults, e.g. args: {})
    assert dumped["line"]["ref"]["name"]["from"] == "po_id" and dumped["line"]["quantity"]["from"] == "qty"
    assert parse_spec({**data, "sink": {**data["sink"], "body": dumped}}).sink_values() == s.sink_values()
    data["sink"]["body"] = {"line": {}}
    with pytest.raises(Exception):
        parse_spec(data)  # an empty object is not a value


@pytest.fixture
def papers_stack(workspace, monkeypatch):
    from demo_apps.fake_papers_api.app import create_app
    from fixtures.papers.pages import PAGES, html, page_record
    from tests.engine.fake_inbox import create_app as inbox_app
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")
    files = ["archive-a.html", "proceedings-b.html"]
    with _Server(create_app()) as api, _Server(inbox_app(records=[page_record(f, i) for i, f in
                                                                    enumerate(files, start=1)])) as inbox:
        monkeypatch.setenv("PATCHWORK_CONTROL_URL", inbox.url)
        dest = {"kind": "api", "base_url": api.url, "operation": {"method": "post", "path": "/papers"}}
        rid, pids = write_teach_draft(NAME, [(PAGES[f]["url"], html(f)) for f in files], dest, "Collect papers")
        yield {"recording_id": rid, "page_ids": pids, "api": api.url}


def test_scripted_examples_teach_into_the_papers_api(papers_stack):
    from agents.scripted import ScriptedAgentService
    from control import ops
    from fixtures.papers.pages import body
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_build(papers_stack["recording_id"], NAME)
    s = wait_for(svc, tid, "waiting")
    p = s["pending"]
    assert p["type"] == "request_deploy" and p["tests"]["passed"], p
    rows = {r["dest_field"]: r for r in p["review"]}
    assert {"title", "year", "venue.name"} <= set(rows)
    pids = papers_stack["page_ids"]
    assert rows["venue.name"]["values"] == {pids[0]: "Journal of Synthetic Learning",
                                            pids[1]: "Workshop on Field Acoustics"}
    assert rows["year"]["values"] == {pids[0]: 2025, pids[1]: 2024}
    svc.resume(tid, p["interrupt_id"], {"decision": "approve"})
    wait_for(svc, tid, "done")
    check_events(svc, tid)
    assert Store().active_version(NAME) == 1

    spec = yaml.safe_load((workspace_dir() / "integrations" / NAME / "v1" / "spec.yaml").read_text())
    assert spec["sink"]["kind"] == "api" and (spec["sink"]["call"]["method"], spec["sink"]["call"]["path"]) == ("POST", "/papers")
    assert set(spec["sink"]["body"]["venue"]) == {"name"}  # nested, not "venue.name"
    assert len(spec["extract"]["parsers"]) == 2

    summary = ops.run_sandboxed(NAME)
    assert summary["status"] == "completed" and summary["counts"]["success"] == 2, summary
    assert summary["llm_calls"] == 0
    papers = httpx.get(papers_stack["api"] + "/papers").json()
    got = [{k: x[k] for k in ("title", "year", "venue")} for x in papers]
    assert got == [body("archive-a.html"), body("proceedings-b.html")]

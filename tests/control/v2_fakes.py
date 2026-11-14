"""Fakes for the v2 control-plane tests, until W1 (engine) and W3 (start_learn) are merged:

- `make_page_integration`: a minimal compiled package (spec.yaml + manifest.json) whose source is
  `patchwork://control`, registered, tested and deployed in the store.
- `fake_runner` fixture: `ops.runner_command` → tests/control/fake_page_runner.py (same CLI and stdout
  protocol), so the real RunManager, run-on-arrival queue and PATCHWORK_CONTROL_URL are exercised.
- `FakeLearnAgent`: the scripted agent plus a `start_learn` that records calls and, when told to,
  "learns" by deploying a new version with a parser for the page's site.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from agents.interface import EVENT_TYPES
from agents.scripted import ScriptedAgentService
from control.api import create_app
from tests.conftest import _Server
from control import ops
from runner.store import Store, workspace_dir

REPO = Path(__file__).resolve().parents[2]
PAGES = REPO / "fixtures" / "pages"
INDEX = {p["file"]: p for p in json.loads((PAGES / "index.json").read_text())}
FAKE_RUNNER = Path(__file__).with_name("fake_page_runner.py")
NAME = "jobs_to_tracker"

LEVER = {"id": "lever", "match": {"site": "jobs.lever.co"}, "fingerprint": ".posting-headline",
         "origin": {"kind": "fixture", "at": None, "thread_id": None, "approved_by": None},
         "fields": {"title": {"css": ".posting-headline h2"}, "company": {"jsonld": "hiringOrganization.name"}}}
GREENHOUSE = {"id": "greenhouse", "match": {"site": "job-boards.greenhouse.io"}, "fingerprint": '[src*=".greenhouse.io/"]',
              "origin": {"kind": "fixture", "at": None, "thread_id": None, "approved_by": None},
              "fields": {"title": {"css": "h1"}, "company": {"jsonld": "hiringOrganization.name"}}}


def page_spec(parsers: list[dict], name: str = NAME) -> dict[str, Any]:
    return {
        "spec_version": 1, "name": name, "intent": "Add every job posting I send to my tracker",
        "source": {"kind": "api", "base_url": "patchwork://control",
                   "list": {"method": "GET", "path": "/api/pages", "params": {"since": "{cursor}"}},
                   "record_id": "id", "text_field": "text", "html_field": "html", "cursor_field": "seq"},
        "extract": {"route_by": "site", "parsers": parsers},
        "validate": [{"field": "title", "rule": "required"}, {"field": "company", "rule": "required"}],
        "dedupe": {"key": ["record.canonical_url"]},
        "sink": {"kind": "form", "url": "http://127.0.0.1:9/", "submit": "Submit",
                 "fields": {"Email": {"const": "demo@example.com"}, "Job Title": {"from": "title"},
                            "Company": {"from": "company"}, "Link": {"from": "record.canonical_url"},
                            "Date Applied": {"from": "record.captured_date"}},
                 "verify": {"confirmation_text": "Your response has been recorded"}},
        "on_error": "quarantine",
    }


def make_page_integration(parsers: list[dict] | None = None, *, name: str = NAME, deploy: bool = True,
                          tests_passed: bool = True, spec: dict | None = None) -> int:
    """Write integrations/<name>/v<N>/{spec.yaml, manifest.json, golden.json}; register (and deploy) it."""
    store = Store()
    version = store.latest_version(name) + 1
    spec = spec or page_spec(list(parsers if parsers is not None else [LEVER]), name)
    vdir = workspace_dir() / "integrations" / name / f"v{version}"
    (vdir / "contract").mkdir(parents=True)
    (vdir / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    (vdir / "golden.json").write_text("[]")
    (vdir / "contract" / "openapi.json").write_text("{}")
    manifest = {"name": name, "version": version, "spec_hash": f"fake{version}", "openapi_hash": None,
                "sink_call": {"method": "POST", "path": "/form"}, "contract_kind": "form",
                "parsers": [{k: p.get(k) for k in ("id", "match", "fingerprint", "origin")}
                            for p in spec["extract"]["parsers"]]}
    (vdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    store.add_version(name, version, f"fake{version}", None)
    store.set_tests(name, version, tests_passed, "fake")
    if deploy:
        store.set_active(name, version)
    store.close()
    return version


def make_v1_integration(name: str = "po_to_stockroom") -> int:
    """A deployed integration whose source is a normal URL: run-on-arrival must leave it alone."""
    spec = page_spec([LEVER], name)
    spec["source"]["base_url"] = "http://localhost:8001"
    return make_page_integration(name=name, spec=spec)


def page_html(file: str) -> str:
    return (PAGES / file).read_text()


def post_page(api: httpx.Client, file: str, *, url: str | None = None, token: str | None = None) -> httpx.Response:
    token = token if token is not None else api.get("/api/extension").json()["token"]
    return api.post("/api/pages", json={"url": url or INDEX[file]["url"], "title": f"title of {file}",
                                        "html": page_html(file)}, headers={"X-Patchwork-Token": token})


def wait_idle(api: httpx.Client, name: str = NAME, timeout: float = 30) -> None:
    """Until nothing runs or is queued for `name`, and every run row is final."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not api.app.state.auto.state(name)["busy"] and all(
                r["status"] != "running" for r in Store().runs(name, limit=100)):
            time.sleep(0.1)  # finish listeners run just after the row is final
            if not api.app.state.auto.state(name)["busy"]:
                return
        time.sleep(0.05)
    raise AssertionError(f"{name} still busy: {api.app.state.auto.state(name)}")


def records_by_page(name: str = NAME) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    store = Store()
    for run in reversed(store.runs(name, limit=100)):
        for r in store.records(run["id"]):
            out.setdefault(r["record_id"], []).append({**r, "kind": run["kind"]})
    return out


@pytest.fixture
def fake_runner(monkeypatch):
    def command(name, *, version="active", dry_run=False, limit=None, replay=False):
        cmd = [sys.executable, str(FAKE_RUNNER), "run", name, "--version", str(version)]
        if dry_run:
            cmd.append("--dry-run")
        if limit is not None:
            cmd += ["--limit", str(limit)]
        if replay:
            cmd.append("--replay-quarantined")
        return cmd
    monkeypatch.setattr(ops, "runner_command", command)
    return command


class gate:
    """`with gate():` holds every fake run after it has fetched, until the block exits."""

    def __enter__(self):
        (workspace_dir() / "fake_runner_gate").write_text("hold")
        return self

    def __exit__(self, *exc):
        (workspace_dir() / "fake_runner_gate").unlink(missing_ok=True)


class FakeLearnAgent(ScriptedAgentService):
    """Scripted agent + V10 `start_learn`. With `learn=True` the thread deploys a new version with a
    parser for the page's site (origin.thread_id = this thread) and emits `deployed`, like LEARN.
    `hold` (a threading.Event) keeps the thread running until set. `model_calls` goes into done.result."""

    def __init__(self, *, learn: bool = True, model_calls: int = 5, **kw):
        super().__init__(pace_s=0, **kw)
        self.learn, self.model_calls = learn, model_calls
        self.learn_calls: list[tuple[str, str]] = []
        self.hold: threading.Event | None = None

    def start_learn(self, page_id: str, integration: str = NAME) -> str:
        self.learn_calls.append((page_id, integration))
        return self._start("learn", page_id, lambda t: self._run_learn(t, page_id, integration))

    def _run_learn(self, t, page_id: str, integration: str) -> dict[str, Any]:
        from control.pages import PageInbox

        if self.hold is not None:
            self.hold.wait(30)
        page = PageInbox().get(page_id)
        site = page["site"]
        if not self.learn:
            raise RuntimeError("model unavailable")
        store = Store()
        active = store.active_version(integration)
        spec = yaml.safe_load((workspace_dir() / "integrations" / integration / f"v{active}" / "spec.yaml").read_text())
        pid = site.split(".")[0].replace("-", "_") + "_learned"
        spec["extract"]["parsers"].append({"id": pid, "match": {"site": site},
                                           "origin": {"kind": "learned", "at": "2026-11-14T15:00:00+00:00",
                                                      "thread_id": t.id, "approved_by": "auto"},
                                           "fields": {"title": {"css": "h1"}, "company": {"css": "title"}}})
        version = make_page_integration(name=integration, spec=spec, deploy=False)
        if "learned" in EVENT_TYPES:  # W3 registers it (V10)
            self.emit(t, "learned", integration=integration, parser_id=pid, site=site, version=version, deployed=True,
                      approval="auto", reason="all checks passed")
        out = ops.deploy(integration, version)
        self.emit(t, "deployed", name=integration, version=version, previous_version=out["previous_version"])
        return {"model_calls": self.model_calls, "seconds": 0.1, "page_id": page_id, "parser_id": pid, "site": site,
                "version": version, "deployed": True, "approval": "auto"}


@pytest.fixture
def v2agent(workspace):
    return FakeLearnAgent()


@pytest.fixture
def v2api(workspace, fake_runner, v2agent, monkeypatch):
    """The real control plane on a free port with the fake page runner and the fake learning agent.
    No demo apps: v2 needs none."""
    monkeypatch.delenv("PATCHWORK_CONTROL_URL", raising=False)
    monkeypatch.delenv("PATCHWORK_AI_ENABLED", raising=False)
    monkeypatch.delenv("PATCHWORK_EXTENSION_TOKEN", raising=False)
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")
    app = create_app(agent=v2agent, dotenv=False)
    with _Server(app) as srv, httpx.Client(base_url=srv.url, timeout=60) as client:
        client.app = app  # type: ignore[attr-defined]
        client.agent = v2agent  # type: ignore[attr-defined]
        yield client
        app.state.runs.stop_all()

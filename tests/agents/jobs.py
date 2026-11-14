"""Shared test data for the v2 agent tests (no langchain at import, so top-level tests can use it too).

- `page(file)` builds a V1 page record from fixtures/pages (text via runner.html.visible_text)
- `jobs_spec()` is a local copy of the V7 + V5 `jobs_to_tracker` spec (W1 owns fixtures/jobs/; until it
  lands the agents build against this copy)
- `FakeEngine` stands in for W1's validator/compiler where the agent tests only need a version that
  exists, has a tests verdict and can be deployed with the real `control.ops.deploy`
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from agents.learn import make_page_record
from agents.learn import ydump as _ydump
from agents.learn import yload as _yload

REPO = Path(__file__).resolve().parents[2]
PAGES = REPO / "fixtures" / "pages"
INDEX = {e["file"].removesuffix(".html"): e for e in json.loads((PAGES / "index.json").read_text())}
NAME = "jobs_to_tracker"
FORM_URL = "http://127.0.0.1:18010/"

# The spike-b readers (tests/engine/test_html.py READERS), one per platform.
READERS = {
    "lever": {"title": {"css": ".posting-headline h2"}, "company": {"jsonld": "hiringOrganization.name"}},
    "greenhouse": {"title": {"css": ".job__title h1"},
                   "company": {"css": "title", "regex": r"^Job Application for .* at (.+)$"}},
    "greenhouse_branded": {"title": {"css": "h1"},
                           "company": {"css": 'meta[property="og:site_name"]', "attr": "content",
                                       "regex": r"^Careers at (.+)$"}},
    "ashby": {"title": {"jsonld": "title"}, "company": {"jsonld": "hiringOrganization.name"}},
}
SITES = {"lever": "jobs.lever.co", "greenhouse": "job-boards.greenhouse.io", "ashby": "jobs.ashbyhq.com",
         "greenhouse_branded": "careers.airbnb.com"}
FINGERPRINTS = {"lever": ".posting-headline", "greenhouse": '[src*=".greenhouse.io/"]',
                "ashby": '[class*="ashby-job-posting"]'}

W1_READY = False
try:  # W1's spec models know css extractors and form sinks
    from spec.models import FieldExtract  # noqa: E402
    W1_READY = "css" in FieldExtract.model_fields
except ImportError:  # pragma: no cover
    pass


def page(name: str, seq: int = 1) -> dict:
    e = INDEX[name]
    return make_page_record(e["url"], (PAGES / e["file"]).read_text(), seq=seq,
                            captured_at="2026-11-14T15:02:11.000-05:00")


def parser(platform: str, pid: str | None = None) -> dict:
    p = {"id": pid or platform, "match": {"site": SITES[platform]}}
    if platform in FINGERPRINTS:
        p["fingerprint"] = FINGERPRINTS[platform]
    p["origin"] = {"kind": "fixture"}
    p["fields"] = json.loads(json.dumps(READERS[platform]))
    return p


def jobs_spec(platforms=("lever", "greenhouse", "ashby"), form_url: str = FORM_URL) -> dict:
    return {
        "spec_version": 1, "name": NAME,
        "intent": "When I press the hotkey on a job posting, add it to my job tracker",
        "source": {"kind": "api", "base_url": "patchwork://control",
                   "list": {"method": "GET", "path": "/api/pages", "params": {"since": "{cursor}"}},
                   "record_id": "id", "text_field": "text", "html_field": "html", "cursor_field": "seq"},
        "extract": {"route_by": "site", "parsers": [parser(p) for p in platforms]},
        "validate": [{"field": "title", "rule": "required"}, {"field": "company", "rule": "required"}],
        "dedupe": {"key": ["record.canonical_url"]},
        "sink": {"kind": "form", "url": form_url,
                 "fields": {"Email": {"const": "demo@example.com"}, "Job Title": {"from": "title"},
                            "Company": {"from": "company"}, "Link": {"from": "record.canonical_url"},
                            "Date Applied": {"from": "record.captured_date"}},
                 "submit": "Submit", "verify": {"confirmation_text": "Your response has been recorded"}},
        "on_error": "quarantine",
        "assumptions": [],
    }


def golden(name: str, seq: int = 1) -> dict:
    rec = page(name, seq)
    exp = INDEX[name]["expected"]
    return {"name": f"page_{name.replace('-', '_')}", "record": rec,
            "expect": {"body": {"Email": "demo@example.com", "Job Title": exp["title"], "Company": exp["company"],
                                "Link": rec["canonical_url"], "Date Applied": rec["captured_date"]}}}


def form_contract(labels=("Email", "Job Title", "Company", "Link", "Date Applied"), required=("Email",),
                  url: str = FORM_URL) -> dict:
    """A V6 form contract document."""
    props = {lab: {"type": "string", "x-input": "email" if lab == "Email" else "text"} for lab in labels}
    return {"openapi": "3.0.3", "info": {"title": "Job tracker", "x-patchwork-kind": "form", "x-form-url": url},
            "paths": {"/form": {"post": {"requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "additionalProperties": False, "required": list(required),
                "properties": props}}}}}}}}


class FakeEngine:
    """Replaces ToolContext.validate_spec_text / compile_and_test / fetch_contract (W1's pieces) and
    ToolContext.get_page_record (W4a's inbox). Versions are registered in the real Store and laid out
    like compiled packages (spec.yaml, golden.json, manifest.json), so `control.ops.deploy`, the
    request_deploy diffs and seed_repair_workdir work unchanged."""

    def __init__(self, pages: dict[str, dict] | None = None):
        self.pages = dict(pages or {})
        self.compiled: list[dict] = []
        self.fail_tests = False
        self.contract = form_contract()

    def install(self, monkeypatch) -> "FakeEngine":
        from agents.deep_tools import ToolContext
        engine = self
        monkeypatch.setattr(ToolContext, "validate_spec_text", lambda self, text: engine.validate(text))
        monkeypatch.setattr(ToolContext, "compile_and_test", lambda self, path: engine.compile(path))
        monkeypatch.setattr(ToolContext, "fetch_contract", lambda self, spec: engine.contract)
        monkeypatch.setattr(ToolContext, "get_page_record", lambda self, pid: engine.page(pid))

        def platform_pages(self, platform, exclude=None, limit=5):
            from runner.lib import match_rank
            return [p for p in reversed(list(engine.pages.values()))
                    if p["id"] != exclude and match_rank({"site": platform}, p) == 1][:limit]
        monkeypatch.setattr(ToolContext, "platform_pages", platform_pages)
        return self

    def add_page(self, rec: dict) -> dict:
        self.pages[rec["id"]] = rec
        return rec

    def page(self, page_id: str) -> dict:
        if page_id not in self.pages:
            raise KeyError(f"page {page_id} not found")
        return self.pages[page_id]

    @staticmethod
    def validate(text: str) -> list[str]:
        if W1_READY:
            from spec import validate_spec
            return validate_spec(text)
        try:
            data = _yload(text)
        except yaml.YAMLError as e:
            return [f"<yaml>: {e}"]
        if not isinstance(data, dict) or not data.get("extract", {}).get("parsers"):
            return ["extract.parsers: must not be empty"]
        return []

    def compile(self, spec_path) -> dict:
        from runner.store import Store, workspace_dir
        spec_path = Path(spec_path)
        spec = _yload(spec_path.read_text())
        name, store = spec["name"], Store()
        version = store.latest_version(name) + 1
        vdir = workspace_dir() / "integrations" / name / f"v{version}"
        vdir.mkdir(parents=True)
        shutil.copy(spec_path, vdir / "spec.yaml")
        ex_path = spec_path.with_name("examples.yaml")
        examples = (_yload(ex_path.read_text()) or []) if ex_path.exists() else []
        (vdir / "golden.json").write_text(json.dumps([{**e, "missing": e.get("missing", [])} for e in examples]))
        (vdir / "parsers.py").write_text("# fake package\n" + "".join(f"# {p['id']}\n" for p in spec["extract"]["parsers"]))
        (vdir / "contract").mkdir()
        (vdir / "contract" / "openapi.json").write_text(json.dumps(self.contract))
        (vdir / "manifest.json").write_text(json.dumps({"parsers": [
            {k: p.get(k) for k in ("id", "match", "fingerprint", "origin")} for p in spec["extract"]["parsers"]]}))
        store.add_version(name, version, f"spec-{version}", "contract")
        passed = not self.fail_tests
        summary = f"{len(examples)} passed" if passed else "1 failed"
        store.set_tests(name, version, passed, summary)
        out = {"name": name, "version": version, "path": str(vdir), "tests_passed": passed,
               "tests_summary": summary, "contract_errors": []}
        self.compiled.append(out)
        return out

    def deploy_baseline(self, spec: dict | None = None, examples: list[dict] | None = None) -> int:
        """Compile + deploy the jobs spec as the active version (like an earlier TEACH)."""
        from agents.scripted import work_dir
        from control import ops
        wd = work_dir(NAME)
        (wd / "spec.yaml").write_text(_ydump(spec or jobs_spec(), sort_keys=False, width=200))
        (wd / "examples.yaml").write_text(_ydump(examples if examples is not None else [golden("lever-1")],
                                                         sort_keys=False, width=200))
        built = self.compile(wd / "spec.yaml")
        ops.deploy(NAME, built["version"])
        return built["version"]


def open_form_incident(renames: dict[str, str], required=("Email",)) -> int:
    """A CONTRACT_CHANGED incident like the runner's form preflight raises (V6) after renaming questions."""
    from runner.store import Store
    labels = ["Email", "Job Title", "Company", "Link", "Date Applied"]
    new_labels = [renames.get(lab, lab) for lab in labels]
    errors = [f"body.{old}: not accepted by destination (no such property)" for old in renames]
    errors += [f"body.{new}: required by destination but missing from body" for new in renames.values() if new in required]
    store = Store()
    return store.open_incident(
        name=NAME, version=store.active_version(NAME), run_id=None, type="CONTRACT_CHANGED",
        reason_codes=["CONTRACT_CHANGED"], summary="The tracker form changed: " + "; ".join(errors),
        detail={"contract_errors": errors, "sink_call": {"method": "POST", "path": "/form"}},
        old_openapi=form_contract(labels), new_openapi=form_contract(new_labels, required))


def set_learn_policy(policy: str) -> None:
    from runner.store import workspace_dir
    path = workspace_dir() / "settings.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data["learn_policy"] = policy
    path.write_text(json.dumps(data))


def active_spec() -> dict:
    from runner.store import Store, workspace_dir
    v = Store().active_version(NAME)
    return _yload((workspace_dir() / "integrations" / NAME / f"v{v}" / "spec.yaml").read_text())

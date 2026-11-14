"""ScriptedAgentService: the BUILD and REPAIR storylines with no LLM.

It emits the same events a real deep agent would and drives the *real* spec/compiler/ops
code, but its decisions are hard-coded for the po_to_stockroom demo. Uses:
  - control-plane and UI development before the deep agent lands
  - tests (deterministic, no network to a model)
  - the live-demo fallback if the model is slow or down (plan section 13, "replay mode")
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import jsonpatch
import yaml

from agents.interface import ThreadedAgentService, _Thread
from control import ops
from runner.lib import Quarantine, cast, extract_product, extract_regex
from runner.store import Store, workspace_dir
from spec import validate_spec

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"
NAME = "po_to_stockroom"
SUPPLIER_WAREHOUSE = {"acme.com": "BOS", "globex.com": "NYC", "initech.com": "BOS"}

# Break 2: the fourth supplier's format ("Item code: GAD-217 / 3 cases (12/case)").
NEW_SUPPLIER_DOMAIN = "umbrella.com"
CASES = r"Item code: \S+ / (\d+) cases \((\d+)/case\)"


def umbrella_parser(units: bool) -> dict:
    qty = ({"regex": CASES, "cast": "int", "combine": "product"} if units
           else {"regex": r"Item code: \S+ / (\d+) cases", "cast": "int"})
    return {"id": "umbrella_v1", "match": {"sender_domain": NEW_SUPPLIER_DOMAIN},
            "fields": {"po_id": {"regex": r"reference\s+(PO-\d+)"},
                       "sku": {"regex": r"Item code:\s*([A-Z0-9-]+)"},
                       "qty": qty}}


def try_parser(parser: dict, record: dict) -> dict:
    """Evaluate a parser dict on one raw record (what the generated parser will do)."""
    text, fields = record.get("body") or "", {}
    for name, f in parser["fields"].items():
        where = f"{parser['id']}.{name}"
        if f.get("combine") == "product":
            fields[name] = extract_product(text, re.compile(f["regex"]), f.get("cast", "int"), where)
        else:
            fields[name] = cast(extract_regex(text, re.compile(f["regex"]), where), f.get("cast", "str"), where)
    return fields


def work_dir(name: str = NAME) -> Path:
    """The agent's filesystem root for an integration (plan: integrations/<name>/work)."""
    d = workspace_dir() / "integrations" / name / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_recording(recording_id: str) -> dict[str, Any]:
    if recording_id == "fixture":
        return json.loads((FIXTURES / "recording.json").read_text())
    path = workspace_dir() / "recordings" / recording_id / "recording.json"
    return json.loads(path.read_text())


class ScriptedAgentService(ThreadedAgentService):
    def __init__(self, root: Path | None = None, pace_s: float | None = None):
        super().__init__(root)
        self.pace_s = float(os.environ.get("PATCHWORK_SCRIPTED_PACE_S", "0.4")) if pace_s is None else pace_s

    def _pause(self) -> None:
        if self.pace_s:
            time.sleep(self.pace_s)

    def _tool(self, t: _Thread, agent: str, tool: str, args: dict, ok: bool, summary: str) -> None:
        self.emit(t, "tool_call", agent=agent, tool=tool, args=args)
        self._pause()
        self.emit(t, "tool_result", agent=agent, tool=tool, ok=ok, summary=summary)

    # ---- BUILD ---------------------------------------------------------------------------
    def _run_build(self, t: _Thread, recording_id: str) -> Any:
        rec = load_recording(recording_id)
        todos = [
            {"content": "Interpret the recording into a draft spec", "status": "in_progress"},
            {"content": "Test parsers against 15 more source records", "status": "pending"},
            {"content": "Confirm open assumptions with the user", "status": "pending"},
            {"content": "Compile the spec and run the generated tests", "status": "pending"},
            {"content": "Dry run against staging, then request deploy", "status": "pending"},
        ]
        self.emit(t, "todos", todos=todos)
        self.emit(t, "message", agent="supervisor", text=f"Intent: “{rec['intent']}”. Delegating interpretation.")
        self.emit(t, "subagent_start", name="interpreter", task="Recording + candidates -> spec draft and questions")
        self._tool(t, "interpreter", "get_recording_summary", {"recording_id": recording_id}, True,
                   f"{len(rec['events'])} events; POST /api/incoming observed with 3 fields")
        self._tool(t, "interpreter", "get_candidates", {"recording_id": recording_id}, True,
                   "quantity ← body 'Qty:' (regex Qty:\\s*(\\d+)); product_id ← 'SKU:'; reference ← 'PO Number:'")
        self._tool(t, "interpreter", "fetch_source_samples", {"limit": 15}, True,
                   "15 messages from 3 sender domains: acme.com, globex.com, initech.com")
        self._tool(t, "interpreter", "test_parser", {"parser": "acme_v1", "samples": 15}, False,
                   "acme_v1 matched 5/15; 10 misses come from globex.com and initech.com")
        self.emit(t, "message", agent="interpreter",
                  text="Two more supplier formats. Adding globex_v1 (prose) and initech_v1 (key=value) parsers.")
        for pid in ("globex_v1", "initech_v1"):
            self._tool(t, "interpreter", "test_parser", {"parser": pid, "samples": 15}, True, f"{pid} matched 5/5 of its sender")
        wd = work_dir()
        for f in ("spec.yaml", "examples.yaml"):
            shutil.copy(FIXTURES / f, wd / f)
        errors = validate_spec((wd / "spec.yaml").read_text())
        self._tool(t, "interpreter", "validate_spec", {"path": "spec.yaml"}, not errors, "valid" if not errors else "; ".join(errors))
        self.emit(t, "artifact", kind="spec", name=NAME, version=None, path=str(wd / "spec.yaml"))
        self.emit(t, "subagent_end", name="interpreter", summary="Draft spec with 3 parsers, 2 rules, dedupe on po_id")
        todos[0]["status"] = todos[1]["status"] = "completed"
        todos[2]["status"] = "in_progress"
        self.emit(t, "todos", todos=todos)

        answer = self.interrupt(t, "ask_user", question="Some messages have no SKU (e.g. holiday notices). What should happen to them?",
                                options=["Quarantine them for review", "Skip them silently"])
        choice = answer.get("answer", "Quarantine them for review")
        self.emit(t, "message", agent="supervisor", text=f"Recorded assumption a2: {choice}.")
        todos[2]["status"] = "completed"
        todos[3]["status"] = "in_progress"
        self.emit(t, "todos", todos=todos)

        self.emit(t, "subagent_start", name="builder", task="Compile the spec into the next version")
        built = ops.compile_and_test(wd / "spec.yaml")
        self._tool(t, "builder", "compile_spec", {"path": "spec.yaml"}, True, f"compiled {NAME} v{built['version']}")
        self._tool(t, "builder", "run_tests", {"version": built["version"]}, built["tests_passed"], built["tests_summary"])
        self.emit(t, "artifact", kind="package", name=NAME, version=built["version"], path=built["path"])
        self.emit(t, "subagent_end", name="builder", summary=f"v{built['version']}: {built['tests_summary']}")
        if not built["tests_passed"]:
            raise RuntimeError(f"tests failed on v{built['version']}: {built['tests_summary']}")

        todos[3]["status"] = "completed"
        todos[4]["status"] = "in_progress"
        self.emit(t, "todos", todos=todos)
        self.emit(t, "subagent_start", name="tester", task="Dry run 10 records against staging")
        dry = self._dry_run(t)
        self.emit(t, "subagent_end", name="tester", summary=dry)
        return self._request_deploy(t, built, "Initial version built from the recording.")

    # ---- REPAIR --------------------------------------------------------------------------
    def _run_repair(self, t: _Thread, incident_id: int) -> Any:
        store = Store()
        inc = store.incident(incident_id)
        if inc is None:
            raise KeyError(f"incident {incident_id} not found")
        todos = [
            {"content": "Diagnose the incident", "status": "in_progress"},
            {"content": "Propose a spec patch", "status": "pending"},
            {"content": "Compile and test the new version", "status": "pending"},
            {"content": "Request deploy and replay", "status": "pending"},
        ]
        self.emit(t, "todos", todos=todos)
        self.emit(t, "subagent_start", name="repairer", task=f"Incident #{incident_id}: {inc['type']}")
        self._tool(t, "repairer", "get_incident", {"incident_id": incident_id}, True, inc["summary"])
        if inc["type"] == "NEW_SOURCE_FORMAT":
            return self._repair_new_source(t, inc, todos)
        if inc["type"] != "CONTRACT_CHANGED":
            raise RuntimeError(f"scripted repair handles CONTRACT_CHANGED and NEW_SOURCE_FORMAT, got {inc['type']}")
        self._tool(t, "repairer", "diff_openapi", {"old": "v1 snapshot", "new": "live"}, True,
                   "renamed product_id→sku, quantity→quantity_delta; new required warehouse_id (BOS|NYC)")
        self.emit(t, "message", agent="repairer",
                  text="The two renames are auto-patchable. warehouse_id is a new required field I can't infer.")
        todos[0]["status"] = "completed"
        todos[1]["status"] = "in_progress"
        self.emit(t, "todos", todos=todos)

        answer = self.interrupt(t, "ask_user", question="Stockroom now requires warehouse_id. Always BOS, or by supplier?",
                                options=["Always BOS", "By supplier (acme→BOS, globex→NYC, initech→BOS)"])
        by_supplier = "supplier" in str(answer.get("answer", "")).lower()

        wd = work_dir()
        old = yaml.safe_load((wd / "spec.yaml").read_text()) if (wd / "spec.yaml").exists() else \
            yaml.safe_load((FIXTURES / "spec.yaml").read_text())
        warehouse = ({"transform": "lookup", "from": "record.sender_domain", "args": {"table": SUPPLIER_WAREHOUSE}}
                     if by_supplier else {"const": "BOS"})
        patch = [
            {"op": "remove", "path": "/sink/body/product_id"},
            {"op": "remove", "path": "/sink/body/quantity"},
            {"op": "add", "path": "/sink/body/sku", "value": {"from": "sku"}},
            {"op": "add", "path": "/sink/body/quantity_delta", "value": {"from": "qty"}},
            {"op": "add", "path": "/sink/body/warehouse_id", "value": warehouse},
        ]
        new = jsonpatch.apply_patch(old, patch)
        self._tool(t, "repairer", "propose_spec_patch", {"patch": patch}, True, f"{len(patch)} operations")
        errors = validate_spec(yaml.safe_dump(new))
        self._tool(t, "repairer", "validate_spec", {"path": "spec.yaml"}, not errors, "valid" if not errors else "; ".join(errors))
        old_text = (wd / "spec.yaml").read_text() if (wd / "spec.yaml").exists() else (FIXTURES / "spec.yaml").read_text()
        (wd / "spec.yaml").write_text(yaml.safe_dump(new, sort_keys=False))
        self._rewrite_examples_v2(wd, by_supplier)
        self.emit(t, "subagent_end", name="repairer", summary="Patched 2 renames + warehouse_id rule")
        todos[1]["status"] = "completed"
        todos[2]["status"] = "in_progress"
        self.emit(t, "todos", todos=todos)

        self.emit(t, "subagent_start", name="builder", task="Compile the patched spec")
        built = ops.compile_and_test(wd / "spec.yaml")
        self._tool(t, "builder", "compile_spec", {"path": "spec.yaml"}, True, f"compiled {NAME} v{built['version']}")
        self._tool(t, "builder", "run_tests", {"version": built["version"]}, built["tests_passed"], built["tests_summary"])
        self.emit(t, "subagent_end", name="builder", summary=f"v{built['version']}: {built['tests_summary']}")
        if not built["tests_passed"]:
            raise RuntimeError(f"tests failed on v{built['version']}: {built['tests_summary']}")
        todos[2]["status"] = "completed"
        todos[3]["status"] = "in_progress"
        self.emit(t, "todos", todos=todos)
        spec_diff = "".join(difflib.unified_diff(old_text.splitlines(True), (wd / "spec.yaml").read_text().splitlines(True),
                                                 "spec.yaml (active)", f"spec.yaml (v{built['version']})"))
        return self._request_deploy(t, built, "Adapt to Stockroom contract v2.", spec_diff=spec_diff, patch=patch)

    def _repair_new_source(self, t: _Thread, inc: dict, todos: list[dict]) -> Any:
        samples = [s["record"] for s in inc.get("samples") or []]
        domains = sorted({r.get("sender_domain") for r in samples})
        if domains != [NEW_SUPPLIER_DOMAIN]:
            raise RuntimeError(f"scripted repair only knows the {NEW_SUPPLIER_DOMAIN} format, got {domains}")
        line = next((ln.strip() for ln in samples[0]["body"].splitlines() if ln.startswith("Item code:")), "?")
        self._tool(t, "repairer", "get_quarantined", {"incident_id": inc["id"]}, True,
                   f"{len(samples)} samples from {NEW_SUPPLIER_DOMAIN}; order line looks like “{line}”")
        self.emit(t, "message", agent="repairer",
                  text=f"New supplier format from {NEW_SUPPLIER_DOMAIN}: PO after “reference”, SKU and quantity on one "
                       "“Item code:” line. The quantity is in cases, not units.")
        todos[0]["status"] = "completed"
        todos[1]["status"] = "in_progress"
        self.emit(t, "todos", todos=todos)

        m = re.search(CASES, samples[0]["body"])
        cases, per_case = (int(m.group(1)), int(m.group(2))) if m else (3, 12)
        answer = self.interrupt(
            t, "ask_user",
            question=f"{NEW_SUPPLIER_DOMAIN} orders in cases (e.g. “{cases} cases ({per_case}/case)”), but Stockroom counts "
                     "units. Record units = cases × per case?",
            options=[f"Yes: units = cases × per case ({cases} × {per_case} = {cases * per_case})",
                     "No: record the number of cases"])
        units = not str(answer.get("answer", "")).strip().lower().startswith("no")
        parser = umbrella_parser(units)

        ok = 0
        for r in samples:
            try:
                try_parser(parser, r)
                ok += 1
            except Quarantine:
                pass
        self._tool(t, "repairer", "test_parser", {"parser": parser["id"], "samples": len(samples)}, ok == len(samples),
                   f"{parser['id']} matched {ok}/{len(samples)} quarantined records")
        if ok != len(samples):
            raise RuntimeError(f"{parser['id']} matched only {ok}/{len(samples)} samples")

        wd = work_dir()
        old_text = (wd / "spec.yaml").read_text() if (wd / "spec.yaml").exists() else (FIXTURES / "spec.yaml").read_text()
        old = yaml.safe_load(old_text)
        new = json.loads(json.dumps(old))
        new["extract"]["parsers"] = [p for p in new["extract"]["parsers"] if p["id"] != parser["id"]] + [parser]
        wh = new["sink"]["body"].get("warehouse_id")
        warehouse = None
        if wh and wh.get("transform") == "lookup":
            ans = self.interrupt(t, "ask_user", question=f"Which warehouse receives {NEW_SUPPLIER_DOMAIN} stock?",
                                 options=["NYC", "BOS"])
            warehouse = "BOS" if "BOS" in str(ans.get("answer", "")).upper() else "NYC"
            wh["args"]["table"][NEW_SUPPLIER_DOMAIN] = warehouse
        elif wh:
            warehouse = wh.get("const")
        patch = jsonpatch.make_patch(old, new).patch
        self._tool(t, "repairer", "propose_spec_patch", {"patch": patch}, True, f"{len(patch)} operations")
        errors = validate_spec(yaml.safe_dump(new))
        self._tool(t, "repairer", "validate_spec", {"path": "spec.yaml"}, not errors, "valid" if not errors else "; ".join(errors))
        if errors:
            raise RuntimeError("; ".join(errors))
        (wd / "spec.yaml").write_text(yaml.safe_dump(new, sort_keys=False))
        added = self._add_new_supplier_examples(wd, samples[:2], units, warehouse, v2="sku" in new["sink"]["body"])
        self._tool(t, "repairer", "add_test_case", {"records": added}, True, f"{len(added)} golden examples from real records")
        self.emit(t, "subagent_end", name="repairer", summary=f"Added parser {parser['id']}"
                  + (f" and {NEW_SUPPLIER_DOMAIN}→{warehouse}" if warehouse and wh.get("transform") == "lookup" else ""))
        todos[1]["status"] = "completed"
        todos[2]["status"] = "in_progress"
        self.emit(t, "todos", todos=todos)

        self.emit(t, "subagent_start", name="builder", task="Compile the patched spec")
        built = ops.compile_and_test(wd / "spec.yaml")
        self._tool(t, "builder", "compile_spec", {"path": "spec.yaml"}, True, f"compiled {NAME} v{built['version']}")
        self._tool(t, "builder", "run_tests", {"version": built["version"]}, built["tests_passed"], built["tests_summary"])
        self.emit(t, "subagent_end", name="builder", summary=f"v{built['version']}: {built['tests_summary']}")
        if not built["tests_passed"]:
            raise RuntimeError(f"tests failed on v{built['version']}: {built['tests_summary']}")
        todos[2]["status"] = "completed"
        todos[3]["status"] = "in_progress"
        self.emit(t, "todos", todos=todos)
        spec_diff = "".join(difflib.unified_diff(old_text.splitlines(True), (wd / "spec.yaml").read_text().splitlines(True),
                                                 "spec.yaml (active)", f"spec.yaml (v{built['version']})"))
        return self._request_deploy(t, built, f"Parse the new {NEW_SUPPLIER_DOMAIN} format.", spec_diff=spec_diff, patch=patch)

    def _add_new_supplier_examples(self, wd: Path, records: list[dict], units: bool, warehouse: str | None,
                                   v2: bool) -> list[str]:
        """Golden examples for the new format, with the expected body worked out from the text
        independently of the parser (so the generated test checks the parser, not itself)."""
        path = wd / "examples.yaml"
        examples = yaml.safe_load(path.read_text()) if path.exists() else yaml.safe_load((FIXTURES / "examples.yaml").read_text())
        names = []
        for r in records:
            po = re.search(r"PO-\d+", r["body"]).group(0)
            sku, cases, per_case = re.search(r"Item code: (\S+) / (\d+) cases \((\d+)/case\)", r["body"]).groups()
            qty = int(cases) * (int(per_case) if units else 1)
            body = ({"sku": sku, "quantity_delta": qty, "reference": po, "warehouse_id": warehouse} if v2
                    else {"product_id": sku, "quantity": qty, "reference": po})
            name = f"umbrella_{r['id'].replace('-', '_')}"
            examples = [e for e in examples if e["name"] != name]
            examples.append({"name": name, "record": r, "expect": {"body": body}})
            names.append(r["id"])
        path.write_text(yaml.safe_dump(examples, sort_keys=False, width=200))
        return names

    # ---- shared --------------------------------------------------------------------------
    def _dry_run(self, t: _Thread) -> str:
        try:
            summary = ops.run_sandboxed(NAME, version=str(Store().latest_version(NAME)), dry_run=True, limit=10, timeout=60)
            ok = summary.get("status") == "completed"
            text = f"dry run {summary.get('status')}: {summary.get('counts')}"
        except Exception as e:  # noqa: BLE001 - staging may be down in dev; report, don't crash
            ok, text = False, f"dry run unavailable: {e}"
        self._tool(t, "tester", "dry_run", {"limit": 10}, ok, text)
        return text

    def _request_deploy(self, t: _Thread, built: dict, summary: str, spec_diff: str = "", patch: list | None = None) -> Any:
        store = Store()
        code_diff = ""
        prev = store.active_version(NAME)
        if prev:
            prev_dir = workspace_dir() / "integrations" / NAME / f"v{prev}"
            new_dir = Path(built["path"])
            for f in ("integration.py", "parsers.py", "adapters.py"):
                code_diff += "".join(difflib.unified_diff(
                    (prev_dir / f).read_text().splitlines(True), (new_dir / f).read_text().splitlines(True),
                    f"v{prev}/{f}", f"v{built['version']}/{f}"))
        answer = self.interrupt(t, "request_deploy", name=NAME, version=built["version"], summary=summary,
                                spec_diff=spec_diff or (workspace_dir() / "integrations" / NAME / f"v{built['version']}" / "spec.yaml").read_text(),
                                code_diff=code_diff,
                                tests={"passed": built["tests_passed"], "summary": built["tests_summary"]},
                                **({"spec_patch": patch} if patch else {}))
        if answer.get("decision") != "approve":
            self.emit(t, "message", agent="supervisor", text=f"Deploy of v{built['version']} rejected: {answer.get('comment', '')}")
            return {"deployed": False, "version": built["version"]}
        result = ops.deploy(NAME, built["version"])
        self.emit(t, "deployed", name=NAME, version=built["version"], previous_version=result["previous_version"])
        replay = getattr(ops, "replay_quarantined", None)
        if t.mode == "repair" and replay is not None:
            self.emit(t, "message", agent="supervisor", text="Replaying quarantined records on the new version.")
            replay(NAME)
        return {"deployed": True, "version": built["version"]}

    def _rewrite_examples_v2(self, wd: Path, by_supplier: bool) -> None:
        path = wd / "examples.yaml"
        src = path if path.exists() else FIXTURES / "examples.yaml"
        examples = yaml.safe_load(src.read_text())
        for ex in examples:
            body = ex["expect"].get("body")
            if body and "product_id" in body:
                domain = ex["record"]["sender_domain"]
                ex["expect"]["body"] = {
                    "sku": body["product_id"], "quantity_delta": body["quantity"], "reference": body["reference"],
                    "warehouse_id": SUPPLIER_WAREHOUSE[domain] if by_supplier else "BOS",
                }
        path.write_text(yaml.safe_dump(examples, sort_keys=False, width=200))

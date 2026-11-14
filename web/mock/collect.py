"""Run the real stack once (demo apps in-process, compiler, runner, ScriptedAgentService) and
dump the canned data the mock control plane replays to web/mock/data.json.

    python web/mock/collect.py            # from the repo root, with the project venv

Nothing here is hand-made except the candidates (the matcher is still being built), which
are computed from the fixture recording the same way the matcher contract describes.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "data.json"
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import httpx  # noqa: E402
import jsonpatch  # noqa: E402
import yaml  # noqa: E402

NAME = "po_to_stockroom"


def wait_for(svc, tid: str, states=("waiting", "done", "failed"), timeout=120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = svc.status(tid)
        if st["state"] in states:
            return st
        time.sleep(0.05)
    raise TimeoutError(tid)


def thread_log(ws: Path, tid: str) -> list[dict]:
    """Read the JSONL log, not svc.events(): for live threads the in-memory events share the
    scripted agent's mutable todos list, so earlier `todos` events show the final state."""
    text = (ws / "agent_threads" / f"{tid}.jsonl").read_text()
    for prefix in sorted({str(ws.resolve()), str(ws)}, key=len, reverse=True):
        text = text.replace(prefix, "workspace")
    return [json.loads(line) for line in text.splitlines()]


def pytest_list(vdir: Path, env: dict) -> list[dict]:
    out = subprocess.run([sys.executable, "-m", "pytest", str(vdir / "tests"), "-v", "-p", "no:cacheprovider",
                          "--rootdir", str(vdir), "-c", os.devnull], capture_output=True, text=True, env=env,
                         cwd=REPO).stdout
    tests = []
    for ln in out.splitlines():
        m = re.match(r".*tests/(test_\w+)\.py::(\S+) (PASSED|FAILED|SKIPPED|ERROR)", ln)
        if m:
            tests.append({"file": f"tests/{m.group(1)}.py", "name": m.group(2), "status": m.group(3)})
    return tests


def package_files(vdir: Path) -> dict[str, str]:
    files = {}
    for p in sorted(vdir.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".yaml", ".json", ".txt", ".md"):
            rel = str(p.relative_to(vdir))
            if rel == "contract/openapi.json":
                continue  # large; the incident carries the OpenAPI documents
            files[rel] = p.read_text()
    return files


def scenario(repair_answer: str) -> dict:
    ws = Path(tempfile.mkdtemp(prefix="patchwork-mock-"))
    os.environ["PATCHWORK_WORKSPACE"] = str(ws)
    from tests.conftest import _Server
    from demo_apps.inbox.app import create_app as inbox_app
    from demo_apps.stockroom.app import create_app as stock_app
    from agents.scripted import ScriptedAgentService
    from control import ops
    from runner.store import Store

    with _Server(inbox_app()) as inbox, _Server(stock_app()) as live, _Server(stock_app()) as staging:
        os.environ.update(PATCHWORK_SOURCE_BASE_URL=inbox.url, PATCHWORK_SINK_BASE_URL=live.url,
                          PATCHWORK_STAGING_BASE_URL=staging.url)
        svc = ScriptedAgentService(root=ws / "agent_threads", pace_s=0)

        # BUILD
        tid = svc.start_build("fixture")
        st = wait_for(svc, tid)
        assert st["pending"]["type"] == "ask_user", st
        svc.resume(tid, st["pending"]["interrupt_id"], {"answer": "Quarantine them for review"})
        time.sleep(0.1)
        st = wait_for(svc, tid)
        assert st["pending"]["type"] == "request_deploy", st
        svc.resume(tid, st["pending"]["interrupt_id"], {"decision": "approve", "comment": ""})
        wait_for(svc, tid, ("done", "failed"))
        build_events = thread_log(ws, tid)

        dry = ops.run_sandboxed(NAME, dry_run=True, limit=10)
        r1 = ops.run_sandboxed(NAME, limit=100)
        r2 = ops.run_sandboxed(NAME)
        old_openapi = httpx.get(live.url + "/openapi.json").json()
        httpx.post(live.url + "/admin/schema", json={"version": 2})
        httpx.post(staging.url + "/admin/schema", json={"version": 2})
        blocked = ops.run_sandboxed(NAME)

        store = Store()
        incident = store.incident(blocked["incident_id"])

        # REPAIR
        rid = svc.start_repair(blocked["incident_id"])
        st = wait_for(svc, rid)
        assert st["pending"]["type"] == "ask_user", st
        svc.resume(rid, st["pending"]["interrupt_id"], {"answer": repair_answer})
        time.sleep(0.1)
        st = wait_for(svc, rid)
        assert st["pending"]["type"] == "request_deploy", st
        svc.resume(rid, st["pending"]["interrupt_id"], {"decision": "approve", "comment": ""})
        wait_for(svc, rid, ("done", "failed"))
        repair_events = thread_log(ws, rid)

        env = ops.runner_env()
        versions = {}
        for v in store.versions(NAME):
            vdir = ws / "integrations" / NAME / f"v{v['version']}"
            versions[v["version"]] = {
                "version": v["version"],
                "manifest": json.loads((vdir / "manifest.json").read_text()),
                "spec_yaml": (vdir / "spec.yaml").read_text(),
                "files": package_files(vdir),
                "tests_passed": bool(v["tests_passed"]),
                "tests_summary": v["tests_summary"],
                "tests": pytest_list(vdir, env),
            }

        v1, v2 = versions[1], versions[2]
        spec_patch = jsonpatch.make_patch(yaml.safe_load(v1["spec_yaml"]), yaml.safe_load(v2["spec_yaml"])).patch
        spec_diff = "".join(difflib.unified_diff(v1["spec_yaml"].splitlines(True), v2["spec_yaml"].splitlines(True),
                                                 "v1/spec.yaml", "v2/spec.yaml"))
        code_diff = {}
        for f in sorted(set(v1["files"]) | set(v2["files"])):
            if f.startswith("tests/") or f == "spec.yaml":
                continue
            d = "".join(difflib.unified_diff(v1["files"].get(f, "").splitlines(True),
                                             v2["files"].get(f, "").splitlines(True), f"v1/{f}", f"v2/{f}"))
            if d:
                code_diff[f] = d

        records = {r["record_id"]: r for r in store.records(r1["run_id"]) + store.records(r2["run_id"])}
        runs = {k: store.run(s["run_id"]) for k, s in
                {"dry": dry, "first": r1, "rest": r2, "blocked": blocked}.items()}
    return {"build_events": build_events, "repair_events": repair_events, "versions": versions,
            "diff_1_2": {"spec_patch": spec_patch, "spec_diff": spec_diff, "code_diff": code_diff},
            "records": records, "runs": runs, "incident": incident, "old_openapi_title": old_openapi.get("info")}


def candidates(rec: dict) -> dict:
    """What the matcher contract (docs/contracts.md section 2) yields for the fixture recording."""
    post = next(e for e in rec["events"] if e["type"] == "net" and e["method"] == "POST")
    src = next(e for e in rec["events"] if e["type"] == "net" and e["url"].startswith("/api/messages/"))
    body = src["resp"]["body"]
    mappings = []
    for dest, label, regex, cast in (("product_id", "SKU:", r"SKU:\s*([A-Z0-9-]+)", None),
                                     ("quantity", "Qty:", r"Qty:\s*(\d+)", "int"),
                                     ("reference", "PO Number:", r"PO Number:\s*(PO-\d+)", None)):
        m = re.search(regex, body)
        mp = {"dest_field": dest, "value": post["req"][dest], "kind": "text", "source_field": "body",
              "offset": [m.start(1), m.end(1)], "label": label, "candidate_regex": regex}
        if cast:
            mp["cast"] = cast
        mappings.append(mp)
    return {"recording_id": "fixture", "intent": rec["intent"],
            "sink": {"base_url": "http://localhost:8002", "method": "POST", "path": "/api/incoming", "body": post["req"]},
            "source": {"base_url": "http://localhost:8001", "list": {"method": "GET", "path": "/api/messages"},
                       "record_id": "id", "text_field": "body", "record": src["resp"]},
            "mappings": mappings, "unexplained": []}


def main() -> None:
    from demo_apps.seed import SUPPLIER_WAREHOUSE, generate
    a = scenario("By supplier (acme→BOS, globex→NYC, initech→BOS)")
    b = scenario("Always BOS")
    seed = generate()
    msgs = []
    for m in seed.messages:
        gt = seed.ground_truth[m["id"]]
        r = a["records"][m["id"]]
        msgs.append({**m, "truth": gt, "parser": r["parser"], "key": r["dedupe_key"], "detail": r["detail"],
                     "status": r["status"], "reason": r["reason"]})
    rec = json.loads((REPO / "fixtures" / "recording.json").read_text())
    inc = a["incident"]
    data = {
        "generated_by": "web/mock/collect.py (real compiler, runner and ScriptedAgentService)",
        "recording": rec,
        "candidates": candidates(rec),
        "messages": msgs,
        "supplier_warehouse": SUPPLIER_WAREHOUSE,
        "runs": a["runs"],
        "incident": inc,
        "build_events": a["build_events"],
        "repair": {"by_supplier": {"events": a["repair_events"], "v2": a["versions"][2], "diff": a["diff_1_2"]},
                   "always_bos": {"events": b["repair_events"], "v2": b["versions"][2], "diff": b["diff_1_2"]}},
        "v1": a["versions"][1],
    }
    OUT.write_text(json.dumps(data, indent=1, default=str))
    print("wrote", OUT, f"{OUT.stat().st_size // 1024} KB;", len(msgs), "messages;",
          len(inc["detail"]["contract_errors"]), "contract errors;", len(a["versions"][1]["tests"]), "v1 tests;",
          len(a["build_events"]), "build events;", len(a["repair_events"]), "repair events")


if __name__ == "__main__":
    main()

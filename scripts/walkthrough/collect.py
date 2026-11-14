"""Run the MVP demo for real and dump everything the visual page needs to data.json."""
import json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

REPO = Path(sys.argv[1]); OUT = Path(sys.argv[2])
sys.path.insert(0, str(REPO)); os.chdir(REPO)
ws = Path(tempfile.mkdtemp()); os.environ["PATCHWORK_WORKSPACE"] = str(ws)

from tests.conftest import _Server
from demo_apps.inbox.app import create_app as inbox_app
from demo_apps.stockroom.app import create_app as stock_app
import httpx, yaml
from control import ops
from runner.store import Store
from bench.accuracy import score
from demo_apps.seed import generate
from spec import load_spec

with _Server(inbox_app()) as inbox, _Server(stock_app()) as live, _Server(stock_app()) as staging:
    os.environ.update(PATCHWORK_SOURCE_BASE_URL=inbox.url, PATCHWORK_SINK_BASE_URL=live.url,
                      PATCHWORK_STAGING_BASE_URL=staging.url)
    t = time.time(); built = ops.compile_and_test("fixtures/spec.yaml"); compile_s = time.time() - t
    ops.deploy("po_to_stockroom", 1)
    dry = ops.run_sandboxed("po_to_stockroom", dry_run=True, limit=10)
    r1 = ops.run_sandboxed("po_to_stockroom", limit=100)
    r2 = ops.run_sandboxed("po_to_stockroom")
    v1_openapi = httpx.get(live.url + "/openapi.json").json()
    httpx.post(live.url + "/admin/schema", json={"version": 2})
    v2_openapi = httpx.get(live.url + "/openapi.json").json()
    writes_before = len(httpx.get(live.url + "/api/incoming").json())
    blocked = ops.run_sandboxed("po_to_stockroom")
    writes_after = len(httpx.get(live.url + "/api/incoming").json())

store = Store()
records = store.records(r1["run_id"]) + store.records(r2["run_id"])
acc = score(records)
vdir = ws / "integrations/po_to_stockroom/v1"
pytest_v = subprocess.run([sys.executable, "-m", "pytest", str(vdir / "tests"), "-v", "-p", "no:cacheprovider",
                           "--rootdir", str(vdir), "-c", os.devnull], capture_output=True, text=True,
                          env=ops.runner_env(), cwd=REPO).stdout
tests = []
for ln in pytest_v.splitlines():
    m = re.match(r".*tests/(test_\w+)\.py::(\S+) (PASSED|FAILED|SKIPPED)", ln)
    if m: tests.append({"file": m.group(1), "name": m.group(2), "status": m.group(3)})

seed = generate(); by_id = {m["id"]: m for m in seed.messages}
spec = load_spec("fixtures/spec.yaml")
parsers = {p.id: p for p in spec.extract.parsers}
order = {m["id"]: i for i, m in enumerate(seed.messages)}
rec_list = sorted(records, key=lambda r: order[r["record_id"]])
first_seen = {}
compact = []
for r in rec_list:
    m = by_id[r["record_id"]]
    pid = next((p.id for p in spec.extract.parsers if all(m.get(k) == v for k, v in p.match.items())), None)
    spans = []
    if pid:
        for fname, f in parsers[pid].fields.items():
            mm = re.search(f.regex, m["body"])
            if mm: spans.append({"field": fname, "start": mm.start(1), "end": mm.end(1), "value": mm.group(1),
                                 "regex": f.regex, "mstart": mm.start(), "mend": mm.end()})
    dup_of = None
    if r["status"] == "duplicate": dup_of = first_seen.get(r["dedupe_key"])
    elif r["dedupe_key"] and r["status"] == "success": first_seen.setdefault(r["dedupe_key"], r["record_id"])
    compact.append({"id": r["record_id"], "sender": m["sender"], "domain": m["sender_domain"], "subject": m["subject"],
                    "body": m["body"], "received_at": m["received_at"], "status": r["status"], "reason": r["reason"],
                    "detail": r["detail"], "parser": pid, "key": r["dedupe_key"], "sink_body": r["sink_body"],
                    "spans": spans, "dup_of": dup_of, "run": 1 if r["run_id"] == r1["run_id"] else 2})

inc = store.incident(blocked["incident_id"])
def req_schema(doc, name): return doc["components"]["schemas"][name]
data = {
    "spec_yaml": Path("fixtures/spec.yaml").read_text(),
    "code": {f: (vdir / f).read_text() for f in ["integration.py", "parsers.py", "adapters.py"]},
    "build": {"version": built["version"], "tests_summary": built["tests_summary"], "compile_s": round(compile_s, 2)},
    "tests": tests,
    "runs": {"dry": dry, "first": r1, "rest": r2, "blocked": blocked},
    "accuracy": {k: acc[k] for k in ("records", "correct", "accuracy")},
    "records": compact,
    "break1": {"contract_errors": inc["detail"]["contract_errors"], "incident": {k: inc[k] for k in ("id", "type", "reason_codes", "summary", "status")},
               "v1_schema": req_schema(v1_openapi, "IncomingV1"), "v2_schema": req_schema(v2_openapi, "IncomingV2"),
               "warehouse_enum": v2_openapi["components"]["schemas"]["Warehouse"]["enum"],
               "writes_before": writes_before, "writes_after": writes_after},
}
OUT.write_text(json.dumps(data, indent=1))
print("ok", len(compact), "records;", acc, "; tests", len(tests), "; blocked", blocked["status"], writes_before, writes_after)

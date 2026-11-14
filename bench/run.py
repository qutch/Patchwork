"""Benchmark harness: Patchwork vs a one-shot script, on the same scenario, scored against ground truth.

    python -m bench.run                 # or: make bench   -> writes bench/results.json
    python -m bench.run --out /tmp/r.json --workspace /tmp/ws

Runs its own in-process stack (Inbox, Stockroom live + staging, control plane, scripted agent) on
free ports, never the live-demo ports, with its own PATCHWORK_WORKSPACE. No model calls.

Scenario (the demo's):
  1. BUILD from fixtures/recording.json, approve v1      (Patchwork only)
  2. run the first 100 seed POs, then the other 50       (150 total: 3 formats, resends, bad rows, junk)
  3. Break 1: Stockroom switches to contract v2, 5 new POs are delivered, run
  4. REPAIR Break 1, run again                           (Patchwork only)
  5. Break 2: 4 POs from a new supplier (umbrella.com, cases instead of units), run
  6. REPAIR Break 2                                      (Patchwork only)

The baseline (bench/baseline_script.py, written once from bench/baseline_prompt.md) runs the same
steps without the repairs. Its Break 2 is measured on a fresh stack with contract v1, so that
Break 1 (which it cannot repair) does not mask it.

Every number in results.json carries `kind` (measured | measured (earlier run) | extrapolated |
estimate | not measured) and `source`. Manual / computer-use timings recorded with
`python -m bench.stopwatch` are kept across reruns.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from bench.accuracy import score as score_records
from bench.scoring import baseline_flagged, crashed, enrich_truth, score_writes
from bench.stack import Stack

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "bench" / "results.json"
BASELINE = REPO / "bench" / "baseline_script.py"
NAME = "po_to_stockroom"
RUN_RECORDS = 100
BREAK1_DELIVERIES = 5
BREAK2_MESSAGES = 4
SRC_RUN = "bench/run.py (this run)"


# ---------------------------------------------------------------------------------------------
# metrics

KINDS = ("measured", "measured (earlier run)", "extrapolated", "estimate", "not measured")


def metric(value: float | int | None, unit: str, kind: str, source: str, note: str | None = None) -> dict:
    assert kind in KINDS, kind
    assert (value is None) == (kind == "not measured"), (value, kind)
    m: dict[str, Any] = {"value": value, "unit": unit, "kind": kind, "source": source}
    if note:
        m["note"] = note
    return m


def not_measured(unit: str, source: str, note: str | None = None) -> dict:
    return metric(None, unit, "not measured", source, note)


def measured(value: float | int, unit: str, note: str | None = None, source: str = SRC_RUN) -> dict:
    return metric(value, unit, "measured", source, note)


# Inputs that are not measured by this harness. Kept here, in one place, with their reasoning.
MANUAL_ESTIMATE_MIN = 1.0
MANUAL_ASSUMPTION = ("Estimate, not timed: open the message, read PO/SKU/qty, switch to the Stockroom, pick the "
                     "product from a 50-item list, type qty, paste the reference, save: ~45-60 s per record for a "
                     "practised person. Replace with `python -m bench.stopwatch manual` (time 5, extrapolate).")
COMPUTER_USE_ESTIMATE_MIN = 3.0
COMPUTER_USE_SOURCE = ("Abhyankar, Qi, Zhang, 'OSWorld-Human: Benchmarking the Efficiency of Computer-Use Agents', "
                       "arXiv:2506.16042 (v2, 2026): agents take 'tens of minutes' for tasks humans do in 'a few "
                       "minutes', and the best agents take 2.7-4.3x more steps than necessary; latency is dominated "
                       "by model calls")
COMPUTER_USE_ASSUMPTION = ("Estimate, not run (no computer-use API key): this is a ~1 min human task of ~15 GUI "
                           "steps; at the cited 2.7-4.3x step overhead and several seconds per model-call step, "
                           "~3 min per record. Replace with `python -m bench.stopwatch computer_use` (run 5, "
                           "extrapolate).")
COMPUTER_USE_CALLS_PER_RECORD = 15
SETUP_HUMAN_MIN = 2.0
SETUP_HUMAN_ASSUMPTION = ("Estimate: demonstrate one transfer in the browser (~1 min) plus answer the agent's "
                          "question and approve v1 (~1 min). The agent's own BUILD time is counted separately.")
COMPUTER_USE_SETUP_MIN = 5.0

DEEP = {
    "date": "2026-11-14",
    "model": "nvidia/nemotron-3-ultra-550b-a55b:free (OpenRouter)",
    "source": ("scripts/smoke_demo.py against `PATCHWORK_AGENT=deep make up`, log smoke-deep2.log (2026-11-14 01:40); "
               "times are from the log, model-call counts from the run report (the log does not print them)"),
    "build_s": 138.2, "build_calls": 30,
    "repair_break1_s": 78.7, "repair_break1_calls": 22,
    "repair_break2_s": 104.3, "repair_break2_calls": 27,
    "demo_total_s": 325,
}


# ---------------------------------------------------------------------------------------------
# Patchwork through the control plane (same calls as the web console and scripts/smoke_demo.py)

def _presenter_answer():
    spec = importlib.util.spec_from_file_location("smoke_demo", REPO / "scripts" / "smoke_demo.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod.presenter_answer


class Control:
    def __init__(self, st: Stack):
        self.st = st
        self.api = httpx.Client(base_url=st.control, timeout=120)
        self.answer = _presenter_answer()

    def get(self, path: str, **kw: Any) -> Any:
        r = self.api.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any = None) -> Any:
        r = self.api.post(path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {r.status_code} {r.text[:300]}")
        return r.json()

    def wait_thread(self, tid: str, want: set[str], timeout: float = 120) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.get(f"/api/agent/{tid}")
            if s["state"] in want:
                return s
            if s["state"] == "failed":
                raise RuntimeError(f"agent thread {tid} failed")
            time.sleep(0.02)
        raise RuntimeError(f"agent thread {tid} not {want}")

    def answer_until_deploy(self, tid: str) -> dict:
        """Answer ask_user like the presenter and approve the deploy instantly (no human think time)."""
        questions = 0
        for _ in range(10):
            s = self.wait_thread(tid, {"waiting", "done"})
            p = s.get("pending") or {}
            if s["state"] != "waiting":
                raise RuntimeError(f"agent finished without asking to deploy: {s}")
            if p["type"] == "ask_user":
                questions += 1
                self.post(f"/api/agent/{tid}/resume", {"interrupt_id": p["interrupt_id"],
                                                       "answer": self.answer(p["question"], p.get("options") or [])})
                continue
            if not p["tests"]["passed"]:
                raise RuntimeError(f"generated tests failed: {p['tests']}")
            self.post(f"/api/agent/{tid}/resume", {"interrupt_id": p["interrupt_id"], "decision": "approve"})
            self.wait_thread(tid, {"done"})
            return {"version": p["version"], "questions": questions, "tests": p["tests"].get("summary")}
        raise RuntimeError("too many questions")

    def run(self, **body: Any) -> tuple[dict, float]:
        t = time.monotonic()
        rid = self.post("/api/runs", {"integration": NAME, **body})["run_id"]
        while True:
            run = self.get(f"/api/runs/{rid}")["run"]
            if run["status"] != "running":
                return run, time.monotonic() - t
            time.sleep(0.01)

    def records(self, run_id: int) -> list[dict]:
        return self.get(f"/api/runs/{run_id}")["records"]

    def accuracy(self) -> dict:
        return self.get("/api/bench/accuracy", params={"integration": NAME})


def stock_rows(url: str) -> list[dict]:
    return httpx.get(f"{url}/api/incoming", timeout=10).json()


def inbox_truth(st: Stack) -> dict[str, dict]:
    truth = httpx.get(f"{st.inbox}/admin/ground_truth", timeout=10).json()
    messages = httpx.get(f"{st.inbox}/api/messages", timeout=10).json()
    return enrich_truth(truth, messages)


def _new_ids(before: dict, after: dict) -> list[str]:
    return [k for k in after if k not in before]


def measure_patchwork(workspace: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with Stack(workspace) as st:
        c = Control(st)
        c.post("/api/demo/reset")

        # 1. BUILD
        t = time.monotonic()
        tid = c.post("/api/integrations/build", {"recording_id": "fixture"})["thread_id"]
        b = c.answer_until_deploy(tid)
        out["build"] = {"wall_s": time.monotonic() - t, **b}

        # 2. run 100, then the rest
        truth = inbox_truth(st)
        run, wall = c.run(limit=RUN_RECORDS)
        recs = c.records(run["id"])
        acc = score_records(recs, truth)
        out["run"] = {"wall_s": wall, "duration_s": run["duration_s"], "records": len(recs),
                      "llm_calls": run["llm_calls"], "counts": run["counts"], "status": run["status"],
                      "accuracy": acc["accuracy"], "correct": acc["correct"]}
        run2, wall2 = c.run()
        seed_acc = c.accuracy()
        rows = stock_rows(st.live)
        seed_writes = score_writes(truth, list(truth), rows, flagged=_quarantined_ids(c, [run["id"], run2["id"]]))
        out["seed"] = {"records": seed_acc["records"], "accuracy": seed_acc["accuracy"],
                       "rest_wall_s": wall2, "rest_records": len(c.records(run2["id"])),
                       "writes": _strip(seed_writes)}

        # 3. Break 1
        before_truth = truth
        rows_before = stock_rows(st.live)
        c.post("/api/demo/stockroom/schema", {"version": 2})
        c.post("/api/demo/inbox/deliver", {"count": BREAK1_DELIVERIES})
        truth = inbox_truth(st)
        b1_ids = _new_ids(before_truth, truth)
        run, wall = c.run()
        rows_after = stock_rows(st.live)
        inc = c.get(f"/api/incidents/{run['incident_id']}") if run.get("incident_id") else None
        recs = c.records(run["id"])
        new_rows = rows_after[len(rows_before):]
        w = score_writes(truth, b1_ids, new_rows, prior_rows=rows_before,
                         flagged=b1_ids if inc else [])  # a blocked run holds every record behind the incident
        b1: dict[str, Any] = {"ids": b1_ids, "status": run["status"], "detect_wall_s": wall,
                              "incident_type": inc and inc["type"], "incident_id": inc and inc["id"],
                              "quarantined": sum(r["status"] == "quarantined" for r in recs),
                              "writes": _strip(w)}
        # 4. REPAIR Break 1, then run the held records
        t = time.monotonic()
        rtid = c.post(f"/api/incidents/{inc['id']}/repair")["thread_id"]
        b1["repair"] = c.answer_until_deploy(rtid)
        b1["repair_s"] = time.monotonic() - t
        c.run()
        b1["accuracy_after"] = c.accuracy()["accuracy"]
        out["break1"] = b1

        # 5. Break 2
        before_truth, rows_before = truth, stock_rows(st.live)
        c.post("/api/demo/inbox/new_supplier", {"count": BREAK2_MESSAGES})
        truth = inbox_truth(st)
        b2_ids = _new_ids(before_truth, truth)
        run, wall = c.run()
        rows_after = stock_rows(st.live)
        recs = c.records(run["id"])
        q = {r["record_id"] for r in recs if r["status"] == "quarantined"}
        incs = [i for i in c.get("/api/incidents", params={"status": "open"}) if i["type"] == "NEW_SOURCE_FORMAT"]
        w = score_writes(truth, b2_ids, rows_after[len(rows_before):], prior_rows=rows_before, flagged=q)
        b2: dict[str, Any] = {"ids": b2_ids, "status": run["status"], "detect_wall_s": wall,
                              "incident_type": incs[0]["type"] if incs else None,
                              "incident_id": incs[0]["id"] if incs else None,
                              "quarantined": len(q & set(b2_ids)), "writes": _strip(w)}
        # 6. REPAIR Break 2 (the agent replays the quarantined records after the deploy)
        t = time.monotonic()
        rtid = c.post(f"/api/incidents/{incs[0]['id']}/repair")["thread_id"]
        b2["repair"] = c.answer_until_deploy(rtid)
        b2["repair_s"] = time.monotonic() - t
        acc = c.accuracy()
        b2["accuracy_after"] = acc["accuracy"]
        b2["records_after"] = acc["records"]
        out["break2"] = b2
    return out


def _quarantined_ids(c: Control, run_ids: list[int]) -> set[str]:
    return {r["record_id"] for rid in run_ids for r in c.records(rid) if r["status"] == "quarantined"}


def _strip(w: dict) -> dict:
    return {k: v for k, v in w.items() if k != "examples"} | {"examples": {k: v[:3] for k, v in w["examples"].items()}}


# ---------------------------------------------------------------------------------------------
# The one-shot baseline script, as cron would run it

def run_baseline(st: Stack, state_file: Path) -> dict[str, Any]:
    env = {"PATH": "/usr/bin:/bin", "INBOX_URL": st.inbox, "STOCKROOM_URL": st.live,
           "PO_STATE_FILE": str(state_file), "HOME": str(state_file.parent)}
    t = time.monotonic()
    p = subprocess.run([sys.executable, str(BASELINE)], env=env, cwd=state_file.parent,
                       capture_output=True, text=True, timeout=300)
    wall = time.monotonic() - t
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    errors = [ln for ln in p.stderr.splitlines() if " ERROR " in ln]
    return {"exit_code": p.returncode, "wall_s": wall, "crashed": crashed(p.returncode, p.stderr),
            "error_lines": len(errors), "error_sample": errors[:3], "state": state}


def measure_baseline(workspace: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with Stack(workspace, control=False) as st:
        sdir = workspace / "baseline-a"
        sdir.mkdir(parents=True, exist_ok=True)
        state = sdir / "state.json"
        truth = inbox_truth(st)

        r = run_baseline(st, state)
        rows = stock_rows(st.live)
        flagged = baseline_flagged(r["state"])
        out["run"] = {**_res(r), "records": len(truth), "writes": _strip(score_writes(truth, list(truth), rows, flagged=flagged))}
        out["steady"] = _res(run_baseline(st, state))

        # Break 1
        rows_before = rows
        httpx.post(f"{st.live}/admin/schema", json={"version": 2}).raise_for_status()
        httpx.post(f"{st.inbox}/admin/deliver", json={"count": BREAK1_DELIVERIES}).raise_for_status()
        new_truth = inbox_truth(st)
        ids = _new_ids(truth, new_truth)
        r = run_baseline(st, state)
        rows_after = stock_rows(st.live)
        w = score_writes(new_truth, ids, rows_after[len(rows_before):], prior_rows=rows_before,
                         flagged=baseline_flagged(r["state"]) & set(ids))
        out["break1"] = {**_res(r), "ids": ids, "writes": _strip(w),
                         "statuses": {i: (r["state"].get("messages", {}).get(i) or {}).get("status") for i in ids}}

        # Break 2, isolated: fresh apps on contract v1 and a fresh state file
        httpx.post(f"{st.inbox}/admin/reset").raise_for_status()
        httpx.post(f"{st.live}/admin/reset").raise_for_status()
        sdir = workspace / "baseline-b"
        sdir.mkdir(parents=True, exist_ok=True)
        state = sdir / "state.json"
        truth = inbox_truth(st)
        run_baseline(st, state)
        rows_before = stock_rows(st.live)
        httpx.post(f"{st.inbox}/admin/new_supplier", json={"count": BREAK2_MESSAGES}).raise_for_status()
        new_truth = inbox_truth(st)
        ids = _new_ids(truth, new_truth)
        r = run_baseline(st, state)
        rows_after = stock_rows(st.live)
        w = score_writes(new_truth, ids, rows_after[len(rows_before):], prior_rows=rows_before,
                         flagged=baseline_flagged(r["state"]) & set(ids))
        out["break2"] = {**_res(r), "ids": ids, "writes": _strip(w),
                         "statuses": {i: (r["state"].get("messages", {}).get(i) or {}).get("status") for i in ids}}
    return out


def _res(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "state"}


# ---------------------------------------------------------------------------------------------
# results.json

def timing_metric(timings_s: list[float], estimate_min: float, unit_source: str, assumption: str,
                  estimate_source: str) -> dict:
    """Per-record minutes: the mean of stopwatch timings if any were recorded, else the estimate."""
    if timings_s:
        mean = sum(timings_s) / len(timings_s) / 60
        return metric(round(mean, 3), "min", "measured", unit_source,
                      f"mean of {len(timings_s)} timed transfers; per-record minutes beyond these are extrapolated")
    return metric(estimate_min, "min", "estimate", estimate_source, assumption)


def build_results(pw: dict[str, Any], bl: dict[str, Any], prior: dict[str, Any] | None = None,
                  generated_at: str | None = None) -> dict[str, Any]:
    prior = prior or {}
    manual_t = list((prior.get("manual") or {}).get("timings_s") or [])
    cu_t = list((prior.get("computer_use") or {}).get("timings_s") or [])

    run = pw["run"]
    per_ms = run["duration_s"] / run["records"] * 1000 if run["records"] else None
    b1, b2 = pw["break1"], pw["break2"]

    def pw_break(b: dict, name: str, n: int, alert: str, outcome: str) -> dict:
        w = b["writes"]
        return {
            "name": name, "records_in_break": n,
            "detected": b["incident_type"] is not None,
            "caught_before_bad_write": b["incident_type"] is not None and w["wrong_writes"] == 0,
            "bad_writes": measured(w["wrong_writes"], "records", "wrong rows written to the Stockroom"),
            "silently_dropped": measured(w["silently_dropped"], "records"),
            "held": measured(w["held"], "records", "held behind an open incident until the repair"),
            "time_to_detect_s": measured(round(b["detect_wall_s"], 3), "s",
                                         "wall time of the first run after the break; the incident is open when it ends"),
            "repair_s": measured(round(b["repair_s"], 3), "s",
                                 f"scripted agent, no model calls, questions answered and v{b['repair']['version']} "
                                 "approved instantly by the harness (no human think time)"),
            "incident_type": b["incident_type"],
            "alert": alert, "outcome": outcome,
        }

    def bl_break(b: dict, name: str, n: int, alert: str, outcome: str) -> dict:
        w = b["writes"]
        detected = w["held"] > 0  # the break's own records were logged at ERROR (not just generic noise)
        src = "bench/run.py (this run): bench/baseline_script.py as cron would run it"
        return {
            "name": name, "records_in_break": n, "detected": detected,
            "caught_before_bad_write": detected and w["wrong_writes"] == 0,
            "bad_writes": measured(w["wrong_writes"], "records", source=src),
            "silently_dropped": measured(w["silently_dropped"], "records", source=src),
            "held": measured(w["held"], "records", "logged at ERROR and retried every run", source=src),
            "time_to_detect_s": not_measured("s", src, "the script has no notion of a break; it never opens an incident"),
            "repair_s": not_measured("s", src, "no repair path: someone has to notice, then rewrite the script"),
            "incident_type": None, "alert": alert, "outcome": outcome,
        }

    b1w, b2w = b1["writes"], b2["writes"]
    pw_b1 = pw_break(b1, "Break 1: Stockroom contract v2", len(b1["ids"]),
                     f"run blocked by preflight; incident #{b1['incident_id']} {b1['incident_type']} with old/new OpenAPI",
                     f"Blocked before any write ({b1w['wrong_writes']} bad writes); {b1w['held']} records held, "
                     f"repaired to v{b1['repair']['version']} and written after approval.")
    pw_b2 = pw_break(b2, "Break 2: new supplier format", len(b2["ids"]),
                     f"{b2['quarantined']} records quarantined PARSE_NO_MATCH; incident #{b2['incident_id']} {b2['incident_type']}",
                     f"Quarantined with a reason code ({b2w['wrong_writes']} bad writes); repaired to "
                     f"v{b2['repair']['version']} (units = cases x per case) and replayed.")

    bb1, bb2 = bl["break1"], bl["break2"]
    bb1w, bb2w = bb1["writes"], bb2["writes"]
    steady = bl["steady"]["exit_code"]
    noise = f"; every run already exits {steady} because the seed's zero-qty and unknown-SKU POs fail on each retry"
    alert1 = f"exit code {bb1['exit_code']}, {bb1['error_lines']} ERROR log lines (Stockroom 422s){noise}"
    if bb1w["wrong_writes"] == 0 and bb1w["silently_dropped"] == 0 and bb1w["held"] == bb1w["expected_writes"]:
        out1 = (f"No bad writes: the Stockroom rejected every v1 body (422) and the script kept {bb1w['held']} "
                "records for retry. It cannot adapt, so they fail on every run until someone rewrites it.")
    else:
        out1 = _generic_outcome(bb1w)
    bl_b1 = bl_break(bb1, "Break 1: Stockroom contract v2", len(bb1["ids"]), alert1, out1)
    statuses2 = set((bb2.get("statuses") or {}).values())
    if statuses2 == {"not_po"} and bb2w["wrong_writes"] == 0:
        alert2 = ("nothing: umbrella.com is not a known supplier, so its messages are filed as 'not a PO' "
                  f"(DEBUG log only){noise}")
        out2 = (f"{bb2w['silently_dropped']} of {len(bb2['ids'])} purchase orders silently dropped and marked done "
                "in the state file, so they are never retried.")
    else:
        alert2 = f"exit code {bb2['exit_code']}, {bb2['error_lines']} ERROR log lines{noise}"
        out2 = _generic_outcome(bb2w)
    bl_b2 = bl_break(bb2, "Break 2: new supplier format", len(bb2["ids"]), alert2, out2)

    blr = bl["run"]
    blw = blr["writes"]
    src_bl = "bench/run.py (this run): bench/baseline_script.py as cron would run it"
    d = DEEP
    deep_src = d["source"]
    em = "measured (earlier run)"

    results = {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": "python -m bench.run  (make bench)",
        "environment": {"python": platform.python_version(), "platform": platform.platform(terse=True),
                        "agent": "scripted (pace 0, no model calls)"},
        "scenario": {
            "seed_records": pw["seed"]["records"], "run_records": run["records"],
            "break1": f"Stockroom switches to contract v2 (product_id->sku, quantity->quantity_delta, required "
                      f"warehouse_id); {len(b1['ids'])} new POs delivered",
            "break2": f"{len(b2['ids'])} POs from a new supplier, umbrella.com: 'Item code: SKU / N cases (M/case)'",
            "notes": [
                "Seed: 150 messages from 3 supplier formats: 120 valid, 10 resends, 9 zero-qty, 9 unknown SKUs, 2 junk.",
                "Patchwork runs through the control-plane API with the scripted agent; questions are answered and "
                "deploys approved instantly by the harness, so repair times exclude human think time.",
                "The baseline script was written once by a fresh Claude session from bench/baseline_prompt.md "
                "(recording, intent, v1 APIs, six sample messages) and is unchanged since.",
                "Baseline Break 2 is measured on a fresh stack with contract v1, so Break 1 (which the script "
                "cannot repair) does not mask it. Sequentially the result is the same: the new sender is "
                "filtered out before any write.",
            ],
        },
        "patchwork": {
            "agent": "scripted",
            "build": {"wall_s": measured(round(pw["build"]["wall_s"], 3), "s",
                                         f"scripted agent: compile + generated tests + approve v{pw['build']['version']}; "
                                         "question answered and deploy approved instantly"),
                      "llm_calls": measured(0, "calls", "scripted agent")},
            "run": {
                "records": measured(run["records"], "records"),
                "wall_s": measured(round(run["wall_s"], 3), "s", "POST /api/runs until the run finished, incl. subprocess start"),
                "per_record_ms": measured(round(per_ms, 3), "ms", "runner duration_s / records (pace 0)"),
                "llm_calls": measured(run["llm_calls"], "calls", "runner's own count; the runner cannot import a model SDK"),
                "accuracy": measured(run["accuracy"], "ratio", f"bench.accuracy.score vs ground truth: {run['correct']}/{run['records']}"),
            },
            "seed_accuracy": measured(pw["seed"]["accuracy"], "ratio",
                                      f"all {pw['seed']['records']} seed records (latest outcome per record)"),
            "setup_human_min": metric(SETUP_HUMAN_MIN, "min", "estimate", "assumption", SETUP_HUMAN_ASSUMPTION),
            "breaks": {"break1": pw_b1, "break2": pw_b2},
            "accuracy_after_repairs": measured(b2["accuracy_after"], "ratio",
                                               f"all {b2['records_after']} records after both repairs"),
            "deep_agent": {
                "date": d["date"], "model": d["model"], "source": deep_src,
                "build_s": metric(d["build_s"], "s", em, deep_src, "BUILD incl. 1 question answered by the smoke script"),
                "build_calls": metric(d["build_calls"], "calls", em, deep_src),
                "repair_break1_s": metric(d["repair_break1_s"], "s", em, deep_src, "incident -> v2 approved, 2 questions"),
                "repair_break1_calls": metric(d["repair_break1_calls"], "calls", em, deep_src),
                "repair_break2_s": metric(d["repair_break2_s"], "s", em, deep_src, "incident -> v3 approved, 1 question"),
                "repair_break2_calls": metric(d["repair_break2_calls"], "calls", em, deep_src),
                "demo_total_s": metric(d["demo_total_s"], "s", em, deep_src, "whole smoke demo incl. runs"),
            },
        },
        "baseline_script": {
            "prompt": "bench/baseline_prompt.md", "script": "bench/baseline_script.py",
            "how_written": ("One prompt, one answer: a fresh Claude session that could read only the prompt (no repo, "
                            "no seed, no ground truth, no Break code) wrote it; it was not run or tuned before the benchmark."),
            "run": {
                "records": measured(blr["records"], "records", source=src_bl),
                "wall_s": measured(round(blr["wall_s"], 3), "s", "one cron-style invocation over the 150-message inbox", src_bl),
                "per_record_ms": measured(round(blr["wall_s"] / blr["records"] * 1000, 3), "ms",
                                          "process wall time / records (incl. Python start-up)", src_bl),
                "llm_calls": measured(0, "calls", "plain script", src_bl),
                "correct_writes": measured(blw["correct_writes"], "records", f"of {blw['expected_writes']} expected", src_bl),
                "wrong_writes": measured(blw["wrong_writes"], "records", source=src_bl),
                "silently_dropped": measured(blw["silently_dropped"], "records", source=src_bl),
                "held": measured(blw["held"], "records", "logged at ERROR, retried every run", src_bl),
                "accuracy": measured(blw["accuracy"], "ratio", f"bench.scoring.score_writes: {blw['correct_records']}/{blw['records']} messages handled right", src_bl),
                "exit_code": measured(blr["exit_code"], "code", source=src_bl),
            },
            "steady_state_exit_code": measured(steady, "code",
                                               "a run with nothing new: the zero-qty and unknown-SKU POs are retried and fail every time", src_bl),
            "breaks": {"break1": bl_b1, "break2": bl_b2},
        },
        "manual": {
            "per_record_min": timing_metric(manual_t, MANUAL_ESTIMATE_MIN, "bench/stopwatch.py (manual)",
                                            MANUAL_ASSUMPTION, "assumption (not timed yet)"),
            "timings_s": manual_t,
        },
        "computer_use": {
            "per_record_min": timing_metric(cu_t, COMPUTER_USE_ESTIMATE_MIN, "bench/stopwatch.py (computer_use)",
                                            COMPUTER_USE_ASSUMPTION, COMPUTER_USE_SOURCE),
            "setup_min": metric(COMPUTER_USE_SETUP_MIN, "min", "estimate", "assumption",
                                "write and test the agent's instructions once"),
            "llm_calls_per_record": metric(COMPUTER_USE_CALLS_PER_RECORD, "calls", "estimate", COMPUTER_USE_SOURCE,
                                           "one model call per GUI step, ~15 steps per record"),
            "timings_s": cu_t,
        },
    }
    pb, bb = results["patchwork"]["breaks"], results["baseline_script"]["breaks"]
    results["summary"] = {
        "breaks_total": 2,
        "caught_before_bad_write": {
            "patchwork": sum(b["caught_before_bad_write"] for b in pb.values()),
            "baseline_script": sum(b["caught_before_bad_write"] for b in bb.values()),
        },
    }
    return results


def _generic_outcome(w: dict) -> str:
    return (f"{w['correct_writes']} correct writes, {w['wrong_writes']} wrong writes, {w['silently_dropped']} "
            f"silently dropped, {w['held']} held for retry (of {w['expected_writes']} expected writes).")


def check_labels(obj: Any, path: str = "") -> list[str]:
    """Every number must sit in a metric with kind + source (except raw stopwatch timings, counts in
    summary/scenario, schema_version). Returns the offending paths."""
    bad: list[str] = []
    if isinstance(obj, dict):
        if "value" in obj and "kind" in obj:
            if obj["kind"] not in KINDS or not obj.get("source"):
                bad.append(path)
            return bad
        for k, v in obj.items():
            bad += check_labels(v, f"{path}.{k}")
    elif isinstance(obj, list):
        pass  # timings_s are raw stopwatch inputs
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        top = path.split(".")[1] if path.count(".") else path
        if top not in ("summary", "scenario", "schema_version") and not path.endswith("records_in_break"):
            bad.append(path)
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=RESULTS)
    ap.add_argument("--workspace", type=Path, default=None, help="default: a fresh temp dir, removed afterwards")
    ap.add_argument("--raw", type=Path, default=None, help="also write the raw measurements here")
    args = ap.parse_args(argv)

    ws = args.workspace or Path(tempfile.mkdtemp(prefix="patchwork-bench-"))
    try:
        print("Patchwork (scripted agent): BUILD, run, Break 1 + REPAIR, Break 2 + REPAIR ...", flush=True)
        pw = measure_patchwork(ws / "patchwork")
        print("Baseline script: run, Break 1, Break 2 ...", flush=True)
        bl = measure_baseline(ws / "baseline")
    finally:
        if args.workspace is None:
            shutil.rmtree(ws, ignore_errors=True)
    if args.raw:
        args.raw.write_text(json.dumps({"patchwork": pw, "baseline": bl}, indent=2, default=str))
    prior = json.loads(args.out.read_text()) if args.out.exists() else None
    results = build_results(pw, bl, prior)
    bad = check_labels(results)
    if bad:
        print(f"unlabelled numbers: {bad}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(results, indent=2) + "\n")
    _print_summary(results)
    print(f"\nwrote {args.out}")
    return 0


def _print_summary(r: dict) -> None:
    p, b = r["patchwork"], r["baseline_script"]
    v = lambda m: m["value"]  # noqa: E731
    print(f"\nPatchwork  BUILD {v(p['build']['wall_s']):.2f}s | run {v(p['run']['records'])} records "
          f"{v(p['run']['wall_s']):.2f}s ({v(p['run']['per_record_ms']):.1f} ms/record) llm_calls={v(p['run']['llm_calls'])} "
          f"accuracy={v(p['run']['accuracy']):.0%}")
    for k in ("break1", "break2"):
        pb, bb = p["breaks"][k], b["breaks"][k]
        print(f"  {pb['name']}")
        print(f"    Patchwork: {pb['incident_type']} detect {v(pb['time_to_detect_s']):.2f}s, bad writes "
              f"{v(pb['bad_writes'])}, held {v(pb['held'])}, repair {v(pb['repair_s']):.2f}s")
        print(f"    baseline:  bad writes {v(bb['bad_writes'])}, silently dropped {v(bb['silently_dropped'])}, "
              f"held {v(bb['held'])}; alert: {bb['alert']}")
    br = b["run"]
    print(f"Baseline   run {v(br['records'])} records {v(br['wall_s']):.2f}s: correct writes {v(br['correct_writes'])}, "
          f"wrong {v(br['wrong_writes'])}, dropped {v(br['silently_dropped'])}, held {v(br['held'])}, "
          f"accuracy {v(br['accuracy']):.0%}, exit {v(br['exit_code'])}")
    s = r["summary"]["caught_before_bad_write"]
    print(f"Breaks caught before any bad write: Patchwork {s['patchwork']}/2, baseline {s['baseline_script']}/2")


if __name__ == "__main__":
    sys.exit(main())

"""v2 benchmark: AI calls per posting over 60 real postings, from a library that knows only the taught platform.

    python -m bench.v2.run                  # or: make bench-v2   -> bench/v2/results.json (GET /api/bench/v2)
    python -m bench.v2.run --limit 10 --out /tmp/r.json
    python -m bench.v2.run --learn-calls 6 --learn-calls-kind "measured (earlier run)" \\
        --learn-calls-source "agents/README.md, test_live_learn 2026-11-15"

Runs its own stack in-process (fixture tracker form, control plane, scripted agent) on free ports, never the
live-demo ports, with its own PATCHWORK_WORKSPACE. No model calls. Steps:

  1. TEACH `jobs_to_tracker` from the `fixture-jobs` recording (one Lever posting -> the tracker form), approve v1.
  2. Post every posting in fixtures/postings, in a fixed shuffled order, through POST /api/pages exactly as the
     extension would (url, document.title, rendered DOM). Wait for each page's TrackerRow to be final; a learn thread
     that waits for a person is answered like one (the option equal to the answer key, then approve), and counted.
  3. Score what landed in the form (bench/v2/scoring.py) against fixtures/postings/answer_key.json.

AI calls: the scripted agent makes none. A posting that started a learn thread is charged the per-learn model calls
of the deep agent (`--learn-calls`, labelled with its own kind/source: `measured (earlier run)` from W3-7's live
runs, or by default `estimate` = the V10 design budget of 8). The browser-agent line is an `estimate` with its
assumption written next to it. Every number carries `kind` and `source`.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import shutil
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from bench.v2 import scoring
from bench.v2.client import (
    NAME,
    Control,
    Fail,
    load_key,
    load_posting,
    presenter_answer,
)
from bench.v2.scoring import metric

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "bench" / "v2" / "results.json"
RECORDING = "fixture-jobs"
SEED = 2026
SRC = "bench/v2/run.py (this run)"

# Inputs this harness does not measure, in one place with their reasoning.
LEARN_CALLS_BUDGET = 8
LEARN_CALLS_BUDGET_SOURCE = ("docs/contracts.md V10: LEARN budget, target <= 8 model calls on the deep agent "
                             "(a ceiling, not a measurement; replace with W3-7's measured runs via --learn-calls)")
BROWSER_AGENT_CALLS = 6
BROWSER_AGENT_ASSUMPTION = (
    "Estimate, not run: a DOM-reading browser agent makes at least one model call per posting (read the page and "
    "pick title/company), then opens the tracker form, fills 5 fields, submits and checks the confirmation. At one "
    "model call per action step with fields batched in pairs, that is ~6 calls per posting, every posting, forever: "
    "it does not learn. Even at the floor of 1 call per posting the line rises by 1 per posting.")
BROWSER_AGENT_SOURCE = "assumption (bench/v2/run.py BROWSER_AGENT_ASSUMPTION); no browser agent was run"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def order(key: list[dict], seed: int = SEED) -> list[dict]:
    out = list(key)
    random.Random(seed).shuffle(out)
    return out


# ---------------------------------------------------------------------------------------------
# measuring

def retarget_form(form_url: str) -> dict[str, Any] | None:
    """If the taught spec's form URL isn't this run's fixture form, compile the same spec with only sink.url
    changed, test it and deploy it (what a person does when the form moves; I-2 does the same for the demo Form).
    Returns what changed, or None if nothing had to."""
    from control import ops
    from runner.store import Store

    store = Store()
    v = store.active_version(NAME)
    vdir = ops.version_dir(NAME, v)
    spec = yaml.safe_load((vdir / "spec.yaml").read_text())
    old = (spec.get("sink") or {}).get("url")
    if spec.get("sink", {}).get("kind") != "form" or old == form_url:
        return None
    spec["sink"]["url"] = form_url
    tmp = Path(tempfile.mkdtemp(prefix="patchwork-retarget-"))
    (tmp / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    (tmp / "examples.yaml").write_text(yaml.safe_dump(json.loads((vdir / "golden.json").read_text()), sort_keys=False))
    if (vdir / "transforms.py").exists():
        shutil.copy(vdir / "transforms.py", tmp / "transforms.py")
    res = ops.compile_and_test(tmp / "spec.yaml")
    if not res["tests_passed"]:
        raise Fail(f"retargeted spec failed its tests: {res['tests_summary']}")
    ops.deploy(NAME, res["version"])
    return {"from": old, "to": form_url, "version": res["version"]}


def measure(workspace: Path, entries: list[dict], *, timeout: float) -> dict[str, Any]:
    from bench.v2.stack import V2Stack

    with V2Stack(workspace) as st:
        return measure_on(Control(st.control, timeout=120), st.form, entries, timeout=timeout, retarget=retarget_form)


def measure_on(c: Control, form_base: str, entries: list[dict], *, timeout: float,
               retarget: Any = None) -> dict[str, Any]:
    """The measurement itself, over HTTP only: a control plane `c` and the fixture form at `form_base`."""
    out: dict[str, Any] = {"postings": [], "questions": []}
    try:
        c.put("/api/settings", {"ai_enabled": True, "learn_policy": "auto"})
    except Fail as e:
        out["settings_warning"] = str(e)[:200]
    out["settings"] = c.get("/api/settings")

    t = time.monotonic()
    tid = c.post("/api/integrations/build", {"recording_id": RECORDING, "name": NAME})["thread_id"]
    out["teach"] = c.answer_until_deploy(tid, timeout, log=out["questions"])
    out["teach"]["seconds"] = time.monotonic() - t
    out["retarget"] = retarget(form_base + "/") if retarget else None
    out["layouts_before"] = c.layouts()

    def on_pending(tid: str, status: dict, entry: dict) -> None:
        p = status["pending"]
        if p["type"] == "ask_user":
            ans, how = presenter_answer(p.get("question", ""), p.get("options") or [], entry)
            out["questions"].append({"file": entry["file"], "question": p.get("question"), "answer": ans, "how": how})
            c.post(f"/api/agent/{tid}/resume", {"interrupt_id": p["interrupt_id"], "answer": ans}, ok=(200,))
        else:  # request_deploy: a person approves (instantly here) only if the tests passed
            decision = "approve" if (p.get("tests") or {}).get("passed", True) else "reject"
            out["questions"].append({"file": entry["file"], "question": "request_deploy", "answer": decision,
                                     "how": "approve if the generated tests passed"})
            c.post(f"/api/agent/{tid}/resume", {"interrupt_id": p["interrupt_id"], "decision": decision}, ok=(200,))

    for n, entry in enumerate(entries, 1):
        t = time.monotonic()
        asked: list[str] = []
        resp = c.post_page(load_posting(entry))

        def handler(tid: str, s: dict, entry: dict = entry, asked: list = asked) -> None:
            asked.append(s["pending"]["type"])
            on_pending(tid, s, entry)

        try:
            row = c.wait_row(resp["id"], timeout=timeout, on_pending=handler)
            err = None
        except Fail as e:
            row, err = c.page(resp["id"])["row"], str(e)[:300]
        secs = time.monotonic() - t
        out["postings"].append({"n": n, "entry": entry, "page_id": resp["id"],
                                "canonical_url": resp.get("canonical_url"), "row": row, "seconds": secs,
                                "asked": asked, "error": err})
        print(f"  {n:3d} {secs:6.2f}s {entry['site'][:26]:26s} {row.get('label') or row.get('status')!s:12s} "
              f"{(row.get('title') or '')[:50]}", flush=True)
    out["form_rows"] = httpx.get(form_base + "/admin/rows", timeout=10).json()
    out["tracker_totals"] = c.tracker(limit=500).get("totals")
    out["layouts_after"] = c.layouts()
    return out


# ---------------------------------------------------------------------------------------------
# results.json (V12)

def build_results(raw: dict[str, Any], *, learn_calls: int, learn_calls_kind: str, learn_calls_source: str,
                  browser_calls: int = BROWSER_AGENT_CALLS, seed: int = SEED,
                  generated_at: str | None = None) -> dict[str, Any]:
    rows = raw["form_rows"]
    postings: list[dict] = []
    scored = []
    for p in raw["postings"]:
        e, row = p["entry"], p["row"]
        held = row.get("label") == "held" or row.get("status") in ("held", "learning", "pending", "running")
        s = scoring.score_posting(e, p["canonical_url"] or e["url"], rows, held=held)
        scored.append(s)
        learn = row.get("learn_thread_id")
        thread_calls = row.get("ai_calls") or 0
        if not learn:
            calls, kind = 0, "measured"
        elif thread_calls > 0:
            calls, kind = thread_calls, "measured"
        else:
            calls, kind = learn_calls, learn_calls_kind
        postings.append({
            "n": p["n"], "file": e["file"], "platform": e["platform"], "site": e["site"],
            "label": row.get("label"), "status": row.get("status"), "reason": row.get("reason"),
            "parser": row.get("parser"), "version": row.get("version"), "routed_by": row.get("routed_by"),
            "learn_thread_id": learn, "thread_model_calls": thread_calls,
            "ai_calls": calls, "ai_calls_kind": kind,
            "asked_person": p["asked"], "seconds": round(p["seconds"], 3),
            "correct": s["correct"], "outcome": s["outcome"], "got": s["got"], "want": s["want"],
            "error": p.get("error"),
        })
    summ = scoring.summarize(scored)
    pw_series = scoring.cumulative(p["ai_calls"] for p in postings)
    ba_series = scoring.cumulative(scoring.browser_agent_calls(len(postings), browser_calls))
    learned = [p for p in postings if p["learn_thread_id"]]
    code = [p["seconds"] for p in postings if p["label"] == "code"]
    learn_s = [p["seconds"] for p in postings if p["label"] == "learned_now"]
    asked = [p for p in postings if p["asked_person"]]
    any_estimated = any(p["ai_calls_kind"] != "measured" for p in postings)
    pw_kind = learn_calls_kind if any_estimated else "measured"

    nums = [
        metric("postings", summ["postings"], "postings", "measured", SRC),
        metric("platforms", len({p["platform"] for p in postings}), "platforms", "measured", SRC),
        metric("accuracy", summ["accuracy"], "ratio", "measured", SRC,
               "right rows / postings, scored on the tracker form's rows vs fixtures/postings/answer_key.json; "
               "held postings count as not right"),
        metric("right_rows", summ["right_write"], "postings", "measured", SRC),
        metric("wrong_rows_written", summ["wrong_write"], "postings", "measured", SRC,
               "a row with another title or company than the answer key, or a second row for a posting"),
        metric("held_for_a_person", summ["held"], "postings", "measured", SRC, "nothing written"),
        metric("silently_dropped", summ["dropped"], "postings", "measured", SRC),
        metric("learn_threads", len(learned), "threads", "measured", SRC, "one per new layout (site)"),
        metric("learns_that_asked_a_person", len({p["learn_thread_id"] for p in asked}), "threads", "measured", SRC,
               "ask_user or request_deploy, answered instantly by the harness"),
        metric("ai_calls_per_known_layout_posting", 0, "calls", "measured", SRC,
               "postings labelled code; the runner cannot import a model SDK"),
        metric("ai_calls_per_learn", learn_calls, "calls", learn_calls_kind, learn_calls_source),
        metric("patchwork_ai_calls_total", pw_series[-1] if pw_series else 0, "calls", pw_kind,
               f"{SRC}: learn threads x ai_calls_per_learn ({learn_calls_kind}); 0 for every other posting"),
        metric("browser_agent_ai_calls_per_posting", browser_calls, "calls", "estimate", BROWSER_AGENT_SOURCE,
               BROWSER_AGENT_ASSUMPTION),
        metric("browser_agent_ai_calls_total", ba_series[-1] if ba_series else 0, "calls", "estimate",
               BROWSER_AGENT_SOURCE, f"{browser_calls} x {len(postings)} postings"),
        (metric("seconds_per_known_posting_median", round(statistics.median(code), 3), "s", "measured", SRC,
                "POST /api/pages until the row was final: run start (subprocess), extract, preflight, headless "
                "form fill + submit on the fixture form; the Google Form adds ~1.3-2 s (spike a)")
         if code else metric("seconds_per_known_posting_median", None, "s", "not measured", SRC, "no code rows")),
        (metric("seconds_per_learn_median", round(statistics.median(learn_s), 3), "s", "measured", SRC,
                f"{raw.get('agent', 'scripted')} agent, page posted until its row was written")
         if learn_s else metric("seconds_per_learn_median", None, "s", "not measured", SRC, "no learned_now rows")),
        metric("teach_seconds", round(raw["teach"]["seconds"], 3), "s", "measured", SRC,
               "scripted TEACH from fixture-jobs, deploy approved instantly"),
    ]
    layouts_before = raw.get("layouts_before") or []
    results = {
        "schema_version": 1,
        "generated_at": generated_at or _now(),
        "command": "python -m bench.v2.run  (make bench-v2)",
        "environment": {"python": platform.python_version(), "platform": platform.platform(terse=True),
                        "agent": f"{raw.get('agent', 'scripted')} (no model calls)", "settings": raw.get("settings")},
        "scenario": {
            "postings": len(postings), "order_seed": seed,
            "platforms": sorted({p["platform"] for p in postings}),
            "taught": RECORDING,
            "library_at_start": [{"parser_id": lay.get("parser_id"), "site": lay.get("site")} for lay in layouts_before],
            "retarget": raw.get("retarget"),
            "notes": [
                "Pages are real, public postings rendered once by scripts/fetch_postings.py and replayed from "
                "fixtures/postings; title/company are scored against answer_key.json (its `method`/`checks`/`notes`).",
                "The library starts with the one taught parser; every other layout must be learned (C8).",
                "The scripted agent makes no model calls. AI calls per learn come from --learn-calls with their own "
                "kind and source; the browser-agent line is an estimate (assumption in numbers).",
                "Questions and deploy approvals from learn threads are answered instantly by the harness like a "
                "person reading the page, and counted in learns_that_asked_a_person.",
            ],
        },
        "postings": postings,
        "series": {"patchwork_cumulative_ai_calls": pw_series, "browser_agent_cumulative_ai_calls": ba_series},
        "series_meta": {
            "patchwork_cumulative_ai_calls": {
                "kind": pw_kind, "source": f"{SRC}; per learn: {learn_calls_source}",
                "note": "0 for postings read by a known layout; a learn's calls at the posting that triggered it"},
            "browser_agent_cumulative_ai_calls": {
                "kind": "estimate", "source": BROWSER_AGENT_SOURCE, "assumption": BROWSER_AGENT_ASSUMPTION},
        },
        "numbers": nums,
        "questions": raw.get("questions", []),
        "summary": summ,
    }
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench.v2.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=RESULTS)
    ap.add_argument("--workspace", type=Path, default=None, help="default: a fresh temp dir, removed afterwards")
    ap.add_argument("--raw", type=Path, default=None, help="also write the raw measurements here")
    ap.add_argument("--limit", type=int, default=None, help="only the first N postings of the shuffled order")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--timeout", type=float, default=90, help="seconds per posting")
    ap.add_argument("--learn-calls", type=int, default=LEARN_CALLS_BUDGET)
    ap.add_argument("--learn-calls-kind", default="estimate", choices=scoring.KINDS[:-1])
    ap.add_argument("--learn-calls-source", default=LEARN_CALLS_BUDGET_SOURCE)
    args = ap.parse_args(argv)

    from bench.v2.stack import missing
    gaps = missing()
    if gaps:
        print("bench-v2 cannot run in this checkout; it needs the integrated stack:", file=sys.stderr)
        for g in gaps:
            print(f"  - {g}", file=sys.stderr)
        return 2

    entries = order(load_key(), args.seed)[: args.limit]
    ws = args.workspace or Path(tempfile.mkdtemp(prefix="patchwork-bench-v2-"))
    try:
        print(f"Patchwork v2 (scripted agent): TEACH {RECORDING}, then {len(entries)} postings ...", flush=True)
        raw = measure(ws / "patchwork", entries, timeout=args.timeout)
        raw["agent"] = "scripted"
    finally:
        if args.workspace is None:
            shutil.rmtree(ws, ignore_errors=True)
    if args.raw:
        args.raw.write_text(json.dumps(raw, indent=2, default=str))
    results = build_results(raw, learn_calls=args.learn_calls, learn_calls_kind=args.learn_calls_kind,
                            learn_calls_source=args.learn_calls_source, seed=args.seed)
    bad = scoring.check_results(results)
    if bad:
        print("results.json would be off-contract or unlabelled:", *bad, sep="\n  ", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    s = results["summary"]
    by = {m["name"]: m for m in results["numbers"]}
    print(f"\n{s['postings']} postings: {s['right_write']} right, {s['wrong_write']} wrong rows, {s['held']} held, "
          f"{s['dropped']} dropped; learns {by['learn_threads']['value']}; AI calls Patchwork "
          f"{by['patchwork_ai_calls_total']['value']} ({by['patchwork_ai_calls_total']['kind']}) vs browser agent "
          f"{by['browser_agent_ai_calls_total']['value']} (estimate)")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

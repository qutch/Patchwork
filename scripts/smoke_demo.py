"""Pre-demo smoke check: drive the 4-minute demo against a running stack (`make up`) over HTTP.

    make up                                  # terminal 1
    .venv/bin/python scripts/smoke_demo.py   # terminal 2 (or: make smoke)

It resets the demo, so don't run it while someone is presenting. Uses whichever agent the control
plane was started with (PATCHWORK_AGENT=scripted|deep); the deep agent spends ~55 model calls.
Exit code 0 = every step behaved; each step prints its wall time.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

NAME = "po_to_stockroom"
MAPPING = "acme.com=BOS, globex.com=NYC, initech.com=BOS, umbrella.com=NYC"


def presenter_answer(question: str, options: list[str]) -> str:
    """Answer like one consistent presenter: unknown messages are quarantined, warehouses go by supplier
    (acme/initech BOS, globex/umbrella NYC), and case quantities become units."""
    q, low = question.lower(), [o.lower() for o in options]

    def pick(*needles):
        return next((o for o, lo in zip(options, low) if any(n in lo for n in needles)), None)

    if "case" in q:
        return pick("units =", "× per case", "x per case", "yes") or "Yes: units = cases × units per case"
    if "warehouse" in q or "warehouse_id" in q:
        if "umbrella" in q:
            return pick("nyc") or "NYC"
        return (pick("acme.com=bos, globex.com=nyc") or pick("supplier", "sender_domain", "sender domain")
                or f"By supplier: {MAPPING}")
    if "not order" in q or "not purchase order" in q or "no sku" in q or "holiday" in q:
        return pick("quarantine") or "Quarantine them for review"
    return options[0] if options else "Use your best judgement and say what you chose."


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


class Demo:
    def __init__(self, base: str, agent_timeout: float):
        self.api = httpx.Client(base_url=base, timeout=60)
        self.agent_timeout = agent_timeout
        self.t0 = time.monotonic()

    def step(self, label: str, fn):
        t = time.monotonic()
        out = fn()
        print(f"  {time.monotonic() - t:6.1f}s  {label}" + (f": {out}" if out else ""), flush=True)

    def get(self, path, **kw):
        r = self.api.get(path, **kw)
        check(r.status_code == 200, f"GET {path} -> {r.status_code} {r.text[:200]}")
        return r.json()

    def post(self, path, body=None, ok=(200,)):
        r = self.api.post(path, json=body)
        check(r.status_code in ok, f"POST {path} -> {r.status_code} {r.text[:300]}")
        return r.json()

    # ---- helpers -------------------------------------------------------------------------
    def wait_thread(self, tid, want: set[str]):
        deadline = time.monotonic() + self.agent_timeout
        while time.monotonic() < deadline:
            s = self.get(f"/api/agent/{tid}")
            if s["state"] in want:
                return s
            if s["state"] == "failed":
                err = [e for e in self.get_events(tid) if e["type"] == "error"]
                raise Fail(f"agent thread {tid} failed: {err[-1]['message'] if err else '?'}")
            time.sleep(0.5)
        raise Fail(f"agent thread {tid} not {want} after {self.agent_timeout:.0f}s")

    def get_events(self, tid):
        # the stream closes once the thread is finished; for a live one, the status has last_seq
        r = self.api.get(f"/api/agent/{tid}/stream", timeout=5, headers={"Accept": "text/event-stream"})
        import json
        return [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]

    def answer_until_deploy(self, tid, after_version: int):
        """Answer ask_user interrupts until the agent asks to deploy; approve that. A failed compile
        still uses up a version number, so expect any version newer than `after_version`."""
        for _ in range(8):
            s = self.wait_thread(tid, {"waiting", "done"})
            check(s["state"] == "waiting", f"agent finished without asking to deploy: {s}")
            p = s["pending"]
            if p["type"] == "ask_user":
                ans = presenter_answer(p["question"], p.get("options") or [])
                print(f"           ask_user: {p['question'][:90]!r} -> {ans!r}")
                self.post(f"/api/agent/{tid}/resume", {"interrupt_id": p["interrupt_id"], "answer": ans})
                continue
            check(p["type"] == "request_deploy", f"unexpected interrupt {p['type']}")
            check(p["version"] > after_version, f"deploy request for v{p['version']}, expected a version after v{after_version}")
            check(p["tests"]["passed"], f"generated tests failed: {p['tests']}")
            self.post(f"/api/agent/{tid}/resume", {"interrupt_id": p["interrupt_id"], "decision": "approve"})
            self.wait_thread(tid, {"done"})
            return f"v{p['version']} approved ({p['tests'].get('summary', 'tests passed')})"
        raise Fail("too many questions from the agent")

    def run(self, body=None):
        rid = self.post("/api/runs", {"integration": NAME, **(body or {})})["run_id"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            run = self.get(f"/api/runs/{rid}")["run"]
            if run["status"] != "running":
                return run
            time.sleep(0.3)
        raise Fail(f"run {rid} still running after 300s")

    def active(self) -> int:
        return self.get(f"/api/integrations/{NAME}")["active_version"] or 0

    def stockroom_rows(self):
        return len(httpx.get(f"{self.stock}/api/incoming", timeout=10).json())

    # ---- the demo ------------------------------------------------------------------------
    def go(self, ai_off_check: bool):
        health = self.get("/api/health")
        settings = self.get("/api/settings")
        print(f"control plane ok: agent={settings['agent']} model={settings['model']}")
        self.stock = os.environ.get("PATCHWORK_STOCKROOM_URL", "http://localhost:8002")
        check(health["status"] == "ok", health)

        self.step("reset demo", lambda: ",".join(f"{k}={v.get('status')}" for k, v in
                                                 self.post("/api/demo/reset")["apps"].items()))
        self.api.put("/api/settings", json={"ai_enabled": True}).raise_for_status()

        state = {}

        def build():
            state["tid"] = self.post("/api/integrations/build", {"recording_id": "fixture"})["thread_id"]
            return self.answer_until_deploy(state["tid"], 0)
        self.step("BUILD from the fixture recording", build)

        if ai_off_check:
            def ai_off():
                self.api.put("/api/settings", json={"ai_enabled": False}).raise_for_status()
                return None
            self.step("AI off (runs must not need it)", ai_off)

        def run_all():
            run = self.run()
            c = run["counts"]
            check(run["status"] == "completed" and run["llm_calls"] == 0, run)
            check(sum(c.get(k, 0) for k in ("success", "duplicate", "quarantined")) == 150, c)
            acc = self.get("/api/bench/accuracy", params={"integration": NAME})
            check(acc["accuracy"] == 1.0, acc.get("mismatches"))
            return f"{c} llm_calls=0 accuracy={acc['accuracy']:.0%} ({acc['records']} records)"
        self.step("run 150 POs on v1", run_all)

        def break1():
            self.post("/api/demo/stockroom/schema", {"version": 2})
            self.post("/api/demo/inbox/deliver", {"count": 5})
            before = self.stockroom_rows()
            run = self.run()
            check(run["status"] == "blocked", run)
            check(self.stockroom_rows() == before, "Break 1 wrote to the Stockroom")
            inc = self.get(f"/api/incidents/{run['incident_id']}")
            check(inc["type"] == "CONTRACT_CHANGED", inc)
            state["inc1"] = inc["id"]
            return f"blocked before any write, incident #{inc['id']} {inc['type']}"
        self.step("Break 1: Stockroom contract v2", break1)

        def repair1():
            self.api.put("/api/settings", json={"ai_enabled": True}).raise_for_status()
            state["v1"] = self.active()
            tid = self.post(f"/api/incidents/{state['inc1']}/repair")["thread_id"]
            return self.answer_until_deploy(tid, state["v1"])
        self.step("REPAIR contract change", repair1)

        def after_repair():
            check(self.active() > state["v1"], "the repaired version is not active")
            run = self.run()
            check(run["status"] == "completed", run)
            acc = self.get("/api/bench/accuracy", params={"integration": NAME})
            check(acc["accuracy"] == 1.0, acc.get("mismatches"))
            return f"{run['counts']} accuracy={acc['accuracy']:.0%} ({acc['records']} records)"
        self.step("run on v2 (new deliveries)", after_repair)

        def break2():
            self.post("/api/demo/inbox/new_supplier", {"count": 4})
            run = self.run()
            q = [r for r in self.get(f"/api/runs/{run['id']}")["records"] if r["status"] == "quarantined"]
            check(q and all(r.get("reason") == "PARSE_NO_MATCH" for r in q), q[:3])
            open_inc = self.get("/api/incidents", params={"status": "open"})
            new = [i for i in open_inc if i["type"] == "NEW_SOURCE_FORMAT"]
            check(new, f"no NEW_SOURCE_FORMAT incident: {open_inc}")
            state["inc2"] = new[0]["id"]
            return f"{len(q)} quarantined PARSE_NO_MATCH, incident #{new[0]['id']} NEW_SOURCE_FORMAT"
        self.step("Break 2: new supplier format", break2)

        def repair2():
            tid = self.post(f"/api/incidents/{state['inc2']}/repair")["thread_id"]
            out = self.answer_until_deploy(tid, self.active())
            acc = self.get("/api/bench/accuracy", params={"integration": NAME})
            check(acc["accuracy"] == 1.0, acc.get("mismatches"))
            return f"{out}; accuracy={acc['accuracy']:.0%} ({acc['records']} records)"
        self.step("REPAIR new supplier format", repair2)

        print(f"\nOK: whole demo in {time.monotonic() - self.t0:.0f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("PATCHWORK_API", "http://127.0.0.1:8000"))
    ap.add_argument("--agent-timeout", type=float, default=600, help="seconds per agent wait (deep agent is slow)")
    ap.add_argument("--no-ai-off", action="store_true", help="skip switching AI off before the runs")
    args = ap.parse_args()
    try:
        Demo(args.api, args.agent_timeout).go(ai_off_check=not args.no_ai_off)
    except Fail as e:
        print(f"\nFAIL: {e}", file=sys.stderr)
        return 1
    except httpx.ConnectError:
        print(f"\nFAIL: no control plane at {args.api} (start it with `make up`)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

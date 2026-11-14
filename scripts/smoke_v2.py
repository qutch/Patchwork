"""Pre-demo smoke check for v2: the 2-minute script (docs/demo-v2.md) driven over HTTP, every beat asserted.

    python scripts/smoke_v2.py --in-process                  # own stack: fixture form + control plane + scripted
                                                             # agent on free ports (what the offline test runs)
    python scripts/smoke_v2.py                               # against `make up` (:8000) and the fixture form at
                                                             # $PATCHWORK_TRACKER_FORM_URL (renamed via /api/demo/form/rename)
    PATCHWORK_FORM=google python scripts/smoke_v2.py         # against `make up` and the demo Google Form + Sheet:
                                                             # waits for you to rename the question by hand, checks
                                                             # the Sheet's CSV row delta (submits ~6 real rows)

Beats: v2 reset to baseline -> 3 known postings (code, 0 AI calls) -> a new platform (learned now) -> a second
posting of it (code, 0 AI calls) -> rename "Company" -> "Employer" -> next posting held before any write ->
repair -> approve -> the held posting is written -> rows confirmed in the form. Prints a timing per beat and exits
0 iff every beat passed. It resets the v2 demo state, so never run it while someone is presenting.

Pages are posted from fixtures/postings exactly as the extension would send them (URL, document.title, rendered DOM).
With --in-process (or --bootstrap) it first builds the baseline the stage needs: TEACH from `fixture-jobs` (Lever),
learn Greenhouse and Ashby from one posting each, save it with /api/demo/v2/save-baseline.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bench.v2.client import NAME, Control, Fail, check, load_key, load_posting

# Which stored postings play which part (fixtures/postings/answer_key.json).
BOOTSTRAP = ["greenhouse/1.html.gz", "ashby/1.html.gz"]          # learned "off camera" (Discord, Ramp)
KNOWN = ["lever/2.html.gz", "greenhouse/5.html.gz", "ashby/5.html.gz"]   # Palantir, Anthropic, OpenAI
NEW = ["greenhouse_branded/1.html.gz", "greenhouse_branded/2.html.gz"]   # careers.airbnb.com
BREAK = "lever/7.html.gz"                                        # Spotify, sent after the rename
KNOWN_SITES = {"jobs.lever.co", "job-boards.greenhouse.io", "jobs.ashbyhq.com"}
NEW_SITE = "careers.airbnb.com"
LEARN_BUDGET = 8  # V10 target for the deep agent


def _norm(s: Any) -> str:
    return " ".join(str(s or "").split()).casefold()


class FixtureForm:
    """The fixture tracker form (V8): rows from /admin/rows, rename through the control plane's demo proxy."""
    kind = "fixture"

    def __init__(self, url: str, control: Control):
        self.url, self.c = url.rstrip("/"), control

    def reset(self) -> None:
        httpx.post(f"{self.url}/admin/reset", timeout=10).raise_for_status()

    def rows(self) -> list[dict]:
        return httpx.get(f"{self.url}/admin/rows", timeout=10).json()

    def rename(self, old: str, new: str) -> None:
        self.c.post("/api/demo/form/rename", {"from": old, "to": new}, ok=(200,))

    def company(self, row: dict) -> Any:
        return row.get("Employer", row.get("Company"))

    def link(self, row: dict) -> Any:
        return row.get("Link")


class GoogleForm:
    """The demo Google Form + Sheet (V0). A person renames the question; rows come from the Sheet's CSV export."""
    kind = "google"

    def __init__(self, csv_url: str):
        self.csv_url = csv_url

    def reset(self) -> None:
        input("  Reset by hand: in the Form editor the question must read 'Company' (not 'Employer'); delete the "
              "rehearsal rows from the Sheet if you want a clean screen. Press Enter when done ... ")

    def rows(self) -> list[dict]:
        r = httpx.get(self.csv_url, timeout=20, follow_redirects=True)
        r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))

    def rename(self, old: str, new: str) -> None:
        input(f"  In the Google Form editor, rename the question '{old}' to '{new}' and click away (it autosaves). "
              "Press Enter when done ... ")

    def company(self, row: dict) -> Any:
        return row.get("Employer") or row.get("Company")

    def link(self, row: dict) -> Any:
        return row.get("Link")


class Smoke:
    def __init__(self, c: Control, form: FixtureForm | GoogleForm, *, timeout: float, agent: str):
        self.c, self.form, self.timeout, self.agent = c, form, timeout, agent
        self.key = {e["file"]: e for e in load_key()}
        self.timings: list[tuple[str, float, bool, str]] = []
        self.last: dict = {}
        self.asked: list = []

    def beat(self, label: str, fn: Callable[[], str | None]) -> None:
        t = time.monotonic()
        try:
            out = fn() or ""
        except (Fail, httpx.HTTPError, KeyError, AssertionError) as e:
            dt = time.monotonic() - t
            self.timings.append((label, dt, False, str(e)))
            print(f"  {dt:6.1f}s  FAIL {label}: {e}", flush=True)
            raise
        dt = time.monotonic() - t
        self.timings.append((label, dt, True, out))
        print(f"  {dt:6.1f}s  ok   {label}" + (f": {out}" if out else ""), flush=True)

    # -- helpers ------------------------------------------------------------------------------
    def send(self, file: str, *, answer_learn: bool = False) -> tuple[dict, dict]:
        """Post a stored page like the extension; wait for its row. A learn that asks a person is answered like
        the presenter would (the option equal to the answer key, then approve) only if `answer_learn`; otherwise
        the beat fails on timeout, because the stage expects D2 auto-use there."""
        entry = self.key[file]
        resp = self.c.post_page(load_posting(entry))
        check(resp.get("integration") == NAME, f"{file}: page not routed to {NAME}: {resp}")
        self.last = resp
        self.asked = []

        def on_pending(tid: str, s: dict) -> None:
            self.asked.append((s.get("pending") or {}).get("type"))
            if answer_learn:
                self.c.answer_until_deploy(tid, self.timeout, entry=entry)

        row = self.c.wait_row(resp["id"], timeout=self.timeout, on_pending=on_pending)
        return entry, row

    def expect_written(self, entry: dict, row: dict) -> None:
        check(row["status"] == "success", f"{entry['file']}: status {row['status']} ({row.get('reason')}: {row.get('detail')})")
        check(_norm(row.get("title")) == _norm(entry["title"]), f"{entry['file']}: title {row.get('title')!r} != {entry['title']!r}")
        check(_norm(row.get("company")) == _norm(entry["company"]),
              f"{entry['file']}: company {row.get('company')!r} != {entry['company']!r}")

    def expect_code(self, entry: dict, row: dict) -> str:
        self.expect_written(entry, row)
        check(row.get("label") == "code", f"{entry['file']}: label {row.get('label')!r}, want 'code'")
        check((row.get("ai_calls") or 0) == 0, f"{entry['file']}: {row.get('ai_calls')} AI calls, want 0")
        return f"{entry['site']} code, 0 AI calls, parser {row.get('parser')} v{row.get('version')} ({row.get('routed_by')})"

    # -- bootstrap (what I-2 does off camera) --------------------------------------------------
    def bootstrap(self, retarget: Callable[[], Any] | None = None) -> None:
        def teach() -> str:
            tid = self.c.post("/api/integrations/build", {"recording_id": "fixture-jobs", "name": NAME})["thread_id"]
            d = self.c.answer_until_deploy(tid, self.timeout)
            moved = retarget() if retarget else None
            return f"v{d.get('version')} from fixture-jobs" + (f"; form URL retargeted to {moved['to']}" if moved else "")

        self.beat("bootstrap: TEACH jobs_to_tracker", teach)
        for f in BOOTSTRAP:
            def learn(f: str = f) -> str:
                entry, row = self.send(f, answer_learn=True)
                self.expect_written(entry, row)
                return f"{entry['site']} {row.get('label')}"
            self.beat(f"bootstrap: learn {self.key[f]['site']}", learn)

        def save() -> str:
            self.c.post("/api/demo/v2/save-baseline", {}, ok=(200,))
            return ", ".join(sorted({lay.get("site") or "?" for lay in self.c.layouts()}))
        self.beat("bootstrap: save baseline", save)

    # -- the 2-minute script ---------------------------------------------------------------------
    def run(self) -> None:
        state: dict[str, Any] = {}

        def reset() -> str:
            self.c.post("/api/demo/v2/reset", {}, ok=(200,))
            self.form.reset()
            tr = self.c.tracker()
            check(not tr["rows"], f"tracker not empty after reset: {len(tr['rows'])} rows")
            check(not (tr.get("totals") or {}).get("ai_calls"), f"AI-calls counter not 0: {tr.get('totals')}")
            sites = {lay.get("site") for lay in self.c.layouts() if lay.get("active", True)}
            check(KNOWN_SITES <= sites, f"baseline layouts {sites} lack {KNOWN_SITES - sites}")
            check(NEW_SITE not in sites, f"{NEW_SITE} already learned in the baseline")
            open_cc = [i for i in self.c.get("/api/incidents", params={"status": "open"})
                       if i.get("integration") in (None, NAME) and i.get("type") == "CONTRACT_CHANGED"]
            check(not open_cc, f"open CONTRACT_CHANGED incidents after reset: {[i.get('id') for i in open_cc]}")
            state["rows0"] = len(self.form.rows())
            return f"layouts {sorted(sites)}; agent {self.agent}; form rows {state['rows0']}"

        self.beat("1 v2 reset to baseline", reset)

        def known() -> str:
            out = []
            for f in KNOWN:
                out.append(self.expect_code(*self.send(f)))
            n = len(self.form.rows()) - state["rows0"]
            check(n == 3, f"{n} new form rows after 3 known postings, want 3")
            return "; ".join(out)

        self.beat("2 three known postings", known)

        def new_platform() -> str:
            entry, row = self.send(NEW[0], answer_learn=True)
            self.expect_written(entry, row)
            check(row.get("label") == "learned_now", f"label {row.get('label')!r}, want 'learned_now'")
            check(row.get("learn_thread_id"), "no learn thread on the learned row")
            calls = row.get("ai_calls") or 0
            if self.agent == "scripted":
                check(calls == 0, f"scripted agent reported {calls} AI calls")
            else:
                check(calls >= 1, "deep agent learn reported 0 AI calls")
                if calls > LEARN_BUDGET:
                    print(f"           warning: learn took {calls} model calls (> V10 target {LEARN_BUDGET})")
            o = row.get("parser_origin") or {}
            asked = f"; asked a person: {self.asked}" if self.asked else ""
            return (f"{entry['site']} learned_now, {calls} AI calls ({self.agent}), parser {row.get('parser')} "
                    f"approved_by {o.get('approved_by')}{asked}")

        self.beat("3 new platform: learned now", new_platform)
        self.beat("4 second posting of the new platform", lambda: self.expect_code(*self.send(NEW[1])))

        def rename() -> str:
            state["rows1"] = len(self.form.rows())
            self.form.rename("Company", "Employer")
            return "Company -> Employer"

        self.beat("5 rename the form question", rename)

        def held() -> str:
            entry, row = self.send(BREAK)
            check(row["status"] == "held", f"status {row['status']} ({row.get('reason')}), want held before any write")
            check(row.get("reason") == "CONTRACT_CHANGED" or row.get("incident_id"),
                  f"held without a CONTRACT_CHANGED incident: {row}")
            n = len(self.form.rows()) - state["rows1"]
            check(n == 0, f"{n} rows written after the rename; want 0 (blocked before any write)")
            inc_id = row.get("incident_id")
            if not inc_id:
                incs = [i for i in self.c.get("/api/incidents", params={"status": "open"})
                        if i.get("type") == "CONTRACT_CHANGED"]
                check(incs, "no open CONTRACT_CHANGED incident")
                inc_id = incs[0]["id"]
            inc = self.c.get(f"/api/incidents/{inc_id}")
            errs = (inc.get("detail") or {}).get("contract_errors") or inc.get("contract_errors") or []
            check(any("Company" in e for e in errs), f"incident {inc_id} does not name Company: {errs}")
            state.update(page=row["page_id"], incident=inc_id, entry=entry, link=self.last.get("canonical_url"))
            return f"held, incident #{inc_id}: {errs[0] if errs else inc.get('type')}"

        self.beat("6 next posting held before any write", held)

        def repair() -> str:
            tid = self.c.post(f"/api/incidents/{state['incident']}/repair", {}, ok=(200,))["thread_id"]
            d = self.c.answer_until_deploy(tid, self.timeout)
            return f"repaired to v{d.get('version')} (approved)"

        self.beat("7 repair + approve", repair)

        def written() -> str:
            row = self.c.wait_row(state["page"], timeout=self.timeout, until={"success", "failed", "duplicate"})
            self.expect_written(state["entry"], row)
            rows = self.form.rows()
            check(len(rows) - state["rows1"] == 1, f"{len(rows) - state['rows1']} rows after the repair, want 1")
            last = [r for r in rows if _norm(self.form.link(r)) == _norm(state["link"])]
            check(last, "the held posting's row is not in the form")
            check(_norm(self.form.company(last[-1])) == _norm(state["entry"]["company"]),
                  f"company landed as {self.form.company(last[-1])!r}")
            return f"row written, Employer = {self.form.company(last[-1])!r}"

        self.beat("8 held posting written", written)

        def confirm() -> str:
            n = len(self.form.rows()) - state["rows0"]
            check(n == 6, f"{n} rows in the {self.form.kind} form for this run, want 6 (3 known + 2 new + 1 held)")
            totals = self.c.tracker().get("totals") or {}
            return f"{n} rows; tracker totals {totals}"

        self.beat("9 rows confirmed", confirm)


def _agent_kind(c: Control) -> str:
    s = c.get("/api/settings")
    return "scripted" if (s.get("agent") or "scripted") == "scripted" else "deep"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="smoke_v2", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--control", default=os.environ.get("PATCHWORK_CONTROL", "http://127.0.0.1:8000"))
    ap.add_argument("--form", choices=("fixture", "google"), default=os.environ.get("PATCHWORK_FORM", "fixture"))
    ap.add_argument("--form-url", default=os.environ.get("PATCHWORK_TRACKER_FORM_URL", "http://127.0.0.1:18010/"))
    ap.add_argument("--sheet-csv", default=os.environ.get("PATCHWORK_DEMO_SHEET_CSV"))
    ap.add_argument("--in-process", action="store_true", help="own stack on free ports, scripted agent, fixture form")
    ap.add_argument("--bootstrap", action="store_true", help="teach + learn the baseline first (implied by --in-process)")
    ap.add_argument("--workspace", type=Path, default=None)
    ap.add_argument("--timeout", type=float, default=float(os.environ.get("PATCHWORK_SMOKE_TIMEOUT", "120")))
    args = ap.parse_args(argv)

    t0 = time.monotonic()
    try:
        if args.in_process:
            return _in_process(args)
        c = Control(args.control)
        c.get("/api/health")
        form = GoogleForm(args.sheet_csv) if args.form == "google" else FixtureForm(args.form_url, c)
        if args.form == "google" and not args.sheet_csv:
            print("PATCHWORK_FORM=google needs PATCHWORK_DEMO_SHEET_CSV (docs/contracts.md V0)", file=sys.stderr)
            return 2
        s = Smoke(c, form, timeout=args.timeout, agent=_agent_kind(c))
        print(f"smoke-v2 against {args.control}, {form.kind} form, {s.agent} agent")
        if args.bootstrap:
            s.bootstrap()
        s.run()
        if args.form == "google":
            print("  reminder: rename 'Employer' back to 'Company' in the Form editor (docs/demo-v2.md §4)")
    except (Fail, httpx.HTTPError, KeyError, AssertionError) as e:
        print(f"\nsmoke-v2 FAILED after {time.monotonic() - t0:.1f}s: {e}", file=sys.stderr)
        return 1
    print(f"\nsmoke-v2 OK in {time.monotonic() - t0:.1f}s")
    return 0


def _in_process(args: argparse.Namespace) -> int:
    import shutil
    import tempfile

    from bench.v2.run import retarget_form
    from bench.v2.stack import V2Stack, missing

    gaps = missing()
    if gaps:
        print("smoke-v2 --in-process needs the integrated stack:", *gaps, sep="\n  - ", file=sys.stderr)
        return 2
    ws = args.workspace or Path(tempfile.mkdtemp(prefix="patchwork-smoke-v2-"))
    try:
        with V2Stack(ws) as st:
            c = Control(st.control, timeout=120)
            s = Smoke(c, FixtureForm(st.form, c), timeout=args.timeout, agent="scripted")
            print(f"smoke-v2 in-process: control {st.control}, fixture form {st.form}, scripted agent")
            s.bootstrap(retarget=lambda: retarget_form(st.form + "/"))
            s.run()
    finally:
        if args.workspace is None:
            shutil.rmtree(ws, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

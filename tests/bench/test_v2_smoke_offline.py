"""`make smoke-v2` offline.

test_smoke_v2_in_process is the real check (W5-3): the whole 2-minute script against an in-process stack with the
fixture tracker form, the control plane and the scripted agent. It skips, saying what is missing, until W1/W3/W4a
are merged; it is never faked.

The other tests exercise the smoke script's own logic (beats, waiting, failure reporting) against a tiny fake V9
server, so mistakes in the harness show up before integration. They say nothing about Patchwork itself.
"""

import importlib.util
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request

from bench.stack import Server
from bench.v2.client import Control, Fail, load_key
from bench.v2.stack import missing

REPO = Path(__file__).resolve().parents[2]


def _smoke():
    spec = importlib.util.spec_from_file_location("smoke_v2", REPO / "scripts" / "smoke_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_smoke_v2_in_process(tmp_path):
    gaps = missing()
    if gaps:
        pytest.skip("needs the integrated stack: " + "; ".join(gaps))
    assert _smoke().main(["--in-process", "--workspace", str(tmp_path), "--timeout", "120"]) == 0


# ---------------------------------------------------------------------------------------------
# a fake V9 control plane + fixture form, only to test the smoke script's own logic

KEY = {e["url"]: e for e in load_key()}


def fake_stack(*, break_writes: bool = False):
    form = FastAPI()
    st = {"rows": [], "labels": ["Email", "Job Title", "Company", "Link", "Date Applied"], "pages": [],
          "known": {"jobs.lever.co", "job-boards.greenhouse.io", "jobs.ashbyhq.com"}, "baseline": None,
          "incidents": [], "threads": {}, "blocked": False}
    lock = threading.Lock()

    @form.get("/admin/rows")
    def rows():
        return st["rows"]

    @form.post("/admin/reset")
    def reset_form():
        st["rows"].clear()
        st["labels"] = ["Email", "Job Title", "Company", "Link", "Date Applied"]
        return {"ok": True}

    def write(page):
        e = KEY[page["url"]]
        company_label = "Company" if "Company" in st["labels"] else "Employer"
        st["rows"].append({"Email": "demo@example.com", "Job Title": e["title"], company_label: e["company"],
                           "Link": page["canonical_url"], "Date Applied": "2026-11-14"})

    def process(page):
        e = KEY[page["url"]]
        if "Company" not in st["labels"] and st["blocked"] is False and not st.get("repaired"):
            st["blocked"] = True
            st["incidents"].append({"id": 1, "type": "CONTRACT_CHANGED", "integration": "jobs_to_tracker",
                                    "status": "open", "detail": {"contract_errors": [
                                        "body.Company: not accepted by destination (no such property)"]}})
        if st["blocked"]:
            page["row"].update(status="held", label="held", reason="CONTRACT_CHANGED", incident_id=1)
            return
        if page["site"] not in st["known"]:
            tid = f"learn-{page['seq']}"
            st["threads"][tid] = {"thread_id": tid, "state": "done", "pending": None}
            st["known"].add(page["site"])
            page["row"].update(learn_thread_id=tid, label="learned_now")
        else:
            page["row"]["label"] = "code"
        if not break_writes:
            write(page)
        page["row"].update(status="success", title=e["title"], company=e["company"], parser=page["site"],
                           version=1, routed_by="match", ai_calls=0)

    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/settings")
    def settings():
        return {"agent": "scripted", "ai_enabled": True, "learn_policy": "auto"}

    @app.put("/api/settings")
    def put_settings():
        return settings()

    @app.post("/api/integrations/build")
    def build():
        st["threads"]["build-1"] = {"thread_id": "build-1", "state": "done", "pending": None}
        st["known"] = {"jobs.lever.co"}
        return {"thread_id": "build-1"}

    @app.get("/api/extension")
    def ext():
        return {"token": "tok", "control_url": ""}

    @app.post("/api/pages", status_code=201)
    async def post_page(request: Request):
        if request.headers.get("X-Patchwork-Token") != "tok":
            raise HTTPException(401)
        body = await request.json()
        with lock:
            seq = len(st["pages"]) + 1
            canon = body["url"].split("?")[0].rstrip("/")
            page = {"id": f"pg-{seq:06d}", "seq": seq, "url": body["url"], "canonical_url": canon,
                    "site": KEY[body["url"]]["site"],
                    "row": {"page_id": f"pg-{seq:06d}", "seq": seq, "site": KEY[body["url"]]["site"],
                            "url": body["url"], "status": "running", "label": None, "ai_calls": 0,
                            "learn_thread_id": None, "incident_id": None, "reason": None}}
            st["pages"].append(page)
        threading.Timer(0.05, lambda: process(page)).start()
        return {"id": page["id"], "seq": seq, "site": page["site"], "canonical_url": canon,
                "integration": "jobs_to_tracker", "run_id": seq, "queued": False}

    @app.get("/api/pages/{pid}")
    def get_page(pid: str):
        p = next(p for p in st["pages"] if p["id"] == pid)
        return {"page": {k: v for k, v in p.items() if k != "row"}, "row": p["row"]}

    @app.get("/api/tracker")
    def tracker():
        return {"rows": [p["row"] for p in reversed(st["pages"])], "totals": {"pages": len(st["pages"]), "ai_calls": 0}}

    @app.get("/api/layouts")
    def layouts():
        return [{"parser_id": s, "site": s, "active": True} for s in sorted(st["known"])]

    @app.post("/api/demo/v2/reset")
    def reset():
        st["pages"].clear()
        st["incidents"].clear()
        st["blocked"] = st["repaired"] = False
        st["known"] = {"jobs.lever.co", "job-boards.greenhouse.io", "jobs.ashbyhq.com"}
        return {"ok": True}

    @app.post("/api/demo/form/rename")
    async def rename(request: Request):
        b = await request.json()
        st["labels"] = [b["to"] if x == b["from"] else x for x in st["labels"]]
        return {"ok": True}

    @app.get("/api/incidents")
    def incidents(status: str | None = None):
        return [i for i in st["incidents"] if status is None or i["status"] == status]

    @app.get("/api/incidents/{iid}")
    def incident(iid: int):
        return next(i for i in st["incidents"] if i["id"] == iid)

    @app.post("/api/incidents/{iid}/repair")
    def repair(iid: int):
        st["threads"]["repair-1"] = {"thread_id": "repair-1", "state": "waiting", "pending": {
            "type": "request_deploy", "interrupt_id": "i1", "version": 2, "tests": {"passed": True}}}
        return {"thread_id": "repair-1"}

    @app.get("/api/agent/{tid}")
    def thread(tid: str):
        return st["threads"][tid]

    @app.post("/api/agent/{tid}/resume")
    async def resume(tid: str, request: Request):
        t = st["threads"][tid]
        t.update(state="done", pending=None)
        st["blocked"], st["repaired"] = False, True
        st["incidents"][0]["status"] = "resolved"
        for p in st["pages"]:
            if p["row"]["status"] == "held":
                process(p)
        return t

    return app, form


@pytest.fixture()
def fake():
    def start(**kw):
        app, form = fake_stack(**kw)
        srv_a, srv_f = Server(app).__enter__(), Server(form).__enter__()
        servers.extend([srv_a, srv_f])
        return srv_a.url, srv_f.url
    servers = []
    yield start
    for s in servers:
        s.__exit__(None, None, None)


def test_smoke_beats_pass_on_a_well_behaved_server(fake):
    smoke = _smoke()
    control, form = fake()
    c = Control(control)
    s = smoke.Smoke(c, smoke.FixtureForm(form, c), timeout=10, agent="scripted")
    s.run()
    assert [ok for _, _, ok, _ in s.timings] == [True] * 9
    assert len(s.timings) == 9


def test_smoke_fails_when_nothing_lands_in_the_form(fake):
    smoke = _smoke()
    control, form = fake(break_writes=True)
    c = Control(control)
    s = smoke.Smoke(c, smoke.FixtureForm(form, c), timeout=10, agent="scripted")
    with pytest.raises(Fail, match="new form rows"):
        s.run()
    assert s.timings[-1][2] is False and s.timings[-1][0] == "2 three known postings"


def test_wait_row_final_held_and_timeout(fake):
    from bench.v2.client import load_posting
    control, _ = fake()
    c = Control(control)
    entries = load_key()
    resp = c.post_page(load_posting(entries[0]))
    with pytest.raises(Fail, match="not final"):
        c.wait_row(resp["id"], timeout=0.01)  # the fake processes pages after 50 ms
    assert c.wait_row(resp["id"], timeout=5)["status"] == "success"
    c.post("/api/demo/form/rename", {"from": "Company", "to": "Employer"})
    resp = c.post_page(load_posting(entries[1]))
    t = time.monotonic()
    row = c.wait_row(resp["id"], timeout=5, settle_s=0.3)  # held, no learn thread: final once it settles
    assert row["status"] == "held" and row["reason"] == "CONTRACT_CHANGED"
    assert 0.3 <= time.monotonic() - t < 5


def test_bench_measure_on_fake_server(fake):
    """bench/v2/run.py's loop and results on the fake: every platform but the taught one is learned once."""
    from bench.v2 import scoring
    from bench.v2.run import build_results, measure_on, order

    control, form = fake()
    entries = order(load_key())[:15]
    raw = measure_on(Control(control), form, entries, timeout=10)
    res = build_results(raw, learn_calls=8, learn_calls_kind="estimate", learn_calls_source="test budget")
    assert scoring.check_results(res) == []
    learned_sites = {p["site"] for p in res["postings"] if p["label"] == "learned_now"}
    assert learned_sites == {e["site"] for e in entries} - {"jobs.lever.co"}
    assert res["series"]["patchwork_cumulative_ai_calls"][-1] == 8 * len(learned_sites)
    assert res["summary"]["right_write"] == 15 and res["summary"]["wrong_write"] == 0

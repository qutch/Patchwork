"""W4a-4: learn_policy, the build name, the fixture-jobs recording, v2 demo controls and bench v2."""

import json
import threading
import time

from fastapi import FastAPI

from control.api import recordings
from control.api.settings import Settings
from control.pages import PageInbox
from runner.pipeline import Outcome
from runner.store import Store
from tests.conftest import _Server, _free_port
from tests.control.v2_fakes import (LEVER, NAME, fake_runner, make_page_integration, make_v1_integration,  # noqa: F401
                                    post_page, v2agent, v2api, wait_idle)


def test_learn_policy_roundtrip(v2api, workspace):
    s = v2api.get("/api/settings").json()
    assert s["learn_policy"] == "auto" and s["ai_enabled"] is True
    s = v2api.put("/api/settings", json={"learn_policy": "approve"}).json()
    assert s["learn_policy"] == "approve" and s["ai_enabled"] is True  # the other setting is untouched
    assert json.loads((workspace / "settings.json").read_text())["learn_policy"] == "approve"
    assert Settings().learn_policy == "approve"  # what the agents' tools read
    assert v2api.put("/api/settings", json={"learn_policy": "sometimes"}).status_code == 422
    assert v2api.put("/api/settings", json={}).status_code == 422
    s = v2api.put("/api/settings", json={"ai_enabled": False}).json()
    assert s["ai_enabled"] is False and s["learn_policy"] == "approve"


def _jobs_fixture(tmp_path):
    d = tmp_path / "recording_jobs"
    d.mkdir()
    (d / "recording.json").write_text(json.dumps({"id": "fixture-jobs", "intent": "Track job postings",
                                                  "allowlist": ["jobs.lever.co", "127.0.0.1"], "events": [{"type": "copy"}]}))
    (d / "candidates.json").write_text(json.dumps({"recording_id": "fixture-jobs", "source": {"kind": "page"},
                                                   "sink": {"kind": "form", "labels": ["Job Title"]}, "mappings": []}))
    return d


def test_fixture_jobs_recording_listed(v2api, tmp_path, monkeypatch):
    monkeypatch.setattr(recordings, "FIXTURE_JOBS_DIR", tmp_path / "missing")
    assert "fixture-jobs" not in [r["id"] for r in v2api.get("/api/recordings").json()]
    monkeypatch.setattr(recordings, "FIXTURE_JOBS_DIR", _jobs_fixture(tmp_path))
    listed = {r["id"]: r for r in v2api.get("/api/recordings").json()}
    assert list(listed)[:2] == ["fixture", "fixture-jobs"]
    assert listed["fixture-jobs"]["fixture"] is True and listed["fixture-jobs"]["n_events"] == 1
    rec = v2api.get("/api/recordings/fixture-jobs").json()
    assert rec["status"] == "stopped" and rec["candidates"]["sink"]["kind"] == "form"
    assert recordings.is_page_form("fixture-jobs") and not recordings.is_page_form("fixture")
    assert v2api.post("/api/recordings/fixture-jobs/stop").json()["status"] == "stopped"


def test_build_name_defaults_for_page_recordings(v2api, tmp_path, monkeypatch):
    monkeypatch.setattr(recordings, "FIXTURE_JOBS_DIR", _jobs_fixture(tmp_path))
    calls = []
    agent = v2api.agent
    monkeypatch.setattr(agent, "start_build", lambda recording_id, name=None: calls.append((recording_id, name)) or "build-x")
    assert v2api.post("/api/integrations/build", json={"recording_id": "fixture-jobs"}).json() == {"thread_id": "build-x"}
    v2api.post("/api/integrations/build", json={"recording_id": "fixture-jobs", "name": "my_tracker"})
    v2api.post("/api/integrations/build", json={"recording_id": "fixture"})
    assert calls == [("fixture-jobs", "jobs_to_tracker"), ("fixture-jobs", "my_tracker"), ("fixture", None)]
    # an agent from before W3 (start_build(recording_id) only) still works
    old = []
    monkeypatch.setattr(agent, "start_build", lambda recording_id: old.append(recording_id) or "build-y")
    assert v2api.post("/api/integrations/build", json={"recording_id": "fixture-jobs"}).json()["thread_id"] == "build-y"


def _v1_rows():
    """A deployed v1 integration with a run, records, processed keys, a cursor and an incident."""
    make_v1_integration()
    store = Store()
    run = store.start_run("po_to_stockroom", 1, False)
    store.add_record(run, Outcome(record_id="msg-0001", status="success", key="PO-1"), {"id": "msg-0001"})
    store.mark_processed("po_to_stockroom", "live", "PO-1", run)
    store.set_cursor("po_to_stockroom", "live", "2026-11-02T10:00:00Z")
    store.finish_run(run, "completed", {"success": 1}, 0.1)
    store.open_incident(name="po_to_stockroom", version=1, run_id=run, type="NEW_SOURCE_FORMAT",
                        reason_codes=["PARSE_NO_MATCH"], summary="x")


def _snapshot(name):
    store = Store()
    db = store.db
    return {"versions": [v["version"] for v in store.versions(name)], "active": store.active_version(name),
            "runs": len(store.runs(name, limit=1000)),
            "records": db.execute("SELECT COUNT(*) FROM records WHERE run_id IN (SELECT id FROM runs WHERE integration=?)",
                                  (name,)).fetchone()[0],
            "processed": db.execute("SELECT COUNT(*) FROM processed WHERE integration=?", (name,)).fetchone()[0],
            "cursor": store.get_cursor(name, "live"), "incidents": len(store.incidents(name, status=None))}


def test_v2_reset_restores_baseline_and_keeps_v1(v2api, workspace):
    assert v2api.post("/api/demo/v2/reset").status_code == 409  # nothing saved yet
    assert v2api.post("/api/demo/v2/save-baseline").status_code == 404  # no jobs_to_tracker yet
    _v1_rows()
    v1_before = _snapshot("po_to_stockroom")
    make_page_integration([LEVER])
    work = workspace / "integrations" / NAME / "work"
    work.mkdir(parents=True)
    (work / "spec.yaml").write_text("baseline spec")
    base = v2api.post("/api/demo/v2/save-baseline").json()
    assert base["integration"] == NAME and base["active_version"] == 1 and base["versions"] == [1]

    # rehearsal: a known page, an unknown one (learned → v2 deployed), a learn left hanging, an incident
    post_page(v2api, "lever-1.html")
    wait_idle(v2api)
    post_page(v2api, "greenhouse-1.html")
    deadline = time.time() + 30
    while Store().active_version(NAME) != 2 or len(Store().runs(NAME)) < 4:
        assert time.time() < deadline
        time.sleep(0.05)
    wait_idle(v2api)
    v2api.agent.hold = threading.Event()  # the next learn blocks
    post_page(v2api, "ashby-1.html")
    wait_idle(v2api)
    hanging = [t for t, th in v2api.agent._threads.items() if th.state == "running"]
    assert len(hanging) == 1
    Store().open_incident(name=NAME, version=2, run_id=None, type="CONTRACT_CHANGED", reason_codes=["SINK_CONTRACT"],
                          summary="form changed")
    (work / "spec.yaml").write_text("learned spec")
    assert _snapshot(NAME)["versions"] == [1, 2]  # greenhouse learned; the ashby learn is still running

    out = v2api.post("/api/demo/v2/reset").json()
    assert out["ok"] and out["active_version"] == 1 and hanging[0] in out["cancelled_threads"]
    assert out["removed"]["pages"] == 3 and out["removed"]["versions"] == 1
    assert _snapshot(NAME) == {"versions": [1], "active": 1, "runs": 0, "records": 0, "processed": 0, "cursor": None,
                               "incidents": 0}
    assert not (workspace / "integrations" / NAME / "v2").exists() and (workspace / "integrations" / NAME / "v1").exists()
    assert (work / "spec.yaml").read_text() == "baseline spec"
    assert PageInbox().list() == [] and PageInbox().learns() == {}
    assert v2api.get("/api/tracker").json()["rows"] == []
    assert _snapshot("po_to_stockroom") == v1_before  # v1 untouched
    assert hanging[0] not in v2api.agent._threads

    # and the rehearsal runs again from the baseline
    v2api.agent.hold = None
    post_page(v2api, "lever-2.html")
    wait_idle(v2api)
    assert [r["label"] for r in v2api.get("/api/tracker").json()["rows"]] == ["code"]


def test_form_rename_proxy(v2api, monkeypatch):
    form = FastAPI()
    calls = []

    @form.post("/admin/rename")
    def rename(body: dict) -> dict:
        calls.append(body)
        return {"labels": ["Email", "Job Title", body["to"]]}

    with _Server(form) as srv:
        monkeypatch.setenv("PATCHWORK_TRACKER_FORM_URL", srv.url)
        r = v2api.post("/api/demo/form/rename", json={"from": "Company", "to": "Employer"})
        assert r.status_code == 200 and r.json()["labels"][-1] == "Employer"
        assert calls == [{"from": "Company", "to": "Employer"}]
        assert v2api.post("/api/demo/form/rename", json={"from": "Company"}).status_code == 422
    monkeypatch.setenv("PATCHWORK_TRACKER_FORM_URL", f"http://127.0.0.1:{_free_port()}")
    r = v2api.post("/api/demo/form/rename", json={"from": "Company", "to": "Employer"})
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]


def test_bench_v2_404_then_200(v2api, tmp_path, monkeypatch):
    path = tmp_path / "results.json"
    monkeypatch.setenv("PATCHWORK_BENCH_V2_RESULTS", str(path))
    r = v2api.get("/api/bench/v2")
    assert r.status_code == 404 and "bench-v2" in r.json()["detail"]
    path.write_text(json.dumps({"postings": [], "series": {}, "numbers": []}))
    assert v2api.get("/api/bench/v2").json() == {"postings": [], "series": {}, "numbers": []}

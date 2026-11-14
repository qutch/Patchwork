"""V10 interface additions: start_learn, start_build(name), the learn mode and its event types."""

import inspect
import time

from agents.interface import EVENT_TYPES, ThreadedAgentService
from agents.scripted import ScriptedAgentService


def wait_done(svc, tid, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if svc.status(tid)["state"] in ("done", "failed"):
            return svc.status(tid)
        time.sleep(0.02)
    raise AssertionError(svc.status(tid))


class Recorder(ThreadedAgentService):
    def _run_learn(self, t, page_id, integration):
        return {"page_id": page_id, "integration": integration, "mode": t.mode}

    def _run_build(self, t, recording_id):
        return {"recording_id": recording_id, "name": t.integration}


def test_start_learn_thread_id_prefix(workspace):
    svc = Recorder()
    tid = svc.start_learn("pg-000003")
    assert tid.startswith("learn-")
    s = wait_done(svc, tid)
    assert s["state"] == "done" and s["mode"] == "learn"
    done = next(e for e in svc.events(tid) if e["type"] == "done")
    assert done["result"] == {"page_id": "pg-000003", "integration": "jobs_to_tracker", "mode": "learn"}
    statuses = [e for e in svc.events(tid) if e["type"] == "status"]
    assert all(e["mode"] == "learn" for e in statuses)
    tid2 = svc.start_learn("pg-000004", "other_integration")
    wait_done(svc, tid2)
    assert next(e for e in svc.events(tid2) if e["type"] == "done")["result"]["integration"] == "other_integration"


def test_new_event_types_registered():
    assert EVENT_TYPES["evidence"] == ["page_id", "parser_id", "fields", "ok"]
    assert EVENT_TYPES["learned"] == ["integration", "parser_id", "site", "version", "deployed", "approval", "reason"]


def test_start_build_accepts_name(workspace):
    svc = Recorder()
    tid = svc.start_build("fixture-jobs", name="jobs_to_tracker")
    wait_done(svc, tid)
    assert next(e for e in svc.events(tid) if e["type"] == "done")["result"] == {
        "recording_id": "fixture-jobs", "name": "jobs_to_tracker"}
    tid = svc.start_build("fixture")  # v1 call shape still works
    wait_done(svc, tid)
    assert next(e for e in svc.events(tid) if e["type"] == "done")["result"]["name"] is None
    for cls in (ScriptedAgentService,):
        params = inspect.signature(cls.start_build).parameters
        assert "name" in params and params["name"].default is None
        assert list(inspect.signature(cls.start_learn).parameters) == ["self", "page_id", "integration"]


def test_learn_without_implementation_fails_cleanly(workspace):
    class Bare(ThreadedAgentService):
        pass
    svc = Bare()
    tid = svc.start_learn("pg-000001")
    assert wait_done(svc, tid)["state"] == "failed"

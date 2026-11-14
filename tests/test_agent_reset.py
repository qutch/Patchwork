"""ThreadedAgentService.reset(): no thread stays blocked, and nothing writes after a reset."""

import shutil
import threading
import time

import pytest

from agents.interface import ThreadedAgentService
from agents.scripted import ScriptedAgentService


def wait_state(svc, tid, state, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if svc.status(tid)["state"] == state:
            return svc.status(tid)
        time.sleep(0.02)
    raise AssertionError(svc.status(tid))


def worker(svc, tid):
    return svc._threads[tid].worker


class Chatty(ThreadedAgentService):
    """Emits forever until cancelled (a thread that is running, not waiting)."""

    def _run_build(self, t, recording_id):
        while True:
            self.emit(t, "message", agent="supervisor", text="working")
            time.sleep(0.01)


class SlowTool(ThreadedAgentService):
    """Sits inside a long 'tool call' (like a model request) and then tries to write."""

    def __init__(self, root, release: threading.Event):
        super().__init__(root)
        self.release = release

    def _run_build(self, t, recording_id):
        self.emit(t, "tool_call", agent="supervisor", tool="slow", args={})
        self.release.wait(10)
        self.emit(t, "tool_result", agent="supervisor", tool="slow", ok=True, summary="late")
        return "should never be reported"


def test_reset_wakes_a_thread_blocked_on_an_interrupt(workspace):
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_build("fixture")
    pending = wait_state(svc, tid, "waiting")["pending"]
    w = worker(svc, tid)

    out = svc.reset(timeout=5)

    assert out == {"cancelled": [tid], "still_running": []}
    assert not w.is_alive()
    assert svc.list_threads() == []
    with pytest.raises(KeyError):
        svc.resume(tid, pending["interrupt_id"], {"answer": "x"})


def test_reset_stops_a_running_thread_and_it_writes_nothing_after(workspace):
    svc = Chatty()
    tid = svc.start_build("r")
    time.sleep(0.1)
    w = worker(svc, tid)
    log = svc.root / f"{tid}.jsonl"

    out = svc.reset(timeout=5)
    assert out["still_running"] == [] and not w.is_alive()
    size = log.stat().st_size
    time.sleep(0.1)
    assert log.stat().st_size == size


def test_a_thread_stuck_in_a_tool_call_exits_without_writing_into_the_fresh_workspace(workspace):
    release = threading.Event()
    svc = SlowTool(workspace / "agent_threads", release)
    tid = svc.start_build("r")
    deadline = time.time() + 5
    while not any(e["type"] == "tool_call" for e in svc.events(tid)) and time.time() < deadline:
        time.sleep(0.01)
    w = worker(svc, tid)

    out = svc.reset(timeout=0.2)
    assert out["still_running"] == [tid]

    shutil.rmtree(svc.root)  # what /api/demo/reset does next
    svc.root.mkdir()
    release.set()  # the "model call" returns
    w.join(5)
    assert not w.is_alive()
    assert list(svc.root.iterdir()) == [], "the cancelled thread wrote into the fresh workspace"


def test_service_works_normally_after_reset(workspace, apps):
    svc = ScriptedAgentService(pace_s=0)
    svc.start_build("fixture")
    time.sleep(0.05)
    svc.reset()
    tid = svc.start_build("fixture")
    s = wait_state(svc, tid, "waiting")
    assert s["pending"]["type"] == "ask_user"
    svc.reset()


def test_emitted_events_are_snapshots_and_match_the_log(workspace):
    """An agent mutating a list after emitting it must not change the recorded event."""
    import json

    class Mutator(ThreadedAgentService):
        def _run_build(self, t, recording_id):
            todos = [{"content": "a", "status": "in_progress"}]
            self.emit(t, "todos", todos=todos)
            todos[0]["status"] = "completed"
            self.emit(t, "todos", todos=todos)

    svc = Mutator()
    tid = svc.start_build("r")
    wait_state(svc, tid, "done")
    mem = [e["todos"][0]["status"] for e in svc.events(tid) if e["type"] == "todos"]
    assert mem == ["in_progress", "completed"]
    log = [json.loads(line) for line in (svc.root / f"{tid}.jsonl").read_text().splitlines()]
    assert log == svc.events(tid)

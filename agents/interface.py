"""The contract between the agent layer and the control plane (see docs/contracts.md).

The control plane only ever talks to an `AgentService`. Three implementations share it:
  - `agents.scripted.ScriptedAgentService`: no LLM, deterministic, drives the real
    compiler/ops on the fixture spec. Used by tests, by the UI before agents land, and as
    the live-demo fallback ("replay mode" in the plan).
  - `agents.deep.DeepAgentService`: deepagents + OpenRouter (Phase 2, agents workstream).
  - anything else that subclasses `ThreadedAgentService`.

Events are append-only, numbered from 1 per thread, persisted as JSONL under
$PATCHWORK_WORKSPACE/agent_threads/<thread_id>.jsonl so a stream can resume from any seq.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from runner.store import workspace_dir


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Every credential value the workspace's specs reference, replaced by *** (contracts V13)."""
    try:
        from control.credentials import redact_obj
    except ImportError:  # pragma: no cover - the agents always run inside the control plane
        return payload
    try:
        return redact_obj(payload)
    except Exception:  # noqa: BLE001 - redaction must not break an agent thread; fall back to the env values
        from runner.credentials import redact_obj as runner_redact_obj
        return runner_redact_obj(payload)


Mode = Literal["build", "repair", "learn"]
ThreadState = Literal["running", "waiting", "done", "failed"]

# Event types and their payload keys (all events also carry seq, t, thread_id, type):
EVENT_TYPES = {
    "status": ["state", "mode"],                    # thread state changed
    "todos": ["todos"],                             # [{content, status: pending|in_progress|completed}]
    "subagent_start": ["name", "task"],             # supervisor delegated to a subagent
    "subagent_end": ["name", "summary"],
    "tool_call": ["agent", "tool", "args"],         # agent = "supervisor" or a subagent name
    "tool_result": ["agent", "tool", "ok", "summary"],
    "message": ["agent", "text"],                   # free-text reasoning/explanation worth showing
    "artifact": ["kind", "name", "version", "path"],  # kind: recording|candidates|spec|package
    "ask_user": ["interrupt_id", "question", "options"],
    "request_deploy": ["interrupt_id", "name", "version", "summary", "spec_diff", "code_diff", "tests"],
    "resumed": ["interrupt_id", "payload"],
    "deployed": ["name", "version", "previous_version"],
    "done": ["result"],
    "error": ["message"],
    # v2 (contracts V10). UIs ignore types they don't know.
    "evidence": ["page_id", "parser_id", "fields", "ok"],  # fields: [{name, value, ok, where, extractor}]
    "learned": ["integration", "parser_id", "site", "version", "deployed", "approval", "reason"],  # auto|human|pending
}

DEFAULT_PAGE_INTEGRATION = "jobs_to_tracker"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AgentService(Protocol):
    def start_build(self, recording_id: str, name: str | None = None) -> str: ...
    def start_repair(self, incident_id: int) -> str: ...
    def start_learn(self, page_id: str, integration: str = DEFAULT_PAGE_INTEGRATION) -> str: ...  # "learn-<hex>"
    def events(self, thread_id: str, after: int = 0) -> list[dict[str, Any]]: ...
    def status(self, thread_id: str) -> dict[str, Any]: ...
    def resume(self, thread_id: str, interrupt_id: str, payload: dict[str, Any]) -> None: ...
    def list_threads(self) -> list[dict[str, Any]]: ...


class InterruptRejected(Exception):
    pass


class ThreadCancelled(BaseException):
    """Raised inside an agent thread after `reset()`. A BaseException so that broad
    `except Exception` blocks in agent or tool code can't swallow it."""


@dataclass
class _Thread:
    id: str
    mode: Mode
    subject: str  # recording id, incident id or page id
    integration: str | None = None  # BUILD: the requested name (None = the service default); LEARN: the integration
    state: ThreadState = "running"
    events: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] | None = None  # the open ask_user / request_deploy event
    answer: dict[str, Any] | None = None
    wake: threading.Event = field(default_factory=threading.Event)
    created_at: str = field(default_factory=now)
    cancelled: bool = False
    worker: threading.Thread | None = None


class ThreadedAgentService:
    """Base class: runs each thread's `_run_build`/`_run_repair` in a background thread and
    provides the event log, interrupts and resume. Subclasses implement the two _run methods
    and call `self.emit(...)` and `self.interrupt(...)`."""

    def __init__(self, root: Path | None = None):
        self.root = root or workspace_dir() / "agent_threads"
        self.root.mkdir(parents=True, exist_ok=True)
        self._threads: dict[str, _Thread] = {}
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    # ---- public API --------------------------------------------------------------------
    def start_build(self, recording_id: str, name: str | None = None) -> str:
        return self._start("build", recording_id, lambda t: self._run_build(t, recording_id), integration=name)

    def start_repair(self, incident_id: int) -> str:
        return self._start("repair", str(incident_id), lambda t: self._run_repair(t, int(incident_id)))

    def start_learn(self, page_id: str, integration: str = DEFAULT_PAGE_INTEGRATION) -> str:
        """LEARN: one page whose layout no parser reads -> a checked parser (contracts V10)."""
        return self._start("learn", page_id, lambda t: self._run_learn(t, page_id, integration),
                           integration=integration)

    def events(self, thread_id: str, after: int = 0) -> list[dict[str, Any]]:
        t = self._threads.get(thread_id)
        if t is not None:
            with self._lock:
                return [e for e in t.events if e["seq"] > after]
        path = self.root / f"{thread_id}.jsonl"
        if not path.exists():
            raise KeyError(thread_id)
        return [e for e in map(json.loads, path.read_text().splitlines()) if e["seq"] > after]

    def status(self, thread_id: str) -> dict[str, Any]:
        t = self._threads.get(thread_id)
        if t is None:
            evs = self.events(thread_id)  # raises KeyError if unknown
            last_state = next((e["state"] for e in reversed(evs) if e["type"] == "status"), "done")
            return {"thread_id": thread_id, "state": last_state, "pending": None, "last_seq": len(evs)}
        return {"thread_id": t.id, "mode": t.mode, "subject": t.subject, "integration": t.integration, "state": t.state,
                "pending": t.pending, "last_seq": len(t.events), "created_at": t.created_at}

    def resume(self, thread_id: str, interrupt_id: str, payload: dict[str, Any]) -> None:
        t = self._threads[thread_id]
        if t.pending is None or t.pending["interrupt_id"] != interrupt_id:
            raise KeyError(f"no pending interrupt {interrupt_id} on {thread_id}")
        t.answer = payload
        t.pending = None
        self.emit(t, "resumed", interrupt_id=interrupt_id, payload=payload)
        self._set_state(t, "running")
        t.wake.set()

    def list_threads(self) -> list[dict[str, Any]]:
        return [self.status(tid) for tid in sorted(self._threads, key=lambda k: self._threads[k].created_at)]

    def reset(self, timeout: float = 5.0) -> dict[str, Any]:
        """Cancel every thread and forget it. Threads blocked on an interrupt wake up and
        exit; running threads stop at their next emit/interrupt and write nothing more.
        Waits up to `timeout` seconds in total for them to finish. A thread still inside a
        long tool call (e.g. a model request) is reported in `still_running`; it exits
        without writing as soon as that call returns."""
        with self._lock:
            threads = list(self._threads.values())
            self._threads.clear()
            for t in threads:
                t.cancelled = True
                t.pending = None
                t.answer = None
        for t in threads:
            t.wake.set()
        deadline = time.monotonic() + timeout
        for t in threads:
            if t.worker is not None:
                t.worker.join(max(0.0, deadline - time.monotonic()))
        still = [t.id for t in threads if t.worker is not None and t.worker.is_alive()]
        self.root.mkdir(parents=True, exist_ok=True)
        return {"cancelled": [t.id for t in threads], "still_running": still}

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Optional push hook (the control plane may also just poll `events(after=...)`)."""
        self._listeners.append(fn)

    # ---- for subclasses ------------------------------------------------------------------
    def emit(self, t: _Thread, type: str, **payload: Any) -> dict[str, Any]:
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type}")
        with self._lock:
            if t.cancelled:  # checked under the lock reset() takes, so no write follows a reset
                raise ThreadCancelled(t.id)
            payload = _redact_payload(payload)  # V13: events reach the UI; never a credential value
            line = json.dumps({"seq": len(t.events) + 1, "t": now(), "thread_id": t.id, "type": type, **payload},
                              default=str)
            # Store a snapshot, not the caller's objects: an agent that later mutates a list it
            # emitted (e.g. its todos) must not rewrite history. Memory then matches the JSONL log.
            ev = json.loads(line)
            t.events.append(ev)
            with (self.root / f"{t.id}.jsonl").open("a") as f:
                f.write(line + "\n")
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - a broken listener must not kill the agent
                pass
        return ev

    def interrupt(self, t: _Thread, type: Literal["ask_user", "request_deploy"], **payload: Any) -> dict[str, Any]:
        """Emit an interrupt event and block this agent thread until `resume` is called."""
        interrupt_id = f"int-{uuid.uuid4().hex[:8]}"
        t.wake.clear()
        t.answer = None
        ev = self.emit(t, type, interrupt_id=interrupt_id, **payload)
        t.pending = ev
        self._set_state(t, "waiting")
        t.wake.wait()
        if t.cancelled:
            raise ThreadCancelled(t.id)
        return t.answer or {}

    def check_cancelled(self, t: _Thread) -> None:
        """For long-running subclass work between emits (e.g. before a model call)."""
        if t.cancelled:
            raise ThreadCancelled(t.id)

    def _set_state(self, t: _Thread, state: ThreadState) -> None:
        t.state = state
        self.emit(t, "status", state=state, mode=t.mode)

    def _start(self, mode: Mode, subject: str, body: Callable[[_Thread], Any], integration: str | None = None) -> str:
        tid = f"{mode}-{uuid.uuid4().hex[:10]}"
        t = _Thread(id=tid, mode=mode, subject=subject, integration=integration)

        def run() -> None:
            try:
                self._set_state(t, "running")
                try:
                    result = body(t)
                    self.emit(t, "done", result=result)
                    self._set_state(t, "done")
                except Exception as e:  # noqa: BLE001
                    self.emit(t, "error", message=f"{type(e).__name__}: {e}")
                    self._set_state(t, "failed")
            except ThreadCancelled:
                pass  # reset() forgot this thread; exit quietly without touching the workspace

        t.worker = threading.Thread(target=run, name=tid, daemon=True)
        with self._lock:
            self._threads[tid] = t
        t.worker.start()
        return tid

    def _run_build(self, t: _Thread, recording_id: str) -> Any:
        raise NotImplementedError

    def _run_repair(self, t: _Thread, incident_id: int) -> Any:
        raise NotImplementedError

    def _run_learn(self, t: _Thread, page_id: str, integration: str) -> Any:
        raise NotImplementedError(f"{type(self).__name__} has no LEARN mode")

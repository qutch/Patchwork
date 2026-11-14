"""Background runs for the API: `python -m runner run ...` as a subprocess (same command, stripped
environment and scratch cwd as `control.ops.run_sandboxed`), without blocking the request.

The runner prints {"event": "started", "run_id": N} first; `start()` waits for that line and
returns N. A watcher thread drains stdout (picking up {"event": "fetched", "total": N} and the final
summary), enforces the timeout, and marks the run failed if the process died mid-run.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from control import ops
from runner.store import Store

STARTUP_TIMEOUT_S = 60


class RunStartError(Exception):
    pass


@dataclass
class LiveRun:
    integration: str
    kind: str
    proc: subprocess.Popen
    scratch: Path
    run_id: int | None = None
    total: int | None = None
    summary: dict[str, Any] | None = None
    done: bool = False
    started: float = field(default_factory=time.monotonic)


@dataclass
class AdoptedRun:
    """A runner still alive from before this control plane started. We didn't launch it, so we
    can't read its stdout; we watch its pid, block concurrent runs, and finalize it if it dies
    without writing its own summary."""

    run_id: int
    integration: str
    pid: int
    started_s: float  # seconds since epoch the run started
    done: bool = False


def runner_alive(pid: int | None) -> bool:
    """True if `pid` is a live `python -m runner run ...` process (guards against pid reuse)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists but not ours; check the command line below
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "runner" in cmd and " run " in f"{cmd} "


def _epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return time.time()


class RunManager:
    def __init__(self, timeout_s: int = ops.RUN_TIMEOUT_S):
        self.timeout_s = timeout_s
        self._runs: dict[int, LiveRun] = {}
        self._adopted: dict[int, AdoptedRun] = {}
        self._lock = threading.Lock()
        self._starting: set[str] = set()

    # ---- startup recovery ------------------------------------------------------------------
    def recover(self) -> dict[str, list[int]]:
        """Reconcile runs left `running` by a previous control plane. A run whose runner process
        is gone is marked failed with the counts it reached. A run whose runner is still alive is
        adopted, not killed: killing mid-record could resend a write that wasn't yet marked
        processed. It blocks concurrent runs of its integration until it exits."""
        out: dict[str, list[int]] = {"failed": [], "adopted": []}
        store = Store()
        try:
            for run in store.running_runs():
                with self._lock:
                    if run["id"] in self._runs or run["id"] in self._adopted:
                        continue  # ours
                if runner_alive(run.get("pid")):
                    adopted = AdoptedRun(run["id"], run["integration"], int(run["pid"]), _epoch(run["started_at"]))
                    with self._lock:
                        self._adopted[run["id"]] = adopted
                    threading.Thread(target=self._watch_adopted, args=(adopted,), daemon=True,
                                     name=f"adopted-run-{run['id']}").start()
                    out["adopted"].append(run["id"])
                else:
                    store.finish_run(run["id"], "failed", store.record_counts(run["id"]),
                                     max(0.0, time.time() - _epoch(run["started_at"])), None,
                                     "control plane restarted while this run was in progress; "
                                     "the runner process is gone. Records it finished are kept; "
                                     "run again to continue from the cursor.")
                    out["failed"].append(run["id"])
        finally:
            store.close()
        return out

    def _watch_adopted(self, a: AdoptedRun) -> None:
        try:
            while runner_alive(a.pid):
                if time.time() - a.started_s > self.timeout_s:
                    try:
                        os.kill(a.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                time.sleep(0.5)
            store = Store()
            try:
                run = store.run(a.run_id)
                if run is not None and run["status"] == "running":  # died without its own summary
                    store.finish_run(a.run_id, "failed", store.record_counts(a.run_id),
                                     max(0.0, time.time() - a.started_s), None,
                                     "runner exited without a summary after a control-plane restart")
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - the workspace may have been reset under us
            pass
        finally:
            a.done = True

    @staticmethod
    def _db_running(run_id: int | None) -> bool:
        """The run row still says running. The runner writes its final status *before* its
        process exits and before our watcher marks it done, so the row is the source of truth
        for "can another run start?"; otherwise a client that sees `completed` and immediately
        starts the next run would get a spurious 409."""
        if run_id is None:
            return True
        try:
            store = Store()
            try:
                run = store.run(run_id)
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - be conservative if the DB is unreadable
            return True
        return run is not None and run["status"] == "running"

    def _active(self, integration: str) -> LiveRun | AdoptedRun | None:
        """Call with self._lock held."""
        for r in self._runs.values():
            if r.integration == integration and not r.done and self._db_running(r.run_id):
                return r
        for a in self._adopted.values():
            if a.integration == integration and not a.done and self._db_running(a.run_id):
                return a
        return None

    def _busy(self, integration: str) -> bool:
        """Call with self._lock held."""
        return integration in self._starting or self._active(integration) is not None

    def in_progress(self, integration: str) -> LiveRun | AdoptedRun | None:
        with self._lock:
            return self._active(integration)

    def get(self, run_id: int) -> LiveRun | None:
        return self._runs.get(run_id)

    def start(self, integration: str, *, version: str | int = "active", dry_run: bool = False,
              limit: int | None = None, replay: bool = False) -> int:
        with self._lock:
            if self._busy(integration):
                raise RuntimeError(f"a run of {integration} is already in progress")
            self._starting.add(integration)
        try:
            return self._start(integration, version=version, dry_run=dry_run, limit=limit, replay=replay)
        finally:
            with self._lock:
                self._starting.discard(integration)

    def _start(self, integration: str, *, version, dry_run, limit, replay) -> int:
        cmd = ops.runner_command(integration, version=version, dry_run=dry_run, limit=limit, replay=replay)
        scratch = Path(tempfile.mkdtemp(prefix="patchwork-run-"))
        stderr = (scratch / "stderr.log").open("w")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr, text=True, bufsize=1,
                                env=ops.runner_env(), cwd=scratch)
        stderr.close()
        live = LiveRun(integration=integration, kind="replay" if replay else "run", proc=proc, scratch=scratch)
        started: queue.Queue[int | None] = queue.Queue()

        def read_stdout() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("event") == "started":
                    live.run_id = int(obj["run_id"])
                    started.put(live.run_id)
                elif obj.get("event") == "fetched":
                    live.total = obj.get("total")
                elif "event" not in obj:
                    live.summary = obj
            started.put(None)

        reader = threading.Thread(target=read_stdout, daemon=True, name="runner-stdout")
        reader.start()
        try:
            run_id = started.get(timeout=STARTUP_TIMEOUT_S)
        except queue.Empty:
            proc.kill()
            run_id = None
        if run_id is None:
            proc.wait(timeout=10)
            reader.join(timeout=5)
            err = self._stderr_tail(scratch)
            shutil.rmtree(scratch, ignore_errors=True)
            raise RunStartError(err or f"runner exited with code {proc.returncode} before starting")
        with self._lock:
            self._runs[run_id] = live
        threading.Thread(target=self._watch, args=(live, reader), daemon=True, name=f"run-{run_id}").start()
        return run_id

    def _watch(self, live: LiveRun, reader: threading.Thread) -> None:
        try:
            live.proc.wait(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            live.proc.kill()
            live.proc.wait()
        reader.join(timeout=5)
        try:
            store = Store()
            run = store.run(live.run_id) if live.run_id is not None else None
            if run is not None and run["status"] == "running":
                rc = live.proc.returncode
                why = "timed out" if rc in (-9, -15) else f"exited with code {rc}"
                store.finish_run(live.run_id, "failed", store.record_counts(live.run_id),
                                 time.monotonic() - live.started, None,
                                 f"runner {why}: {self._stderr_tail(live.scratch)[-1500:]}")
            store.close()
        except Exception:  # noqa: BLE001 - the workspace may have been reset under us
            pass
        finally:
            live.done = True
            shutil.rmtree(live.scratch, ignore_errors=True)

    @staticmethod
    def _stderr_tail(scratch: Path) -> str:
        try:
            return (scratch / "stderr.log").read_text()[-2000:].strip()
        except OSError:
            return ""

    def stop_all(self, wait_s: float = 5) -> None:
        with self._lock:
            live = [r for r in self._runs.values() if not r.done]
            adopted = [a for a in self._adopted.values() if not a.done]
        for r in live:
            if r.proc.poll() is None:
                r.proc.kill()
        for a in adopted:
            try:
                os.kill(a.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + wait_s
        for r in [*live, *adopted]:
            while not r.done and time.monotonic() < deadline:
                time.sleep(0.02)

    def clear(self) -> None:
        with self._lock:
            self._runs = {k: v for k, v in self._runs.items() if not v.done}
            self._adopted = {k: v for k, v in self._adopted.items() if not v.done}

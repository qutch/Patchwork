"""Background runs for the API: `python -m runner run ...` as a subprocess (same command, stripped
environment and scratch cwd as `control.ops.run_sandboxed`), without blocking the request.

The runner prints {"event": "started", "run_id": N} first; `start()` waits for that line and
returns N. A watcher thread drains stdout (picking up {"event": "fetched", "total": N} and the final
summary), enforces the timeout, and marks the run failed if the process died mid-run.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
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


class RunManager:
    def __init__(self, timeout_s: int = ops.RUN_TIMEOUT_S):
        self.timeout_s = timeout_s
        self._runs: dict[int, LiveRun] = {}
        self._lock = threading.Lock()
        self._starting: set[str] = set()

    def in_progress(self, integration: str) -> LiveRun | None:
        with self._lock:
            return next((r for r in self._runs.values() if r.integration == integration and not r.done), None)

    def get(self, run_id: int) -> LiveRun | None:
        return self._runs.get(run_id)

    def start(self, integration: str, *, version: str | int = "active", dry_run: bool = False,
              limit: int | None = None, replay: bool = False) -> int:
        with self._lock:
            busy = integration in self._starting or any(
                r.integration == integration and not r.done for r in self._runs.values())
            if busy:
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
        for r in live:
            if r.proc.poll() is None:
                r.proc.kill()
        deadline = time.monotonic() + wait_s
        for r in live:
            while not r.done and time.monotonic() < deadline:
                time.sleep(0.02)

    def clear(self) -> None:
        with self._lock:
            self._runs = {k: v for k, v in self._runs.items() if not v.done}

"""Recording sessions for the control plane. Each recording runs `python -m capture record`
in its own subprocess, so nothing here blocks (except `stop()`, which waits for the
recorder to write recording.json) and every function is safe to call from any thread.

Layout: $PATCHWORK_WORKSPACE/recordings/<id>/{meta.json, events.jsonl, recording.json,
candidates.json, recorder.log}. The id `fixture` maps to fixtures/recording.json.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from capture.launcher import STOP_FILE, finalize, read_meta
from capture.recorder import read_events

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_RECORDING = REPO_ROOT / "fixtures" / "recording.json"
STOP_TIMEOUT_S = 20

_lock = threading.Lock()
_sessions: dict[str, "RecordingSession"] = {}


def recordings_dir() -> Path:
    return Path(os.environ.get("PATCHWORK_WORKSPACE", "workspace")).resolve() / "recordings"


def new_recording_id() -> str:
    return f"rec-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RecordingSession:
    def __init__(self, recording_id: str, dir: Path, proc: subprocess.Popen | None = None):
        self.id = recording_id
        self.dir = dir
        self._proc = proc
        self._stop_lock = threading.Lock()

    # ---- read side ----
    def events(self, after: int = 0) -> list[dict]:
        """Captured events after line `after` (1-based index = line number in events.jsonl)."""
        if self.id == "fixture":
            return json.loads(FIXTURE_RECORDING.read_text())["events"][after:]
        return read_events(self.dir / "events.jsonl")[after:]

    @property
    def running(self) -> bool:
        if self.id == "fixture":
            return False
        if self._proc is not None:
            return self._proc.poll() is None
        meta = read_meta(self.dir)
        return meta.get("status") == "recording" and _pid_alive(meta.get("pid"))

    def meta(self) -> dict:
        if self.id == "fixture":
            rec = json.loads(FIXTURE_RECORDING.read_text())
            return {"id": "fixture", "intent": rec["intent"], "allowlist": rec["allowlist"], "status": "stopped",
                    "started_at": rec["started_at"], "stopped_at": rec["stopped_at"]}
        meta = read_meta(self.dir)
        if not meta and (self.dir / "recording.json").exists():
            rec = json.loads((self.dir / "recording.json").read_text())
            meta = {k: rec.get(k) for k in ("id", "intent", "allowlist", "started_at", "stopped_at")}
        meta["id"] = self.id
        return meta

    def summary(self) -> dict:
        m = self.meta()
        return {"id": self.id, "intent": m.get("intent"), "allowlist": m.get("allowlist", []),
                "status": "recording" if self.running else "stopped",
                "started_at": m.get("started_at"), "stopped_at": None if self.running else m.get("stopped_at"),
                "n_events": len(self.events())}

    # ---- stop ----
    def stop(self) -> dict:
        """Stop the recorder (if running), return recording.json. Idempotent."""
        if self.id == "fixture":
            return json.loads(FIXTURE_RECORDING.read_text())
        with self._stop_lock:
            if self.running:
                (self.dir / STOP_FILE).touch()
                pid = self._proc.pid if self._proc is not None else read_meta(self.dir).get("pid")
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, TypeError):
                    pass
                deadline = time.time() + STOP_TIMEOUT_S
                while self.running and time.time() < deadline:
                    time.sleep(0.1)
                if self.running:  # wedged: kill it and finalize from events.jsonl ourselves
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, TypeError):
                        pass
                    if self._proc is not None:
                        self._proc.wait(timeout=5)
            rec_path = self.dir / "recording.json"
            meta = read_meta(self.dir)
            if not rec_path.exists() or meta.get("status") != "stopped":
                return finalize(self.dir, {**meta, "id": self.id})
            return json.loads(rec_path.read_text())


def start_recording(intent: str, allowlist: list[str], start_url: str | None = None,
                    headless: bool = False) -> RecordingSession:
    """Launch the recorder subprocess and return immediately."""
    if not allowlist:
        raise ValueError("allowlist must name at least one host:port")
    rid = new_recording_id()
    dir = recordings_dir() / rid
    dir.mkdir(parents=True, exist_ok=False)
    meta = {"id": rid, "intent": intent, "allowlist": list(allowlist), "start_url": start_url,
            "status": "recording", "started_at": int(time.time() * 1000), "pid": None}
    (dir / "meta.json").write_text(json.dumps(meta, indent=2))
    (dir / "events.jsonl").touch()
    cmd = [sys.executable, "-m", "capture", "record", str(dir), "--intent", intent]
    for a in allowlist:
        cmd += ["--allow", a]
    if start_url:
        cmd += ["--start-url", start_url]
    if headless:
        cmd.append("--headless")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO_ROOT), env.get("PYTHONPATH")) if p)
    log = (dir / "recorder.log").open("ab")
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    log.close()
    session = RecordingSession(rid, dir, proc)
    with _lock:
        _sessions[rid] = session
    return session


def get_session(recording_id: str) -> RecordingSession | None:
    """A running or finished recording, or None. `fixture` is fixtures/recording.json."""
    with _lock:
        if recording_id in _sessions:
            return _sessions[recording_id]
        if recording_id == "fixture":
            return RecordingSession("fixture", FIXTURE_RECORDING.parent)
        if "/" in recording_id or recording_id.startswith("."):
            return None
        dir = recordings_dir() / recording_id
        if not dir.is_dir() or not any((dir / f).exists() for f in ("meta.json", "recording.json")):
            return None
        session = RecordingSession(recording_id, dir)
        _sessions[recording_id] = session
        return session


def list_recordings() -> list[dict]:
    """[{id, intent, allowlist, status, started_at, stopped_at, n_events}], newest first."""
    root = recordings_dir()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir(), reverse=True):
        if d.is_dir():
            s = get_session(d.name)
            if s is not None:
                out.append(s.summary())
    return out
